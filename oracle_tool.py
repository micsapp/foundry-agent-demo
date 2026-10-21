"""
Oracle Database Connector & Action Tool for Azure AI Foundry Agent.

Supports:
1. Live Oracle Database via `python-oracledb` (Thin mode, no Instant Client required)
2. Built-in Oracle-compatible SQLite mock database pre-seeded with enterprise tables
   (`SYSTEM_INCIDENTS`, `DEPARTMENTS`, `EMPLOYEES`) for instant local demos.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from guardrails import GuardrailEngine, GuardrailResult

DB_FILE_PATH = Path(__file__).parent / "demo_oracle_mock.db"


class OracleDatabaseTool:
    """Provides guarded read/write operations against Oracle Database (or local Oracle-compatible mock)."""

    def __init__(
        self,
        guardrails: GuardrailEngine,
        db_mode: str = "sqlite_mock",
        oracle_user: Optional[str] = None,
        oracle_password: Optional[str] = None,
        oracle_dsn: Optional[str] = None,
    ) -> None:
        self.guardrails = guardrails
        self.db_mode = db_mode
        self.oracle_user = oracle_user or os.getenv("ORACLE_USER", "system")
        self.oracle_password = oracle_password or os.getenv("ORACLE_PASSWORD", "")
        self.oracle_dsn = oracle_dsn or os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")

        if self.db_mode == "sqlite_mock":
            self._init_mock_oracle_db()

    def _init_mock_oracle_db(self) -> None:
        """Initializes and seeds the local Oracle-compatible demo database if not already populated."""
        conn = sqlite3.connect(DB_FILE_PATH)
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS DEPARTMENTS (
                DEPT_ID TEXT PRIMARY KEY,
                DEPT_NAME TEXT NOT NULL,
                COST_CENTER TEXT NOT NULL,
                ONCALL_LEAD TEXT NOT NULL,
                LOCATION TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS EMPLOYEES (
                EMP_ID TEXT PRIMARY KEY,
                FULL_NAME TEXT NOT NULL,
                DEPT_ID TEXT NOT NULL,
                ROLE_TITLE TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                SSN TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS SYSTEM_INCIDENTS (
                INCIDENT_ID TEXT PRIMARY KEY,
                SERVICE_NAME TEXT NOT NULL,
                ORACLE_ERROR_CODE TEXT,
                SEVERITY TEXT NOT NULL,
                STATUS TEXT NOT NULL,
                DEPT_ID TEXT NOT NULL,
                SUMMARY TEXT NOT NULL,
                RESOLUTION_NOTE TEXT,
                REPORTED_AT TEXT NOT NULL
            )
            """
        )

        cur.execute("SELECT COUNT(*) FROM DEPARTMENTS")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO DEPARTMENTS VALUES (?, ?, ?, ?, ?)",
                [
                    ("DEPT-DBA", "Database Reliability Engineering", "CC-101", "Alice Chen", "New York"),
                    ("DEPT-PAY", "Core Payment Processing", "CC-204", "Marcus Vance", "Toronto"),
                    ("DEPT-DATA", "Enterprise Data Platform", "CC-309", "Priya Nair", "Seattle"),
                ],
            )

            cur.executemany(
                "INSERT INTO EMPLOYEES VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("EMP-001", "Alice Chen", "DEPT-DBA", "Principal Oracle DBA", "ACTIVE", "999-01-0001"),
                    ("EMP-002", "Marcus Vance", "DEPT-PAY", "Staff Payment Engineer", "ACTIVE", "999-01-0002"),
                    ("EMP-003", "Priya Nair", "DEPT-DATA", "Data Platform Lead", "ACTIVE", "999-01-0003"),
                    ("EMP-004", "David Kim", "DEPT-DBA", "Database SRE", "INACTIVE", "999-01-0004"),
                ],
            )

            cur.executemany(
                "INSERT INTO SYSTEM_INCIDENTS VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        "INC-1041",
                        "PROD-ORA-EAST",
                        "ORA-00060",
                        "P1",
                        "OPEN",
                        "DEPT-PAY",
                        "Row-level deadlock on PAYMENT_LEDGER table during settlement batch",
                        "",
                        "2026-10-18 21:15:00",
                    ),
                    (
                        "INC-1042",
                        "PROD-ORA-WEST",
                        "ORA-12514",
                        "P2",
                        "INVESTIGATING",
                        "DEPT-DBA",
                        "TNS:listener does not currently know of service requested in connect descriptor",
                        "Listener service registration check in progress",
                        "2026-10-18 22:30:00",
                    ),
                    (
                        "INC-1039",
                        "ANALYTICS-ORA-RPT",
                        "ORA-01555",
                        "P3",
                        "RESOLVED",
                        "DEPT-DATA",
                        "Snapshot too old on nightly warehouse ETL job",
                        "Increased UNDO_RETENTION to 7200s per KB-ORA-001",
                        "2026-10-17 04:10:00",
                    ),
                ],
            )
            conn.commit()
        conn.close()

    def reset_demo_db(self) -> None:
        """Resets the demo database back to initial seed state."""
        if DB_FILE_PATH.exists():
            DB_FILE_PATH.unlink()
        self._init_mock_oracle_db()

    def execute_guarded_query(self, sql_query: str) -> Dict[str, Any]:
        """
        Validates the SQL query through GuardrailEngine and executes it if safe.
        Returns a structured dictionary with guardrail status and rows/columns.
        """
        guardrail_check: GuardrailResult = self.guardrails.validate_sql_query(sql_query)
        if not guardrail_check.passed:
            return {
                "success": False,
                "guardrail": guardrail_check,
                "error": guardrail_check.message,
                "rows": [],
                "columns": [],
            }

        try:
            if self.db_mode == "oracle_live":
                import oracledb

                with oracledb.connect(
                    user=self.oracle_user,
                    password=self.oracle_password,
                    dsn=self.oracle_dsn,
                ) as conn:
                    with conn.cursor() as cur:
                        cur.execute(sql_query.rstrip(";"))
                        columns = [col[0] for col in cur.description] if cur.description else []
                        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
            else:
                conn = sqlite3.connect(DB_FILE_PATH)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(sql_query)
                fetched = cur.fetchall()
                columns = list(fetched[0].keys()) if fetched else []
                rows = [dict(r) for r in fetched]
                conn.close()

            return {
                "success": True,
                "guardrail": guardrail_check,
                "rows": rows,
                "columns": columns,
                "row_count": len(rows),
            }
        except Exception as exc:
            return {
                "success": False,
                "guardrail": guardrail_check,
                "error": f"Database execution error: {exc}",
                "rows": [],
                "columns": [],
            }

    def commit_incident_update(
        self, incident_id: str, new_status: str, resolution_note: str
    ) -> Dict[str, Any]:
        """Executes a parameterized UPDATE on SYSTEM_INCIDENTS after HITL approval."""
        clean_id = incident_id.strip().upper()
        clean_status = new_status.strip().upper()
        try:
            if self.db_mode == "oracle_live":
                import oracledb

                with oracledb.connect(
                    user=self.oracle_user,
                    password=self.oracle_password,
                    dsn=self.oracle_dsn,
                ) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE SYSTEM_INCIDENTS
                            SET STATUS = :1, RESOLUTION_NOTE = :2
                            WHERE INCIDENT_ID = :3
                            """,
                            [clean_status, resolution_note, clean_id],
                        )
                    conn.commit()
            else:
                conn = sqlite3.connect(DB_FILE_PATH)
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE SYSTEM_INCIDENTS
                    SET STATUS = ?, RESOLUTION_NOTE = ?
                    WHERE INCIDENT_ID = ?
                    """,
                    (clean_status, resolution_note, clean_id),
                )
                conn.commit()
                conn.close()

            return {
                "success": True,
                "message": f"Oracle DB Incident {clean_id} updated to {clean_status}.",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to update Oracle DB incident: {exc}",
            }
