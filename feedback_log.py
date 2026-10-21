"""
Feedback log: questions the agent couldn't answer from the Knowledge Base, and problems users report.

Two stores share one entry format:
- `FeedbackLog`: `feedback_log.jsonl` in the project folder (one JSON object per line). Used when the
  app itself runs the `log_feedback` tool.
- `TableFeedbackLog`: an Azure Table. The `azure_function/` app writes here when Foundry calls
  `log_feedback` as an OpenAPI tool, which also works in the Foundry portal playground; the Streamlit
  app reads the same table. Set FEEDBACK_TABLE_ENDPOINT to use it (`deploy_feedback_function.sh` does).

The log is append-only and nothing the agent reads comes from it, so writing an entry needs no
Human-in-the-Loop approval. SSNs, API keys and passwords are masked before anything is written.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from guardrails import PII_AND_SECRET_PATTERNS

FEEDBACK_LOG_FILE = Path(__file__).parent / "feedback_log.jsonl"
FEEDBACK_KINDS = ("unanswered_question", "problem_report")
MAX_FIELD_CHARS = 4000  # Azure Table string properties max out at 32K characters


def mask_sensitive(text: str) -> Tuple[str, List[str]]:
    """Replaces anything matching the PII/secret patterns; returns the text and what was masked."""
    found = []
    for pattern, label in PII_AND_SECRET_PATTERNS:
        text, count = re.subn(pattern, f"[REDACTED: {label}]", text)
        if count:
            found.append(label)
    return text, found


def build_entry(kind: str, user_message: str, summary: str, details: str = "", **context: Any) -> Dict[str, Any]:
    """Validates and masks one entry. `context` (agent, conversation ID, ...) is kept when not empty."""
    if kind not in FEEDBACK_KINDS:
        raise ValueError(f"kind must be one of {', '.join(FEEDBACK_KINDS)}")
    entry: Dict[str, Any] = {
        "id": f"fb-{uuid.uuid4().hex[:8]}",
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
    }
    masked: List[str] = []
    for name, value in (("user_message", user_message), ("summary", summary), ("details", details)):
        entry[name], found = mask_sensitive(str(value or "").strip()[:MAX_FIELD_CHARS])
        masked += found
    entry.update({key: value for key, value in context.items() if value})
    if masked:
        entry["masked"] = sorted(set(masked))
    return entry


class FeedbackLog:
    """Appends feedback entries to a JSON Lines file and reads them back."""

    def __init__(self, path: Path = FEEDBACK_LOG_FILE) -> None:
        self.path = Path(path)
        self.location = self.path.name

    def record(
        self, kind: str, user_message: str, summary: str, details: str = "", **context: Any
    ) -> Dict[str, Any]:
        entry = build_entry(kind, user_message, summary, details, **context)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def read_entries(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Returns entries newest first, skipping any line that isn't valid JSON."""
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries[:limit] if limit else entries

    def delete(self, entry_ids: Iterable[str]) -> int:
        """Removes entries by ID (used by kb_eval.py to clean up after itself); returns how many."""
        ids = set(entry_ids)
        if not ids or not self.path.exists():
            return 0
        kept, removed = [], 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                drop = json.loads(line).get("id") in ids
            except json.JSONDecodeError:
                drop = False
            removed += drop
            if not drop:
                kept.append(line)
        self.path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        return removed


class TableFeedbackLog:
    """Stores feedback entries in an Azure Table (PartitionKey = kind, RowKey = entry ID).

    Signs in with DefaultAzureCredential: the Function App's managed identity in Azure, or your
    `az login` locally. Both need the Storage Table Data Contributor role on the storage account.
    """

    def __init__(self, endpoint: str, table_name: str = "feedback", credential: Any = None) -> None:
        from azure.data.tables import TableServiceClient
        from azure.identity import DefaultAzureCredential

        service = TableServiceClient(endpoint=endpoint, credential=credential or DefaultAzureCredential())
        self.table = service.get_table_client(table_name)
        self.location = f"Azure Table '{table_name}' ({service.account_name})"

    def record(
        self, kind: str, user_message: str, summary: str, details: str = "", **context: Any
    ) -> Dict[str, Any]:
        entry = build_entry(kind, user_message, summary, details, **context)
        row = {k: v for k, v in entry.items() if k not in ("id", "kind", "masked")}
        if entry.get("masked"):
            row["masked"] = ", ".join(entry["masked"])  # tables can't store lists
        self.table.create_entity({"PartitionKey": kind, "RowKey": entry["id"], **row})
        return entry

    def read_entries(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        entries = []
        for row in self.table.list_entities():
            entry = {"id": row["RowKey"], "kind": row["PartitionKey"]}
            entry.update({k: v for k, v in row.items() if k not in ("PartitionKey", "RowKey")})
            if entry.get("masked"):
                entry["masked"] = entry["masked"].split(", ")
            entries.append(entry)
        entries.sort(key=lambda e: e.get("time", ""), reverse=True)
        return entries[:limit] if limit else entries

    def delete(self, entry_ids: Iterable[str]) -> int:
        removed = 0
        for entry_id in set(entry_ids):
            for row in self.table.query_entities("RowKey eq @id", parameters={"id": entry_id}):
                self.table.delete_entity(row["PartitionKey"], row["RowKey"])
                removed += 1
        return removed


def open_feedback_log() -> Any:
    """The Azure Table log when FEEDBACK_TABLE_ENDPOINT is set, otherwise feedback_log.jsonl."""
    endpoint = os.getenv("FEEDBACK_TABLE_ENDPOINT", "").strip()
    if endpoint:
        return TableFeedbackLog(endpoint, os.getenv("FEEDBACK_TABLE_NAME", "feedback").strip() or "feedback")
    return FeedbackLog()
