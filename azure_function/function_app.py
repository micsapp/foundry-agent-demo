"""
Azure Function serving the agent's `log_feedback` tool over MCP (Model Context Protocol).

Foundry connects to https://<app>/runtime/webhooks/mcp (streamable HTTP) with the app's `mcp_extension`
system key, which it keeps in a Foundry project connection, not in code. Each entry goes to the Azure Table
named by FEEDBACK_TABLE_NAME, written with the Function App's managed identity. Because Foundry calls the
server itself, logging also works in the portal playground, and MCP is a tool type gpt-5-mini supports.

Deployed by ../deploy_feedback_function.sh, which copies feedback_log.py and guardrails.py next to this file
so masking and the entry format match the Streamlit app exactly, and writes build_id.txt so the script can
tell when the new code is live (GET /api/health returns it).
"""

import json
import logging
import os
from pathlib import Path

import azure.functions as func

from feedback_log import FEEDBACK_KINDS, TableFeedbackLog

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
_store = None
_build_file = Path(__file__).with_name("build_id.txt")
BUILD_ID = _build_file.read_text().strip() if _build_file.exists() else "dev"


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
    """Records a question the Knowledge Base couldn't answer, or a problem the user reported, in the team's
    feedback log for review. Append-only, so no approval is needed."""
    global _store
    if kind not in FEEDBACK_KINDS:
        return json.dumps({"success": False, "message": f"kind must be {' or '.join(FEEDBACK_KINDS)}."})
    if _store is None:
        _store = TableFeedbackLog(os.environ["FEEDBACK_TABLE_ENDPOINT"], os.environ.get("FEEDBACK_TABLE_NAME", "feedback"))
    entry = _store.record(kind, user_message, summary, details or "", agent="Foundry agent (MCP tool)")
    logging.info("Logged %s %s", entry["kind"], entry["id"])
    return json.dumps({"success": True, "entry_id": entry["id"], "message": "Logged for the team to review."})


@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Reports the deployed build so the deploy script knows when new code is live. Reveals nothing else."""
    return func.HttpResponse(json.dumps({"build": BUILD_ID}), mimetype="application/json")
