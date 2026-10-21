#!/usr/bin/env bash
# Deploys the Azure Function that serves the agent's log_feedback, query_oracle_db, update_oracle_incident and
# propose_kb_file_update tools as an MCP server, which Foundry calls itself (so they also work in the Foundry
# portal playground). Safe to re-run: existing resources are reused and the code is redeployed.
#
#   1. Storage account in the Foundry resource's group and region, with the Azure Table "feedback", the blob
#      container "oracle-mock" (the mock Oracle database, a SQLite file the function creates on first use) and the
#      blob container "knowledge-base" (seeded from knowledge_base/ and .kb_vector_store.json; files already
#      there are kept, so edits made through the agent survive a re-run)
#   2. Function App (Flex Consumption, Python) with a system-assigned managed identity
#   3. For the Function App and for you: Storage Table Data Contributor on the storage account and Storage Blob
#      Data Contributor on both containers; for the Function App also Foundry User on the Foundry resource, so it
#      can re-index updated KB files in the vector store
#   4. Code from azure_function/ plus feedback_log.py, oracle_tool.py, kb_tool.py and guardrails.py, built remotely
#   5. Waits until the new build answers on /api/health
#   6. The app's mcp_extension system key, stored in a Foundry MCP ("remote tool") connection and nowhere else
#   7. MCP smoke test (lists the tools, runs one read-only query), then FEEDBACK_MCP_URL,
#      FEEDBACK_CONNECTION_NAME, FEEDBACK_TABLE_ENDPOINT, FEEDBACK_TABLE_NAME, DB_MODE=azure_blob,
#      ORACLE_MOCK_BLOB_URL, KB_MODE=azure_blob and KB_BLOB_CONTAINER_URL in .env
#
# Reads PROJECT_ENDPOINT from .env. Override names with FEEDBACK_STORAGE_ACCOUNT / FEEDBACK_FUNCTION_APP.
# The next app start (or kb_eval.py run) creates a new agent version that uses the function.
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python; [[ -x $PY ]] || PY=python3

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
env_get() { "$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(".env").get(sys.argv[1]) or "")' "$1"; }
env_set() { # env_set KEY VALUE: replaces or appends KEY="VALUE" in .env
  "$PY" - "$1" "$2" <<'EOF'
import re, sys
from pathlib import Path
key, value = sys.argv[1], sys.argv[2]
env = Path(".env")
lines = env.read_text().splitlines() if env.exists() else []
new = f'{key}="{value}"'
for i, line in enumerate(lines):
    if re.match(rf"\s*{re.escape(key)}\s*=", line):
        lines[i] = new
        break
else:
    lines.append(new)
env.write_text("\n".join(lines) + "\n")
EOF
}
env_unset() { # env_unset KEY: removes KEY=... from .env
  "$PY" - "$1" <<'EOF'
import re, sys
from pathlib import Path
env = Path(".env")
lines = [l for l in env.read_text().splitlines() if not re.match(rf"\s*{re.escape(sys.argv[1])}\s*=", l)]
env.write_text("\n".join(lines) + "\n")
EOF
}
my_object_id() { # from the ARM token's oid claim; Microsoft Graph lookups can be blocked by security defaults
  az account get-access-token --query accessToken -o tsv | "$PY" -c '
import base64, json, sys
payload = sys.stdin.read().split(".")[1]
print(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["oid"])'
}
grant() { # grant ROLE PRINCIPAL_ID PRINCIPAL_TYPE SCOPE (skipped when already assigned; no Graph lookups)
  if [[ -z $(az role assignment list --role "$1" --scope "$4" --fill-principal-name false --query "[?principalId=='$2'].id | [0]" -o tsv) ]]; then
    az role assignment create --role "$1" --assignee-object-id "$2" --assignee-principal-type "$3" --scope "$4" -o none
    echo "Granted $1 to $3 $2"
  else
    echo "$3 already has $1"
  fi
}

ENDPOINT=$(env_get PROJECT_ENDPOINT)
[[ $ENDPOINT =~ ^https://([^.]+)\.services\.ai\.azure\.com/api/projects/([^/]+)/?$ ]] \
  || { echo "PROJECT_ENDPOINT in .env must look like https://<resource>.services.ai.azure.com/api/projects/<project>"; exit 1; }
ACCOUNT=${BASH_REMATCH[1]} PROJECT=${BASH_REMATCH[2]}
read -r RG LOCATION ACCOUNT_ID < <(az cognitiveservices account list \
  --query "[?name=='$ACCOUNT'] | [0].join(' ', [resourceGroup, location, id])" -o tsv)
[[ -n ${ACCOUNT_ID:-} ]] || { echo "Foundry resource $ACCOUNT not found in the current subscription."; exit 1; }
SUFFIX=$(printf %s "$ACCOUNT_ID" | sha1sum | cut -c1-8)
STORAGE=${FEEDBACK_STORAGE_ACCOUNT:-feedbacklog$SUFFIX}
APP=${FEEDBACK_FUNCTION_APP:-feedback-log-$SUFFIX}
TABLE=feedback
DB_CONTAINER=oracle-mock
DB_BLOB_URL="https://$STORAGE.blob.core.windows.net/$DB_CONTAINER/demo_oracle_mock.db"
KB_CONTAINER=knowledge-base
KB_CONTAINER_URL="https://$STORAGE.blob.core.windows.net/$KB_CONTAINER"
FOUNDRY_USER_ROLE=53ca6127-db72-4b80-b1b0-d745d6d5456d # Foundry User (formerly Azure AI User); IDs survive renames
CONNECTION=feedback-mcp
OLD_CONNECTION=feedback-function # used by the earlier OpenAPI version of this tool
echo "Foundry: $ACCOUNT / $PROJECT   resource group: $RG   region: $LOCATION"
echo "Storage account: $STORAGE   Function App: $APP"

step "1. Storage account, table and containers"
if ! az storage account show -n "$STORAGE" -g "$RG" -o none 2>/dev/null; then
  az storage account create -n "$STORAGE" -g "$RG" -l "$LOCATION" --sku Standard_LRS --kind StorageV2 \
    --min-tls-version TLS1_2 --allow-blob-public-access false -o none
fi
STORAGE_ID=$(az storage account show -n "$STORAGE" -g "$RG" --query id -o tsv)
az rest --method put -o none --body '{}' \
  --url "https://management.azure.com$STORAGE_ID/tableServices/default/tables/$TABLE?api-version=2023-05-01"
echo "Table $TABLE ready"
DB_CONTAINER_ID="$STORAGE_ID/blobServices/default/containers/$DB_CONTAINER"
az rest --method put -o none --body '{}' --url "https://management.azure.com$DB_CONTAINER_ID?api-version=2023-05-01"
echo "Container $DB_CONTAINER ready"
KB_CONTAINER_ID="$STORAGE_ID/blobServices/default/containers/$KB_CONTAINER"
az rest --method put -o none --body '{}' --url "https://management.azure.com$KB_CONTAINER_ID?api-version=2023-05-01"
STORAGE_KEY=$(az storage account keys list -n "$STORAGE" -g "$RG" --query "[0].value" -o tsv)
seed_blob() { # seed_blob FILE NAME: uploads FILE as NAME unless the container already has NAME
  [[ $(az storage blob exists --account-name "$STORAGE" --account-key "$STORAGE_KEY" -c "$KB_CONTAINER" -n "$2" \
    --query exists -o tsv) == true ]] && return 1
  az storage blob upload --account-name "$STORAGE" --account-key "$STORAGE_KEY" -c "$KB_CONTAINER" -n "$2" -f "$1" \
    -o none --only-show-errors --no-progress
}
SEEDED=0 KEPT=0
for f in knowledge_base/*.md knowledge_base/*.pdf; do
  if seed_blob "$f" "$(basename "$f")"; then SEEDED=$((SEEDED + 1)); else KEPT=$((KEPT + 1)); fi
done
# The sync record says which vector store file holds each KB file; seeding it from this project's local record
# means nothing has to be uploaded again
if [[ -f .kb_vector_store.json ]] && E="$ENDPOINT" "$PY" -c 'import json, os, sys
sys.exit(json.load(open(".kb_vector_store.json")).get("project_endpoint") != os.environ["E"])'; then
  seed_blob .kb_vector_store.json vector_store_sync.json && echo "Sync record seeded from .kb_vector_store.json" || true
fi
echo "Container $KB_CONTAINER ready ($SEEDED file(s) seeded, $KEPT already there)"

step "2. Function App (Flex Consumption)"
if ! az functionapp show -n "$APP" -g "$RG" -o none 2>/dev/null; then
  az functionapp create -n "$APP" -g "$RG" --storage-account "$STORAGE" --flexconsumption-location "$LOCATION" \
    --runtime python --runtime-version 3.13 --instance-memory 512 --maximum-instance-count 10 -o none
fi
APP_PRINCIPAL=$(az functionapp identity assign -n "$APP" -g "$RG" --query principalId -o tsv)
az functionapp config appsettings set -n "$APP" -g "$RG" -o none --settings \
  "FEEDBACK_TABLE_ENDPOINT=https://$STORAGE.table.core.windows.net" "FEEDBACK_TABLE_NAME=$TABLE" \
  "ORACLE_MOCK_BLOB_URL=$DB_BLOB_URL" "KB_BLOB_CONTAINER_URL=$KB_CONTAINER_URL" "PROJECT_ENDPOINT=$ENDPOINT"
read -r APP_ID HOST < <(az functionapp show -n "$APP" -g "$RG" \
  --query "join(' ', [id, properties.defaultHostName || defaultHostName])" -o tsv)

step "3. Storage and Foundry access for the Function App and for you"
ME=$(my_object_id)
grant "Storage Table Data Contributor" "$APP_PRINCIPAL" ServicePrincipal "$STORAGE_ID"
grant "Storage Table Data Contributor" "$ME" User "$STORAGE_ID"
grant "Storage Blob Data Contributor" "$APP_PRINCIPAL" ServicePrincipal "$DB_CONTAINER_ID"
grant "Storage Blob Data Contributor" "$ME" User "$DB_CONTAINER_ID"
grant "Storage Blob Data Contributor" "$APP_PRINCIPAL" ServicePrincipal "$KB_CONTAINER_ID"
grant "Storage Blob Data Contributor" "$ME" User "$KB_CONTAINER_ID"
grant "$FOUNDRY_USER_ROLE" "$APP_PRINCIPAL" ServicePrincipal "$ACCOUNT_ID"

step "4. Deploy code"
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
mkdir "$BUILD/src"
cp azure_function/function_app.py azure_function/host.json azure_function/requirements.txt \
  feedback_log.py oracle_tool.py kb_tool.py guardrails.py "$BUILD/src/"
BUILD_ID=$(cat "$BUILD"/src/* | sha256sum | cut -c1-12)
echo "$BUILD_ID" > "$BUILD/src/build_id.txt"
(cd "$BUILD/src" && zip -qr ../function.zip .)
# Deploy through Azure Resource Manager: the onedeploy extension pulls the package from a one-hour read-only
# link. (`az functionapp deployment source config-zip` needs an App Service token that security defaults
# can block without an interactive MFA sign-in.)
az rest --method put -o none --body '{}' \
  --url "https://management.azure.com$STORAGE_ID/blobServices/default/containers/feedback-deploy?api-version=2023-05-01"
az storage blob upload --account-name "$STORAGE" --account-key "$STORAGE_KEY" -c feedback-deploy -n function.zip \
  -f "$BUILD/function.zip" --overwrite -o none --only-show-errors --no-progress
PACKAGE_URL=$(az storage blob generate-sas --account-name "$STORAGE" --account-key "$STORAGE_KEY" -c feedback-deploy \
  -n function.zip --permissions r --https-only --expiry "$(date -u -d '+1 hour' +%Y-%m-%dT%H:%MZ)" --full-uri -o tsv)
U="$PACKAGE_URL" "$PY" -c '
import json, os, sys
json.dump({"properties": {"packageUri": os.environ["U"], "remoteBuild": True}}, open(sys.argv[1], "w"))' "$BUILD/onedeploy.json"
for attempt in $(seq 1 20); do
  if az rest --method put -o none --body "@$BUILD/onedeploy.json" >/dev/null \
      --url "https://management.azure.com$APP_ID/extensions/onedeploy?api-version=2024-04-01" 2>"$BUILD/err"; then
    break
  elif grep -q DeploymentConflict "$BUILD/err" && (( attempt < 20 )); then
    echo "Waiting for an earlier deployment to finish..."; sleep 15
  else
    head -c 600 "$BUILD/err"; echo; exit 1
  fi
done
echo "Deployment of build $BUILD_ID started (remote build)"

step "5. Wait for the new build"
LIVE=""
for _ in $(seq 1 40); do # remote build plus cold start; /api/health is anonymous and only returns the build ID
  LIVE=$(curl -s --max-time 20 "https://$HOST/api/health" | "$PY" -c 'import json, sys
try: print(json.load(sys.stdin)["build"])
except Exception: pass')
  [[ $LIVE == "$BUILD_ID" ]] && break
  sleep 10
done
[[ $LIVE == "$BUILD_ID" ]] || { echo "Build $BUILD_ID isn't live yet (live: ${LIVE:-none}); re-run this script in a few minutes."; exit 1; }
echo "Build $BUILD_ID is live"
az storage blob delete --account-name "$STORAGE" --account-key "$STORAGE_KEY" -c feedback-deploy -n function.zip \
  -o none --only-show-errors

step "6. MCP key in a Foundry project connection"
KEY=""
for _ in $(seq 1 18); do # the MCP extension creates its system key when it first starts
  KEY=$(az functionapp keys list -n "$APP" -g "$RG" --query "systemKeys.mcp_extension" -o tsv 2>/dev/null)
  [[ -n $KEY && $KEY != None ]] && break
  KEY=""; sleep 10
done
[[ -n $KEY ]] || { echo "The mcp_extension system key didn't appear; re-run this script."; exit 1; }
umask 077
CONNECTIONS="https://management.azure.com$ACCOUNT_ID/projects/$PROJECT/connections"
K="$KEY" T="https://$HOST/runtime/webhooks/mcp" "$PY" -c '
import json, os, sys
json.dump({"properties": {"category": "RemoteTool", "authType": "CustomKeys", "target": os.environ["T"],
           "isSharedToAll": False, "credentials": {"keys": {"x-functions-key": os.environ["K"]}},
           "metadata": {"type": "generic_mcp"}}},
          open(sys.argv[1], "w"))' "$BUILD/connection.json"
# RemoteTool + generic_mcp is what the portal's "Add tool > Custom > MCP" creates, so the portal lists the tool
if ! az rest --method put -o none --body "@$BUILD/connection.json" --url "$CONNECTIONS/$CONNECTION?api-version=2025-06-01" \
    2>"$BUILD/err"; then # an older Custom keys connection of the same name can't change category in place
  az rest --method delete -o none --url "$CONNECTIONS/$CONNECTION?api-version=2025-06-01"
  az rest --method put -o none --body "@$BUILD/connection.json" --url "$CONNECTIONS/$CONNECTION?api-version=2025-06-01"
fi
echo "Connection $CONNECTION holds the key (sent as the x-functions-key header)"
if az rest --method get -o none --url "$CONNECTIONS/$OLD_CONNECTION?api-version=2025-06-01" 2>/dev/null; then
  az rest --method delete -o none --url "$CONNECTIONS/$OLD_CONNECTION?api-version=2025-06-01"
  echo "Removed the old connection $OLD_CONNECTION (OpenAPI version)"
fi

step "7. MCP smoke test and .env"
MCP_URL="https://$HOST/runtime/webhooks/mcp"
printf 'x-functions-key: %s\n' "$KEY" > "$BUILD/key-header" # keeps the key off the command line
mcp_post() { # mcp_post JSON [SESSION_ID]: one streamable-HTTP JSON-RPC request; prints the body
  curl -s --max-time 30 -D "$BUILD/mcp-headers" -X POST "$MCP_URL" -H "@$BUILD/key-header" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    ${2:+-H "Mcp-Session-Id: $2"} -d "$1"
}
mcp_post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1"}}}' >/dev/null
SESSION=$(tr -d '\r' < "$BUILD/mcp-headers" | awk -F': ' 'tolower($1) == "mcp-session-id" { print $2 }')
mcp_post '{"jsonrpc":"2.0","method":"notifications/initialized"}' "$SESSION" >/dev/null
TOOLS=$(mcp_post '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' "$SESSION")
for tool in log_feedback query_oracle_db update_oracle_incident propose_kb_file_update; do
  grep -q "\"$tool\"" <<<"$TOOLS" || { echo "MCP tools/list didn't include $tool: $(head -c 400 <<<"$TOOLS")"; exit 1; }
done
echo "MCP server lists log_feedback, query_oracle_db, update_oracle_incident and propose_kb_file_update"
QUERY='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_oracle_db","arguments":{"sql_query":"SELECT COUNT(*) AS INCIDENTS FROM SYSTEM_INCIDENTS","reason":"deploy check"}}}'
for attempt in $(seq 1 12); do # a new role assignment can take a few minutes to reach the storage account
  RESULT=$(mcp_post "$QUERY" "$SESSION" | "$PY" -c '
import json, sys
body = sys.stdin.read()
lines = [l[5:] for l in body.splitlines() if l.startswith("data:")] or [body]  # SSE or plain JSON
try:
    out = json.loads(json.loads(lines[-1])["result"]["content"][0]["text"])
    print(str(out["rows"][0]["INCIDENTS"]) + " incidents" if out.get("success") else out.get("error") or out)
except Exception:
    print(body[:300])')
  [[ $RESULT == *" incidents" ]] && break
  (( attempt < 12 )) && { echo "Query not working yet (${RESULT:0:120}); retrying..."; sleep 20; }
done
[[ $RESULT == *" incidents" ]] || { echo "query_oracle_db failed: $RESULT"; exit 1; }
echo "query_oracle_db works: $RESULT in the mock database"
NOKEY=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$MCP_URL" -H 'Content-Type: application/json' -d '{}')
echo "Without the key: HTTP $NOKEY (expected 401)"
cp -p .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
env_unset FEEDBACK_FUNCTION_URL
env_set FEEDBACK_MCP_URL "$MCP_URL"
env_set FEEDBACK_CONNECTION_NAME "$CONNECTION"
env_set FEEDBACK_TABLE_ENDPOINT "https://$STORAGE.table.core.windows.net"
env_set FEEDBACK_TABLE_NAME "$TABLE"
env_set DB_MODE azure_blob
env_set ORACLE_MOCK_BLOB_URL "$DB_BLOB_URL"
env_set KB_MODE azure_blob
env_set KB_BLOB_CONTAINER_URL "$KB_CONTAINER_URL"
echo "Wrote FEEDBACK_*, DB_MODE, ORACLE_MOCK_BLOB_URL, KB_MODE and KB_BLOB_CONTAINER_URL to .env. The next app"
echo "start creates an agent version that uses the MCP server for log_feedback, the database tools and KB updates."
