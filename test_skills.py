"""Run with python -m unittest -v test_skills (no Azure account required)."""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_service import FoundryAgentOrchestrator, public_agent_definition
from guardrails import GuardrailEngine
from oracle_tool import OracleDatabaseTool
from skill_tool import SKILL_TOOL_NAMES, SkillTool

ROOT = Path(__file__).parent


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_patch = patch("oracle_tool.DB_FILE_PATH", self.root / "test.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.database = OracleDatabaseTool(GuardrailEngine(), db_mode="sqlite_mock")
        self.skills_root = self.root / "skills"
        shutil.copytree(ROOT / "skills", self.skills_root, ignore=shutil.ignore_patterns("__pycache__"))
        self.skills = SkillTool(lambda: self.database, self.skills_root)
        self.script = self.skills_root / "incident-report/scripts/generate_report.py"

    def run_report(self, arguments="{}"):
        return self.skills.run_skill_script("incident-report", "generate_report", arguments)

    def test_discovery_load_and_reference(self):
        self.assertEqual(self.skills.list_skills()["skills"][0]["name"], "incident-report")
        skill = self.skills.load_skill("incident-report")
        self.assertTrue(skill["success"])
        self.assertIn("run_skill_script", skill["content"])
        self.assertEqual(skill["scripts"][0]["name"], "generate_report")
        reference = self.skills.read_skill_resource("incident-report", skill["resources"][0]["name"])
        self.assertIn("RESOLVED", reference["content"])

    def test_report_uses_current_database_and_makes_no_writes(self):
        before = (self.root / "test.db").read_bytes()
        output = self.run_report('{"incident_id":"INC-1042"}')
        self.assertTrue(output["success"], output)
        report = output["result"]
        self.assertEqual(report["summary"], {
            "total": 1, "open": 1, "completed": 0, "by_severity": {"P2": 1}, "by_status": {"INVESTIGATING": 1},
        })
        self.assertIn("Alice Chen", report["report_markdown"])
        self.assertIn("ORA-12514", report["report_markdown"])
        self.assertEqual((self.root / "test.db").read_bytes(), before)
        self.database.commit_incident_update("INC-1042", "RESOLVED", "Test resolution")
        report = self.run_report('{"incident_id":"INC-1042"}')["result"]
        self.assertEqual(report["summary"]["open"], 0)
        self.assertEqual(report["summary"]["completed"], 1)

    def test_all_incidents_and_unknown_incident(self):
        report = self.run_report()["result"]
        rows = self.database.execute_guarded_query("SELECT INCIDENT_ID FROM SYSTEM_INCIDENTS")["rows"]
        self.assertEqual(report["summary"]["total"], len(rows))
        missing = self.run_report('{"incident_id":"INC-9999"}')["result"]
        self.assertEqual(missing["summary"]["total"], 0)
        self.assertIn("No matching incidents", missing["report_markdown"])

    def test_unknown_scripts_paths_and_shell_commands_are_blocked(self):
        for name in ("../incident-report", "/tmp", "incident-report;echo hello"):
            with self.subTest(skill=name):
                self.assertFalse(self.skills.load_skill(name)["success"])
                self.assertFalse(self.skills.run_skill_script(name, "generate_report")["success"])
        for name in ("../../skill_tool.py", "scripts/generate_report.py", "python -c 'print(1)'"):
            with self.subTest(script=name):
                self.assertEqual(self.skills.run_skill_script("incident-report", name)["guardrail_status"], "BLOCKED")
        for name in ("../SKILL.md", "../../.env", "/etc/passwd"):
            self.assertFalse(self.skills.read_skill_resource("incident-report", name)["success"])

    def test_argument_validation_precedes_database_access(self):
        with patch.object(self.skills, "database_factory", side_effect=AssertionError("Shouldn't reach database")):
            for arguments in (
                "not json", "[]", "null", "true", '{"command":"rm -rf /"}', "\ud800",
                '{"incidents":[]}', '{"incident_id":1}', '{"incident_id":"INC-1042\' OR 1=1"}',
                '{"incident_id":"INC-1042\\n"}', " " * 4097,
            ):
                with self.subTest(arguments=arguments[:60]):
                    self.assertEqual(self.run_report(arguments)["guardrail_status"], "BLOCKED")

    def test_symlink_cannot_escape_packaged_skill(self):
        outside = self.root / "outside.py"
        outside.write_text('raise RuntimeError("Should not execute")')
        self.script.unlink()
        self.script.symlink_to(outside)
        self.assertEqual(self.run_report()["guardrail_status"], "BLOCKED")

    def test_database_failure_does_not_generate_empty_report(self):
        with patch.object(self.database, "execute_guarded_query", return_value={
            "success": False, "guardrail": self.database.guardrails.validate_sql_query("SELECT INCIDENT_ID FROM SYSTEM_INCIDENTS"),
            "error": "Database unavailable", "rows": [],
        }):
            output = self.run_report()
        self.assertFalse(output["success"])
        self.assertIn("database query failed", output["message"])
        self.assertNotIn("result", output)

    def test_script_failure_timeout_invalid_and_oversize_output(self):
        cases = [
            ("raise SystemExit(7)", "exit code 7"),
            ("import time; time.sleep(5)", "timeout"),
            ("print('not JSON')", "valid JSON"),
            ("print('[]')", "JSON object"),
            ("print('x' * 40000)", "size limit"),
        ]
        for code, expected in cases:
            with self.subTest(expected=expected):
                self.script.write_text(code)
                with patch("skill_tool.SCRIPT_TIMEOUT_SECONDS", 0.5):
                    output = self.run_report()
                self.assertFalse(output["success"])
                self.assertIn(expected, output["message"])

    def test_script_receives_masked_snapshot_and_no_inherited_secrets(self):
        self.script.write_text(
            "import json, os, sys\n"
            "payload = json.load(sys.stdin)\n"
            "print(json.dumps({'payload': payload, 'secret': os.getenv('SKILL_TEST_SECRET'), 'cwd': os.getcwd()}))\n"
        )
        rows = [{"SUMMARY": "Password: hunter22 and SSN 123-45-6789", "INCIDENT_ID": "INC-1042"}]
        with patch.object(self.database, "query_tool_output", return_value={"success": True, "rows": rows}), \
                patch.dict(os.environ, {"SKILL_TEST_SECRET": "must-not-inherit"}):
            output = self.run_report()
        self.assertTrue(output["success"], output)
        self.assertIsNone(output["result"]["secret"])
        self.assertNotIn("hunter22", json.dumps(output))
        self.assertNotIn("123-45-6789", json.dumps(output))
        self.assertFalse(Path(output["result"]["cwd"]).exists())

    def test_output_redaction_preserves_json_and_does_not_return_tracebacks(self):
        self.script.write_text(
            "import json\n"
            "print(json.dumps({'value': 'password: hunter22', 'nested': ['123-45-6789']}))\n"
        )
        output = self.run_report()
        self.assertTrue(output["success"], output)
        self.assertNotIn("hunter22", json.dumps(output))
        self.assertNotIn("123-45-6789", json.dumps(output))
        self.script.write_text("raise RuntimeError('secret-in-traceback')")
        output = self.run_report()
        self.assertFalse(output["success"])
        self.assertNotIn("secret-in-traceback", json.dumps(output))

    def test_demo_chat_runs_complete_skill_lifecycle(self):
        orch = FoundryAgentOrchestrator(agent_mode="demo", db_mode="sqlite_mock", kb_mode="local")
        orch.skill_tool = self.skills
        response = orch.handle_user_message("Generate an incident report for INC-1042 using the incident-report skill.")
        self.assertEqual([trace["tool"] for trace in response.tool_traces], list(SKILL_TOOL_NAMES))
        self.assertTrue(all(trace["guardrail_status"] == "PASSED" for trace in response.tool_traces))
        self.assertIn("INC-1042", response.reply_markdown)
        self.assertEqual(response.pending_approvals, [])

    def test_public_agent_exposes_only_read_only_skill_tools(self):
        definition = public_agent_definition("test-model", "vs-test", "https://example.com/mcp").as_dict()
        skill_tools = [tool for tool in definition["tools"] if tool.get("server_label") == "skills"]
        self.assertEqual(len(skill_tools), 1)
        self.assertEqual(skill_tools[0]["allowed_tools"]["tool_names"], list(SKILL_TOOL_NAMES))
        self.assertEqual(skill_tools[0]["require_approval"], "never")

    @unittest.skipUnless(importlib.util.find_spec("azure.functions"), "Install azure_function/requirements.txt for MCP binding checks")
    def test_azure_function_bindings_and_wrappers(self):
        sys.path.insert(0, str(ROOT / "azure_function"))
        self.addCleanup(sys.path.pop, 0)
        import function_app

        functions = {function.get_function_name(): function for function in function_app.app.get_functions()}
        for name in SKILL_TOOL_NAMES:
            self.assertIn(name, functions)
            bindings = json.loads(functions[name].get_function_json())["bindings"]
            self.assertTrue(any(binding["type"] == "mcpToolTrigger" for binding in bindings))
        with patch.object(function_app, "_skills", self.skills):
            output = json.loads(asyncio.run(functions["run_skill_script"].get_user_function()(json.dumps({
                "arguments": {"skill_name": "incident-report", "script_name": "generate_report",
                              "arguments_json": '{"incident_id":"INC-1042"}'},
            }))))
        self.assertTrue(output["success"], output)
        self.assertEqual(output["result"]["summary"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
