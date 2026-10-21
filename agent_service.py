"""
Microsoft Foundry Agent Orchestrator with Action Guardrails & Human-in-the-Loop (HITL) Gating.

Supports two execution modes:
1. `foundry`: Uses the `azure-ai-projects` 2.x SDK against a Foundry project endpoint (`PROJECT_ENDPOINT`).
   It creates a prompt agent (`PromptAgentDefinition` with `FileSearchTool` + `FunctionTool`s) and runs it
   through the OpenAI Responses API (conversations + `agent_reference`), with every function call guarded.
2. `demo`: Works out-of-the-box locally (without requiring Azure credentials first) to demonstrate
   the exact same tool call lifecycle, SQL & KB guardrails, and Human-in-the-Loop approval flow.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from feedback_log import FEEDBACK_KINDS, open_feedback_log
from guardrails import GuardrailEngine, GuardrailResult
from kb_tool import KnowledgeBaseTool
from oracle_tool import OracleDatabaseTool

FOUNDRY_AGENT_NAME = "Oracle-And-KB-Guarded-Agent"
KB_VECTOR_STORE_NAME = "Enterprise-Oracle-KB-Store"
MAX_TOOL_ROUNDS = 8  # function-call round trips allowed in one user turn
ORACLE_MCP_TOOLS = ("query_oracle_db", "update_oracle_incident")  # served by the Azure Function in azure_blob mode
DEMO_KB_MIN_SCORE = 2.0  # demo-mode KB hits below this matched only common words
PROBLEM_REPORT_PATTERN = re.compile(
    r"\b(report(ing)? (a|an) (problem|issue|bug)|broken|not working|(doesn't|does not|isn't|is not) working"
    r"|wrong answer|outdated)\b",
    re.IGNORECASE,
)


@dataclass
class PendingApprovalAction:
    """Represents a guarded agent tool call awaiting Human-in-the-Loop approval."""

    action_id: str
    tool_name: str
    arguments: Dict[str, Any]
    guardrail_result: GuardrailResult
    foundry_response_id: Optional[str] = None
    foundry_conversation_id: Optional[str] = None
    foundry_call_id: Optional[str] = None
    # Set for an MCP tool call that Foundry holds until the operator answers (it then calls the Azure Function)
    foundry_approval_request_id: Optional[str] = None


@dataclass
class AgentTurnResponse:
    """Structured response from a single agent turn."""

    reply_markdown: str
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    pending_approvals: List[PendingApprovalAction] = field(default_factory=list)


# ============================================================================
# Function Tool Schemas for Foundry Function Calling (Responses API format)
# ============================================================================

FOUNDRY_TOOL_SCHEMAS = [
    {
        "name": "query_oracle_db",
        "description": (
            "Executes a guarded read-only SQL SELECT query against the Oracle Database. "
            "Approved tables and their columns (use these exact names): "
            "SYSTEM_INCIDENTS(INCIDENT_ID, SERVICE_NAME, ORACLE_ERROR_CODE, SEVERITY, STATUS, DEPT_ID, SUMMARY, "
            "RESOLUTION_NOTE, REPORTED_AT); DEPARTMENTS(DEPT_ID, DEPT_NAME, COST_CENTER, ONCALL_LEAD, LOCATION); "
            "EMPLOYEES(EMP_ID, FULL_NAME, DEPT_ID, ROLE_TITLE, STATUS). "
            "SEVERITY values are 'P1' (most severe) to 'P4'. Incident STATUS is one of OPEN, INVESTIGATING, "
            "MITIGATED, RESOLVED, CLOSED; everything before RESOLVED counts as open. Employee STATUS is ACTIVE "
            "or INACTIVE. Join incidents and employees to departments on DEPT_ID. "
            "Never select restricted PII columns like SSN."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql_query": {
                    "type": "string",
                    "description": "Read-only SQL SELECT query to execute on Oracle DB.",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why this query is needed.",
                },
            },
            "required": ["sql_query", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_oracle_incident",
        "description": (
            "Updates the status and resolution note of an incident in the Oracle SYSTEM_INCIDENTS table. "
            "Protected by Human-in-the-Loop approval guardrail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "Incident ID in format INC-XXXX (e.g., INC-1042).",
                },
                "new_status": {
                    "type": "string",
                    "enum": ["OPEN", "INVESTIGATING", "MITIGATED", "RESOLVED", "CLOSED"],
                },
                "resolution_note": {
                    "type": "string",
                    "description": "Summary of the fix or diagnostic findings.",
                },
            },
            "required": ["incident_id", "new_status", "resolution_note"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_kb_file_update",
        "description": (
            "Proposes an update to a Markdown source file in the Knowledge Base "
            "(e.g., oracle_db_runbook.md or sla_and_escalation_guide.md) and syncs it to the "
            "Foundry vector store after passing PII guardrails and Human-in-the-Loop approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Target Markdown filename in knowledge_base/ (e.g. oracle_db_runbook.md).",
                },
                "updated_content": {
                    "type": "string",
                    "description": "Complete updated Markdown content for the file.",
                },
                "change_summary": {
                    "type": "string",
                    "description": "Concise summary of what was added or changed.",
                },
            },
            "required": ["filename", "updated_content", "change_summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_feedback",
        "description": (
            "Records a question the Knowledge Base couldn't answer, or a problem the user reported, in the "
            "team's feedback log for review. Append-only, so no approval is needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": list(FEEDBACK_KINDS),
                },
                "user_message": {
                    "type": "string",
                    "description": "The user's question or problem report, in their words.",
                },
                "summary": {
                    "type": "string",
                    "description": "One-line summary of the missing information or the problem.",
                },
                "details": {
                    "type": "string",
                    "description": (
                        "For a question: what you searched for and what was missing. For a problem: the "
                        "affected system, error messages and impact. Empty string if there is nothing to add."
                    ),
                },
            },
            "required": ["kind", "user_message", "summary", "details"],
            "additionalProperties": False,
        },
    },
]



def feedback_mcp_tool(server_url: str, connection_name: str) -> Any:
    """`log_feedback` from the Azure Function's MCP server (deploy_feedback_function.sh sets it up).

    Foundry calls the server itself and sends the key stored in the project connection as the
    x-functions-key header, so the key never appears in this code or in the agent definition.
    """
    from azure.ai.projects.models import MCPTool, MCPToolFilter

    return MCPTool(
        server_label="feedback_log",
        server_url=server_url,
        allowed_tools=MCPToolFilter(tool_names=["log_feedback"]),  # the form Foundry stores, so reuse checks match
        require_approval="never",  # append-only log; an approval step would stall the portal playground
        project_connection_id=connection_name,
    )


def oracle_mcp_tool(server_url: str, connection_name: str) -> Any:
    """`query_oracle_db` and `update_oracle_incident` from the same MCP server, working on the blob database.

    Foundry holds every `update_oracle_incident` call until someone approves it: the app shows its approval card
    and answers with an mcp_approval_response, and the portal playground shows its own approve/deny prompt.
    """
    from azure.ai.projects.models import MCPTool, MCPToolFilter, MCPToolRequireApproval

    return MCPTool(
        server_label="oracle_db",
        server_url=server_url,
        allowed_tools=MCPToolFilter(tool_names=list(ORACLE_MCP_TOOLS)),
        require_approval=MCPToolRequireApproval(
            always=MCPToolFilter(tool_names=["update_oracle_incident"]),
            never=MCPToolFilter(tool_names=["query_oracle_db"]),
        ),
        project_connection_id=connection_name,
    )


FOUNDRY_AGENT_INSTRUCTIONS = (
    "You are an Enterprise Operations AI Agent built on Microsoft Foundry. "
    "You answer questions using the Knowledge Base (File Search), query the Oracle DB "
    "using `query_oracle_db`, update incidents using `update_oracle_incident`, "
    "update Knowledge Base files using `propose_kb_file_update`, and record gaps and problems using "
    "`log_feedback`. "
    "The Knowledge Base also holds product manuals (PDF). For questions about them, answer only from "
    "what File Search returns: quote exact values (ports, limits, codes, versions), name the source "
    "document, keep products apart, and if the Knowledge Base doesn't contain the answer, say so "
    "instead of guessing. "
    "Whenever the Knowledge Base can't answer a question, or part of one, call `log_feedback` with kind "
    "unanswered_question so the team can fill the gap. A manual not mentioning something is such a gap, not "
    "proof that it doesn't exist: say the Knowledge Base doesn't cover it instead of answering no.When the user reports a problem (something broken "
    "or failing, or an answer of yours that is wrong or outdated), call `log_feedback` with kind "
    "problem_report and give them the entry ID. Log each question or report once, and leave passwords, "
    "keys and personal data out of log entries. "
    "You have no web access: never offer to search the internet, check vendor websites, or look up "
    "outside sources. "
    "When a tool returns status PENDING_HITL, tell the user the change is waiting for a human "
    "operator's approval and don't call the tool again. Messages starting with [Operator update] "
    "report the operator's approval decisions. Approvals happen only in the operator's approval step, never "
    "in the chat: if a change is rejected, say it wasn't made and don't ask the user to approve it by replying."
)


class FoundryAgentOrchestrator:
    """Orchestrates conversations, Knowledge Base retrieval, Oracle DB queries, and Guardrail checks."""

    def __init__(
        self,
        agent_mode: str = "demo",
        db_mode: str = "sqlite_mock",
    ) -> None:
        self.agent_mode = agent_mode
        self.guardrails = GuardrailEngine()
        self.oracle_tool = OracleDatabaseTool(guardrails=self.guardrails, db_mode=db_mode)
        self.kb_tool = KnowledgeBaseTool(guardrails=self.guardrails)
        self.feedback_log = open_feedback_log()
        self.feedback_via_function = False  # True when Foundry calls the log_feedback MCP server itself
        self.db_via_function = False  # True when Foundry calls the same server's DB tools (blob database)
        self.pending_actions: Dict[str, PendingApprovalAction] = {}

        # Optional Microsoft Foundry state (azure-ai-projects 2.x + OpenAI Responses API)
        self.project_client: Optional[Any] = None
        self.openai_client: Optional[Any] = None
        self.foundry_agent_name: Optional[str] = None
        self.foundry_agent_version: Optional[str] = None
        self.foundry_conversation_id: Optional[str] = None
        self.foundry_status: str = "Not connected — using local demo mode."
        # Approval decisions made since the last Foundry turn, sent to the agent with the next message
        self._operator_notes: List[str] = []

        if self.agent_mode == "foundry":
            self._init_foundry_client()

    def _init_foundry_client(self) -> None:
        """Connects to the Foundry project, syncs the KB vector store, and reuses or creates the agent version."""
        endpoint = (os.getenv("PROJECT_ENDPOINT") or os.getenv("FOUNDRY_PROJECT_ENDPOINT") or "").strip()
        if not endpoint:
            if os.getenv("PROJECT_CONNECTION_STRING", "").strip():
                self.foundry_status = (
                    "PROJECT_CONNECTION_STRING is only for classic hub-based projects, which this version "
                    "no longer supports. Set PROJECT_ENDPOINT to your Foundry project endpoint "
                    "(https://<resource>.services.ai.azure.com/api/projects/<project>)."
                )
            else:
                self.foundry_status = "PROJECT_ENDPOINT is not set — using local demo mode."
            return

        try:
            from azure.ai.projects import AIProjectClient
            from azure.ai.projects.models import FileSearchTool, FunctionTool, PromptAgentDefinition
            from azure.identity import DefaultAzureCredential

            self.project_client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
            # Retries back off and honour Retry-After, which rides out short 429 rate-limit bursts
            self.openai_client = self.project_client.get_openai_client(max_retries=6)

            # Reuse this project's KB vector store; only new or changed files are uploaded
            store_status = self.kb_tool.connect_vector_store(
                self.openai_client,
                KB_VECTOR_STORE_NAME,
                endpoint,
                os.getenv("AZURE_VECTOR_STORE_ID", "").strip(),
            )

            tools = [FileSearchTool(vector_store_ids=[self.kb_tool.vector_store_id])]
            mcp_url = os.getenv("FEEDBACK_MCP_URL", "").strip()
            mcp_connection = os.getenv("FEEDBACK_CONNECTION_NAME", "").strip()
            self.feedback_via_function = bool(mcp_url and mcp_connection)
            # The function's DB tools work on the blob database, so use them only when this app shows that one too
            self.db_via_function = self.feedback_via_function and self.oracle_tool.db_mode == "azure_blob"
            for schema in FOUNDRY_TOOL_SCHEMAS:
                if schema["name"] == "log_feedback" and self.feedback_via_function:
                    tools.append(feedback_mcp_tool(mcp_url, mcp_connection))
                elif schema["name"] in ORACLE_MCP_TOOLS and self.db_via_function:
                    if schema["name"] == ORACLE_MCP_TOOLS[0]:  # one MCP tool entry covers both
                        tools.append(oracle_mcp_tool(mcp_url, mcp_connection))
                else:
                    tools.append(FunctionTool(strict=True, **schema))
            definition = PromptAgentDefinition(
                model=os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o"),
                instructions=FOUNDRY_AGENT_INSTRUCTIONS,
                tools=tools,
            )
            agent_name, agent_version, agent_how = self._get_or_create_agent_version(definition)
            self.foundry_agent_name = agent_name
            self.foundry_agent_version = agent_version
            self.foundry_conversation_id = self.openai_client.conversations.create().id
            # Calls Foundry held in the previous conversation can no longer be answered
            for action_id in [a for a, act in self.pending_actions.items() if act.foundry_approval_request_id]:
                del self.pending_actions[action_id]
            feedback_where = "Azure Function (MCP)" if self.feedback_via_function else self.feedback_log.location
            db_where = "Azure Function (MCP)" if self.db_via_function else "this app"
            self.foundry_status = (
                f"Connected to {endpoint} — agent {agent_name} v{agent_version} ({agent_how}), {store_status}, "
                f"feedback log: {feedback_where}, database tools: {db_where} ({self.oracle_tool.location})."
            )
        except Exception as exc:
            # Fall back to demo mode if credentials, roles, or the endpoint aren't ready yet
            self.project_client = None
            self.openai_client = None
            self.foundry_agent_name = None
            self.foundry_agent_version = None
            self.feedback_via_function = False
            self.db_via_function = False
            self.foundry_status = f"Foundry connection failed — using local demo mode. {type(exc).__name__}: {exc}"

    # ========================================================================
    # Main Chat Entrypoint
    # ========================================================================
    def _get_or_create_agent_version(self, definition: Any) -> Tuple[str, str, str]:
        """Reuses the agent's latest version if its definition is unchanged; otherwise creates a new version."""
        from azure.core.exceptions import ResourceNotFoundError

        try:
            latest = self.project_client.agents.get(agent_name=FOUNDRY_AGENT_NAME).as_dict()["versions"]["latest"]
            if latest.get("definition") == definition.as_dict():
                return latest["name"], latest["version"], "reused"
        except ResourceNotFoundError:
            pass  # first run in this project
        agent = self.project_client.agents.create_version(
            agent_name=FOUNDRY_AGENT_NAME,
            definition=definition,
            description="Oracle DB + Knowledge Base agent with guarded, human-approved actions.",
        )
        return agent.name, agent.version, "new version"

    def handle_user_message(self, user_message: str) -> AgentTurnResponse:
        """Processes a user prompt, runs appropriate tools through guardrails, and returns response."""
        if self.agent_mode == "foundry" and self.openai_client and self.foundry_agent_name:
            return self._handle_foundry_sdk_turn(user_message)
        return self._handle_intelligent_demo_turn(user_message)

    # ========================================================================
    # Live Microsoft Foundry Turn Handler (Responses API)
    # ========================================================================
    def _handle_foundry_sdk_turn(self, user_message: str) -> AgentTurnResponse:
        """Runs one turn on the Foundry agent, executing each function call through the guardrails."""
        held = [act for act in self.pending_actions.values() if act.foundry_approval_request_id]
        if held:
            # Foundry rejects new input (HTTP 400) while an MCP approval request is unanswered
            names = ", ".join(f"`{act.tool_name}` (`{act.action_id}`)" for act in held)
            return AgentTurnResponse(
                reply_markdown=(
                    f"⏸️ Foundry is holding {names} until you approve or reject it below. The agent can't take "
                    "another message until then, so decide on it first and then ask again."
                ),
                pending_approvals=held,
            )
        return self._run_foundry_turn([{"type": "message", "role": "user", "content": user_message}])

    def _run_foundry_turn(self, items: List[Dict[str, Any]]) -> AgentTurnResponse:
        """Sends `items` (after any queued operator notes) to the agent and answers its tool calls until it replies.

        Function calls run here through the guardrails. MCP calls Foundry holds for approval are checked against
        the same guardrails and then either answered at once (blocked, or approval switched off) or queued for
        the operator, whose Approve/Reject goes back to Foundry through `_answer_mcp_approval`.
        """
        traces: List[Dict[str, Any]] = []
        pending_list: List[PendingApprovalAction] = []
        agent_ref = {"agent_reference": {"name": self.foundry_agent_name, "type": "agent_reference"}}

        turn_input = [{"type": "message", "role": "user", "content": note} for note in self._operator_notes]
        turn_input.extend(items)

        try:
            response = self.openai_client.responses.create(
                conversation=self.foundry_conversation_id,
                input=turn_input,
                extra_body=agent_ref,
            )
            self._operator_notes.clear()
            self._trace_server_tool_calls(response, traces)

            # Every function call must get an output in the same conversation before the next turn,
            # so HITL-gated calls are answered with PENDING_HITL instead of leaving the call open.
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                over_limit = round_no == MAX_TOOL_ROUNDS
                next_input: List[Dict[str, Any]] = []
                for call in (item for item in response.output if item.type == "function_call"):
                    if over_limit:
                        outcome: Dict[str, Any] = {
                            "error": "Tool call limit for this turn reached. Answer with what you have."
                        }
                    else:
                        try:
                            fn_args = json.loads(call.arguments or "{}")
                        except json.JSONDecodeError:
                            fn_args = None
                        if fn_args is None:
                            outcome = {"error": "Arguments were not valid JSON."}
                        else:
                            outcome = self._dispatch_guarded_tool(
                                call.name,
                                fn_args,
                                traces,
                                pending_list,
                                foundry_response_id=response.id,
                                foundry_conversation_id=self.foundry_conversation_id,
                                foundry_call_id=call.call_id,
                            )
                    next_input.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(outcome, default=str),
                        }
                    )
                for request in (item for item in response.output if item.type == "mcp_approval_request"):
                    answer = self._review_mcp_approval(request, traces, pending_list, response.id, over_limit)
                    if answer:
                        next_input.append(answer)
                if not next_input:
                    break
                response = self.openai_client.responses.create(
                    conversation=self.foundry_conversation_id,
                    input=next_input,
                    extra_body=agent_ref,
                )
                self._trace_server_tool_calls(response, traces)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 429:
                hint = (
                    "The model deployment's rate limit was hit. Wait a minute and ask again, or raise the "
                    "deployment's capacity (tokens per minute) in the Foundry portal or `./az_tui.sh` → menu 6."
                )
            else:
                hint = (
                    "Check your sign-in (`az login`), your role on the Foundry project (Foundry User, formerly "
                    "Azure AI User), and that MODEL_DEPLOYMENT_NAME matches a deployment."
                )
            return AgentTurnResponse(
                reply_markdown=f"⚠️ **Foundry request failed:** `{type(exc).__name__}: {exc}`\n\n{hint}",
                tool_traces=traces,
                pending_approvals=pending_list,
            )

        reply = response.output_text or ("" if pending_list else "Completed run.")
        if pending_list:
            reply = (
                "⏸️ **Action Paused by Human-in-the-Loop (HITL) Guardrail**\n\n"
                "The Foundry agent requested a state-modifying action that passed "
                "automated safety checks and now requires your explicit approval below.\n\n" + reply
            ).rstrip()
        return AgentTurnResponse(
            reply_markdown=reply,
            tool_traces=traces,
            pending_approvals=pending_list,
        )

    def _review_mcp_approval(
        self,
        request: Any,
        traces: List[Dict[str, Any]],
        pending_list: List[PendingApprovalAction],
        response_id: str,
        over_limit: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Checks an MCP call Foundry holds for approval. Returns the answer to send now, or None once it's
        queued for the operator."""
        try:
            args = json.loads(request.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if request.name == "update_oracle_incident":
            gr = self.guardrails.validate_db_incident_update(
                str(args.get("incident_id") or ""),
                str(args.get("new_status") or ""),
                str(args.get("resolution_note") or ""),
            )
        else:
            gr = self.guardrails.log_result(
                GuardrailResult(
                    passed=True,
                    requires_hitl=True,
                    status="PENDING_HITL",
                    action_type=request.name,
                    rule_triggered="MCP_APPROVAL_REQUIRED",
                    message=f"Foundry holds this {request.name} call until it's approved.",
                )
            )
        traces.append(
            {
                "tool": request.name,
                "arguments": args,
                "guardrail_status": gr.status,
                "rule": gr.rule_triggered,
                "message": gr.message,
            }
        )
        if over_limit:
            return {"type": "mcp_approval_response", "approval_request_id": request.id, "approve": False}
        if not gr.passed or not gr.requires_hitl:
            # Blocked by a guardrail: deny it. Approval switched off in the sidebar: approve it without asking.
            return {"type": "mcp_approval_response", "approval_request_id": request.id, "approve": gr.passed}
        act = PendingApprovalAction(
            action_id=f"act-{uuid.uuid4().hex[:8]}",
            tool_name=request.name,
            arguments=args,
            guardrail_result=gr,
            foundry_response_id=response_id,
            foundry_conversation_id=self.foundry_conversation_id,
            foundry_approval_request_id=request.id,
        )
        self.pending_actions[act.action_id] = act
        pending_list.append(act)
        return None

    def _trace_server_tool_calls(self, response: Any, traces: List[Dict[str, Any]]) -> None:
        """Records MCP tool calls Foundry ran itself on the Azure Function, for the UI and kb_eval."""
        for item in response.output:
            if item.type != "mcp_call":
                continue
            try:
                args = json.loads(getattr(item, "arguments", None) or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = json.loads(getattr(item, "output", None) or "{}")
            except json.JSONDecodeError:
                result = {}
            if not isinstance(result, dict):
                result = {}
            tool = getattr(item, "name", "") or "mcp"
            error = getattr(item, "error", None)
            if error or not result:
                status = "ERROR"
            elif result.get("success"):
                status = "PASSED"
            else:  # the function's guardrails blocked it, or the database or table write failed
                status = "BLOCKED" if result.get("guardrail_status") == "BLOCKED" else "ERROR"
            if tool == "log_feedback":
                message = (
                    f"Foundry called the MCP server; logged {args.get('kind', 'entry')} as {result.get('entry_id')}."
                    if status == "PASSED"
                    else f"Foundry called the MCP server; no confirmed entry: {error or result or 'no reply'}"
                )
            else:
                message = f"Foundry called the MCP server: {error or result.get('message') or result.get('error') or 'no reply'}"
            gr = self.guardrails.log_result(
                GuardrailResult(
                    passed=status == "PASSED",
                    requires_hitl=False,
                    status=status,
                    action_type=tool,
                    rule_triggered="AZURE_FUNCTION_MCP_TOOL",
                    message=message,
                )
            )
            trace = {
                "tool": tool,
                "arguments": args,
                "guardrail_status": gr.status,
                "rule": gr.rule_triggered,
                "message": message,
            }
            if tool == "log_feedback":
                trace["entry_id"] = result.get("entry_id")
            if "rows" in result:
                trace["rows"] = result["rows"]
            traces.append(trace)

    # ========================================================================
    # Guarded Tool Dispatcher (Shared by Foundry SDK & Local Demo Mode)
    # ========================================================================
    def _dispatch_guarded_tool(
        self,
        fn_name: str,
        fn_args: Dict[str, Any],
        traces: List[Dict[str, Any]],
        pending_list: List[PendingApprovalAction],
        foundry_response_id: Optional[str] = None,
        foundry_conversation_id: Optional[str] = None,
        foundry_call_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes a tool call through the GuardrailEngine and records trace/HITL state."""
        if fn_name == "query_oracle_db":
            sql_query = fn_args.get("sql_query", "")
            res = self.oracle_tool.execute_guarded_query(sql_query)
            output = self.oracle_tool.query_tool_output(res)
            traces.append(
                {
                    "tool": "query_oracle_db",
                    "arguments": fn_args,
                    "guardrail_status": output["guardrail_status"],
                    "rule": res["guardrail"].rule_triggered,
                    "message": output["message"],
                    "rows": output["rows"],
                }
            )
            return output

        if fn_name == "update_oracle_incident":
            inc_id = fn_args.get("incident_id", "INC-1042")
            new_status = fn_args.get("new_status", "RESOLVED")
            note = fn_args.get("resolution_note", "")
            gr = self.guardrails.validate_db_incident_update(inc_id, new_status, note)
            traces.append(
                {
                    "tool": "update_oracle_incident",
                    "arguments": fn_args,
                    "guardrail_status": gr.status,
                    "rule": gr.rule_triggered,
                    "message": gr.message,
                }
            )
            if not gr.passed:
                return {"success": False, "guardrail_status": gr.status, "message": gr.message}

            if gr.requires_hitl:
                act = PendingApprovalAction(
                    action_id=f"act-{uuid.uuid4().hex[:8]}",
                    tool_name="update_oracle_incident",
                    arguments=fn_args,
                    guardrail_result=gr,
                    foundry_response_id=foundry_response_id,
                    foundry_conversation_id=foundry_conversation_id,
                    foundry_call_id=foundry_call_id,
                )
                self.pending_actions[act.action_id] = act
                pending_list.append(act)
                return {"status": "PENDING_HITL", "action_id": act.action_id, "message": gr.message}

            commit_res = self.oracle_tool.commit_incident_update(inc_id, new_status, note)
            return commit_res

        if fn_name == "propose_kb_file_update":
            filename = fn_args.get("filename", "oracle_db_runbook.md")
            updated_content = fn_args.get("updated_content", "")
            change_summary = fn_args.get("change_summary", "Updated KB documentation")
            gr = self.kb_tool.propose_kb_update(filename, updated_content, change_summary)
            traces.append(
                {
                    "tool": "propose_kb_file_update",
                    "arguments": {"filename": filename, "change_summary": change_summary},
                    "guardrail_status": gr.status,
                    "rule": gr.rule_triggered,
                    "message": gr.message,
                    "diff_preview": gr.diff_preview,
                }
            )
            if not gr.passed:
                return {"success": False, "guardrail_status": gr.status, "message": gr.message}

            if gr.requires_hitl:
                act = PendingApprovalAction(
                    action_id=f"act-{uuid.uuid4().hex[:8]}",
                    tool_name="propose_kb_file_update",
                    arguments=fn_args,
                    guardrail_result=gr,
                    foundry_response_id=foundry_response_id,
                    foundry_conversation_id=foundry_conversation_id,
                    foundry_call_id=foundry_call_id,
                )
                self.pending_actions[act.action_id] = act
                pending_list.append(act)
                return {"status": "PENDING_HITL", "action_id": act.action_id, "message": gr.message}

            commit_res = self.kb_tool.commit_kb_update(filename, updated_content, change_summary)
            return commit_res

        if fn_name == "log_feedback":
            kind = fn_args.get("kind", "")
            if kind not in FEEDBACK_KINDS:
                return {"success": False, "message": f"kind must be one of {', '.join(FEEDBACK_KINDS)}."}
            # Append-only log the agent never reads back: secrets are masked, no HITL needed
            try:
                entry = self.feedback_log.record(
                    kind,
                    fn_args.get("user_message", ""),
                    fn_args.get("summary", ""),
                    fn_args.get("details", ""),
                    agent=f"{self.foundry_agent_name} v{self.foundry_agent_version}" if foundry_call_id else "demo",
                    conversation_id=foundry_conversation_id,
                )
            except Exception as exc:
                message = f"Couldn't write to {self.feedback_log.location}: {type(exc).__name__}: {str(exc)[:200]}"
                traces.append(
                    {"tool": "log_feedback", "arguments": fn_args, "guardrail_status": "ERROR", "rule": "LOG_WRITE_FAILED",
                     "message": message}
                )
                return {"success": False, "message": message}
            gr = self.guardrails.log_result(
                GuardrailResult(
                    passed=True,
                    requires_hitl=False,
                    status="PASSED",
                    action_type="log_feedback",
                    rule_triggered="PII_MASKED" if entry.get("masked") else "APPEND_ONLY_FEEDBACK_LOG",
                    message=f"Logged {kind} as {entry['id']} in {self.feedback_log.path.name}.",
                )
            )
            traces.append(
                {
                    "tool": "log_feedback",
                    "arguments": {k: entry[k] for k in ("kind", "user_message", "summary", "details")},
                    "guardrail_status": gr.status,
                    "rule": gr.rule_triggered,
                    "message": gr.message,
                    "entry_id": entry["id"],
                }
            )
            return {"success": True, "entry_id": entry["id"], "message": "Logged for the team to review."}

        return {"error": f"Unknown tool: {fn_name}"}

    # ========================================================================
    # Human-in-the-Loop Approval / Rejection Handlers
    # ========================================================================
    def approve_pending_action(self, action_id: str) -> AgentTurnResponse:
        """Executes a previously queued action after the human operator clicks Approve."""
        act = self.pending_actions.pop(action_id, None)
        if not act:
            return AgentTurnResponse("Action ID not found or already processed.")
        if act.foundry_approval_request_id:
            return self._answer_mcp_approval(act, approve=True)

        if act.tool_name == "propose_kb_file_update":
            res = self.kb_tool.commit_kb_update(
                filename=act.arguments["filename"],
                new_content=act.arguments["updated_content"],
                change_summary=act.arguments["change_summary"],
            )
            self.guardrails.log_result(
                GuardrailResult(
                    passed=True,
                    requires_hitl=False,
                    status="APPROVED",
                    action_type="update_kb_file",
                    rule_triggered="HUMAN_OPERATOR_APPROVED",
                    message=(
                        f"Approved & committed update to '{res['filename']}'. "
                        f"{res['foundry_sync']}"
                    ),
                )
            )
            self._note_operator_decision(
                act, f"approved; '{res['filename']}' was updated and re-indexed ({res['foundry_sync']})"
            )
            return AgentTurnResponse(
                f"✅ **Approved & Committed KB Update:** `{res['filename']}`\n\n"
                f"- **Summary:** {res['change_summary']}\n"
                f"- **Vector Store Status:** {res['foundry_sync']}"
            )

        if act.tool_name == "update_oracle_incident":
            res = self.oracle_tool.commit_incident_update(
                incident_id=act.arguments["incident_id"],
                new_status=act.arguments["new_status"],
                resolution_note=act.arguments["resolution_note"],
            )
            outcome = res.get("message") or res.get("error", "")
            self.guardrails.log_result(
                GuardrailResult(
                    passed=res["success"],
                    requires_hitl=False,
                    status="APPROVED" if res["success"] else "ERROR",
                    action_type="update_oracle_incident",
                    rule_triggered="HUMAN_OPERATOR_APPROVED",
                    message=outcome or "Approved Oracle DB incident update.",
                )
            )
            if not res["success"]:
                self._note_operator_decision(act, f"approved, but the update failed: {outcome}")
                return AgentTurnResponse(f"⚠️ **Approved, but the Oracle DB update failed:** {outcome}")
            self._note_operator_decision(act, f"approved and executed: {outcome}")
            return AgentTurnResponse(f"✅ **Approved & Executed Oracle DB Update:** {outcome}")

        return AgentTurnResponse("Action executed.")

    def reject_pending_action(self, action_id: str) -> AgentTurnResponse:
        """Cancels a queued HITL action when the human operator clicks Reject."""
        act = self.pending_actions.pop(action_id, None)
        if not act:
            return AgentTurnResponse("Action ID not found or already processed.")
        if act.foundry_approval_request_id:
            return self._answer_mcp_approval(act, approve=False)

        self.guardrails.log_result(
            GuardrailResult(
                passed=False,
                requires_hitl=False,
                status="REJECTED",
                action_type=act.tool_name,
                rule_triggered="HUMAN_OPERATOR_REJECTED",
                message=f"Operator rejected pending {act.tool_name} action.",
            )
        )
        self._note_operator_decision(act, "rejected; no changes were made")
        return AgentTurnResponse(
            f"🛑 **Rejected Action:** `{act.tool_name}` was cancelled by operator. No changes were made."
        )

    def _answer_mcp_approval(self, act: PendingApprovalAction, approve: bool) -> AgentTurnResponse:
        """Sends the operator's decision on an MCP call Foundry is holding. On approval Foundry calls the Azure
        Function; either way the agent then carries on, and its reply follows the decision."""
        self.guardrails.log_result(
            GuardrailResult(
                passed=approve,
                requires_hitl=False,
                status="APPROVED" if approve else "REJECTED",
                action_type=act.tool_name,
                rule_triggered="HUMAN_OPERATOR_APPROVED" if approve else "HUMAN_OPERATOR_REJECTED",
                message=f"Operator {'approved' if approve else 'rejected'} the {act.tool_name} call held by Foundry.",
            )
        )
        turn = self._run_foundry_turn(
            [
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": act.foundry_approval_request_id,
                    "approve": approve,
                }
            ]
        )
        head = (
            f"✅ **Approved:** Foundry sent `{act.tool_name}` to the Azure Function."
            if approve
            else f"🛑 **Rejected Action:** `{act.tool_name}` was cancelled by operator. No changes were made."
        )
        turn.reply_markdown = f"{head}\n\n{turn.reply_markdown}".rstrip()
        return turn

    def _note_operator_decision(self, act: PendingApprovalAction, outcome: str) -> None:
        """Queues an approval decision so the Foundry agent learns about it on the next turn."""
        if act.foundry_call_id:
            self._operator_notes.append(
                f"[Operator update] The {act.tool_name} action {act.action_id} "
                f"(call {act.foundry_call_id}) was {outcome}."
            )

    # ========================================================================
    # Intelligent Local Demo Mode (Zero-Config Out-of-the-Box Experience)
    # ========================================================================
    def _handle_intelligent_demo_turn(self, user_message: str) -> AgentTurnResponse:
        """
        Routes the user prompt to Knowledge Base File Search, Oracle DB SQL tools,
        and KB Update tools so every guardrail and feature can be tested immediately.
        """
        traces: List[Dict[str, Any]] = []
        pending_list: List[PendingApprovalAction] = []
        lower_msg = user_message.lower()

        # --------------------------------------------------------------------
        # Case A: User asks to run a raw/destructive SQL query or test SQL guardrails
        # --------------------------------------------------------------------
        explicit_sql_match = re.search(
            r"`((?:SELECT|DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|WITH)\b[^`]+)`",
            user_message,
            re.IGNORECASE,
        )
        if explicit_sql_match or any(
            kw in lower_msg
            for kw in ["drop table", "delete from", "truncate table", "select ssn", "payroll_secrets"]
        ):
            if explicit_sql_match:
                sql_to_test = explicit_sql_match.group(1)
            elif "drop table" in lower_msg:
                sql_to_test = "DROP TABLE SYSTEM_INCIDENTS"
            elif "delete from" in lower_msg:
                sql_to_test = "DELETE FROM EMPLOYEES WHERE STATUS = 'INACTIVE'"
            elif "ssn" in lower_msg:
                sql_to_test = "SELECT EMP_ID, FULL_NAME, SSN FROM EMPLOYEES"
            else:
                sql_to_test = "SELECT * FROM PAYROLL_SECRETS"

            outcome = self._dispatch_guarded_tool(
                "query_oracle_db",
                {"sql_query": sql_to_test, "reason": "Executing user-requested SQL statement"},
                traces,
                pending_list,
            )
            if not outcome["success"]:
                return AgentTurnResponse(
                    reply_markdown=(
                        f"🛡️ **Action Blocked by Oracle SQL Guardrail**\n\n"
                        f"I attempted to invoke `query_oracle_db` with:\n"
                        f"```sql\n{sql_to_test}\n```\n"
                        f"**Guardrail Verdict:** {outcome['message']}"
                    ),
                    tool_traces=traces,
                    pending_approvals=pending_list,
                )

        # --------------------------------------------------------------------
        # Case B: User asks to update a Knowledge Base file (or test KB PII guardrails)
        # --------------------------------------------------------------------
        if any(
            phrase in lower_msg
            for phrase in [
                "update kb",
                "update the kb",
                "update knowledge base",
                "update `oracle_db_runbook.md`",
                "update oracle_db_runbook.md",
                "add a new section",
                "update runbook",
                "update `sla_and_escalation_guide.md`",
                "update sla_and_escalation_guide.md",
            ]
        ):
            target_file = (
                "sla_and_escalation_guide.md"
                if "sla" in lower_msg
                else "oracle_db_runbook.md"
            )

            # Check if the user is testing path traversal
            if "../" in user_message or "/etc/" in user_message:
                target_file = "../../etc/passwd"

            current_md = self.kb_tool.read_kb_file(target_file)

            # Check if the user is testing PII / Secret Guardrail in KB updates
            if "ssn" in lower_msg or "secret" in lower_msg or "password" in lower_msg or "sk-" in lower_msg:
                proposed_md = (
                    current_md
                    + "\n\n## Emergency Credentials (TEST)\n"
                    + "- Admin SSN: 123-45-6789\n"
                    + "- API Key: sk-proj1234567890abcdefghijklmnop\n"
                )
                summary = "Attempted to append credentials/PII to Knowledge Base file"
            else:
                new_section = (
                    "\n\n### ORA-12514: TNS:listener Does Not Currently Know of Service\n"
                    "- **Symptoms:** Clients fail to connect to `PROD-ORA-WEST` with `ORA-12514`.\n"
                    "- **Manual UI Diagnostic & Resolution Steps (OEM Cloud Control UI):**\n"
                    "  1. Open **Oracle Enterprise Manager (OEM) Cloud Control UI** -> **Targets** -> **All Targets**.\n"
                    "  2. Filter by **Target Type: Listener** and click **`LISTENER_PROD_WEST`**.\n"
                    "  3. Open the **Services** tab in the Listener UI to inspect which database services are currently registered.\n"
                    "  4. Navigate to **Targets** -> **Databases** -> **`PROD-ORA-WEST`** -> **Administration** -> **Initialization Parameters**.\n"
                    "  5. Verify the `LOCAL_LISTENER` and `SERVICE_NAMES` parameters in the UI table, then click **Oracle Net Services Administration** -> **Register Service** in the UI to re-register the service without CLI access.\n"
                )
                if "ORA-12514" not in current_md:
                    proposed_md = current_md.rstrip() + new_section
                else:
                    proposed_md = (
                        current_md.rstrip()
                        + f"\n\n### Runbook Note ({user_message[:60]}...)\n"
                        + "- Verified listener registration via OEM Cloud Control UI and updated manual runbook.\n"
                    )
                summary = "Added ORA-12514 manual UI troubleshooting steps (OEM Cloud Control UI) to Oracle DB Runbook"

            kb_outcome = self._dispatch_guarded_tool(
                "propose_kb_file_update",
                {
                    "filename": target_file,
                    "updated_content": proposed_md,
                    "change_summary": summary,
                },
                traces,
                pending_list,
            )

            # Also check if user asked to resolve an incident in the same prompt
            if "inc-" in lower_msg and ("resolve" in lower_msg or "mitigate" in lower_msg):
                inc_match = re.search(r"(inc-\d{4})", lower_msg)
                inc_id = inc_match.group(1).upper() if inc_match else "INC-1042"
                self._dispatch_guarded_tool(
                    "update_oracle_incident",
                    {
                        "incident_id": inc_id,
                        "new_status": "RESOLVED",
                        "resolution_note": "Re-registered service via OEM Cloud Control UI (Net Services Administration) and updated KB-ORA-001.",
                    },
                    traces,
                    pending_list,
                )

            if not kb_outcome.get("success", True) and kb_outcome.get("guardrail_status") == "BLOCKED":
                return AgentTurnResponse(
                    reply_markdown=(
                        f"🛡️ **Knowledge Base Update Blocked by Content Guardrail**\n\n"
                        f"**Target File:** `{target_file}`\n\n"
                        f"**Reason:** {kb_outcome['message']}"
                    ),
                    tool_traces=traces,
                    pending_approvals=pending_list,
                )

            if pending_list:
                return AgentTurnResponse(
                    reply_markdown=(
                        "⏸️ **Action Paused for Human-in-the-Loop (HITL) Approval**\n\n"
                        f"I have prepared the update for `{target_file}` "
                        + (
                            "and the status update for Oracle DB incident `INC-1042`. "
                            if len(pending_list) > 1
                            else ". "
                        )
                        + "All automated guardrails (**Path Validation**, **PII/Secret Scan**, and **Content Integrity**) "
                        "**PASSED**.\n\n"
                        "👉 Please review the **Unified Diff** in the Approval Card below and click **Approve & Execute** "
                        "to commit the file and re-index the Vector Store."
                    ),
                    tool_traces=traces,
                    pending_approvals=pending_list,
                )

            return AgentTurnResponse(
                reply_markdown=f"✅ Updated `{target_file}`: {summary}",
                tool_traces=traces,
                pending_approvals=pending_list,
            )

        # --------------------------------------------------------------------
        # Case C: User asks to update an Oracle DB incident directly
        # --------------------------------------------------------------------
        if "inc-" in lower_msg and any(
            w in lower_msg for w in ["mark", "update", "resolve", "close", "mitigate"]
        ):
            inc_match = re.search(r"(inc-\d{4})", lower_msg)
            inc_id = inc_match.group(1).upper() if inc_match else "INC-1042"
            new_status = "RESOLVED" if "resolve" in lower_msg else "MITIGATED"
            self._dispatch_guarded_tool(
                "update_oracle_incident",
                {
                    "incident_id": inc_id,
                    "new_status": new_status,
                    "resolution_note": f"Updated via AI Foundry Agent per request: {user_message}",
                },
                traces,
                pending_list,
            )
            if pending_list:
                return AgentTurnResponse(
                    reply_markdown=(
                        f"⏸️ **Oracle DB Write Paused for Human-in-the-Loop Approval**\n\n"
                        f"I validated the parameterized update for `{inc_id}` -> `{new_status}`. "
                        f"Please approve the action below to commit the transaction to Oracle DB."
                    ),
                    tool_traces=traces,
                    pending_approvals=pending_list,
                )

        # --------------------------------------------------------------------
        # Case C2: User reports a problem -> feedback log
        # --------------------------------------------------------------------
        if PROBLEM_REPORT_PATTERN.search(user_message):
            outcome = self._dispatch_guarded_tool(
                "log_feedback",
                {
                    "kind": "problem_report",
                    "user_message": user_message,
                    "summary": user_message[:120],
                    "details": "",
                },
                traces,
                pending_list,
            )
            return AgentTurnResponse(
                reply_markdown=(
                    f"📝 **Problem logged** as `{outcome['entry_id']}` in the team's feedback log "
                    f"({self.feedback_log.location}) for review."
                    if outcome.get("success")
                    else f"⚠️ I couldn't log this problem. {outcome['message']}"
                ),
                tool_traces=traces,
                pending_approvals=pending_list,
            )

        # --------------------------------------------------------------------
        # Case D: User asks to query Oracle DB (Incidents, Departments, Employees)
        # --------------------------------------------------------------------
        db_sections: List[str] = []
        if any(
            w in lower_msg
            for w in [
                "oracle",
                "database",
                "db",
                "incident",
                "open",
                "p1",
                "p2",
                "department",
                "employee",
                "on-call",
                "oncall",
                "who owns",
            ]
        ):
            if "employee" in lower_msg or "staff" in lower_msg:
                sql = (
                    "SELECT E.EMP_ID, E.FULL_NAME, E.ROLE_TITLE, E.STATUS, D.DEPT_NAME, D.LOCATION "
                    "FROM EMPLOYEES E JOIN DEPARTMENTS D ON E.DEPT_ID = D.DEPT_ID "
                    "ORDER BY E.EMP_ID"
                )
                reason = "Query active/inactive employees and their department details (excluding restricted SSN column)"
            else:
                sql = (
                    "SELECT I.INCIDENT_ID, I.SERVICE_NAME, I.ORACLE_ERROR_CODE, I.SEVERITY, "
                    "I.STATUS, D.DEPT_NAME, D.ONCALL_LEAD, I.SUMMARY "
                    "FROM SYSTEM_INCIDENTS I "
                    "JOIN DEPARTMENTS D ON I.DEPT_ID = D.DEPT_ID "
                    "ORDER BY I.SEVERITY, I.INCIDENT_ID"
                )
                reason = "Join SYSTEM_INCIDENTS and DEPARTMENTS to inspect active Oracle DB incidents and on-call owners"

            outcome = self._dispatch_guarded_tool(
                "query_oracle_db",
                {"sql_query": sql, "reason": reason},
                traces,
                pending_list,
            )
            rows = outcome.get("rows", [])
            if rows:
                headers = list(rows[0].keys())
                md_table = "| " + " | ".join(headers) + " |\n"
                md_table += "| " + " | ".join(["---"] * len(headers)) + " |\n"
                for r in rows:
                    md_table += "| " + " | ".join(str(r.get(h, "")) for h in headers) + " |\n"
                db_sections.append(
                    f"### 🗄️ Oracle Database Query Results (`{ len(rows) }` rows)\n"
                    f"**Executed Guarded SQL:**\n```sql\n{sql}\n```\n\n{md_table}"
                )

        # --------------------------------------------------------------------
        # Case E: Knowledge Base Retrieval (File Search / RAG)
        # --------------------------------------------------------------------
        kb_hits = [h for h in self.kb_tool.search_kb(user_message) if float(h["score"]) >= DEMO_KB_MIN_SCORE]
        traces.append(
            {
                "tool": "file_search (Knowledge Base Vector Store)",
                "arguments": {"query": user_message},
                "guardrail_status": "PASSED",
                "rule": "READ_ONLY_KB_GROUNDING",
                "message": f"Retrieved {len(kb_hits)} relevant document(s) from Knowledge Base.",
            }
        )

        kb_summary_parts: List[str] = []
        for hit in kb_hits[:2]:
            kb_summary_parts.append(
                f"#### 📄 Source: `{hit['filename']}`\n"
                + self._extract_relevant_excerpt(hit["content"], user_message)
            )

        reply_blocks: List[str] = []
        if db_sections:
            reply_blocks.extend(db_sections)
        if kb_summary_parts:
            reply_blocks.append(
                "### 📚 Knowledge Base Grounding (Azure AI Foundry File Search)\n\n"
                + "\n\n".join(kb_summary_parts)
            )

        if not reply_blocks:
            outcome = self._dispatch_guarded_tool(
                "log_feedback",
                {
                    "kind": "unanswered_question",
                    "user_message": user_message,
                    "summary": user_message[:120],
                    "details": "Local keyword search found no matching Knowledge Base section.",
                },
                traces,
                pending_list,
            )
            logged = (
                f"so I logged the question as `{outcome['entry_id']}` for the team to review"
                if outcome.get("success")
                else f"and couldn't log the question ({outcome['message']})"
            )
            reply_blocks.append(
                f"I couldn't find this in the Knowledge Base, {logged}. Ask me about our **Oracle DB incidents**, "
                "**P1/P2 SLAs**, **ORA error troubleshooting**, or ask me to **update a Knowledge Base file**!"
            )

        return AgentTurnResponse(
            reply_markdown="\n\n---\n\n".join(reply_blocks),
            tool_traces=traces,
            pending_approvals=pending_list,
        )

    @staticmethod
    def _extract_relevant_excerpt(markdown_text: str, query: str) -> str:
        """Extracts the most relevant sections from a Markdown document based on query keywords."""
        sections = re.split(r"\n(?=##+ )", markdown_text)
        q_words = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
        scored = []
        for sec in sections:
            score = sum(2 if w in sec.lower().splitlines()[0] else 1 for w in q_words if w in sec.lower())
            scored.append((score, sec.strip()))
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen = [s for sc, s in scored[:2] if sc > 0]
        if not chosen:
            chosen = [s.strip() for s in sections[:2]]
        return "\n\n".join(chosen)
