"""
The public chat page's backend: anyone can chat with the read-only agent PUBLIC_CHAT_AGENT without signing in.

The function calls Foundry as its own managed identity (Foundry User role), so every visitor's message is billed
to this subscription. Limits, each an app setting:
- CHAT_PER_VISITOR_PER_HOUR (default 20): requests per visitor per hour. A visitor is the client IP, stored only
  as a hash salted with CHAT_VISITOR_SALT.
- CHAT_DAILY_LIMIT (default 100): agent replies per UTC day for all visitors together.
- CHAT_MAX_MESSAGE_CHARS (default 1000): longest message accepted.
The counters live in the Azure Table "publicchat" next to the feedback table.

The agent's tools (File Search, query_oracle_db, log_feedback and packaged read-only skills) need no approval; if Foundry asks for one anyway,
it's refused. Conversations made here carry the metadata source=public-chat and only those are continued, so a
visitor can't reach any other conversation in the project.
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
LIMITS_TABLE = "publicchat"
MAX_APPROVAL_ROUNDS = 3  # an approval request shouldn't happen at all; stop refusing after this many
TOOL_LABELS = {
    "query_oracle_db": "database", "log_feedback": "feedback",
    "list_skills": "skills", "load_skill": "skills", "read_skill_resource": "skills",
    "run_skill_script": "incident report",
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
        max_chars = _setting("CHAT_MAX_MESSAGE_CHARS", 1000)
        if not isinstance(message, str) or not message.strip():
            return 400, {"error": "Type a message first."}
        if len(message) > max_chars:
            return 400, {"error": f"Please keep messages under {max_chars} characters."}

        if not self.limiter.take(f"day-{now:%Y%m%d}", "all", _setting("CHAT_DAILY_LIMIT", 100)):
            return 429, {"error": "This demo has reached its message limit for today. Please try again tomorrow."}

        try:
            conversation_id, new_conversation = self._conversation(data.get("conversation_id"))
            reply, tools, usage = self._run_turn(agent, conversation_id, message.strip())
        except Exception as exc:
            logging.exception("public chat turn failed")
            if getattr(exc, "status_code", None) == 429:
                return 503, {"error": "The assistant is busy right now. Please try again in a minute."}
            return 502, {"error": "The assistant couldn't answer just now. Please try again."}
        logging.info(
            "public chat: visitor %s, conversation %s, tools %s, tokens %s",
            visitor[:8], conversation_id[-8:], tools or "-", usage,
        )
        return 200, {
            "conversation_id": conversation_id,
            "new_conversation": new_conversation,
            "reply": reply,
            "tools": tools,
        }

    def _conversation(self, requested: Any) -> Tuple[str, bool]:
        """Continues `requested` if this page created it; otherwise starts a new conversation."""
        if isinstance(requested, str) and CONVERSATION_ID.match(requested):
            try:
                conversation = self.openai.conversations.retrieve(requested)
                if (conversation.metadata or {}).get("source") == CONVERSATION_SOURCE["source"]:
                    return conversation.id, False
            except Exception as exc:  # gone, or not reachable as this identity
                logging.info("public chat: not continuing %s: %s", requested[-8:], type(exc).__name__)
        return self.openai.conversations.create(metadata=CONVERSATION_SOURCE).id, bool(requested)

    def _run_turn(self, agent: str, conversation_id: str, message: str) -> Tuple[str, List[str], Dict[str, int]]:
        agent_ref = {"agent_reference": {"name": agent, "type": "agent_reference"}}
        response = self.openai.responses.create(
            conversation=conversation_id,
            input=[{"type": "message", "role": "user", "content": message}],
            extra_body=agent_ref,
        )
        tools: List[str] = []
        usage = {"input": 0, "output": 0}
        for round_no in range(MAX_APPROVAL_ROUNDS + 1):
            for item in response.output:
                label = "knowledge_base" if item.type == "file_search_call" else (
                    TOOL_LABELS.get(getattr(item, "name", "")) if item.type == "mcp_call" else None
                )
                if label and label not in tools:
                    tools.append(label)
            if response.usage:
                usage["input"] += response.usage.input_tokens or 0
                usage["output"] += response.usage.output_tokens or 0
            refusals = [
                {"type": "mcp_approval_response", "approval_request_id": item.id, "approve": False}
                for item in response.output
                if item.type == "mcp_approval_request"
            ]
            if not refusals or round_no == MAX_APPROVAL_ROUNDS:
                break
            logging.warning("public chat: refused %d approval request(s)", len(refusals))
            response = self.openai.responses.create(conversation=conversation_id, input=refusals, extra_body=agent_ref)
        return response.output_text or "Sorry, I don't have an answer for that.", tools, usage


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
