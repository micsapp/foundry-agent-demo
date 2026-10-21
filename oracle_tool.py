"""
Oracle Database Connector & Action Tool for Azure AI Foundry Agent.

Supports:
1. Live Oracle Database via `python-oracledb` (Thin mode, no Instant Client required)
2. Built-in Oracle-compatible SQLite mock database pre-seeded with enterprise tables
   (`SYSTEM_INCIDENTS`, `DEPARTMENTS`, `EMPLOYEES`) for instant local demos.
3. `azure_blob`: the same SQLite mock kept as a blob in Azure Storage (ORACLE_MOCK_BLOB_URL), so the Azure
   Function that serves the DB tools over MCP and this app see the same data. Reads download the blob only
   when its ETag changed; a write uploads only if nobody else wrote in between, and otherwise starts again
   from the newer copy.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from guardrails import GuardrailEngine, GuardrailResult

DB_FILE_PATH = Path(__file__).parent / "demo_oracle_mock.db"
BLOB_WRITE_ATTEMPTS = 5  # conditional uploads tried before an update gives up


def seed_mock_db(path: Path) -> None:
    """Creates the demo tables in the SQLite file at `path` and seeds them if they're empty."""
    conn = sqlite3.connect(path)
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


def _sqlite_select(path: Path, sql_query: str, read_only: bool = False) -> Tuple[List[str], List[Dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if read_only else sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        fetched = conn.execute(sql_query).fetchall()
    finally:
        conn.close()
    columns = list(fetched[0].keys()) if fetched else []
    return columns, [dict(r) for r in fetched]


def _sqlite_write(path: Path, sql: str, params: Sequence[Any]) -> int:
    conn = sqlite3.connect(path)
    try:
        changed = conn.execute(sql, params).rowcount
        conn.commit()
    finally:
        conn.close()
    return changed


class OracleDatabaseTool:
    """Provides guarded read/write operations against Oracle Database (or local Oracle-compatible mock)."""

    def __init__(
        self,
        guardrails: GuardrailEngine,
        db_mode: str = "sqlite_mock",
        oracle_user: Optional[str] = None,
        oracle_password: Optional[str] = None,
        oracle_dsn: Optional[str] = None,
        blob_url: Optional[str] = None,
    ) -> None:
        self.guardrails = guardrails
        self.db_mode = db_mode
        self.oracle_user = oracle_user or os.getenv("ORACLE_USER", "system")
        self.oracle_password = oracle_password or os.getenv("ORACLE_PASSWORD", "")
        self.oracle_dsn = oracle_dsn or os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
        self.blob_url = (blob_url or os.getenv("ORACLE_MOCK_BLOB_URL", "")).strip()
        self._blob: Optional[Any] = None  # BlobClient, created on first use
        self._blob_etag: Optional[str] = None  # ETag of the local copy of the blob
        self._blob_lock = threading.Lock()  # one download or update of the local copy at a time

        if self.db_mode == "sqlite_mock":
            seed_mock_db(DB_FILE_PATH)

    @property
    def location(self) -> str:
        if self.db_mode == "azure_blob":
            return f"Azure blob {self.blob_url or '(ORACLE_MOCK_BLOB_URL not set)'}"
        if self.db_mode == "oracle_live":
            return f"Oracle {self.oracle_dsn}"
        return DB_FILE_PATH.name

    # ------------------------------------------------------------------------
    # azure_blob mode: a local copy of the SQLite file, kept in step with the blob
    # ------------------------------------------------------------------------
    def _blob_client(self) -> Any:
        if self._blob is None:
            if not self.blob_url:
                raise RuntimeError("ORACLE_MOCK_BLOB_URL is not set; ./deploy_feedback_function.sh writes it to .env.")
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobClient

            # The Function App's managed identity in Azure, or your `az login` locally; both need
            # Storage Blob Data Contributor on the container.
            self._blob = BlobClient.from_blob_url(self.blob_url, credential=DefaultAzureCredential())
        return self._blob

    @property
    def _blob_copy_path(self) -> Path:
        digest = hashlib.sha256(self.blob_url.encode()).hexdigest()[:12]
        return Path(tempfile.gettempdir()) / f"oracle_mock_{digest}.db"

    @staticmethod
    def _seeded_bytes() -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.db"
            seed_mock_db(path)
            return path.read_bytes()

    def _pull_blob(self) -> Path:
        """Refreshes the local copy if the blob changed (seeding the blob on first use) and returns its path."""
        from azure.core import MatchConditions
        from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

        path = self._blob_copy_path
        blob = self._blob_client()
        try:
            if self._blob_etag and path.exists():
                download = blob.download_blob(etag=self._blob_etag, match_condition=MatchConditions.IfModified)
            else:
                download = blob.download_blob()
            data = download.readall()
        except ResourceNotFoundError:
            try:
                blob.upload_blob(self._seeded_bytes(), overwrite=False)
            except ResourceExistsError:
                pass  # someone else seeded it first
            download = blob.download_blob()
            data = download.readall()
        except HttpResponseError as exc:
            if exc.status_code != 304:  # 304 Not Modified (the SDK raises it as a failed condition): copy is current
                raise
            return path
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        self._blob_etag = download.properties.etag
        return path

    def _blob_write(self, sql: str, params: Sequence[Any]) -> int:
        """Runs one write on the latest copy and uploads it unless the blob changed meanwhile; returns rows changed."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError

        for _ in range(BLOB_WRITE_ATTEMPTS):
            path = self._pull_blob()
            changed = _sqlite_write(path, sql, params)
            if not changed:
                return 0
            try:
                result = self._blob_client().upload_blob(
                    path.read_bytes(), overwrite=True, etag=self._blob_etag, match_condition=MatchConditions.IfNotModified
                )
            except Exception as exc:
                self._blob_etag = None  # the local copy holds a change the blob doesn't: download it again
                if isinstance(exc, ResourceModifiedError):
                    continue  # someone else wrote first; apply the change to their version
                raise
            self._blob_etag = result["etag"]
            return changed
        raise RuntimeError("The database blob kept changing during the update; try again.")

    def reset_demo_db(self) -> None:
        """Resets the demo database back to initial seed state."""
        if self.db_mode == "azure_blob":
            with self._blob_lock:
                self._blob_client().upload_blob(self._seeded_bytes(), overwrite=True)
                self._blob_etag = None
            return
        if DB_FILE_PATH.exists():
            DB_FILE_PATH.unlink()
        seed_mock_db(DB_FILE_PATH)

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
            elif self.db_mode == "azure_blob":
                # Read-only connection: a query can't change the shared copy even if a guardrail is switched off
                with self._blob_lock:
                    columns, rows = _sqlite_select(self._pull_blob(), sql_query, read_only=True)
            else:
                columns, rows = _sqlite_select(DB_FILE_PATH, sql_query)

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

    @staticmethod
    def query_tool_output(res: Dict[str, Any]) -> Dict[str, Any]:
        """What the agent gets back from `query_oracle_db` (the app and the Azure Function both use this)."""
        gr: GuardrailResult = res["guardrail"]
        # A query can pass the guardrail and still fail in the database (e.g. a wrong column name);
        # report that error, or the agent reads the empty row list as "no matching rows".
        message = gr.message if res["success"] or not gr.passed else res.get("error", gr.message)
        output = {
            "success": res["success"],
            "guardrail_status": gr.status,
            "message": message,
            "rows": res.get("rows", []),
        }
        if not res["success"]:
            output["error"] = res.get("error", gr.message)
        return output

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
                        changed = cur.rowcount
                    conn.commit()
            else:
                sql = "UPDATE SYSTEM_INCIDENTS SET STATUS = ?, RESOLUTION_NOTE = ? WHERE INCIDENT_ID = ?"
                params = (clean_status, resolution_note, clean_id)
                if self.db_mode == "azure_blob":
                    with self._blob_lock:
                        changed = self._blob_write(sql, params)
                else:
                    changed = _sqlite_write(DB_FILE_PATH, sql, params)

            if not changed:
                return {
                    "success": False,
                    "error": f"Incident {clean_id} doesn't exist in SYSTEM_INCIDENTS; nothing was changed.",
                }
            return {
                "success": True,
                "message": f"Oracle DB Incident {clean_id} updated to {clean_status}.",
            }
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to update Oracle DB incident: {exc}",
            }
