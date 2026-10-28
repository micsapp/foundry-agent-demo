"""
The public chat page's backend: anyone can chat with PUBLIC_CHAT_AGENT without signing in.
PUBLIC_CHAT_AGENT_VERSION optionally pins an immutable agent version.

The function calls Foundry as its own managed identity (Foundry User role), so every visitor's message is billed
to this subscription. Limits, each an app setting:
- CHAT_PER_VISITOR_PER_HOUR (default 20): requests per visitor per hour. A visitor is the client IP, stored only
  as a hash salted with CHAT_VISITOR_SALT.
- CHAT_DAILY_LIMIT (default 100): agent replies per UTC day for all visitors together.
- CHAT_MAX_MESSAGE_CHARS (default 1000): longest message accepted.
The counters live in the Azure Table "publicchat" next to the feedback table.

Write tools pause for a visitor's explicit approval. Decisions are accepted only for the
server-recorded pending response in that visitor's conversation, once per response.
An anonymous browser token binds conversations to a session; the salted IP is used only
for rate limits. Neither agent selection nor tool arguments come from the browser.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

CONVERSATION_SOURCE = {"source": "public-chat"}
CONVERSATION_ID = re.compile(r"^conv_[A-Za-z0-9]{8,128}$")
SESSION_TOKEN = re.compile(r"^[a-f0-9]{64}$")
LIMITS_TABLE = "publicchat"
TOOL_LABELS = {
    "query_oracle_db": "database", "log_feedback": "feedback",
    "list_skills": "skills", "load_skill": "skills", "read_skill_resource": "skills",
    "run_skill_script": "incident report",
    "update_oracle_incident": "incident update", "propose_kb_file_update": "knowledge base update",
}  # file_search_call -> knowledge_base


def _setting(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def client_ip(headers: Dict[str, str]) -> str:
    """The caller's address as the platform's front end adds it: the last X-Forwarded-For entry, port removed.
    Earlier entries come from the request itself, so a client could forge them to dodge its hourly limit."""
    forwarded = (headers.get("x-forwarded-for") or "").split(",")[-1].strip()
    if forwarded.startswith("["):  # [IPv6]:port
        return forwarded[1:].split("]")[0]
    if forwarded.count(":") == 1:  # IPv4:port
        return forwarded.split(":")[0]
    return forwarded or "unknown"


class RateLimiter:
    """Counters in an Azure Table, updated with ETag checks so concurrent instances don't lose counts."""

    def __init__(self, table: Any) -> None:
        self.table = table

    def take(self, partition: str, row: str, limit: int) -> bool:
        """Counts one use of (partition, row); False, without counting, once `limit` uses are recorded."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        for _ in range(8):
            try:
                entity = self.table.get_entity(partition, row)
            except ResourceNotFoundError:
                try:
                    self.table.create_entity({"PartitionKey": partition, "RowKey": row, "count": 1})
                    return True
                except ResourceExistsError:
                    continue  # another request created it first
            if entity["count"] >= limit:
                return False
            entity["count"] += 1
            try:
                self.table.update_entity(
                    entity, mode=UpdateMode.REPLACE, etag=entity.metadata["etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
                return True
            except ResourceModifiedError:
                continue  # changed meanwhile; read it again
        return False  # heavy contention: refuse rather than count wrongly

    def claim_approval(self, conversation_id: str, response_id: str) -> bool:
        """Atomically prevent the same approval round being submitted twice across instances."""
        from azure.core.exceptions import ResourceExistsError

        try:
            self.table.create_entity({
                "PartitionKey": f"approval-{conversation_id}", "RowKey": response_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            })
            return True
        except ResourceExistsError:
            return False


class ChatRequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class PublicChat:
    """Handles one POST from the page. Takes the clients as factories so tests can pass fakes."""

    def __init__(self, openai_client: Callable[[], Any], limiter: Callable[[], RateLimiter]) -> None:
        self._openai_factory = openai_client
        self._limiter_factory = limiter
        self._openai: Optional[Any] = None
        self._limiter: Optional[RateLimiter] = None

    @property
    def openai(self) -> Any:
        if self._openai is None:
            self._openai = self._openai_factory()
        return self._openai

    @property
    def limiter(self) -> RateLimiter:
        if self._limiter is None:
            self._limiter = self._limiter_factory()
        return self._limiter

    def handle(self, body: bytes, headers: Dict[str, str], now: Optional[datetime] = None) -> Tuple[int, Dict[str, Any]]:
        """Returns (HTTP status, JSON body) for one message."""
        agent = os.environ.get("PUBLIC_CHAT_AGENT", "").strip()
        if not agent:
            return 503, {"error": "The public chat isn't set up yet (PUBLIC_CHAT_AGENT is not set)."}
        now = now or datetime.now(timezone.utc)
        salt = os.environ.get("CHAT_VISITOR_SALT", "")
        visitor = hashlib.sha256(f"{salt}|{client_ip(headers)}".encode()).hexdigest()[:32]

        # Every request counts toward the visitor's limit, valid or not
        if not self.limiter.take(f"visitor-{now:%Y%m%d%H}", visitor, _setting("CHAT_PER_VISITOR_PER_HOUR", 20)):
            return 429, {"error": "You've reached this demo's hourly message limit. Please try again later."}

        try:
            data = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return 400, {"error": "Send JSON with a message."}
        message = data.get("message")
        decisions = data.get("approvals")
        is_decision = "approvals" in data
        if is_decision:
            if not isinstance(decisions, list) or not decisions or len(decisions) > 20 or "message" in data:
                return 400, {"error": "Send approval decisions for the pending actions."}
            if any(not isinstance(d, dict) or not isinstance(d.get("approval_request_id"), str)
                   or type(d.get("approve")) is not bool for d in decisions):
                return 400, {"error": "Each decision needs an approval_request_id and a boolean approve value."}
        else:
            max_chars = _setting("CHAT_MAX_MESSAGE_CHARS", 1000)
            if not isinstance(message, str) or not message.strip():
                return 400, {"error": "Type a message first."}
            if len(message) > max_chars:
                return 400, {"error": f"Please keep messages under {max_chars} characters."}

        token = data.get("session_token")
        if not isinstance(token, str) or SESSION_TOKEN.fullmatch(token) is None:
            return 400, {"error": "Please reload the page to start a chat session."}
        owner = hashlib.sha256(f"{salt}|session|{token}".encode()).hexdigest()
        version = os.environ.get("PUBLIC_CHAT_AGENT_VERSION", "").strip()

        try:
            conversation_id, new_conversation, metadata = self._conversation(
                data.get("conversation_id"), owner, agent, version, is_decision,
            )
            pending_response = metadata.get("pending_response", "")
            if is_decision:
                if not pending_response:
                    raise ChatRequestError(409, "There are no pending actions. This decision may already have been submitted.")
                previous = self.openai.responses.retrieve(pending_response)
                pending_ids = {item.id for item in previous.output if item.type == "mcp_approval_request"}
                decision_ids = [d["approval_request_id"] for d in decisions]
                if not pending_ids or len(decision_ids) != len(pending_ids) or set(decision_ids) != pending_ids:
                    raise ChatRequestError(400, "Review and decide on every pending action before submitting.")
                items = [
                    {"type": "mcp_approval_response", "approval_request_id": d["approval_request_id"], "approve": d["approve"]}
                    for d in decisions
                ]
            elif pending_response:
                # Recover pending cards after a reload/network failure without sending another model request.
                response = self.openai.responses.retrieve(pending_response)
                return 200, self._payload(response, conversation_id, new_conversation, agent, version)
            else:
                items = [{"type": "message", "role": "user", "content": message.strip()}]

            if not self.limiter.take(f"day-{now:%Y%m%d}", "all", _setting("CHAT_DAILY_LIMIT", 100)):
                return 429, {"error": "This demo has reached its message limit for today. Please try again tomorrow."}
            if is_decision and not self.limiter.claim_approval(conversation_id, pending_response):
                raise ChatRequestError(409, "These decisions have already been submitted. Start a new chat if the reply was lost.")
            response = self._run_turn(agent, version, conversation_id, items)
            metadata["pending_response"] = response.id if any(
                item.type == "mcp_approval_request" for item in response.output
            ) else ""
            self.openai.conversations.update(conversation_id, metadata=metadata)
            payload = self._payload(response, conversation_id, new_conversation, agent, version)
        except ChatRequestError as exc:
            return exc.status, {"error": str(exc)}
        except Exception as exc:
            logging.exception("public chat turn failed")
            if getattr(exc, "status_code", None) == 429:
                return 503, {"error": "The assistant is busy right now. Please try again in a minute."}
            return 502, {"error": "The assistant couldn't answer just now. Please try again."}
        logging.info(
            "public chat: visitor %s, conversation %s, tools %s, tokens %s",
            visitor[:8], conversation_id[-8:], payload["tools"] or "-", response.usage,
        )
        return 200, payload

    def _conversation(
        self, requested: Any, owner: str, agent: str, version: str, must_exist: bool,
    ) -> Tuple[str, bool, Dict[str, str]]:
        """Continue only this browser's conversation with this agent and version."""
        expected = {**CONVERSATION_SOURCE, "owner": owner, "agent": agent, "agent_version": version}
        if isinstance(requested, str) and CONVERSATION_ID.fullmatch(requested):
            try:
                conversation = self.openai.conversations.retrieve(requested)
                metadata = conversation.metadata or {}
                if all(metadata.get(key) == value for key, value in expected.items()):
                    return conversation.id, False, dict(metadata)
            except Exception as exc:  # gone, or not reachable as this identity
                logging.info("public chat: not continuing %s: %s", requested[-8:], type(exc).__name__)
        if must_exist:
            raise ChatRequestError(403, "This approval doesn't belong to this chat session. Start a new chat.")
        return self.openai.conversations.create(metadata=expected).id, bool(requested), expected

    def _run_turn(self, agent: str, version: str, conversation_id: str, items: List[Dict[str, Any]]) -> Any:
        reference = {"name": agent, "type": "agent_reference"}
        if version:
            reference["version"] = version
        return self.openai.responses.create(
            conversation=conversation_id,
            input=items,
            extra_body={"agent_reference": reference},
        )

    @staticmethod
    def _payload(response: Any, conversation_id: str, new_conversation: bool, agent: str, version: str) -> Dict[str, Any]:
        tools: List[str] = []
        approvals = []
        for item in response.output:
            label = "knowledge_base" if item.type == "file_search_call" else (
                TOOL_LABELS.get(getattr(item, "name", "")) if item.type == "mcp_call" else None
            )
            if label and label not in tools:
                tools.append(label)
            if item.type == "mcp_approval_request":
                try:
                    arguments = json.loads(item.arguments)
                except (ValueError, TypeError):
                    arguments = item.arguments
                approvals.append({"id": item.id, "tool": item.name, "arguments": arguments})
        return {
            "conversation_id": conversation_id, "new_conversation": new_conversation,
            "agent": {"name": agent, "version": version or "latest"},
            "reply": response.output_text or (
                "Review the proposed changes below. They will run only if you approve them."
                if approvals else "Sorry, I don't have an answer for that."
            ),
            "tools": tools, "approvals": approvals,
        }


def default_chat() -> PublicChat:
    """The chat as deployed: Foundry and the counters table reached with the Function App's identity."""

    def openai_client() -> Any:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential

        project = AIProjectClient(endpoint=os.environ["PROJECT_ENDPOINT"], credential=DefaultAzureCredential())
        # Stay inside the platform's 230-second HTTP limit
        return project.get_openai_client(timeout=150, max_retries=2)

    def limiter() -> RateLimiter:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        service = TableServiceClient(os.environ["FEEDBACK_TABLE_ENDPOINT"], credential=DefaultAzureCredential())
        return RateLimiter(service.create_table_if_not_exists(LIMITS_TABLE))

    return PublicChat(openai_client, limiter)
