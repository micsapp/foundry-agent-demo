"""
Azure Function serving the agent's server-side tools over MCP (Model Context Protocol):

- `log_feedback` writes to the Azure Table named by FEEDBACK_TABLE_NAME.
- `query_oracle_db` and `update_oracle_incident` work on the Oracle-compatible mock database kept as a SQLite
  blob (ORACLE_MOCK_BLOB_URL), with the same guardrails as the Streamlit app.
- `propose_kb_file_update` edits a Knowledge Base Markdown file in the blob container KB_BLOB_CONTAINER_URL and
  re-indexes it in the vector store File Search uses (the one named in the container's sync record), with the
  same guardrails as the app. It reaches the vector store as the Function App's identity (Foundry User role).
- `list_skills`, `load_skill`, `read_skill_resource` and `run_skill_script` expose packaged skills.
  Registered Python scripts run here with validated arguments and a timeout; the incident-report
  skill receives a masked, read-only database snapshot and returns Markdown and JSON.

The agent definition makes Foundry ask a person to approve each `update_oracle_incident` and
`propose_kb_file_update` call before it reaches this function, so neither asks again here.

Foundry connects to https://<app>/runtime/webhooks/mcp (streamable HTTP) with the app's `mcp_extension`
system key, which it keeps in a Foundry project connection, not in code. Storage is reached with the
Function App's managed identity. Because Foundry calls the server itself, the tools also work in the portal
playground, and MCP is a tool type gpt-5-mini supports.

It also serves a public chat page, GET /api/chat, where anyone can talk to a read-only agent without signing in
(public_chat.py has the limits and safeguards).

Deployed by ../deploy_mcp_tools.sh, which copies feedback_log.py, oracle_tool.py, kb_tool.py,
guardrails.py, skill_tool.py and skills/ next to this file so the tools match the local demo, and writes
build_id.txt so the script can tell when the new code is live (GET /api/health returns it).
"""

import json
import logging
import os
from html import escape
from pathlib import Path

import azure.functions as func

from feedback_log import FEEDBACK_KINDS, TableFeedbackLog
from guardrails import GuardrailEngine
from kb_tool import KnowledgeBaseTool
from oracle_tool import OracleDatabaseTool
from public_chat import default_chat
from skill_tool import SkillTool

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
_store = None
_db = None
_kb = None
_build_file = Path(__file__).with_name("build_id.txt")
BUILD_ID = _build_file.read_text().strip() if _build_file.exists() else "dev"


def _database() -> OracleDatabaseTool:
    global _db
    if _db is None:
        # Foundry asks a person before each update reaches this function, so no second approval step here
        _db = OracleDatabaseTool(GuardrailEngine(require_hitl_for_db_write=False), db_mode="azure_blob")
    return _db


def _knowledge_base() -> KnowledgeBaseTool:
    global _kb
    if _kb is None:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        endpoint = os.environ["PROJECT_ENDPOINT"]
        kb = KnowledgeBaseTool(GuardrailEngine(require_hitl_for_kb_update=False), kb_mode="azure_blob")
        project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
        kb.attach_vector_store(project.get_openai_client(), endpoint)
        _kb = kb
    return _kb


_skills = SkillTool(database_factory=_database)


@app.mcp_tool()
def list_skills() -> str:
    """Discover packaged agent skills before selecting one for an incident report or handover."""
    return json.dumps(_skills.list_skills())


@app.mcp_tool()
@app.mcp_tool_property(arg_name="skill_name", description="Exact skill name returned by list_skills.")
def load_skill(skill_name: str) -> str:
    """Load a skill's instructions, script argument schemas and resource names before using it."""
    return json.dumps(_skills.load_skill(skill_name))


@app.mcp_tool()
@app.mcp_tool_property(arg_name="skill_name", description="Exact skill name returned by list_skills.")
@app.mcp_tool_property(arg_name="resource_name", description="Exact resource name returned by load_skill.")
def read_skill_resource(skill_name: str, resource_name: str) -> str:
    """Read a packaged reference resource using an exact name returned by load_skill."""
    return json.dumps(_skills.read_skill_resource(skill_name, resource_name))


@app.mcp_tool()
@app.mcp_tool_property(arg_name="skill_name", description="Exact skill name returned by list_skills.")
@app.mcp_tool_property(arg_name="script_name", description="Exact registered script name returned by load_skill.")
@app.mcp_tool_property(
    arg_name="arguments_json",
    description="A JSON object encoded as a string matching the script's arguments_schema. Use '{}' for defaults.",
    is_required=False,
)
def run_skill_script(skill_name: str, script_name: str, arguments_json: str = "{}") -> str:
    """Run a named packaged read-only skill script on this Azure Function and return its JSON result.

    Load the skill first. Only registered scripts run, with argument validation and
    a timeout. Command lines, arbitrary code and user-supplied paths are unsupported.
    """
    result = _skills.run_skill_script(skill_name, script_name, arguments_json)
    logging.info("Skill %s/%s: success=%s", skill_name, script_name, result["success"])
    return json.dumps(result, ensure_ascii=False)


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


@app.mcp_tool()
@app.mcp_tool_property(
    arg_name="filename", description="Target Markdown filename in knowledge_base/ (e.g. oracle_db_runbook.md)."
)
@app.mcp_tool_property(arg_name="updated_content", description="Complete updated Markdown content for the file.")
@app.mcp_tool_property(arg_name="change_summary", description="Concise summary of what was added or changed.")
def propose_kb_file_update(filename: str, updated_content: str, change_summary: str) -> str:
    """
    Proposes an update to a Markdown source file in the Knowledge Base (e.g., oracle_db_runbook.md or
    sla_and_escalation_guide.md) and syncs it to the Foundry vector store after passing PII guardrails and
    Human-in-the-Loop approval."""
    kb = _knowledge_base()
    gr = kb.propose_kb_update(filename, updated_content, change_summary)
    if not gr.passed:
        return json.dumps({"success": False, "guardrail_status": gr.status, "message": gr.message})
    res = kb.commit_kb_update(filename, updated_content, change_summary)
    logging.info("propose_kb_file_update %s: %s", res["filename"], res["foundry_sync"])
    return json.dumps(
        {"success": True, "filename": res["filename"], "message": f"Saved {res['filename']}. {res['foundry_sync']}"}
    )


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Reports the deployed build so the deploy script knows when new code is live. Reveals nothing else."""
    return func.HttpResponse(json.dumps({"build": BUILD_ID}), mimetype="application/json")


# ----------------------------------------------------------------------------------------------------------------
# Public chat page (no sign-in); agent selection, limits and approvals are described in public_chat.py
# ----------------------------------------------------------------------------------------------------------------
_PUBLIC_DIR = Path(__file__).with_name("public")
_chat = default_chat()
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://cdnjs.cloudflare.com; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@app.route(route="chat", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def chat_page(req: func.HttpRequest) -> func.HttpResponse:
    agent = os.environ.get("PUBLIC_CHAT_AGENT", "Operations Assistant")
    version = os.environ.get("PUBLIC_CHAT_AGENT_VERSION", "").strip()
    label = f"{agent} · v{version}" if version else agent
    page = (_PUBLIC_DIR / "chat.html").read_text(encoding="utf-8").replace("{{AGENT_LABEL}}", escape(label))
    return func.HttpResponse(
        page, mimetype="text/html", charset="utf-8", headers={**_PAGE_HEADERS, "Cache-Control": "no-store"}
    )


@app.route(route="chat/app.js", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def chat_script(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(
        (_PUBLIC_DIR / "chat.js").read_bytes(), mimetype="text/javascript", charset="utf-8",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"},
    )


@app.route(route="chat/message", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def chat_message(req: func.HttpRequest) -> func.HttpResponse:
    status, payload = _chat.handle(req.get_body(), {k.lower(): v for k, v in req.headers.items()})
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
