"""Packaged agent skills shared by the local demo and the Azure Function MCP server.

Only scripts registered here can execute. Scripts receive validated JSON on stdin,
run with a short timeout, and return JSON on stdout. This is a runner for trusted
deployment code, not an operating-system sandbox for uploaded or generated code.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator

from feedback_log import mask_sensitive

SKILLS_ROOT = Path(__file__).with_name("skills")
SKILL_TOOL_NAMES = ("list_skills", "load_skill", "read_skill_resource", "run_skill_script")
MAX_ARGUMENT_BYTES = 4096
MAX_CONTENT_BYTES = 32_768
SCRIPT_TIMEOUT_SECONDS = 10

# This registry is reviewed and shipped with the Function App. Files added to a
# directory, chat attachments, and user-supplied paths are never executable entries.
# All current entries are read-only, so both agents may use them without approval.
SKILL_CATALOG = {
    "incident-report": {
        "description": "Generate an incident report from the current demo database, with severity and owner summaries.",
        "resources": {"references/report-format.md": "Report sections and interpretation of incident statuses."},
        "scripts": {
            "generate_report": {
                "path": "scripts/generate_report.py",
                "description": "Create a read-only incident report. Omit incident_id to report on all incidents.",
                "arguments_schema": {
                    "type": "object",
                    "properties": {"incident_id": {"type": "string", "pattern": r"^INC-[0-9]{4}$"}},
                    "additionalProperties": False,
                },
                "input_source": "incident_snapshot",
            }
        },
    }
}


def _mask_values(value: Any) -> Any:
    """Mask strings before JSON encoding so redaction cannot corrupt JSON syntax."""
    if isinstance(value, str):
        return mask_sensitive(value)[0]
    if isinstance(value, dict):
        return {key: _mask_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_values(item) for item in value]
    return value


class SkillTool:
    def __init__(self, database_factory: Callable[[], Any], skills_root: Path = SKILLS_ROOT) -> None:
        self.database_factory = database_factory
        self.skills_root = Path(skills_root).resolve()

    @staticmethod
    def _blocked(message: str) -> dict[str, Any]:
        return {"success": False, "guardrail_status": "BLOCKED", "message": message}

    def _file(self, skill_name: str, relative_path: str) -> Path:
        path = (self.skills_root / skill_name / relative_path).resolve(strict=True)
        if not path.is_relative_to(self.skills_root / skill_name) or not path.is_file():
            raise ValueError("Skill files must remain inside their packaged skill directory.")
        return path

    def _read(self, skill_name: str, relative_path: str) -> str:
        path = self._file(skill_name, relative_path)
        if path.stat().st_size > MAX_CONTENT_BYTES:
            raise ValueError("Skill content exceeds the size limit.")
        return path.read_text(encoding="utf-8")

    def list_skills(self) -> dict[str, Any]:
        return {
            "success": True,
            "skills": [{"name": name, "description": spec["description"]} for name, spec in SKILL_CATALOG.items()],
            "message": "Load a skill before using its resources or scripts.",
        }

    def load_skill(self, skill_name: str) -> dict[str, Any]:
        spec = SKILL_CATALOG.get(skill_name) if isinstance(skill_name, str) else None
        if spec is None:
            return self._blocked("Unknown skill. Use list_skills to discover packaged skills.")
        try:
            content = self._read(skill_name, "SKILL.md")
        except (OSError, ValueError) as exc:
            return {"success": False, "message": f"Couldn't load packaged skill: {type(exc).__name__}."}
        return {
            "success": True,
            "skill_name": skill_name,
            "content": content,
            "resources": [{"name": name, "description": desc} for name, desc in spec["resources"].items()],
            "scripts": [
                {"name": name, "description": script["description"], "arguments_schema": script["arguments_schema"]}
                for name, script in spec["scripts"].items()
            ],
            "message": f"Loaded {skill_name}.",
        }

    def read_skill_resource(self, skill_name: str, resource_name: str) -> dict[str, Any]:
        spec = SKILL_CATALOG.get(skill_name) if isinstance(skill_name, str) else None
        if spec is None or not isinstance(resource_name, str) or resource_name not in spec["resources"]:
            return self._blocked("Unknown skill resource. Use the exact resource name returned by load_skill.")
        try:
            content = self._read(skill_name, resource_name)
        except (OSError, ValueError) as exc:
            return {"success": False, "message": f"Couldn't read packaged resource: {type(exc).__name__}."}
        return {"success": True, "resource_name": resource_name, "content": content, "message": "Read skill resource."}

    def _prepare_input(self, source: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if source == "arguments":
            return {"arguments": arguments}
        if source != "incident_snapshot":
            raise ValueError("Unknown script input source.")
        # Fetch authoritative rows on the server. The agent supplies a validated
        # incident ID, never SQL or the incident records used to generate a report.
        query = (
            "SELECT I.INCIDENT_ID, I.SERVICE_NAME, I.ORACLE_ERROR_CODE, I.SEVERITY, I.STATUS, I.SUMMARY, "
            "D.DEPT_NAME, D.ONCALL_LEAD FROM SYSTEM_INCIDENTS I "
            "JOIN DEPARTMENTS D ON I.DEPT_ID = D.DEPT_ID"
        )
        incident_id = arguments.get("incident_id")
        if incident_id:
            # A full match also rejects a trailing newline (JSON Schema's $ allows one).
            if re.fullmatch(r"INC-[0-9]{4}", incident_id) is None:
                raise ValueError("incident_id must use INC-XXXX format.")
            query += f" WHERE I.INCIDENT_ID = '{incident_id}'"
        query += " ORDER BY I.SEVERITY, I.INCIDENT_ID"
        database = self.database_factory()
        result = database.query_tool_output(database.execute_guarded_query(query))
        if not result["success"]:
            raise RuntimeError("The guarded database query failed; no report was generated.")
        return {"arguments": arguments, "incidents": result["rows"]}

    def run_skill_script(self, skill_name: str, script_name: str, arguments_json: str = "{}") -> dict[str, Any]:
        spec = SKILL_CATALOG.get(skill_name) if isinstance(skill_name, str) else None
        script = spec["scripts"].get(script_name) if spec and isinstance(script_name, str) else None
        if script is None:
            return self._blocked("Unknown script. Only named scripts returned by load_skill can run.")
        try:
            valid_size = isinstance(arguments_json, str) and len(arguments_json.encode("utf-8")) <= MAX_ARGUMENT_BYTES
        except UnicodeError:
            valid_size = False
        if not valid_size:
            return self._blocked("arguments_json must be a JSON object encoded as a string of at most 4096 bytes.")
        try:
            arguments = json.loads(arguments_json)
        except (ValueError, RecursionError):
            return self._blocked("arguments_json must contain valid JSON.")
        if not Draft202012Validator(script["arguments_schema"]).is_valid(arguments):
            return self._blocked("Script arguments do not match the arguments_schema returned by load_skill.")
        try:
            script_path = self._file(skill_name, script["path"])
            script_input = self._prepare_input(script["input_source"], arguments)
            # Scripts see a masked data snapshot, not the Function App's credentials.
            input_text = json.dumps(_mask_values(script_input), ensure_ascii=False, default=str)
            result = self._execute(script_path, input_text)
        except ValueError:
            return self._blocked("Invalid script input or packaged script path.")
        except (OSError, RuntimeError) as exc:
            detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            return {"success": False, "message": f"Couldn't run packaged script: {detail}"}
        if result.get("success"):
            result.update(skill_name=skill_name, script_name=script_name)
        return result

    @staticmethod
    def _execute(script_path: Path, input_text: str) -> dict[str, Any]:
        # Temporary files bound how much output is read into memory, including on
        # error. A separate working directory prevents files from leaking between calls.
        with (
            tempfile.TemporaryDirectory(prefix="foundry-skill-") as workdir,
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            with subprocess.Popen(
                [sys.executable, "-I", "-u", str(script_path)],
                stdin=subprocess.PIPE,
                stdout=stdout,
                stderr=stderr,
                cwd=workdir,
                env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"},
                start_new_session=True,
            ) as process:
                try:
                    process.communicate(input=input_text.encode("utf-8"), timeout=SCRIPT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    process.communicate()
                    return {"success": False, "message": "Skill script exceeded its execution timeout."}
                if process.returncode:
                    # Don't return tracebacks or arbitrary stderr to the model.
                    return {"success": False, "message": f"Skill script failed with exit code {process.returncode}."}
            stdout.seek(0)
            output = stdout.read(MAX_CONTENT_BYTES + 1)
            if len(output) > MAX_CONTENT_BYTES:
                return {"success": False, "message": "Skill script output exceeded the size limit."}
            try:
                result = _mask_values(json.loads(output.decode("utf-8")))
            except (UnicodeError, ValueError, RecursionError):
                return {"success": False, "message": "Skill script did not return valid JSON."}
            if not isinstance(result, dict):
                return {"success": False, "message": "Skill script must return a JSON object."}
            return {"success": True, "result": result, "message": "Packaged skill script completed."}

    def dispatch(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Use the same entry points in local function tools and Azure MCP wrappers."""
        if tool_name == "list_skills":
            return self.list_skills()
        if tool_name == "load_skill":
            return self.load_skill(str(arguments.get("skill_name") or ""))
        if tool_name == "read_skill_resource":
            return self.read_skill_resource(
                str(arguments.get("skill_name") or ""), str(arguments.get("resource_name") or ""),
            )
        if tool_name == "run_skill_script":
            return self.run_skill_script(
                str(arguments.get("skill_name") or ""),
                str(arguments.get("script_name") or ""),
                arguments.get("arguments_json", "{}"),
            )
        return self._blocked("Unknown skill tool.")
