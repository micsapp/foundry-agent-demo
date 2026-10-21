"""
Azure Function serving the agent's server-side tools over MCP (Model Context Protocol):

- `log_feedback` writes to the Azure Table named by FEEDBACK_TABLE_NAME.
- `query_oracle_db` and `update_oracle_incident` work on the Oracle-compatible mock database kept as a SQLite
  blob (ORACLE_MOCK_BLOB_URL), with the same guardrails as the Streamlit app. The agent definition makes Foundry
  ask a person to approve each `update_oracle_incident` call before it reaches this function.

Foundry connects to https://<app>/runtime/webhooks/mcp (streamable HTTP) with the app's `mcp_extension`
system key, which it keeps in a Foundry project connection, not in code. Storage is reached with the
Function App's managed identity. Because Foundry calls the server itself, the tools also work in the portal
playground, and MCP is a tool type gpt-5-mini supports.

Deployed by ../deploy_feedback_function.sh, which copies feedback_log.py, oracle_tool.py and guardrails.py next
to this file so masking, guardrails and the database code match the Streamlit app exactly, and writes
build_id.txt so the script can tell when the new code is live (GET /api/health returns it).
"""

import json
import logging
import os
from pathlib import Path

import azure.functions as func

from feedback_log import FEEDBACK_KINDS, TableFeedbackLog
from guardrails import GuardrailEngine
from oracle_tool import OracleDatabaseTool

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
_store = None
_db = None
_build_file = Path(__file__).with_name("build_id.txt")
BUILD_ID = _build_file.read_text().strip() if _build_file.exists() else "dev"


def _database() -> OracleDatabaseTool:
    global _db
    if _db is None:
        # Foundry asks a person before each update reaches this function, so no second approval step here
        _db = OracleDatabaseTool(GuardrailEngine(require_hitl_for_db_write=False), db_mode="azure_blob")
    return _db


@app.mcp_tool()
@app.mcp_tool_property(arg_name="kind", description="unanswered_question or problem_report.")
@app.mcp_tool_property(arg_name="user_message", description="The user's question or problem report, in their words.")
@app.mcp_tool_property(arg_name="summary", description="One-line summary of the missing information or the problem.")
@app.mcp_tool_property(
    arg_name="details",
    description=(
        "For a question: what you searched for and what was missing. For a problem: the affected system, "
        "error messages and impact. Empty string if there is nothing to add."
    ),
    is_required=False,
)
def log_feedback(kind: str, user_message: str, summary: str, details: str = "") -> str:
    """
    Records a question the Knowledge Base couldn't answer, or a problem the user reported, in the team's
    feedback log for review. Append-only, so no approval is needed."""
    global _store
    if kind not in FEEDBACK_KINDS:
        return json.dumps({"success": False, "message": f"kind must be {' or '.join(FEEDBACK_KINDS)}."})
    if _store is None:
        _store = TableFeedbackLog(os.environ["FEEDBACK_TABLE_ENDPOINT"], os.environ.get("FEEDBACK_TABLE_NAME", "feedback"))
    entry = _store.record(kind, user_message, summary, details or "", agent="Foundry agent (MCP tool)")
    logging.info("Logged %s %s", entry["kind"], entry["id"])
    return json.dumps({"success": True, "entry_id": entry["id"], "message": "Logged for the team to review."})


@app.mcp_tool()
@app.mcp_tool_property(arg_name="sql_query", description="Read-only SQL SELECT query to execute on Oracle DB.")
@app.mcp_tool_property(arg_name="reason", description="Brief explanation of why this query is needed.")
def query_oracle_db(sql_query: str, reason: str) -> str:
    """
    Executes a guarded read-only SQL SELECT query against the Oracle Database. Approved tables and their columns
    (use these exact names): SYSTEM_INCIDENTS(INCIDENT_ID, SERVICE_NAME, ORACLE_ERROR_CODE, SEVERITY, STATUS,
    DEPT_ID, SUMMARY, RESOLUTION_NOTE, REPORTED_AT); DEPARTMENTS(DEPT_ID, DEPT_NAME, COST_CENTER, ONCALL_LEAD,
    LOCATION); EMPLOYEES(EMP_ID, FULL_NAME, DEPT_ID, ROLE_TITLE, STATUS). SEVERITY values are 'P1' (most severe)
    to 'P4'. Incident STATUS is one of OPEN, INVESTIGATING, MITIGATED, RESOLVED, CLOSED; everything before RESOLVED
    counts as open. Employee STATUS is ACTIVE or INACTIVE. Join incidents and employees to departments on DEPT_ID.
    Never select restricted PII columns like SSN."""
    db = _database()
    output = db.query_tool_output(db.execute_guarded_query(sql_query))
    logging.info("query_oracle_db %s: %s", output["guardrail_status"], output["message"])
    return json.dumps(output, default=str)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="incident_id", description="Incident ID in format INC-XXXX (e.g., INC-1042).")
@app.mcp_tool_property(
    arg_name="new_status", description="One of OPEN, INVESTIGATING, MITIGATED, RESOLVED, CLOSED."
)
@app.mcp_tool_property(arg_name="resolution_note", description="Summary of the fix or diagnostic findings.")
def update_oracle_incident(incident_id: str, new_status: str, resolution_note: str) -> str:
    """
    Updates the status and resolution note of an incident in the Oracle SYSTEM_INCIDENTS table.
    Protected by Human-in-the-Loop approval guardrail."""
    db = _database()
    gr = db.guardrails.validate_db_incident_update(incident_id, new_status, resolution_note)
    if not gr.passed:
        return json.dumps({"success": False, "guardrail_status": gr.status, "message": gr.message})
    result = db.commit_incident_update(incident_id, new_status, resolution_note)
    logging.info("update_oracle_incident %s: %s", incident_id, result.get("message") or result.get("error"))
    return json.dumps(result)


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Reports the deployed build so the deploy script knows when new code is live. Reveals nothing else."""
    return func.HttpResponse(json.dumps({"build": BUILD_ID}), mimetype="application/json")
