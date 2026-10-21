#!/usr/bin/env bash
# az_tui.sh - Azure & Microsoft Foundry Manager TUI for foundry-agent-demo
# Sign-in, resource groups, resource browsing, Foundry resources / projects /
# model deployments, access checks, this demo's .env, tests and Streamlit UI, its feedback log,
# and the subscription's cost, free credit, invoices and budgets.
#
# Options: AZ_BIN=/path/to/az  PYTHON=/path/to/python
set -uo pipefail

# ── colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

# ── helpers ────────────────────────────────────────────────────────────────────
die()   { echo -e "${RED}Error: $*${RESET}" >&2; exit 1; }
info()  { echo -e "${CYAN}$*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $*${RESET}"; }
pause() { echo; read -rp "Press Enter to continue…" || true; }

require() {
  for cmd in "$@"; do
    command -v "$cmd" &>/dev/null || die "'$cmd' is required but not installed."
  done
}

ask() { # ask VAR PROMPT [DEFAULT] — reads a value into VAR
  local __v
  read -rp "  $2${3:+ [$3]}: " __v || return 1
  printf -v "$1" '%s' "${__v:-${3:-}}"
}

confirm() { local yn; read -rp "  $1 (y/n): " yn || return 1; [[ $yn == [yY]* ]]; }

confirm_name() { # confirm_name WHAT NAME — destructive actions need the name typed back
  warn "Delete $1? This is IRREVERSIBLE."
  local c; read -rp "  Type '$2' to confirm: " c || return 1
  [[ $c == "$2" ]] || { warn "Mismatch — aborted"; return 1; }
}

run() { # run CMD... — shows the command, runs it, reports the result
  echo -e "${DIM}\$ $(printf '%q ' "$@")${RESET}"
  "$@"; local rc=$?
  if (( rc == 0 )); then ok "Done"; else warn "Command failed (exit $rc)"; fi
  return "$rc"
}

run_in_project() { # run_in_project CMD... — runs in the project dir; Ctrl+C stops only the command
  echo -e "${DIM}\$ cd $PROJECT_DIR && $(printf '%q ' "$@")${RESET}"
  trap 'true' INT
  (cd "$PROJECT_DIR" && "$@"); local rc=$?
  trap - INT
  if (( rc == 0 )); then ok "Done"; else warn "Exited with code $rc"; fi
  return "$rc"
}

# table [--num] < TSV — first line is the header; aligned columns, coloured states
table() {
  local num=0; [[ ${1:-} == --num ]] && num=1
  awk -F'\t' -v num="$num" \
      -v B="$(printf '%b' "$BOLD")" -v N="$(printf '%b' "$RESET")" -v D="$(printf '%b' "$DIM")" \
      -v G="$(printf '%b' "$GREEN")" -v Y="$(printf '%b' "$YELLOW")" -v E="$(printf '%b' "$RED")" '
    {
      for (i = 1; i <= NF; i++) {
        v = $i; if (length(v) > 46) v = substr(v, 1, 43) "..."
        cell[NR, i] = v; if (length(v) > w[i]) w[i] = length(v)
      }
      if (NF > nf) nf = NF; rows = NR
    }
    END {
      if (rows < 2) { print "  " D "(none found)" N; exit }
      line = "  " (num ? sprintf("%-4s", "#") : "")
      for (i = 1; i <= nf; i++) line = line sprintf("%-" w[i] "s  ", cell[1, i])
      print B line N
      total = (num ? 4 : 0); for (i = 1; i <= nf; i++) total += w[i] + 2
      sep = "  "; for (i = 2; i < total; i++) sep = sep "─"; print sep
      for (r = 2; r <= rows; r++) {
        line = "  " (num ? sprintf("%-4s", r - 1) : "")
        for (i = 1; i <= nf; i++) {
          v = cell[r, i]; pad = sprintf("%-" w[i] "s  ", v); c = ""
          if (v ~ /^(Succeeded|Enabled|yes|true|default)$/) c = G
          else if (v ~ /^(Failed|Disabled|Deleted|no|false)$/) c = E
          else if (v ~ /^(Creating|Updating|Deleting|Accepted|Moving|Canceled)$/) c = Y
          line = line (c != "" ? c pad N : pad)
        }
        print line
      }
      print "  " D "Total: " (rows - 1) N
    }'
}

# pick TITLE JSON HEADER JQ — numbered table of a JSON array; sets PICKED (0-based)
PICKED=""
pick() {
  local title=$1 json=$2 hdr=$3 filter=$4 count sel
  count=$(jq 'length' <<<"$json")
  (( count > 0 )) || { warn "Nothing found."; return 1; }
  echo
  echo -e "  ${BOLD}${title}${RESET}"
  { printf '%s\n' "$hdr"; jq -r ".[] | $filter" <<<"$json"; } | table --num
  echo
  read -rp "  Select (1-${count}, q=cancel): " sel || return 1
  [[ -z $sel || $sel == [qQ] ]] && return 1
  if ! [[ $sel =~ ^[0-9]+$ ]] || (( sel < 1 || sel > count )); then warn "Invalid selection"; return 1; fi
  PICKED=$(( sel - 1 ))
}

# ── project, state and .env ────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/az_tui"
STATE_FILE="$STATE_DIR/state"
AZ="${AZ_BIN:-az}"
PY="${PYTHON:-python3}"
[[ -z ${PYTHON:-} && -x "$PROJECT_DIR/.venv/bin/python" ]] && PY="$PROJECT_DIR/.venv/bin/python"  # project venv wins
AZ_VENV="$HOME/.local/share/az-cli-venv"

# Selections, remembered between runs
SEL_SUB="" SEL_RG="" SEL_LOC="" SEL_FOUNDRY="" SEL_PROJECT="" SEL_DEPLOY=""
# Results from this session, shown on the setup checklist
SMOKE_RESULT="" TEST_RESULT=""

# shellcheck source=/dev/null
load_state() { [[ -f $STATE_FILE ]] && source "$STATE_FILE"; return 0; }

save_state() {
  mkdir -p "$STATE_DIR"
  local v
  for v in SEL_SUB SEL_RG SEL_LOC SEL_FOUNDRY SEL_PROJECT SEL_DEPLOY; do
    printf '%s=%q\n' "$v" "${!v}"
  done > "$STATE_FILE"
}

clear_selection() { SEL_RG="" SEL_LOC="" SEL_FOUNDRY="" SEL_PROJECT="" SEL_DEPLOY=""; save_state; }

env_get() { # env_get KEY — value from .env without surrounding quotes
  [[ -f $ENV_FILE ]] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -E 's/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/'
}

ENV_BACKED_UP=""
env_set() { # env_set KEY VALUE — replaces or appends KEY="VALUE"; backs up .env once per session
  local key=$1 val=$2 tmp
  if [[ ! -f $ENV_FILE ]]; then
    if [[ -f $PROJECT_DIR/.env.example ]]; then cp "$PROJECT_DIR/.env.example" "$ENV_FILE"; else : > "$ENV_FILE"; fi
    chmod 600 "$ENV_FILE"
  elif [[ -z $ENV_BACKED_UP ]]; then
    cp -p "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  ENV_BACKED_UP=1
  tmp=$(mktemp)
  K="$key" V="$val" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"]; done = 0 }
    index($0, k "=") == 1 { print k "=\"" v "\""; done = 1; next }
    { print }
    END { if (!done) print k "=\"" v "\"" }' "$ENV_FILE" > "$tmp" && cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

env_has_project() { [[ -n $(env_get PROJECT_ENDPOINT) ]]; }

# SDK_CACHE is filled in the main shell (startup and after pip install) so the
# $(sdk_state) subshells used while drawing menus don't each start Python.
SDK_CACHE=""
sdk_load() { # sets SDK_CACHE: missing | old <ver> (1.x beta) | ok <ver> (2.x, what agent_service.py needs)
  SDK_CACHE=$("$PY" - <<'EOF' 2>/dev/null || echo missing
from importlib.metadata import version
v = version("azure-ai-projects")
from azure.ai.projects import AIProjectClient
print(("old " if hasattr(AIProjectClient, "from_connection_string") else "ok ") + v)
EOF
)
}
sdk_state() { [[ -n $SDK_CACHE ]] || sdk_load; printf '%s' "$SDK_CACHE"; }

# ── Azure context ──────────────────────────────────────────────────────────────
AZ_OK=""
az_works() { # cached once it succeeds; rechecked after a failure so a fresh install is noticed
  [[ $AZ_OK == "$AZ" ]] && return 0
  "$AZ" version -o none >/dev/null 2>&1 && AZ_OK=$AZ
}
az_version() { "$AZ" version --query '"azure-cli"' -o tsv 2>/dev/null; }

ACCT_USER="" ACCT_TYPE="" ACCT_SUB_NAME="" ACCT_SUB_ID="" ACCT_STATE=""
refresh_account_info() {
  ACCT_USER="" ACCT_TYPE="" ACCT_SUB_NAME="" ACCT_SUB_ID=""
  if ! az_works; then ACCT_STATE="Azure CLI not working ($AZ) — use menu 9"; return 1; fi
  local j
  j=$("$AZ" account show -o json 2>/dev/null) || { ACCT_STATE="not signed in — use menu 2"; return 1; }
  ACCT_USER=$(jq -r '.user.name' <<<"$j"); ACCT_TYPE=$(jq -r '.user.type' <<<"$j")
  ACCT_SUB_NAME=$(jq -r '.name' <<<"$j"); ACCT_SUB_ID=$(jq -r '.id' <<<"$j")
  # Selections belong to one subscription
  if [[ $ACCT_SUB_ID != "$SEL_SUB" ]]; then clear_selection; SEL_SUB=$ACCT_SUB_ID; save_state; fi
}

azj() { # azj ARGS... — prints az JSON output; on failure prints the error and returns non-zero
  local out errf rc
  errf=$(mktemp)
  out=$("$AZ" "$@" --only-show-errors -o json 2>"$errf"); rc=$?
  if (( rc )); then
    warn "az $* failed:" >&2
    tail -n 15 "$errf" | sed 's/^/    /' >&2
  else
    printf '%s' "$out"
  fi
  rm -f "$errf"
  return "$rc"
}

need_az() {
  az_works && return 0
  warn "Azure CLI isn't working ($AZ). Use menu 9 to install or repair it."
  return 1
}

need_login() {
  [[ -n $ACCT_USER ]] && return 0
  need_az || return 1
  refresh_account_info && return 0
  warn "You're not signed in to Azure."
  confirm "Sign in now (you'll open a link in any browser)?" || return 1
  login_paste
  [[ -n $ACCT_USER ]]
}

need_rg() { need_login || return 1; [[ -n $SEL_RG ]] || rg_select; [[ -n $SEL_RG ]]; }
need_foundry() { need_login || return 1; [[ -n $SEL_FOUNDRY ]] || foundry_select; [[ -n $SEL_FOUNDRY ]]; }
need_project() { need_foundry || return 1; [[ -n $SEL_PROJECT ]] || project_select; [[ -n $SEL_PROJECT ]]; }
need_deploy() { need_foundry || return 1; [[ -n $SEL_DEPLOY ]] || deploy_select; [[ -n $SEL_DEPLOY ]]; }

PICKED_LOC=""
pick_location() { # pick_location [DEFAULT] — sets PICKED_LOC
  local -a regions=(eastus2 eastus swedencentral westus3 westus northcentralus southcentralus
                    uksouth francecentral australiaeast japaneast canadaeast)
  local def=${1:-eastus2} i sel
  echo
  info "Regions (model availability differs; East US 2 and Sweden Central usually offer the most):"
  for i in "${!regions[@]}"; do printf "  %2d) %s\n" $(( i + 1 )) "${regions[$i]}"; done
  echo "   o) Other region"
  read -rp "  Choice (Enter = $def): " sel || return 1
  if [[ -z $sel ]]; then PICKED_LOC=$def
  elif [[ $sel == [oO] ]]; then ask PICKED_LOC "Region name (az account list-locations -o table)" "$def" || return 1
  elif [[ $sel =~ ^[0-9]+$ ]] && (( sel >= 1 && sel <= ${#regions[@]} )); then PICKED_LOC=${regions[$(( sel - 1 ))]}
  else warn "Invalid selection"; return 1
  fi
}

# ── header ─────────────────────────────────────────────────────────────────────
header() {
  clear
  echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}║       Azure & Foundry Manager (TUI)          ║${RESET}"
  echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
  if [[ -n $ACCT_USER ]]; then
    echo -e "  Account:       ${GREEN}${ACCT_USER}${RESET}  ${DIM}(${ACCT_TYPE})${RESET}"
    echo -e "  Subscription:  ${GREEN}${ACCT_SUB_NAME}${RESET}  ${DIM}(${ACCT_SUB_ID})${RESET}"
  else
    echo -e "  Account:       ${RED}${ACCT_STATE}${RESET}"
  fi
  echo -e "  Group:         ${GREEN}${SEL_RG:--}${RESET}  Foundry: ${GREEN}${SEL_FOUNDRY:--}${RESET}  Project: ${GREEN}${SEL_PROJECT:--}${RESET}  Model: ${GREEN}${SEL_DEPLOY:--}${RESET}"
  local mode; mode=$(env_get AGENT_MODE)
  echo -e "  Demo:          AGENT_MODE=${GREEN}${mode:-demo}${RESET}$( [[ -f $ENV_FILE ]] || printf ' (no .env yet)')  ${DIM}${PROJECT_DIR}${RESET}"
  echo
}

next_step() {
  if ! az_works; then echo "Install or repair the Azure CLI (menu 9)."; return; fi
  if [[ -z $ACCT_USER ]]; then echo "Sign in to Azure (menu 2)."; return; fi
  if [[ -z $SEL_FOUNDRY ]]; then echo "Create or select a Foundry resource (menu 5)."; return; fi
  if [[ -z $SEL_DEPLOY ]]; then echo "Deploy a model, e.g. gpt-4o (menu 6)."; return; fi
  if [[ -z $SEL_PROJECT ]]; then echo "Create or select a Foundry project (menu 5)."; return; fi
  if ! env_has_project; then echo "Run the smoke test (menu 7), then write the project to .env (menu 8)."; return; fi
  case $(sdk_state) in
    ok*) echo "Run the test suite, then launch Streamlit (menu 8)." ;;
    *) echo "Install the Python requirements (menu 8)." ;;
  esac
}

# ══════════════════════════════════════════════════════════════════════════════
#  1) SETUP CHECKLIST
# ══════════════════════════════════════════════════════════════════════════════

STEP_NAMES=(
  "Azure CLI works"
  "Signed in to Azure"
  "Resource group selected"
  "Foundry resource selected"
  "Model deployment selected"
  "Foundry project selected"
  "You have a data-plane role (Foundry User)"
  "Smoke test: one chat call to the deployment"
  "PROJECT_ENDPOINT written to .env"
  "azure-ai-projects 2.x installed"
  "Run the test suite"
  "Launch the Streamlit UI"
)

step_check() { # step_check N — sets STEP_STATE (pass | todo | run) and STEP_DETAIL
  STEP_STATE=todo STEP_DETAIL=""
  case $1 in
    1) if az_works; then STEP_STATE=pass; STEP_DETAIL="azure-cli $(az_version)"; fi ;;
    2) if [[ -n $ACCT_USER ]]; then STEP_STATE=pass; STEP_DETAIL=$ACCT_USER; fi ;;
    3) if [[ -n $SEL_RG ]]; then STEP_STATE=pass; STEP_DETAIL=$SEL_RG; fi ;;
    4) if [[ -n $SEL_FOUNDRY ]]; then STEP_STATE=pass; STEP_DETAIL=$SEL_FOUNDRY; fi ;;
    5) if [[ -n $SEL_DEPLOY ]]; then STEP_STATE=pass; STEP_DETAIL=$SEL_DEPLOY; fi ;;
    6) if [[ -n $SEL_PROJECT ]]; then STEP_STATE=pass; STEP_DETAIL=$SEL_PROJECT; fi ;;
    7) if [[ -n $ACCT_USER ]] && has_ai_role; then STEP_STATE=pass; STEP_DETAIL=$HAS_ROLE; fi ;;
    8) STEP_STATE=run; STEP_DETAIL=${SMOKE_RESULT:-not run this session} ;;
    9) if env_has_project; then STEP_STATE=pass; STEP_DETAIL=$(env_get PROJECT_ENDPOINT); fi ;;
    10) STEP_DETAIL=$(sdk_state); if [[ $STEP_DETAIL == ok* ]]; then STEP_STATE=pass; fi ;;
    11) STEP_STATE=run; STEP_DETAIL=${TEST_RESULT:-not run this session} ;;
    12) STEP_STATE=run; STEP_DETAIL="Ctrl+C stops it" ;;
  esac
}

step_do() {
  case $1 in
    1) menu_install ;;
    2) need_az && login_paste ;;
    3) choose_or_create "resource group" rg_select rg_create ;;
    4) choose_or_create "Foundry resource" foundry_select foundry_create ;;
    5) choose_or_create "model deployment" deploy_select deploy_create ;;
    6) choose_or_create "Foundry project" project_select project_create ;;
    7) role_grant ;;
    8) smoke_test ;;
    9) env_write_project ;;
    10) pip_install ;;
    11) run_tests ;;
    12) run_streamlit ;;
  esac
}

choose_or_create() { # choose_or_create WHAT SELECT_FN CREATE_FN
  echo
  echo "  1) Use an existing $1"
  echo "  2) Create a new $1"
  local c; read -rp "  Choice: " c || return
  case $c in
    1) "$2" ;;
    2) "$3" ;;
    *) warn "Invalid choice" ;;
  esac
}

menu_checklist() {
  while true; do
    header
    info "Checking setup…"
    local -a states=() details=()
    local i first_todo="" mark
    for i in "${!STEP_NAMES[@]}"; do
      step_check $(( i + 1 ))
      states[i]=$STEP_STATE; details[i]=$STEP_DETAIL
      [[ -z $first_todo && $STEP_STATE == todo ]] && first_todo=$(( i + 1 ))
    done
    header
    echo -e "  ${BOLD}Setup Checklist${RESET}  ${DIM}(from zero to the demo running on Foundry)${RESET}"
    echo "  ───────────────────────────────────────────────────────────────────"
    for i in "${!STEP_NAMES[@]}"; do
      case ${states[i]} in
        pass) mark="${GREEN}✓${RESET}" ;;
        todo) mark="${RED}✗${RESET}" ;;
        *)    mark="${DIM}·${RESET}" ;;
      esac
      printf "  %2d) " $(( i + 1 ))
      echo -en "$mark "
      printf "%-46s" "${STEP_NAMES[i]}"
      echo -e "${DIM}${details[i]:0:60}${RESET}"
    done
    echo "  ───────────────────────────────────────────────────────────────────"
    if [[ -n $first_todo ]]; then
      echo -e "   n) Do the next open step (${YELLOW}${first_todo}${RESET})"
    else
      echo -e "  ${GREEN}All setup steps are done.${RESET} Steps 8, 11 and 12 can be run any time."
    fi
    echo "  1-12) Do that step"
    echo "   0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case $c in
      0) return ;;
      n|N) if [[ -n $first_todo ]]; then step_do "$first_todo"; fi ;;
      *) if [[ $c =~ ^[0-9]+$ ]] && (( c >= 1 && c <= ${#STEP_NAMES[@]} )); then step_do "$c"; else warn "Invalid choice"; fi ;;
    esac
    pause
  done
}

# ══════════════════════════════════════════════════════════════════════════════
#  2) SIGN-IN & SUBSCRIPTION
# ══════════════════════════════════════════════════════════════════════════════

menu_login() {
  while true; do
    header
    echo -e "  ${BOLD}Sign-in & Subscription${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) Sign in with a link opened on any computer (SSH; works where device codes are blocked)"
    echo "  2) Sign in with a device code (some tenants block this)"
    echo "  3) Sign in with a browser on this machine"
    echo "  4) Sign in as a service principal"
    echo "  5) Switch subscription"
    echo "  6) Account details"
    echo "  7) Check that an access token can be issued"
    echo "  8) Sign out"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) need_az && login_paste ;;
      2) need_az && login_device ;;
      3) need_az && { run "$AZ" login; refresh_account_info; } ;;
      4) need_az && login_sp ;;
      5) subscription_switch ;;
      6) need_login && "$AZ" account show -o json | jq . ;;
      7) need_login && run "$AZ" account get-access-token --query "{expiresOn:expiresOn, tenant:tenant, subscription:subscription}" -o table ;;
      8) confirm "Sign out of the Azure CLI?" && run "$AZ" logout && { clear_selection; refresh_account_info; } ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

login_device() {
  local tenant
  ask tenant "Tenant ID or domain (Enter = your home tenant)" "" || return
  run "$AZ" login --use-device-code ${tenant:+--tenant "$tenant"}
  if refresh_account_info; then ok "Signed in as $ACCT_USER"; fi
}

login_paste() { # browser sign-in without a browser here: open the link anywhere, paste back where it lands
  local def tenant dir pid url pasted rc i
  def=$("$AZ" account show --query tenantId -o tsv 2>/dev/null)
  ask tenant "Tenant ID or domain (Enter = ${def:-your home tenant})" "$def" || return
  dir=$(mktemp -d) || return 1
  cat > "$dir/browser" <<'EOF'
#!/bin/sh
# Stands in for a web browser: saves the sign-in link instead of opening it
printf '%s\n' "$1" > "$(dirname "$0")/url"
EOF
  chmod 700 "$dir/browser"
  # With BROWSER set, az uses its normal browser sign-in (MFA works) and waits on a localhost port
  BROWSER="$dir/browser" "$AZ" login ${tenant:+--tenant "$tenant"} -o none >"$dir/log" 2>&1 &
  pid=$!
  for i in $(seq 1 60); do
    [[ -s $dir/url ]] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if [[ ! -s $dir/url ]]; then
    warn "az login didn't produce a sign-in link:"; sed 's/^/    /' "$dir/log"
    kill "$pid" 2>/dev/null; rm -rf "$dir"; return 1
  fi
  # Ask for the result in the address bar (query) instead of a form POST, so it can be copied
  url=$(sed 's/response_mode=form_post/response_mode=query/' "$dir/url")
  echo
  info "1. Open this link in a browser on any computer and sign in:"
  echo; echo "$url"; echo
  info "2. The browser ends on a page that doesn't load, at http://localhost:<port>/?code=… — that's expected."
  info "   Copy that whole address from the address bar and paste it here. It works once, for this sign-in only."
  echo
  read -rp "  Address (Enter = cancel): " pasted || pasted=""
  if [[ $pasted =~ ^http://localhost:([0-9]+)/?\?(.*code=.*)$ ]]; then
    # az's listener only accepts the form POST a browser would have sent; hand it the same fields
    curl -s -o /dev/null --max-time 30 -X POST -H 'Content-Type: application/x-www-form-urlencoded' \
      --data-binary "${BASH_REMATCH[2]}" "http://localhost:${BASH_REMATCH[1]}/" \
      || warn "Couldn't reach the waiting sign-in on port ${BASH_REMATCH[1]}."
    wait "$pid"; rc=$?
    if (( rc == 0 )) && refresh_account_info; then
      ok "Signed in as $ACCT_USER"
    else
      warn "Sign-in didn't complete:"; tail -n 5 "$dir/log" | sed 's/^/    /'
    fi
  else
    if [[ $pasted == *error=* ]]; then
      warn "Sign-in was refused: $(sed -nE 's/.*error_description=([^&]*).*/\1/p' <<<"$pasted" | sed 's/+/ /g; s/%20/ /g' | cut -c1-300)"
    elif [[ -n $pasted ]]; then
      warn "That isn't the http://localhost:<port>/?code=… address; nothing was sent."
    fi
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  fi
  rm -rf "$dir"
}

login_sp() {
  local app tenant secret
  ask app "Service principal application (client) ID" "" || return
  ask tenant "Tenant ID" "" || return
  read -rsp "  Client secret: " secret || return; echo
  [[ -n $app && -n $tenant && -n $secret ]] || { warn "All three values are required."; return 1; }
  echo -e "${DIM}\$ az login --service-principal -u $app -p •••••••• --tenant $tenant${RESET}"
  if "$AZ" login --service-principal -u "$app" -p "$secret" --tenant "$tenant" -o table; then ok "Signed in"; else warn "Sign-in failed"; fi
  refresh_account_info
}

subscription_switch() {
  need_login || return
  info "Fetching subscriptions…"
  local json
  json=$(azj account list --query "[?state=='Enabled']") || return
  pick "Subscriptions" "$json" $'Name\tSubscription ID\tCurrent' \
    '"\(.name)\t\(.id)\t\(if .isDefault then "yes" else "-" end)"' || return
  run "$AZ" account set --subscription "$(jq -r ".[$PICKED].id" <<<"$json")" && refresh_account_info
}

# ══════════════════════════════════════════════════════════════════════════════
#  3) RESOURCE GROUPS
# ══════════════════════════════════════════════════════════════════════════════

menu_rg() {
  while true; do
    header
    echo -e "  ${BOLD}Resource Groups${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) List resource groups"
    echo "  2) Select resource group"
    echo "  3) Create resource group"
    echo "  4) Resources in the selected group"
    echo "  5) Delete the selected group"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) rg_list ;;
      2) rg_select ;;
      3) rg_create ;;
      4) need_rg && res_table "Resources in $SEL_RG" -g "$SEL_RG" ;;
      5) rg_delete ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

RG_HDR=$'Name\tLocation\tState'
RG_ROW='"\(.name)\t\(.location)\t\(.properties.provisioningState // "-")"'

rg_list() {
  need_login || return
  info "Fetching resource groups…"
  local json; json=$(azj group list --query "sort_by(@, &name)") || return
  echo
  { printf '%s\n' "$RG_HDR"; jq -r ".[] | $RG_ROW" <<<"$json"; } | table
}

rg_select() {
  need_login || return 1
  info "Fetching resource groups…"
  local json; json=$(azj group list --query "sort_by(@, &name)") || return 1
  pick "Resource groups" "$json" "$RG_HDR" "$RG_ROW" || return 1
  SEL_RG=$(jq -r ".[$PICKED].name" <<<"$json"); SEL_LOC=$(jq -r ".[$PICKED].location" <<<"$json")
  SEL_FOUNDRY="" SEL_PROJECT="" SEL_DEPLOY=""
  save_state
  ok "Selected resource group $SEL_RG ($SEL_LOC)"
}

rg_create() {
  need_login || return 1
  local name
  ask name "New resource group name" "rg-foundry-agent-demo" || return 1
  pick_location "${SEL_LOC:-eastus2}" || return 1
  confirm "Create resource group '$name' in $PICKED_LOC?" || return 1
  run "$AZ" group create -n "$name" -l "$PICKED_LOC" -o table || return 1
  SEL_RG=$name SEL_LOC=$PICKED_LOC SEL_FOUNDRY="" SEL_PROJECT="" SEL_DEPLOY=""
  save_state
}

rg_delete() {
  need_rg || return
  confirm_name "resource group '$SEL_RG' and EVERY resource in it" "$SEL_RG" || return
  run "$AZ" group delete -n "$SEL_RG" --yes --no-wait || return
  info "Deletion runs in the background and can take several minutes."
  info "Foundry resources inside it are soft-deleted; purge them (menu 5) to reuse their names."
  clear_selection
}

# ══════════════════════════════════════════════════════════════════════════════
#  4) BROWSE RESOURCES
# ══════════════════════════════════════════════════════════════════════════════

RES_HDR=$'Name\tType\tGroup\tLocation'
RES_ROW='"\(.name)\t\(.type | sub("^Microsoft\\."; ""))\t\(.resourceGroup)\t\(.location // "-")"'

menu_browse() {
  while true; do
    header
    echo -e "  ${BOLD}Browse Resources${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) All resources in the subscription"
    echo "  2) Resources in the selected group"
    echo "  3) Search by name or type"
    echo "  4) Resource details (JSON + portal link)"
    echo "  5) Count resources by type"
    echo "  6) Delete a resource"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) need_login && res_table "All resources" ;;
      2) need_rg && res_table "Resources in $SEL_RG" -g "$SEL_RG" ;;
      3) res_search ;;
      4) res_details ;;
      5) res_types ;;
      6) res_delete ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

res_table() { # res_table TITLE [az resource list args...]
  local title=$1; shift
  info "Fetching resources…"
  local json; json=$(azj resource list "$@") || return
  echo; echo -e "  ${BOLD}${title}${RESET}"
  { printf '%s\n' "$RES_HDR"; jq -r "sort_by(.resourceGroup, .type, .name) | .[] | $RES_ROW" <<<"$json"; } | table
}

res_search() {
  need_login || return
  local q; ask q "Text to find in name or type (e.g. foundry, cognitive, storage)" "" || return
  info "Fetching resources…"
  local json; json=$(azj resource list) || return
  echo
  { printf '%s\n' "$RES_HDR"
    jq -r --arg q "${q,,}" "[.[] | select((.name | ascii_downcase | contains(\$q)) or (.type | ascii_downcase | contains(\$q)))]
      | sort_by(.type, .name) | .[] | $RES_ROW" <<<"$json"; } | table
}

PICKED_RES=""
res_pick() { # sets PICKED_RES to the chosen resource's JSON
  need_login || return 1
  echo
  echo "  Pick from:"
  echo "  1) Selected group (${SEL_RG:-none})"
  echo "  2) Whole subscription"
  local c json; read -rp "  Choice: " c || return 1
  case $c in
    1) need_rg || return 1; info "Fetching resources…"; json=$(azj resource list -g "$SEL_RG") || return 1 ;;
    2) info "Fetching resources…"; json=$(azj resource list) || return 1 ;;
    *) warn "Invalid choice"; return 1 ;;
  esac
  json=$(jq 'sort_by(.type, .name)' <<<"$json")
  pick "Resources" "$json" "$RES_HDR" "$RES_ROW" || return 1
  PICKED_RES=$(jq ".[$PICKED]" <<<"$json")
}

res_details() {
  res_pick || return
  local id; id=$(jq -r .id <<<"$PICKED_RES")
  info "Fetching details…"
  echo
  echo -e "  Portal: ${CYAN}https://portal.azure.com/#@/resource${id}${RESET}"
  echo
  azj resource show --ids "$id" | jq -C .
}

res_types() {
  need_login || return
  info "Fetching resources…"
  local json; json=$(azj resource list) || return
  echo
  { printf 'Count\tType\n'; jq -r 'group_by(.type) | map({n: length, t: .[0].type}) | sort_by(-.n) | .[] | "\(.n)\t\(.t)"' <<<"$json"; } | table
}

res_delete() {
  res_pick || return
  local name type; name=$(jq -r .name <<<"$PICKED_RES"); type=$(jq -r .type <<<"$PICKED_RES")
  confirm_name "$type '$name'" "$name" || return
  run "$AZ" resource delete --ids "$(jq -r .id <<<"$PICKED_RES")"
}

# ══════════════════════════════════════════════════════════════════════════════
#  5) FOUNDRY RESOURCES & PROJECTS
# ══════════════════════════════════════════════════════════════════════════════

menu_foundry() {
  while true; do
    header
    echo -e "  ${BOLD}Foundry Resources & Projects${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) List Foundry resources"
    echo "  2) Select Foundry resource"
    echo "  3) Create Foundry resource"
    echo "  4) Overview (endpoints, deployments, projects)"
    echo "  ─────────────────────────────────"
    echo "  5) List projects"
    echo "  6) Select project"
    echo "  7) Create project"
    echo "  ─────────────────────────────────"
    echo "  8) Delete the selected Foundry resource"
    echo "  9) Purge a soft-deleted Foundry resource"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) foundry_list ;;
      2) foundry_select ;;
      3) foundry_create ;;
      4) foundry_overview ;;
      5) need_foundry && { info "Fetching projects in $SEL_FOUNDRY…"; echo; project_table; } ;;
      6) project_select ;;
      7) project_create ;;
      8) foundry_delete ;;
      9) foundry_purge ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

FOUNDRY_HDR=$'Name\tGroup\tRegion\tProjects\tState'
FOUNDRY_ROW='"\(.name)\t\(.resourceGroup)\t\(.location)\t\(if .properties.allowProjectManagement then "yes" else "no" end)\t\(.properties.provisioningState // "-")"'

foundry_list() {
  need_login || return
  info "Fetching Foundry resources…"
  local json; json=$(azj cognitiveservices account list --query "[?kind=='AIServices']") || return
  echo
  { printf '%s\n' "$FOUNDRY_HDR"; jq -r ".[] | $FOUNDRY_ROW" <<<"$json"; } | table
}

foundry_select() {
  need_login || return 1
  info "Fetching Foundry resources…"
  local json; json=$(azj cognitiveservices account list --query "[?kind=='AIServices']") || return 1
  pick "Foundry resources" "$json" "$FOUNDRY_HDR" "$FOUNDRY_ROW" || return 1
  SEL_FOUNDRY=$(jq -r ".[$PICKED].name" <<<"$json")
  SEL_RG=$(jq -r ".[$PICKED].resourceGroup" <<<"$json")
  SEL_LOC=$(jq -r ".[$PICKED].location" <<<"$json")
  SEL_PROJECT="" SEL_DEPLOY=""
  save_state
  ok "Selected Foundry resource $SEL_FOUNDRY ($SEL_RG, $SEL_LOC)"
  if [[ $(jq -r ".[$PICKED].properties.allowProjectManagement" <<<"$json") != true ]]; then
    warn "It was created without project management, so it can't hold Foundry projects."
    warn "Model deployments still work. Create a new Foundry resource if you need projects."
  fi
}

foundry_create() {
  need_rg || return 1
  local name
  ask name "Foundry resource name (globally unique; lowercase, digits, hyphens)" \
    "foundry-demo-$(whoami | tr -cd 'a-z0-9')-$RANDOM" || return 1
  [[ $name =~ ^[a-z0-9][a-z0-9-]{1,63}$ ]] || { warn "'$name' isn't valid: use 2-64 lowercase letters, digits and hyphens."; return 1; }
  pick_location "$SEL_LOC" || return 1
  echo
  echo -e "  Name:            ${GREEN}${name}${RESET}  ${DIM}(endpoint: ${name}.services.ai.azure.com)${RESET}"
  echo -e "  Resource group:  ${GREEN}${SEL_RG}${RESET}"
  echo -e "  Region:          ${GREEN}${PICKED_LOC}${RESET}"
  echo -e "  Kind / SKU:      AIServices / S0, project management on, system identity on"
  echo -e "  ${DIM}The resource itself is free; model calls are billed per token.${RESET}"
  confirm "Create it?" || return 1
  run "$AZ" cognitiveservices account create -n "$name" -g "$SEL_RG" -l "$PICKED_LOC" \
    --kind AIServices --sku S0 --custom-domain "$name" --allow-project-management --assign-identity --yes \
    --query "{name:name, location:location, state:properties.provisioningState, endpoint:properties.endpoint}" -o table || return 1
  SEL_FOUNDRY=$name SEL_LOC=$PICKED_LOC SEL_PROJECT="" SEL_DEPLOY=""
  save_state
}

foundry_json() { azj cognitiveservices account show -n "$SEL_FOUNDRY" -g "$SEL_RG"; }

foundry_overview() {
  need_foundry || return
  info "Fetching $SEL_FOUNDRY…"
  local acct; acct=$(foundry_json) || return
  echo
  jq -r '"  Foundry resource:  \(.name)\n  Resource group:    \(.resourceGroup)\n  Region:            \(.location)\n  SKU:               \(.sku.name)\n  State:             \(.properties.provisioningState)\n  Projects enabled:  \(.properties.allowProjectManagement // false)"' <<<"$acct"
  echo -e "  Portal:            ${CYAN}https://portal.azure.com/#@/resource$(jq -r .id <<<"$acct")${RESET}"
  echo -e "  Foundry portal:    ${CYAN}https://ai.azure.com${RESET}"
  if [[ -n $SEL_PROJECT ]]; then echo -e "  Project endpoint:  ${GREEN}$(project_endpoint)${RESET}"; fi
  echo; echo -e "  ${BOLD}Endpoints${RESET}"
  { printf 'Name\tURL\n'; jq -r '(.properties.endpoints // {}) | to_entries[] | "\(.key)\t\(.value)"' <<<"$acct"; } | table
  echo; echo -e "  ${BOLD}Model deployments${RESET}"
  deploy_table
  echo; echo -e "  ${BOLD}Projects${RESET}"
  project_table
}

foundry_delete() {
  need_foundry || return
  confirm_name "Foundry resource '$SEL_FOUNDRY' with all its projects and deployments" "$SEL_FOUNDRY" || return
  run "$AZ" cognitiveservices account delete -n "$SEL_FOUNDRY" -g "$SEL_RG" || return
  info "'$SEL_FOUNDRY' is now soft-deleted and its name stays reserved."
  if confirm "Purge it now so the name can be reused?"; then
    run "$AZ" cognitiveservices account purge -n "$SEL_FOUNDRY" -g "$SEL_RG" -l "$SEL_LOC"
  fi
  SEL_FOUNDRY="" SEL_PROJECT="" SEL_DEPLOY=""
  save_state
}

foundry_purge() {
  need_login || return
  info "Fetching soft-deleted resources…"
  local json; json=$(azj cognitiveservices account list-deleted) || return
  pick "Soft-deleted resources" "$json" $'Name\tRegion\tGroup\tKind' \
    '"\(.name)\t\(.location)\t\(.id | capture("resourceGroups/(?<g>[^/]+)").g // "-")\t\(.kind // "-")"' || return
  local name loc rg
  name=$(jq -r ".[$PICKED].name" <<<"$json"); loc=$(jq -r ".[$PICKED].location" <<<"$json")
  rg=$(jq -r ".[$PICKED].id | capture(\"resourceGroups/(?<g>[^/]+)\").g" <<<"$json")
  confirm_name "soft-deleted resource '$name' permanently" "$name" || return
  run "$AZ" cognitiveservices account purge -n "$name" -g "$rg" -l "$loc"
}

PROJECT_HDR=$'Project\tDisplay name\tDefault\tState'
PROJECT_ROW='"\(.name | split("/") | last)\t\(.properties.displayName // "-")\t\(if .properties.isDefault then "default" else "-" end)\t\(.properties.provisioningState // "-")"'

project_table() {
  local json; json=$(azj cognitiveservices account project list -n "$SEL_FOUNDRY" -g "$SEL_RG") || return
  { printf '%s\n' "$PROJECT_HDR"; jq -r ".[] | $PROJECT_ROW" <<<"$json"; } | table
}

project_select() {
  need_foundry || return 1
  info "Fetching projects in $SEL_FOUNDRY…"
  local json; json=$(azj cognitiveservices account project list -n "$SEL_FOUNDRY" -g "$SEL_RG") || return 1
  pick "Projects in $SEL_FOUNDRY" "$json" "$PROJECT_HDR" "$PROJECT_ROW" || return 1
  SEL_PROJECT=$(jq -r ".[$PICKED].name | split(\"/\") | last" <<<"$json")
  save_state
  ok "Selected project $SEL_PROJECT"
}

project_create() {
  need_foundry || return 1
  local name loc
  ask name "New project name (letters, digits, hyphens)" "agent-demo" || return 1
  loc=$("$AZ" cognitiveservices account show -n "$SEL_FOUNDRY" -g "$SEL_RG" --query location -o tsv 2>/dev/null)
  confirm "Create project '$name' in $SEL_FOUNDRY?" || return 1
  run "$AZ" cognitiveservices account project create -n "$SEL_FOUNDRY" -g "$SEL_RG" \
    --project-name "$name" -l "${loc:-$SEL_LOC}" --display-name "Foundry agent demo" \
    --query "{name:name, state:properties.provisioningState}" -o table || return 1
  SEL_PROJECT=$name
  save_state
}

project_endpoint() { # prints the selected project's endpoint URL
  local p ep sub
  p=$(azj cognitiveservices account project show -n "$SEL_FOUNDRY" -g "$SEL_RG" --project-name "$SEL_PROJECT") || return 1
  ep=$(jq -r '[(.properties.endpoints // {})[] | select(test("/api/projects/"))] | first // empty' <<<"$p")
  if [[ -z $ep ]]; then
    sub=$("$AZ" cognitiveservices account show -n "$SEL_FOUNDRY" -g "$SEL_RG" --query properties.customSubDomainName -o tsv 2>/dev/null)
    ep="https://${sub:-$SEL_FOUNDRY}.services.ai.azure.com/api/projects/$SEL_PROJECT"
  fi
  printf '%s' "$ep"
}

# ══════════════════════════════════════════════════════════════════════════════
#  6) MODEL DEPLOYMENTS & QUOTA
# ══════════════════════════════════════════════════════════════════════════════

menu_models() {
  while true; do
    header
    echo -e "  ${BOLD}Model Deployments & Quota${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) List deployments"
    echo "  2) Select deployment"
    echo "  3) Deploy a model"
    echo "  4) Delete the selected deployment"
    echo "  5) Model quota in a region"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) need_foundry && { info "Fetching deployments in $SEL_FOUNDRY…"; echo; deploy_table; } ;;
      2) deploy_select ;;
      3) deploy_create ;;
      4) deploy_delete ;;
      5) quota_show ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

DEPLOY_HDR=$'Deployment\tModel\tVersion\tSKU\tCapacity\tState'
DEPLOY_ROW='"\(.name)\t\(.properties.model.name)\t\(.properties.model.version // "-")\t\(.sku.name // "-")\t\(.sku.capacity // "-")\t\(.properties.provisioningState // "-")"'

deploy_table() {
  local json; json=$(azj cognitiveservices account deployment list -n "$SEL_FOUNDRY" -g "$SEL_RG") || return
  { printf '%s\n' "$DEPLOY_HDR"; jq -r ".[] | $DEPLOY_ROW" <<<"$json"; } | table
}

deploy_select() {
  need_foundry || return 1
  info "Fetching deployments in $SEL_FOUNDRY…"
  local json; json=$(azj cognitiveservices account deployment list -n "$SEL_FOUNDRY" -g "$SEL_RG") || return 1
  pick "Model deployments in $SEL_FOUNDRY" "$json" "$DEPLOY_HDR" "$DEPLOY_ROW" || return 1
  SEL_DEPLOY=$(jq -r ".[$PICKED].name" <<<"$json")
  save_state
  ok "Selected deployment $SEL_DEPLOY"
}

deploy_create() {
  need_foundry || return 1
  info "Fetching models available to $SEL_FOUNDRY (a few seconds)…"
  local models; models=$(azj cognitiveservices account list-models -n "$SEL_FOUNDRY" -g "$SEL_RG") || return 1
  # Chat models that can be deployed and aren't being retired; A→Z, newest version first
  models=$(jq '[.[] | select((.capabilities.chatCompletion // "") == "true")
                    | select((.skus // []) | length > 0)
                    | select((.lifecycleStatus // "") | test("Deprecat") | not)]
               | group_by(.name) | map(sort_by(.version) | reverse) | add // []' <<<"$models")
  info "The demo was written for gpt-4o; gpt-4o-mini is cheaper."
  pick "Chat models" "$models" $'Model\tVersion\tFormat\tStatus\tDefault' \
    '"\(.name)\t\(.version)\t\(.format)\t\(.lifecycleStatus // "-")\t\(if .isDefaultVersion then "default" else "-" end)"' || return 1
  local model ver fmt sku cap dname sel i note
  model=$(jq -r ".[$PICKED].name" <<<"$models"); ver=$(jq -r ".[$PICKED].version" <<<"$models"); fmt=$(jq -r ".[$PICKED].format" <<<"$models")

  # Deployment types, recommended first
  local -a skus
  mapfile -t skus < <(jq -r --argjson order '{"GlobalStandard":0,"DataZoneStandard":1,"Standard":2}' \
    "[.[$PICKED].skus[].name] | unique | sort_by(\$order[.] // 9) | .[]" <<<"$models")
  echo
  echo -e "  ${BOLD}Deployment type for $model $ver${RESET}"
  for i in "${!skus[@]}"; do
    case ${skus[$i]} in
      GlobalStandard) note="pay per token, routed globally (recommended)" ;;
      DataZoneStandard) note="pay per token, stays in the US / EU data zone" ;;
      Standard) note="pay per token, stays in this region" ;;
      *) note="" ;;
    esac
    printf "  %d) %-18s " $(( i + 1 )) "${skus[$i]}"
    echo -e "${DIM}${note}${RESET}"
  done
  read -rp "  Choice [1]: " sel || return 1
  sel=${sel:-1}
  if ! [[ $sel =~ ^[0-9]+$ ]] || (( sel < 1 || sel > ${#skus[@]} )); then warn "Invalid selection"; return 1; fi
  sku=${skus[$(( sel - 1 ))]}

  ask cap "Capacity in units of 1,000 tokens/minute (uses your quota)" "30" || return 1
  [[ $cap =~ ^[0-9]+$ ]] || { warn "Capacity must be a whole number."; return 1; }
  ask dname "Deployment name (your code calls the model by this name)" "$model" || return 1
  confirm "Deploy $model $ver as '$dname' ($sku, capacity $cap)?" || return 1
  run "$AZ" cognitiveservices account deployment create -n "$SEL_FOUNDRY" -g "$SEL_RG" \
    --deployment-name "$dname" --model-name "$model" --model-version "$ver" --model-format "$fmt" \
    --sku-name "$sku" --sku-capacity "$cap" \
    --query "{deployment:name, model:properties.model.name, version:properties.model.version, state:properties.provisioningState}" -o table || {
      warn "If the error says InsufficientQuota: lower the capacity, pick another deployment type"
      warn "or region, or check the quota (menu 6 → 5)."
      return 1
    }
  SEL_DEPLOY=$dname
  save_state
}

deploy_delete() {
  need_deploy || return
  confirm_name "model deployment '$SEL_DEPLOY'" "$SEL_DEPLOY" || return
  run "$AZ" cognitiveservices account deployment delete -n "$SEL_FOUNDRY" -g "$SEL_RG" --deployment-name "$SEL_DEPLOY" \
    && { SEL_DEPLOY=""; save_state; }
}

quota_show() {
  need_login || return
  local loc=$SEL_LOC q
  if [[ -z $loc ]]; then pick_location eastus2 || return; loc=$PICKED_LOC; fi
  ask q "Show quota lines containing (Enter = all)" "gpt-4o" || return
  info "Fetching quota for $loc…"
  local json; json=$(azj cognitiveservices usage list -l "$loc") || return
  echo; echo -e "  ${BOLD}Quota in $loc${RESET}  ${DIM}(units of 1,000 tokens per minute)${RESET}"
  { printf 'Quota\tUsed\tLimit\n'
    jq -r --arg q "${q,,}" '.[] | select(.name.value | ascii_downcase | contains($q)) | "\(.name.value)\t\(.currentValue)\t\(.limit)"' <<<"$json"; } | table
}

# ══════════════════════════════════════════════════════════════════════════════
#  7) ACCESS & SMOKE TEST
# ══════════════════════════════════════════════════════════════════════════════

menu_access() {
  while true; do
    header
    echo -e "  ${BOLD}Access & Smoke Test${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) My roles on the selected Foundry resource"
    echo "  2) Give me a role (Foundry User)"
    echo "  3) Smoke test: one chat call to the selected deployment"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) roles_show ;;
      2) role_grant ;;
      3) smoke_test ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

my_object_id() { # prints "objectId principalType" for the signed-in identity
  # Read the oid claim of the ARM token: Microsoft Graph lookups (az ad ...) can be blocked by security defaults
  local ptype=User oid
  [[ $ACCT_TYPE == servicePrincipal ]] && ptype=ServicePrincipal
  oid=$("$AZ" account get-access-token --query accessToken -o tsv 2>/dev/null | "$PY" -c '
import base64, json, sys
try:
    payload = sys.stdin.read().split(".")[1]
    print(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["oid"])
except Exception:
    pass')
  printf '%s %s' "$oid" "$ptype"
}

foundry_id() { "$AZ" cognitiveservices account show -n "$SEL_FOUNDRY" -g "$SEL_RG" --query id -o tsv 2>/dev/null; }

# Data-plane roles that let you call models and agents (Owner / Contributor alone don't).
# "Foundry User" / "Foundry Project Manager" / "Foundry Owner" were "Azure AI User" / "... Project Manager" / "... Owner".
AI_ROLES=("Foundry User" "Foundry Project Manager" "Foundry Owner" "Azure AI Developer"
          "Azure AI User" "Azure AI Project Manager" "Azure AI Owner"
          "Cognitive Services OpenAI User" "Cognitive Services OpenAI Contributor" "Cognitive Services User")
HAS_ROLE=""
has_ai_role() { # sets HAS_ROLE to the first matching role
  HAS_ROLE=""
  [[ -n $SEL_FOUNDRY ]] || return 1
  local id oid ptype
  id=$(foundry_id) || return 1
  read -r oid ptype <<<"$(my_object_id)"
  [[ -n $oid ]] || return 1
  HAS_ROLE=$("$AZ" role assignment list --assignee "$oid" --scope "$id" --include-inherited \
    --query "[].roleDefinitionName" -o tsv 2>/dev/null | grep -xF -f <(printf '%s\n' "${AI_ROLES[@]}") | head -n 1)
  [[ -n $HAS_ROLE ]]
}

roles_show() {
  need_foundry || return
  info "Fetching your role assignments on $SEL_FOUNDRY…"
  local id oid ptype json
  id=$(foundry_id) || return
  read -r oid ptype <<<"$(my_object_id)"
  json=$(azj role assignment list --assignee "$oid" --scope "$id" --include-inherited) || return
  echo
  { printf 'Role\tScope\n'; jq -r '.[] | "\(.roleDefinitionName)\t\(.scope | sub("^/subscriptions/[^/]+"; "") | if . == "" then "(whole subscription)" else . end)"' <<<"$json"; } | table
  if has_ai_role; then ok "You can call models and agents ($HAS_ROLE)."
  else warn "No data-plane role yet: agent and model calls will return 403. Use option 2."; fi
}

role_grant() {
  need_foundry || return 1
  local id oid ptype role c
  id=$(foundry_id) || return 1
  read -r oid ptype <<<"$(my_object_id)"
  [[ -n $oid ]] || { warn "Couldn't look up your object ID in Microsoft Entra ID."; return 1; }
  echo
  echo -e "  ${BOLD}Role to give yourself on $SEL_FOUNDRY${RESET}"
  echo "  1) Foundry User                   - build and run agents, call models (recommended)"
  echo "  2) Foundry Project Manager        - Foundry User + manage projects"
  echo "  3) Cognitive Services OpenAI User - call model deployments only"
  echo -e "  ${DIM}Granting a role needs Owner or User Access Administrator on the resource or its group.${RESET}"
  read -rp "  Choice [1]: " c || return 1
  case ${c:-1} in
    1) role=53ca6127-db72-4b80-b1b0-d745d6d5456d ;; # role IDs survive renames
    2) role=eadc314b-1a2d-4efa-be10-5d325db5065e ;;
    3) role=5e0bd9bd-7b93-4f28-af87-19fc36ad61bd ;;
    *) warn "Invalid choice"; return 1 ;;
  esac
  run "$AZ" role assignment create --assignee-object-id "$oid" --assignee-principal-type "$ptype" \
    --role "$role" --scope "$id" --query "{role:roleDefinitionName, scope:scope}" -o table || return 1
  info "It can take up to 5 minutes before calls stop returning 403."
}

smoke_test() {
  need_deploy || return 1
  info "Calling deployment '$SEL_DEPLOY' on $SEL_FOUNDRY…"
  local acct base token body resp code
  acct=$(foundry_json) || return 1
  base=$(jq -r '[(.properties.endpoints // {})[] | select(test("openai\\.azure\\.com"))] | first // empty' <<<"$acct")
  [[ -n $base ]] || base="https://$(jq -r '.properties.customSubDomainName // .name' <<<"$acct").openai.azure.com/"
  token=$("$AZ" account get-access-token --resource https://cognitiveservices.azure.com --query accessToken -o tsv 2>/dev/null) \
    || { warn "Couldn't get an access token. Sign in again (menu 2)."; return 1; }
  # Reasoning models (gpt-5*, o*) spend part of the budget thinking, so leave room for the reply
  body=$(jq -n --arg m "$SEL_DEPLOY" '{model: $m, max_completion_tokens: 800,
           messages: [{role: "user", content: "Reply with exactly: Foundry connection OK"}]}')
  echo -e "${DIM}\$ POST ${base%/}/openai/v1/chat/completions  (Entra ID token)${RESET}"
  resp=$(curl -sS --max-time 60 -w $'\n%{http_code}' -H @<(printf 'Authorization: Bearer %s\n' "$token") \
    -H "Content-Type: application/json" -d "$body" "${base%/}/openai/v1/chat/completions" 2>&1)
  code=${resp##*$'\n'}; resp=${resp%$'\n'*}
  echo
  case $code in
    200) ok "HTTP 200 — $SEL_DEPLOY answered: $(jq -r '.choices[0] | (.message.content // "") as $c
           | if $c == "" then "(empty reply, finish_reason=\(.finish_reason))" else $c end' <<<"$resp" 2>/dev/null)"
         ok "Sign-in, role and model deployment all work." ;;
    401) warn "HTTP 401: token rejected. Check you're signed in to the tenant that owns $SEL_FOUNDRY." ;;
    403) warn "HTTP 403: signed in, but no data-plane role on $SEL_FOUNDRY. Use menu 7 → 2, wait a few minutes, retry." ;;
    404) warn "HTTP 404: deployment '$SEL_DEPLOY' wasn't found at ${base%/}/openai/v1/chat/completions" ;;
    429) warn "HTTP 429: rate limit or quota hit. Raise the deployment capacity or wait." ;;
    *)   warn "HTTP $code from ${base%/}/openai/v1/chat/completions" ;;
  esac
  if [[ $code != 200 && -n $resp ]]; then echo -e "${DIM}${resp:0:600}${RESET}"; fi
  SMOKE_RESULT="HTTP $code ($(date +%H:%M))"
  [[ $code == 200 ]]
}

# ══════════════════════════════════════════════════════════════════════════════
#  8) DEMO PROJECT
# ══════════════════════════════════════════════════════════════════════════════

menu_demo() {
  while true; do
    header
    echo -e "  ${BOLD}Demo Project${RESET}  ${DIM}(azure-ai-projects: $(sdk_state), python: $PY)${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) Show .env (passwords and keys hidden)"
    echo "  2) Write the selected Foundry project to .env"
    echo "  3) Switch AGENT_MODE (demo / foundry)"
    echo "  4) Check the azure-ai-projects SDK"
    echo "  5) Install Python requirements"
    echo "  6) Run the test suite (test_demo_flows.py)"
    echo "  7) Launch the Streamlit UI (Ctrl+C to stop)"
    echo "  8) Run the Knowledge Base eval (kb_eval.py)"
    echo "  9) Clean up Foundry objects (old agent versions, vector stores, files)"
    echo " 10) Read MANUAL_UI_SETUP_RUNBOOK.md"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) env_show ;;
      2) env_write_project ;;
      3) env_mode ;;
      4) sdk_check ;;
      5) pip_install ;;
      6) run_tests ;;
      7) run_streamlit ;;
      8) run_kb_eval ;;
      9) foundry_cleanup ;;
      10) "${PAGER:-less}" "$PROJECT_DIR/MANUAL_UI_SETUP_RUNBOOK.md" 2>/dev/null || cat "$PROJECT_DIR/MANUAL_UI_SETUP_RUNBOOK.md" ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

run_kb_eval() {
  [[ $(env_get AGENT_MODE) == foundry ]] \
    || warn "AGENT_MODE isn't foundry, so this only tests the local keyword lookup, not the agent."
  run_in_project "$PY" kb_eval.py
}

foundry_cleanup() {
  [[ -n $(env_get PROJECT_ENDPOINT) ]] || { warn "No PROJECT_ENDPOINT in .env yet (option 2)."; return 1; }
  info "Checking what the demo has created in the Foundry project…"
  run_in_project "$PY" foundry_cleanup.py || return 1
  echo
  echo "  1) Remove the items marked 'remove'"
  echo "  2) Remove everything the demo created (agent, vector store in use, files)"
  echo "  0) Cancel"
  local c; read -rp "  Choice [0]: " c || return 1
  case ${c:-0} in
    1) run_in_project "$PY" foundry_cleanup.py --yes ;;
    2) confirm_name "the demo agent, its vector store and files" delete-all || return 1
       run_in_project "$PY" foundry_cleanup.py --all --yes ;;
    *) return 0 ;;
  esac
}

env_show() {
  [[ -f $ENV_FILE ]] || { warn "No .env yet. Option 2 creates it from .env.example."; return; }
  echo; echo -e "  ${BOLD}${ENV_FILE}${RESET}"; echo
  local line
  sed -E 's/^([A-Z_]*(PASSWORD|KEY|SECRET)[A-Z_]*=).+/\1"••••••••"/' "$ENV_FILE" | grep -vE '^[[:space:]]*$' \
    | while IFS= read -r line; do
        if [[ $line == \#* ]]; then echo -e "  ${DIM}${line}${RESET}"; else echo "  $line"; fi
      done
}

env_write_project() {
  need_project || return 1
  info "Looking up the project endpoint…"
  local ep model
  ep=$(project_endpoint) || return 1
  model=${SEL_DEPLOY:-$(env_get MODEL_DEPLOYMENT_NAME)}
  echo
  echo -e "  PROJECT_ENDPOINT       ${GREEN}${ep}${RESET}"
  echo -e "  MODEL_DEPLOYMENT_NAME  ${GREEN}${model:-gpt-4o}${RESET}"
  echo -e "  AGENT_MODE             ${GREEN}foundry${RESET}"
  echo -e "  ${DIM}The app signs in with your az login (DefaultAzureCredential), so no keys are stored.${RESET}"
  confirm "Write these to .env? (the current file is backed up first)" || return 1
  env_set PROJECT_ENDPOINT "$ep"
  env_set MODEL_DEPLOYMENT_NAME "${model:-gpt-4o}"
  env_set AGENT_MODE foundry
  ok "Saved to $ENV_FILE"
}

env_mode() {
  echo
  echo -e "  AGENT_MODE is now: ${GREEN}$(env_get AGENT_MODE)${RESET}"
  echo "  1) demo    - local rule-based agent, no cloud needed"
  echo "  2) foundry - live Foundry Agent Service (needs PROJECT_ENDPOINT + SDK)"
  local c; read -rp "  Choice: " c || return
  case $c in
    1) env_set AGENT_MODE demo && ok "AGENT_MODE=demo" ;;
    2) env_set AGENT_MODE foundry && ok "AGENT_MODE=foundry" ;;
    *) warn "Invalid choice" ;;
  esac
}

sdk_check() {
  local s; s=$(sdk_state)
  echo
  case $s in
    missing) warn "azure-ai-projects isn't installed for $PY. Use option 5." ;;
    old*) warn "azure-ai-projects ${s#old } is a 1.x beta with the old threads/runs API."
          warn "agent_service.py needs 2.x (project endpoint + Responses API). Use option 5 to upgrade." ;;
    ok*) ok "azure-ai-projects ${s#ok } matches agent_service.py (AIProjectClient(endpoint=...) + Responses API)." ;;
  esac
}

pip_install() {
  run_in_project "$PY" -m pip install -U -r requirements.txt
  sdk_load
}

run_tests() {
  if run_in_project "$PY" test_demo_flows.py; then TEST_RESULT="passed ($(date +%H:%M))"; else TEST_RESULT="failed ($(date +%H:%M))"; fi
}

run_streamlit() {
  info "Starting Streamlit — open the URL it prints. Ctrl+C stops it and returns here."
  run_in_project "$PY" -m streamlit run app.py
}

# ══════════════════════════════════════════════════════════════════════════════
#  10) FEEDBACK LOG
# ══════════════════════════════════════════════════════════════════════════════

menu_feedback() {
  while true; do
    header
    fb_source
    echo -e "  ${BOLD}Feedback Log${RESET}  ${DIM}($(fb_where))${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) List all entries (newest first)"
    echo "  2) List unanswered questions"
    echo "  3) List problem reports"
    echo "  4) Show one entry in full"
    echo "  5) Delete an entry"
    echo "  6) Deploy or update the log function (deploy_feedback_function.sh)"
    echo "  0) ← Back"
    [[ -n $FB_ACCOUNT ]] || echo -e "  ${DIM}No feedback table in .env yet, so this shows the app's local file; option 6 sets up the table.${RESET}"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) fb_list ;;
      2) fb_list unanswered_question ;;
      3) fb_list problem_report ;;
      4) fb_show ;;
      5) fb_delete ;;
      6) fb_deploy ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

FB_ACCOUNT="" FB_TABLE="" FB_FILE="$PROJECT_DIR/feedback_log.jsonl"
fb_source() { # sets FB_ACCOUNT / FB_TABLE from .env; empty = the app's local feedback_log.jsonl
  local ep; ep=$(env_get FEEDBACK_TABLE_ENDPOINT)
  FB_ACCOUNT="" FB_TABLE=""
  if [[ $ep =~ ^https://([a-z0-9]+)\.table\.core\.windows\.net ]]; then
    FB_ACCOUNT=${BASH_REMATCH[1]}
    FB_TABLE=$(env_get FEEDBACK_TABLE_NAME); FB_TABLE=${FB_TABLE:-feedback}
  fi
}
fb_where() {
  if [[ -n $FB_ACCOUNT ]]; then printf "Azure Table '%s' in %s" "$FB_TABLE" "$FB_ACCOUNT"; else printf '%s, local' "${FB_FILE##*/}"; fi
}

FB_JSON=""
FB_HDR=$'Time (UTC)\tKind\tID\tSummary'
FB_ROW='"\(.time // "" | .[0:16] | sub("T"; " "))\t\(.kind)\t\(.id)\t\(.summary // "" | gsub("[\\t\\n]"; " "))"'
fb_load() { # fb_load [KIND] — sets FB_JSON: entries newest first, in the app's entry format
  local raw kind=${1:-}
  if [[ -n $FB_ACCOUNT ]]; then
    need_login || return 1
    raw=$(azj storage entity query --account-name "$FB_ACCOUNT" --table-name "$FB_TABLE" --auth-mode login) \
      || { warn "Reading the table needs a recent sign-in (menu 2) and the Storage Table Data role."; return 1; }
    raw=$(jq '[.items[] | {id: .RowKey, kind: .PartitionKey} + del(.RowKey, .PartitionKey, .Timestamp, .etag)]' <<<"$raw")
  elif [[ -f $FB_FILE ]]; then
    raw=$(jq -cR 'fromjson? // empty' "$FB_FILE" | jq -s '.') # skips damaged lines, like the app
  else
    raw='[]'
  fi
  FB_JSON=$(jq --arg k "$kind" 'map(select($k == "" or .kind == $k)) | sort_by(.time) | reverse' <<<"$raw")
}

fb_list() { # fb_list [KIND]
  fb_load "${1:-}" || return
  echo
  echo -e "  ${BOLD}$(fb_where)${RESET}${1:+  ${DIM}(${1//_/ }s only)${RESET}}"
  { printf '%s\n' "$FB_HDR"; jq -r ".[] | $FB_ROW" <<<"$FB_JSON"; } | table --num
}

fb_show() {
  fb_load || return
  pick "$(fb_where)" "$FB_JSON" "$FB_HDR" "$FB_ROW" || return
  local e; e=$(jq ".[$PICKED]" <<<"$FB_JSON")
  echo
  jq -r '"  ID:            \(.id)",
         "  Time (UTC):    \(.time // "-")",
         "  Kind:          \(.kind)",
         "  Agent:         \(.agent // "-")",
         "  Conversation:  \(.conversation_id // "-")",
         "  Masked:        \(.masked // "nothing" | if type == "array" then join(", ") else . end)"' <<<"$e"
  echo; echo -e "  ${BOLD}User's words${RESET}"; jq -r '.user_message // ""' <<<"$e" | fold -s -w 96 | sed 's/^/    /'
  echo; echo -e "  ${BOLD}Summary${RESET}";      jq -r '.summary // ""' <<<"$e" | fold -s -w 96 | sed 's/^/    /'
  echo; echo -e "  ${BOLD}Details${RESET}";      jq -r '.details // ""' <<<"$e" | fold -s -w 96 | sed 's/^/    /'
}

fb_delete() {
  fb_load || return
  pick "Delete which entry? ($(fb_where))" "$FB_JSON" "$FB_HDR" "$FB_ROW" || return
  local id kind tmp
  id=$(jq -r ".[$PICKED].id" <<<"$FB_JSON"); kind=$(jq -r ".[$PICKED].kind" <<<"$FB_JSON")
  confirm "Delete $id ($kind)? This can't be undone." || return
  if [[ -n $FB_ACCOUNT ]]; then
    run "$AZ" storage entity delete --account-name "$FB_ACCOUNT" --table-name "$FB_TABLE" \
      --partition-key "$kind" --row-key "$id" --auth-mode login --only-show-errors -o none
  else
    tmp=$(mktemp)
    jq -rR --arg id "$id" '. as $line | ((fromjson? | .id) // null) as $i | if $i == $id then empty else $line end' \
      "$FB_FILE" > "$tmp" && cat "$tmp" > "$FB_FILE" && ok "Deleted $id"
    rm -f "$tmp"
  fi
}

fb_deploy() {
  need_login || return
  info "Creates or updates the function, its table and the Foundry connection, then writes FEEDBACK_* to .env."
  info "Safe to re-run. The next app start (or eval) creates an agent version that uses it, if needed."
  confirm "Run deploy_feedback_function.sh?" || return
  run_in_project bash deploy_feedback_function.sh
}

# ══════════════════════════════════════════════════════════════════════════════
#  11) COST & CREDITS
# ══════════════════════════════════════════════════════════════════════════════

menu_cost() {
  while true; do
    header
    echo -e "  ${BOLD}Cost & Credits${RESET}  ${DIM}(selected subscription; Cost Management data can lag by up to a day)${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) This month's cost by service"
    echo "  2) This month's cost by resource group"
    echo "  3) This month's cost by resource"
    echo "  4) Daily cost, last 30 days"
    echo "  5) Free credit: balance and expiry"
    echo "  6) Invoices, last 12 months"
    echo "  7) Budgets"
    echo "  8) Create or update a monthly budget alert"
    echo "  9) Delete a budget"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) cost_by ServiceName "Cost by service" Service ;;
      2) cost_by ResourceGroupName "Cost by resource group" "Resource group" ;;
      3) cost_by ResourceId "Cost by resource" Resource ;;
      4) cost_daily ;;
      5) cost_credits ;;
      6) cost_invoices ;;
      7) budget_list ;;
      8) budget_create ;;
      9) budget_delete ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

ARM="https://management.azure.com"
# Money for tables: 2 decimals; amounts under half a cent show as <0.01 rather than 0.00
JQ_MONEY='def money: if . == 0 then "0.00" elif . < 0.005 then "<0.01" else ((. * 100 | round) as $c | "\($c / 100 | floor).\($c % 100 | tostring | if length == 1 then "0" + . else . end)") end;'

cost_query() { # cost_query BODY — a Cost Management query on the subscription; prints its JSON
  need_login || return 1
  azj rest --method post --body "$1" \
    --url "$ARM/subscriptions/$ACCT_SUB_ID/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
}

cost_by() { # cost_by DIMENSION TITLE COLUMN — month-to-date cost grouped by one dimension, biggest first
  local body json
  body=$(jq -nc --arg d "$1" '{type: "ActualCost", timeframe: "MonthToDate",
    dataset: {granularity: "None", aggregation: {totalCost: {name: "Cost", function: "Sum"}},
              grouping: [{type: "Dimension", name: $d}]}}')
  json=$(cost_query "$body") || return 1
  echo; echo -e "  ${BOLD}$2, $(date +%B) to date${RESET}"
  # Resource IDs are long: show "name (type)" from their last two segments
  { printf '%s\tCost\tCurrency\n' "$3"
    jq -r "$JQ_MONEY"' .properties.rows | sort_by(-.[0]) | .[]
      | "\(.[1] // "-" | if startswith("/subscriptions/") then (split("/") | "\(.[-1]) (\(.[-2]))") elif . == "" then "(none)" else . end)\t\(.[0] | money)\t\(.[2])"' <<<"$json"
  } | table
  jq -r "$JQ_MONEY"' "  Sum: \(.properties.rows | map(.[0]) | add // 0 | money) \(.properties.rows[0][2] // "")"' <<<"$json"
}

cost_daily() {
  local body json
  body=$(jq -nc --arg f "$(date -u -d '29 days ago' +%Y-%m-%dT00:00:00Z)" --arg t "$(date -u +%Y-%m-%dT23:59:59Z)" \
    '{type: "ActualCost", timeframe: "Custom", timePeriod: {from: $f, to: $t},
      dataset: {granularity: "Daily", aggregation: {totalCost: {name: "Cost", function: "Sum"}}}}')
  json=$(cost_query "$body") || return 1
  echo; echo -e "  ${BOLD}Daily cost, last 30 days${RESET}  ${DIM}(days with no charges are left out)${RESET}"
  { printf 'Date\tCost\tCurrency\t\n'
    jq -r "$JQ_MONEY"' (.properties.rows | map(.[0]) | max // 0) as $max | .properties.rows | sort_by(.[1]) | .[]
      | "\(.[1] | tostring | "\(.[0:4])-\(.[4:6])-\(.[6:8])")\t\(.[0] | money)\t\(.[2])\t\(if $max > 0 then "▇" * ([(.[0] / $max * 30 | ceil), 1] | max) else "" end)"' <<<"$json"
  } | table
  jq -r "$JQ_MONEY"' "  Sum: \(.properties.rows | map(.[0]) | add // 0 | money) \(.properties.rows[0][2] // "")"' <<<"$json"
}

billing_profiles() { # prints "account-id profile-id" per Microsoft Customer Agreement billing profile you can see
  local accts a
  accts=$(azj rest --method get --url "$ARM/providers/Microsoft.Billing/billingAccounts?api-version=2024-04-01") || return 1
  for a in $(jq -r '.value[] | select(.properties.agreementType == "MicrosoftCustomerAgreement") | .name' <<<"$accts"); do
    azj rest --method get --url "$ARM/providers/Microsoft.Billing/billingAccounts/$a/billingProfiles?api-version=2024-04-01" \
      | jq -r --arg a "$a" '.value[] | "\($a) \(.name)"'
  done
}

cost_credits() {
  need_login || return 1
  local sub pairs a p lots sum
  sub=$(azj rest --method get --url "$ARM/subscriptions/$ACCT_SUB_ID?api-version=2022-12-01") || return 1
  echo
  jq -r '"  Offer:          \(.subscriptionPolicies.quotaId)", "  Spending limit: \(.subscriptionPolicies.spendingLimit)  (On = Azure stops paid services instead of billing your card)"' <<<"$sub"
  pairs=$(billing_profiles) || return 1
  if [[ -z $pairs ]]; then
    warn "Credit balances are only available here for Microsoft Customer Agreement accounts. See Cost Management → Credits in the portal."
    return
  fi
  while read -r a p; do
    [[ -n $p ]] || continue
    sum=$(azj rest --method get --url "$ARM/providers/Microsoft.Billing/billingAccounts/$a/billingProfiles/$p/providers/Microsoft.Consumption/credits/balanceSummary?api-version=2023-05-01") || continue
    lots=$(azj rest --method get --url "$ARM/providers/Microsoft.Billing/billingAccounts/$a/billingProfiles/$p/providers/Microsoft.Consumption/lots?api-version=2023-05-01") || continue
    echo
    jq -r "$JQ_MONEY"' .properties.balanceSummary as $b | "  Credit left:    \($b.currentBalance.value | money) \($b.currentBalance.currency)  (about \($b.estimatedBalanceInBillingCurrency.value // 0 | money) \($b.estimatedBalanceInBillingCurrency.currency // ""))"' <<<"$sum"
    echo
    { printf 'Credit\tOriginal\tLeft\tCurrency\tExpires (UTC)\tDays left\n'
      jq -r "$JQ_MONEY"' .value[].properties
        | "\(.source)\t\(.originalAmount.value | money)\t\(.closedBalance.value | money)\t\(.originalAmount.currency)\t\(.expirationDate[0:16] | sub("T"; " "))\t\(((.expirationDate[0:19] + "Z" | fromdateiso8601) - now) / 86400 | if . < 0 then "expired" else (ceil | tostring) end)"' <<<"$lots"
    } | table
  done <<<"$pairs"
  echo -e "  ${DIM}Upgrading to pay-as-you-go keeps unused credit only until the expiry above; it isn't extended.${RESET}"
}

cost_invoices() {
  need_login || return 1
  local pairs a p json from to
  from=$(date -u -d '12 months ago' +%Y-%m-%d); to=$(date -u +%Y-%m-%d)
  pairs=$(billing_profiles) || return 1
  [[ -n $pairs ]] || { warn "Invoices are only listed here for Microsoft Customer Agreement accounts; see Cost Management → Invoices in the portal."; return; }
  while read -r a p; do
    [[ -n $p ]] || continue
    json=$(azj rest --method get --url "$ARM/providers/Microsoft.Billing/billingAccounts/$a/billingProfiles/$p/invoices?api-version=2024-04-01&periodStartDate=$from&periodEndDate=$to") || continue
    echo; echo -e "  ${BOLD}Invoices since $from${RESET}"
    { printf 'Invoice\tDate\tDue\tStatus\tTotal\tAmount due\tCurrency\n'
      jq -r "$JQ_MONEY"' .value | sort_by(.properties.invoiceDate) | reverse | .[] | .name as $n | .properties
        | "\($n)\t\(.invoiceDate[0:10])\t\(.dueDate[0:10])\t\(.status)\t\(.totalAmount.value // .billedAmount.value // 0 | money)\t\(.amountDue.value // 0 | money)\t\(.amountDue.currency // .billedAmount.currency // "")"' <<<"$json"
    } | table
  done <<<"$pairs"
  echo -e "  ${DIM}A Free Trial has no invoices: the first one comes after an upgrade, once usage exceeds the credit.${RESET}"
}

budget_list() {
  need_login || return 1
  local json
  json=$(azj rest --method get --url "$ARM/subscriptions/$ACCT_SUB_ID/providers/Microsoft.Consumption/budgets?api-version=2023-05-01") || return 1
  echo; echo -e "  ${BOLD}Budgets on ${ACCT_SUB_NAME}${RESET}"
  { printf 'Budget\tAmount\tSpent this period\tPeriod\tAlerts at\n'
    jq -r "$JQ_MONEY"' .value[] | .properties as $p
      | "\(.name)\t\($p.amount | money)\t\($p.currentSpend.amount // 0 | money) \($p.currentSpend.unit // "")\t\($p.timeGrain)\t\([$p.notifications[]] | sort_by(.thresholdType, .threshold) | map("\(.threshold | floor)%\(if .thresholdType == "Forecasted" then " forecast" else "" end)") | join(", "))"' <<<"$json"
  } | table
}

budget_create() {
  need_login || return 1
  local name amount email who body
  echo
  info "A monthly cost budget for the whole subscription. Azure emails when spend passes 80% and 100%, and when it is forecast to pass 100%."
  info "Amounts are in your billing currency. A budget only alerts; it never stops or deletes anything."
  ask name "Budget name" "monthly-budget" || return
  [[ $name =~ ^[A-Za-z0-9_-]{1,63}$ ]] || { warn "Use letters, digits, - and _ only."; return 1; }
  ask amount "Monthly amount" "20" || return
  [[ $amount =~ ^[0-9]+([.][0-9]{1,2})?$ ]] && [[ $amount != 0 ]] || { warn "Enter an amount like 20 or 12.50."; return 1; }
  ask email "Email for alerts (Enter = the subscription's Owners)" "" || return
  if [[ -n $email && ! $email =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then warn "That isn't an email address."; return 1; fi
  who=${email:-the subscription Owners}
  confirm "Create or update budget '$name': $amount a month, alerts to $who?" || return
  body=$(jq -nc --argjson amt "$amount" --arg start "$(date -u +%Y-%m-01T00:00:00Z)" --arg email "$email" '
    ({contactRoles: ["Owner"]} + (if $email == "" then {} else {contactEmails: [$email]} end)) as $to
    | {properties: {category: "Cost", amount: $amt, timeGrain: "Monthly", timePeriod: {startDate: $start},
        notifications: {
          actual_80: ({enabled: true, operator: "GreaterThan", threshold: 80, thresholdType: "Actual"} + $to),
          actual_100: ({enabled: true, operator: "GreaterThan", threshold: 100, thresholdType: "Actual"} + $to),
          forecast_100: ({enabled: true, operator: "GreaterThan", threshold: 100, thresholdType: "Forecasted"} + $to)}}}')
  azj rest --method put --body "$body" \
    --url "$ARM/subscriptions/$ACCT_SUB_ID/providers/Microsoft.Consumption/budgets/$name?api-version=2023-05-01" >/dev/null \
    && ok "Budget '$name' saved" && budget_list
}

budget_delete() {
  need_login || return 1
  local json name
  json=$(azj rest --method get --url "$ARM/subscriptions/$ACCT_SUB_ID/providers/Microsoft.Consumption/budgets?api-version=2023-05-01") || return 1
  json=$(jq '.value' <<<"$json")
  pick "Delete which budget?" "$json" $'Budget\tAmount\tPeriod' '"\(.name)\t\(.properties.amount)\t\(.properties.timeGrain)"' || return
  name=$(jq -r ".[$PICKED].name" <<<"$json")
  confirm_name "budget '$name'" "$name" || return
  run "$AZ" rest --method delete --url "$ARM/subscriptions/$ACCT_SUB_ID/providers/Microsoft.Consumption/budgets/$name?api-version=2023-05-01"
}

# ══════════════════════════════════════════════════════════════════════════════
#  9) AZURE CLI INSTALL / REPAIR
# ══════════════════════════════════════════════════════════════════════════════

menu_install() {
  while true; do
    header
    local cur="not working"
    if az_works; then cur="azure-cli $(az_version)"; fi
    echo -e "  ${BOLD}Azure CLI Install / Repair${RESET}  ${DIM}($AZ → $cur)${RESET}"
    echo "  ─────────────────────────────────"
    echo "  1) Diagnose: which az runs and why it fails"
    echo "  2) Install with apt, Microsoft's official package (needs sudo)"
    echo "  3) Install into a private Python venv (no sudo)"
    echo "  4) Upgrade the CLI"
    echo "  0) ← Back"
    echo
    local c; read -rp "  Choice: " c || return
    case "$c" in
      1) cli_diagnose ;;
      2) install_apt ;;
      3) install_venv ;;
      4) if [[ $(command -v "$AZ") == "$AZ_VENV"/* ]]; then run "$AZ_VENV/bin/pip" install -U azure-cli; else run "$AZ" upgrade --yes; fi ;;
      0) return ;;
      *) warn "Invalid choice" ;;
    esac
    pause
  done
}

cli_diagnose() {
  echo
  echo -e "  ${BOLD}PATH lookups for az${RESET}"
  type -a az 2>&1 | sed 's/^/    /'
  local f; f=$(type -P az)
  if [[ -n $f ]] && head -c 2 "$f" | grep -q '#!'; then
    echo; echo -e "  ${BOLD}$f is a script:${RESET}"; sed 's/^/    /' "$f"
  fi
  echo; echo -e "  ${BOLD}Running '$AZ version'${RESET}"
  "$AZ" version -o table 2>&1 | sed 's/^/    /'
}

venv_python() { # prints a Python that can create venvs with pip
  local p
  for p in "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3" python3; do
    command -v "$p" >/dev/null && "$p" -c 'import ensurepip, venv' 2>/dev/null && { printf '%s' "$p"; return 0; }
  done
  return 1
}

install_apt() {
  echo
  info "This runs Microsoft's official installer for Ubuntu/Debian:"
  echo "    curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
  confirm "Continue? (asks for your sudo password)" || return
  run bash -c 'curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash' || return
  fix_shadowing /usr/bin/az
}

install_venv() {
  local p; p=$(venv_python) || { warn "No Python with venv + pip found. Use option 2 instead."; return 1; }
  echo
  echo -e "  Python:  ${GREEN}$p${RESET}"
  echo -e "  Target:  ${GREEN}$AZ_VENV${RESET}"
  confirm "Install the Azure CLI there?" || return
  run "$p" -m venv "$AZ_VENV" || return
  run "$AZ_VENV/bin/pip" install -q --upgrade pip azure-cli || return
  fix_shadowing "$AZ_VENV/bin/az"
}

# A broken az earlier in PATH (like a stale wrapper in ~/bin) hides a working install
fix_shadowing() {
  local good=$1 first
  first=$(type -P az 2>/dev/null)
  AZ=$good
  if [[ -n $first && $first != "$good" ]] && ! "$first" version -o none >/dev/null 2>&1; then
    warn "'$first' comes first in your PATH and doesn't work, so typing 'az' still fails."
    if confirm "Replace it with a wrapper that runs $good? (old file kept as $first.bak)"; then
      mv "$first" "$first.bak" && printf '#!/usr/bin/env bash\nexec %q "$@"\n' "$good" > "$first" && chmod +x "$first" \
        && ok "'az' now runs $good"
    fi
  fi
  if az_works; then ok "Azure CLI works: azure-cli $(az_version)"; refresh_account_info; fi
}

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

main_menu() {
  header
  echo -e "  ${YELLOW}Next:${RESET} $(next_step)"
  echo
  echo -e "  ${BOLD}Main Menu${RESET}"
  echo "  ─────────────────────────────────"
  echo "  1) Setup checklist (get the demo running on Foundry)"
  echo "  2) Sign-in & Subscription"
  echo "  3) Resource Groups"
  echo "  4) Browse Resources"
  echo "  5) Foundry Resources & Projects"
  echo "  6) Model Deployments & Quota"
  echo "  7) Access & Smoke Test"
  echo "  8) Demo Project (.env, SDK, tests, Streamlit)"
  echo "  9) Azure CLI Install / Repair"
  echo " 10) Feedback Log (unanswered questions, problem reports)"
  echo " 11) Cost & Credits (spend, free credit, invoices, budgets)"
  echo "  0) Exit"
  echo
  local c; read -rp "  Choice: " c || return 1
  case "$c" in
    1) menu_checklist ;;
    2) menu_login ;;
    3) menu_rg ;;
    4) menu_browse ;;
    5) menu_foundry ;;
    6) menu_models ;;
    7) menu_access ;;
    8) menu_demo ;;
    9) menu_install ;;
    10) menu_feedback ;;
    11) menu_cost ;;
    0) echo -e "\n${GREEN}Goodbye!${RESET}"; return 1 ;;
    *) warn "Invalid choice"; pause ;;
  esac
}

# Run only when executed, so tests can `source az_tui.sh` to reach the functions
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  require jq curl awk
  load_state
  info "Checking Azure CLI, sign-in and Python SDK…"
  refresh_account_info || true
  sdk_load
  while true; do
    main_menu || break
  done
fi
