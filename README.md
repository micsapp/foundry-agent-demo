# Microsoft AI Foundry Agent Demo: Oracle DB & Knowledge Base Action Guardrails

An end-to-end demo project showcasing how a **Microsoft (Azure) AI Foundry Agent** can:
1. **Answer Questions from a Knowledge Base** using Foundry's **Vector Store (`FileSearchTool`)**.
2. **Query & Update an Oracle Database** using custom **Function Tools** (`python-oracledb` with a built-in SQLite mock fallback for instant local testing), or as **MCP tools** on an Azure Function so they also work in the Foundry portal playground.
3. **Update Knowledge Base Source Files** (`knowledge_base/*.md`, or the same files in Azure Storage) and re-sync the Vector Store.
4. **Enforce Multi-Layer Guardrails & Human-in-the-Loop (HITL) Approval** on every agent action.
5. **Offer a Public Chat Page** (no sign-in) for a read-only version of the agent, with per-visitor and daily limits (section 7).

---

## Architecture & Guardrail Layers

| Agent Action | Tool Name | Guardrails Enforced | Execution Mode |
| :--- | :--- | :--- | :--- |
| **Answer KB Questions** | `file_search` (Vector Store) | Read-only grounding over approved `.md` files | Automatic |
| **Query Oracle DB** | `query_oracle_db` | **SQL Read-Only Guardrail:** Blocks `DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `TRUNCATE`, `;` chaining, unauthorized tables, and PII columns (`SSN`). | Automatic (if `PASSED`) |
| **Update Oracle Incident** | `update_oracle_incident` | **Parameterized Validation + HITL Gate:** Validates `INC-XXXX` format and allowed status values, then pauses for human operator approval (as an MCP tool, Foundry holds the call until it's approved in the app or the portal playground). | **Human-in-the-Loop Approval** |
| **Update KB Source File** | `propose_kb_file_update` | **KB Content Guardrail + HITL Gate:** Blocks path traversal (`../`), scans for PII/Secrets (`SSN`, `sk-...` API keys), prevents >50% file wipeouts, and renders a **Unified Diff** for human approval before overwriting & re-indexing the Vector Store. | **Human-in-the-Loop Approval** |
| **Log Gaps & Problems** | `log_feedback` | **Append-Only Log + Masking:** Questions the KB can't answer and problems users report are appended to `feedback_log.jsonl`, or to an Azure Table through an Azure Function that Foundry calls as an MCP tool; SSNs, API keys and passwords are masked first. The agent never reads the log back. | Automatic |

---

## Quick Start

### 1. Run the Automated End-to-End Verification Suite
```bash
python3 test_demo_flows.py
```

### 2. Launch the Interactive Streamlit Web UI
```bash
pip install -r requirements.txt
streamlit run app.py
```

### 3. Connect Live Azure AI Foundry & Live Oracle DB (Optional)
Copy `.env.example` to `.env` and set:
- `AGENT_MODE=foundry` and `PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"` (sign in with `az login`; you need the **Foundry User** role, formerly Azure AI User on the project). `./az_tui.sh` can create the Foundry resource, project and model deployment and write these values for you. On a machine without a browser, or where your tenant blocks device-code sign-in, use `./az_tui.sh` → 2 → 1: it prints a sign-in link to open in any browser and takes back the address the browser ends on.
  The app reuses one vector store and agent version across runs (only changed `knowledge_base/` files are uploaded; state in `.kb_vector_store.json`). `python3 foundry_cleanup.py` lists leftover versions of both agents (the main one and the public chat page's), vector stores and files; add `--yes` to delete them.
- `DB_MODE`: `sqlite_mock` (default, a local file), `azure_blob` (the same mock tables in Azure Storage, shared with the Azure Function; see section 6), or `oracle_live` with `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` (uses `python-oracledb` Thin mode—no Oracle Instant Client required).

### 4. Check Knowledge Base Answer Accuracy
`knowledge_base/` includes three PDF manuals for fictional products, so answers can only come from File Search. `kb_eval_questions.json` holds 35 questions with expected answers (including cross-document and "not in the KB" questions):
```bash
python3 kb_eval.py            # needs AGENT_MODE=foundry; add -v to print every answer
```
For "not in the KB" questions the agent must also log the gap with `log_feedback`; the entries a run creates are removed from the feedback log at the end (`--keep-log` keeps them).

### 5. Review Unanswered Questions & Problem Reports
When the Knowledge Base can't answer a question, or a user reports a problem (something broken, or a wrong or outdated answer), the agent calls `log_feedback` and gives the user an entry ID. Each entry holds the time, kind, the user's words, a summary and details; SSNs, API keys and passwords are masked. Entries show in the app's **Feedback Log** tab, where you can also download them.

By default the app runs the tool and appends to `feedback_log.jsonl`. With the Azure Function deployed (section 6), Foundry calls the function instead and entries go to an Azure Table, which the app reads with your `az login`. `./az_tui.sh` → 10 lists entries (all, or one kind), shows one in full, deletes entries and runs the deploy script. Its options 7–9 cover the Function App: its functions, whether the public chat page is up, portal links, the chat limits and how much of them is used, and changing a limit.

### 6. Use the Tools from the Foundry Portal Playground (Azure Function MCP server)
The portal playground can't run the app's function tools: it stops at "Enter function output as JSON". The deploy script moves the tools that don't need this machine onto an Azure Function that Foundry calls itself:
```bash
./deploy_mcp_tools.sh   # needs az login and PROJECT_ENDPOINT in .env; safe to re-run
```
It creates a storage account (the `feedback` table and the `oracle-mock` and `knowledge-base` blob containers) and a Flex Consumption Function App named `agent-tools-<suffix>` (`azure_function/`) in the Foundry resource's resource group. The function is an **MCP server** (Azure Functions MCP tool trigger) with four tools:
- `log_feedback` writes to the table, with no approval step.
- `query_oracle_db` and `update_oracle_incident` work on the mock Oracle database: the same SQLite tables, kept as a blob (`demo_oracle_mock.db`) that the function creates on first use, with the same guardrails as the app.
- `propose_kb_file_update` edits the Knowledge Base files, which now live in the `knowledge-base` container (seeded from `knowledge_base/`; files already there are kept). File Search never reads these files directly; it searches the vector store's indexed copy. So after the guardrail checks the function saves the new file (the old one goes to `backups/` in the container), uploads it to the vector store, waits until it's indexed and only then removes the old copy. From then on File Search returns the new text. The function reaches the vector store as its own identity, which gets the Foundry User role on the Foundry resource.

Foundry holds every `update_oracle_incident` and `propose_kb_file_update` call until someone approves it: the app shows its approval card (with the diff for KB updates), and the playground shows an approve/deny prompt.

The script stores the server's `mcp_extension` key in three Foundry project connections, one per tool group (`feedback-log-mcp`, `oracle-db-mcp`, `knowledge-base-mcp`; sent as the `x-functions-key` header), because the portal lists each MCP tool by its connection name and URL. It writes `MCP_SERVER_URL`, `FEEDBACK_TABLE_*`, `DB_MODE=azure_blob`, `ORACLE_MOCK_BLOB_URL`, `KB_MODE=azure_blob` and `KB_BLOB_CONTAINER_URL` to `.env`, then creates (or reuses) the agent versions, so the agent has all four as MCP tools right away; the app and `kb_eval.py` do the same check when they start. The app's DB and KB tabs, Reset buttons and demo mode then use the same blobs, so changes approved in the playground show up in the app (the script grants you Storage Table Data Contributor and Storage Blob Data Contributor on the containers). The record of which vector store file holds each KB file moves with them (`vector_store_sync.json` in the container), so the app and the function keep the vector store in step. MCP is used rather than an OpenAPI tool because Microsoft's tool-support table lists MCP, but not OpenAPI or function tools, for gpt-5-mini. Earlier versions of the script (`deploy_feedback_function.sh`) named the Function App `feedback-log-<suffix>` and used one connection, `feedback-mcp`; running this one moves to the new names and removes those. Once you use `KB_MODE=azure_blob`, keep every run of the app and `kb_eval.py` on it: a run with `KB_MODE=local` makes the shared vector store match `knowledge_base/` instead (and creates a different agent version).

The sidebar's guardrail switches only change the app: the function always enforces read-only SQL and the table and column allowlist. Switching off approval for DB writes or KB updates makes the app approve Foundry's requests itself; the playground still asks.

### 7. Public Chat Page (no sign-in)
The Function App also serves a chat page at `https://agent-tools-<suffix>.azurewebsites.net/api/chat` that anyone can open without signing in (Foundry's own preview link needs an Azure sign-in). The deploy script prints the address. The page talks to a second agent, `Oracle-And-KB-Public-Agent`, which the deploy script, the app and `kb_eval.py` create next to the main one. It has only read-only tools: File Search, `query_oracle_db` and `log_feedback`. None of them needs an approval, and a request to change something gets a "this demo is read-only" answer. The function calls Foundry as its own identity, so visitors spend your subscription's credit. These app settings cap that:
- `CHAT_PER_VISITOR_PER_HOUR` (default 20): requests per visitor (client IP, stored only as a salted hash) per hour.
- `CHAT_DAILY_LIMIT` (default 100): agent replies per UTC day, for all visitors together.
- `CHAT_MAX_MESSAGE_CHARS` (default 1000).

`./az_tui.sh` → 10 → 8 shows them with today's use and the last 7 days, and → 9 changes one; or use `az functionapp config appsettings set -n agent-tools-<suffix> -g <group> --settings CHAT_DAILY_LIMIT=200`. Re-running the deploy script keeps your values. The counters live in the `publicchat` table. The page keeps its conversation in the browser tab, and the function continues only conversations the page created. The full agent, with approvals, stays behind a sign-in (the Streamlit app, the portal playground or Foundry's preview link). Foundry's preview link (agent → Publish) names an agent version in its address (`?version=12`), so it keeps using that version; after the app or the deploy script creates a new one, publish it in the portal (**Publish Updates**) and use the link it gives. The public page always uses the public agent's latest version.

`./az_tui.sh` → 11 (Cost & Credits) shows this month's cost by service, resource group or resource, daily cost, the free-credit balance and expiry, and invoices, and manages monthly budget alerts.
