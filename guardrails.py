"""
Multi-Layer Guardrail Engine for Azure AI Foundry Agent Actions.

Provides deterministic safety checks and Human-in-the-Loop (HITL) gating for:
1. Oracle Database SQL queries (read-only enforcement, DDL/DML blocking, table/column allowlists)
2. Knowledge Base source file updates (path traversal protection, PII/secret scanning, diff generation)
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ============================================================================
# Policy Constants
# ============================================================================

ALLOWED_ORACLE_TABLES = {"EMPLOYEES", "DEPARTMENTS", "SYSTEM_INCIDENTS"}

BLOCKED_SQL_KEYWORDS = {
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "CREATE",
    "REPLACE",
    "MERGE",
    "EXEC",
    "EXECUTE",
    "CALL",
    "DBMS_",
    "UTL_FILE",
    "UTL_HTTP",
}

RESTRICTED_COLUMNS = {
    "SSN",
    "SOCIAL_SECURITY",
    "PASSWORD",
    "PASSWORD_HASH",
    "BANK_ACCOUNT",
    "API_KEY",
}

PII_AND_SECRET_PATTERNS = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "US Social Security Number (SSN) pattern"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "OpenAI / API Secret Key pattern"),
    (r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{4,}", "Hardcoded password assignment"),
    (r"(?i)AccountKey=[A-Za-z0-9+/=]{20,}", "Azure Storage AccountKey secret"),
]


@dataclass
class GuardrailResult:
    """Result of evaluating an agent tool call against safety guardrails."""

    passed: bool
    requires_hitl: bool
    status: str  # "PASSED", "BLOCKED", "PENDING_HITL"
    action_type: str
    rule_triggered: str
    message: str
    diff_preview: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


class GuardrailEngine:
    """Evaluates SQL queries, DB mutations, and Knowledge Base updates against policy rules."""

    def __init__(
        self,
        enforce_sql_readonly: bool = True,
        enforce_table_allowlist: bool = True,
        enforce_pii_scan: bool = True,
        require_hitl_for_kb_update: bool = True,
        require_hitl_for_db_write: bool = True,
    ) -> None:
        self.enforce_sql_readonly = enforce_sql_readonly
        self.enforce_table_allowlist = enforce_table_allowlist
        self.enforce_pii_scan = enforce_pii_scan
        self.require_hitl_for_kb_update = require_hitl_for_kb_update
        self.require_hitl_for_db_write = require_hitl_for_db_write
        self.audit_log: List[GuardrailResult] = []

    def log_result(self, result: GuardrailResult) -> GuardrailResult:
        self.audit_log.insert(0, result)
        return result

    # ------------------------------------------------------------------------
    # 1. SQL Query Guardrail
    # ------------------------------------------------------------------------
    def validate_sql_query(self, sql_query: str) -> GuardrailResult:
        """Validates an agent-generated SQL query before executing against Oracle DB."""
        cleaned = sql_query.strip()
        if not cleaned:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="query_oracle_db",
                    rule_triggered="EMPTY_QUERY",
                    message="SQL query cannot be empty.",
                )
            )

        # Strip trailing semicolon if present, then check for multi-statement chaining
        normalized = cleaned[:-1].strip() if cleaned.endswith(";") else cleaned
        if ";" in normalized:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="query_oracle_db",
                    rule_triggered="SQL_INJECTION_MULTI_STATEMENT",
                    message="Blocked: Multi-statement SQL execution (';') is prohibited by guardrail policy.",
                    metadata={"sql": sql_query},
                )
            )

        # Block SQL comments used to bypass filters
        if "--" in normalized or "/*" in normalized:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="query_oracle_db",
                    rule_triggered="SQL_COMMENT_DISALLOWED",
                    message="Blocked: Inline SQL comments ('--' or '/*') are disallowed in agent queries.",
                    metadata={"sql": sql_query},
                )
            )

        upper_sql = normalized.upper()
        tokens = set(re.findall(r"\b[A-Z0-9_]+\b", upper_sql))

        # Check read-only requirement and destructive keywords
        if self.enforce_sql_readonly:
            blocked_found = sorted(tokens.intersection(BLOCKED_SQL_KEYWORDS))
            # Also check prefix keywords like DBMS_*
            for tok in tokens:
                if tok.startswith("DBMS_") or tok.startswith("UTL_"):
                    blocked_found.append(tok)

            if blocked_found:
                return self.log_result(
                    GuardrailResult(
                        passed=False,
                        requires_hitl=False,
                        status="BLOCKED",
                        action_type="query_oracle_db",
                        rule_triggered=f"DESTRUCTIVE_SQL_KEYWORD ({', '.join(blocked_found)})",
                        message=(
                            f"Blocked by SQL Read-Only Guardrail: Prohibited keyword(s) "
                            f"{blocked_found} detected. Direct SQL queries must be read-only SELECT statements."
                        ),
                        metadata={"sql": sql_query, "blocked_keywords": blocked_found},
                    )
                )

            if not (upper_sql.startswith("SELECT") or upper_sql.startswith("WITH")):
                return self.log_result(
                    GuardrailResult(
                        passed=False,
                        requires_hitl=False,
                        status="BLOCKED",
                        action_type="query_oracle_db",
                        rule_triggered="NON_SELECT_STATEMENT",
                        message="Blocked: Only SELECT or WITH (CTE) read queries are permitted.",
                        metadata={"sql": sql_query},
                    )
                )

        # Check restricted sensitive columns
        restricted_cols_found = sorted(tokens.intersection(RESTRICTED_COLUMNS))
        if restricted_cols_found:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="query_oracle_db",
                    rule_triggered=f"RESTRICTED_PII_COLUMN ({', '.join(restricted_cols_found)})",
                    message=(
                        f"Blocked by Data Governance Guardrail: Query attempts to access restricted "
                        f"column(s): {restricted_cols_found}."
                    ),
                    metadata={"sql": sql_query},
                )
            )

        # Check table allowlist (extract tables after FROM or JOIN)
        if self.enforce_table_allowlist:
            referenced_tables = re.findall(
                r"\b(?:FROM|JOIN)\s+([A-Z0-9_]+)\b", upper_sql
            )
            cte_aliases = set(
                re.findall(r"\b([A-Z0-9_]+)\s+AS\s*\(", upper_sql)
            )
            unauthorized_tables = [
                t
                for t in referenced_tables
                if t not in ALLOWED_ORACLE_TABLES
                and t not in cte_aliases
                and t != "DUAL"
            ]
            if unauthorized_tables:
                return self.log_result(
                    GuardrailResult(
                        passed=False,
                        requires_hitl=False,
                        status="BLOCKED",
                        action_type="query_oracle_db",
                        rule_triggered=f"UNAUTHORIZED_TABLE ({', '.join(unauthorized_tables)})",
                        message=(
                            f"Blocked by Table Allowlist Guardrail: Table(s) {unauthorized_tables} "
                            f"are not in the approved list {sorted(ALLOWED_ORACLE_TABLES)}."
                        ),
                        metadata={"sql": sql_query},
                    )
                )

        return self.log_result(
            GuardrailResult(
                passed=True,
                requires_hitl=False,
                status="PASSED",
                action_type="query_oracle_db",
                rule_triggered="SQL_SAFE_SELECT",
                message="SQL query passed read-only, column-privacy, and table-allowlist checks.",
                metadata={"sql": sql_query},
            )
        )

    # ------------------------------------------------------------------------
    # 2. Parameterized Oracle DB Write Guardrail (Human-in-the-Loop)
    # ------------------------------------------------------------------------
    def validate_db_incident_update(
        self, incident_id: str, new_status: str, resolution_note: str
    ) -> GuardrailResult:
        """Validates a structured incident update before touching Oracle DB."""
        allowed_statuses = {"OPEN", "INVESTIGATING", "MITIGATED", "RESOLVED", "CLOSED"}
        clean_status = new_status.strip().upper()

        if clean_status not in allowed_statuses:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="update_oracle_incident",
                    rule_triggered="INVALID_STATUS_VALUE",
                    message=f"Blocked: Status '{new_status}' is not in {sorted(allowed_statuses)}.",
                )
            )

        if not re.match(r"^INC-\d{4,6}$", incident_id.strip().upper()):
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="update_oracle_incident",
                    rule_triggered="INVALID_INCIDENT_ID",
                    message=f"Blocked: Incident ID '{incident_id}' does not match format INC-XXXX.",
                )
            )

        if self.require_hitl_for_db_write:
            return self.log_result(
                GuardrailResult(
                    passed=True,
                    requires_hitl=True,
                    status="PENDING_HITL",
                    action_type="update_oracle_incident",
                    rule_triggered="HITL_REQUIRED_DB_MUTATION",
                    message=(
                        f"Guardrail Check Passed — Awaiting Human Approval to update "
                        f"Oracle DB incident {incident_id} -> {clean_status}."
                    ),
                    metadata={
                        "incident_id": incident_id.strip().upper(),
                        "new_status": clean_status,
                        "resolution_note": resolution_note,
                    },
                )
            )

        return self.log_result(
            GuardrailResult(
                passed=True,
                requires_hitl=False,
                status="PASSED",
                action_type="update_oracle_incident",
                rule_triggered="DB_MUTATION_ALLOWED",
                message=f"Incident {incident_id} update validated.",
                metadata={
                    "incident_id": incident_id.strip().upper(),
                    "new_status": clean_status,
                    "resolution_note": resolution_note,
                },
            )
        )

    # ------------------------------------------------------------------------
    # 3. Knowledge Base File Update Guardrail
    # ------------------------------------------------------------------------
    def validate_kb_update(
        self,
        filename: str,
        current_content: str,
        proposed_content: str,
        change_summary: str,
    ) -> GuardrailResult:
        """
        Validates a proposed Knowledge Base source file update:
        - Path traversal & extension check
        - PII / secret leak scan
        - Content deletion ratio check
        - Unified diff generation & Human-in-the-Loop gating
        """
        clean_name = filename.strip()

        # 1. Path traversal / extension check
        if (
            "/" in clean_name
            or "\\" in clean_name
            or ".." in clean_name
            or not clean_name.endswith(".md")
        ):
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="update_kb_file",
                    rule_triggered="PATH_TRAVERSAL_OR_INVALID_EXT",
                    message=(
                        f"Blocked by File Path Guardrail: '{filename}' must be a valid '.md' "
                        f"filename inside the knowledge_base directory without path separators."
                    ),
                    metadata={"filename": filename},
                )
            )

        # 2. Minimum content & accidental wipe-out check
        if len(proposed_content.strip()) < 50:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="update_kb_file",
                    rule_triggered="CONTENT_TOO_SHORT_OR_EMPTY",
                    message="Blocked: Proposed KB content is too short (< 50 characters) and may wipe the document.",
                    metadata={"filename": clean_name},
                )
            )

        if current_content and len(proposed_content) < len(current_content) * 0.5:
            return self.log_result(
                GuardrailResult(
                    passed=False,
                    requires_hitl=False,
                    status="BLOCKED",
                    action_type="update_kb_file",
                    rule_triggered="EXCESSIVE_CONTENT_DELETION",
                    message=(
                        "Blocked by Content Integrity Guardrail: Proposed update removes more than 50% "
                        "of the existing Knowledge Base document."
                    ),
                    metadata={"filename": clean_name},
                )
            )

        # 3. PII & Secret Scanning
        if self.enforce_pii_scan:
            for pattern, label in PII_AND_SECRET_PATTERNS:
                if re.search(pattern, proposed_content):
                    return self.log_result(
                        GuardrailResult(
                            passed=False,
                            requires_hitl=False,
                            status="BLOCKED",
                            action_type="update_kb_file",
                            rule_triggered=f"PII_OR_SECRET_DETECTED ({label})",
                            message=(
                                f"Blocked by Content Safety Guardrail: Detected {label} in proposed "
                                f"Knowledge Base update for '{clean_name}'."
                            ),
                            metadata={"filename": clean_name, "violation": label},
                        )
                    )

        # 4. Compute Unified Diff for Human Review
        diff_lines = list(
            difflib.unified_diff(
                current_content.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=f"a/knowledge_base/{clean_name}",
                tofile=f"b/knowledge_base/{clean_name}",
                lineterm="",
            )
        )
        diff_text = "".join(diff_lines) if diff_lines else "(No line differences detected)"

        if self.require_hitl_for_kb_update:
            return self.log_result(
                GuardrailResult(
                    passed=True,
                    requires_hitl=True,
                    status="PENDING_HITL",
                    action_type="update_kb_file",
                    rule_triggered="HITL_REQUIRED_KB_UPDATE",
                    message=(
                        f"KB Content Guardrails Passed (Path OK, PII Scan Clean, Integrity OK) — "
                        f"Awaiting Human Approval to update '{clean_name}' and re-index Vector Store."
                    ),
                    diff_preview=diff_text,
                    metadata={
                        "filename": clean_name,
                        "change_summary": change_summary,
                        "proposed_content": proposed_content,
                    },
                )
            )

        return self.log_result(
            GuardrailResult(
                passed=True,
                requires_hitl=False,
                status="PASSED",
                action_type="update_kb_file",
                rule_triggered="KB_UPDATE_SAFE",
                message=f"KB file '{clean_name}' passed all content guardrails.",
                diff_preview=diff_text,
                metadata={
                    "filename": clean_name,
                    "change_summary": change_summary,
                    "proposed_content": proposed_content,
                },
            )
        )
