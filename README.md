# Microsoft AI Foundry Agent Demo: Oracle DB & Knowledge Base Action Guardrails

An end-to-end demo project showcasing how a **Microsoft (Azure) AI Foundry Agent** can:
1. **Answer Questions from a Knowledge Base** using Foundry's **Vector Store (`FileSearchTool`)**.
2. **Query & Update an Oracle Database** using custom **Function Tools** (`python-oracledb` with a built-in SQLite mock fallback for instant local testing), or as **MCP tools** on an Azure Function so they also work in the Foundry portal playground.
3. **Update Knowledge Base Source Files** (`knowledge_base/*.md`) and re-sync the Vector Store.
4. **Enforce Multi-Layer Guardrails & Human-in-the-Loop (HITL) Approval** on every agent action.

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
  The app reuses one vector store and agent version across runs (only changed `knowledge_base/` files are uploaded; state in `.kb_vector_store.json`). `python3 foundry_cleanup.py` lists leftover agent versions, vector stores and files; add `--yes` to delete them.
- `DB_MODE`: `sqlite_mock` (default, a local file), `azure_blob` (the same mock tables in Azure Storage, shared with the Azure Function; see section 6), or `oracle_live` with `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` (uses `python-oracledb` Thin mode—no Oracle Instant Client required).

### 4. Check Knowledge Base Answer Accuracy
`knowledge_base/` includes three PDF manuals for fictional products, so answers can only come from File Search. `kb_eval_questions.json` holds 35 questions with expected answers (including cross-document and "not in the KB" questions):
```bash
python3 kb_eval.py            # needs AGENT_MODE=foundry; add -v to print every answer
```
For "not in the KB" questions the agent must also log the gap with `log_feedback`; the entries a run creates are removed from the feedback log at the end (`--keep-log` keeps them).

### 5. Review Unanswered Questions & Problem Reports
When the Knowledge Base can't answer a question, or a user reports a problem (something broken, or a wrong or outdated answer), the agent calls `log_feedback` and gives the user an entry ID. Each entry holds the time, kind, the user's words, a summary and details; SSNs, API keys and passwords are masked. Entries show in the app's **Feedback Log** tab, where you can also download them.

By default the app runs the tool and appends to `feedback_log.jsonl`. With the Azure Function deployed (section 6), Foundry calls the function instead and entries go to an Azure Table, which the app reads with your `az login`. `./az_tui.sh` → 10 lists entries (all, or one kind), shows one in full, deletes entries and runs the deploy script.

### 6. Use the Tools from the Foundry Portal Playground (Azure Function MCP server)
The portal playground can't run the app's function tools: it stops at "Enter function output as JSON". The deploy script moves the tools that don't need this machine onto an Azure Function that Foundry calls itself:
```bash
./deploy_feedback_function.sh   # needs az login and PROJECT_ENDPOINT in .env; safe to re-run
```
It creates a storage account (the `feedback` table and the `oracle-mock` blob container) and a Flex Consumption Function App (`azure_function/`) in the Foundry resource's resource group. The function is an **MCP server** (Azure Functions MCP tool trigger) with three tools:
- `log_feedback` writes to the table, with no approval step.
- `query_oracle_db` and `update_oracle_incident` work on the mock Oracle database: the same SQLite tables, kept as a blob (`demo_oracle_mock.db`) that the function creates on first use, with the same guardrails as the app. Foundry holds every `update_oracle_incident` call until someone approves it: the app shows its approval card, and the playground shows an approve/deny prompt.

The script stores the server's `mcp_extension` key in a Foundry project connection (Custom keys, sent as the `x-functions-key` header), and writes `FEEDBACK_*`, `DB_MODE=azure_blob` and `ORACLE_MOCK_BLOB_URL` to `.env`. On the next start the agent gets these tools as MCP tools; `propose_kb_file_update` stays a function tool that the app runs, because it edits files on this machine. With `DB_MODE=azure_blob` the app's DB tab, Reset button and demo mode use the same blob, so changes approved in the playground show up in the app (the script grants you Storage Table Data Contributor and Storage Blob Data Contributor on the container). MCP is used rather than an OpenAPI tool because Microsoft's tool-support table lists MCP, but not OpenAPI or function tools, for gpt-5-mini.

The sidebar's guardrail switches only change the app: the function always enforces read-only SQL and the table and column allowlist. Switching off approval for DB writes makes the app approve Foundry's requests itself; the playground still asks.

`./az_tui.sh` → 11 (Cost & Credits) shows this month's cost by service, resource group or resource, daily cost, the free-credit balance and expiry, and invoices, and manages monthly budget alerts.
