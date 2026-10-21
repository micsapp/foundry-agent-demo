"""
End-to-End Verification Script for the Azure AI Foundry Agent Demo.
Tests all 6 scenarios:
1. Knowledge Base Q&A (File Search / RAG)
2. Safe Oracle DB SELECT Query
3. SQL Guardrail Blocking (DROP TABLE & Restricted PII Column SSN)
4. KB Content Guardrail Blocking (PII / Secret key detection)
5. KB File Update + Oracle DB Update with Human-in-the-Loop (HITL) Approval
6. Feedback Log for unanswered questions and problem reports (secrets masked)
"""

import tempfile
from pathlib import Path

from agent_service import FoundryAgentOrchestrator
from feedback_log import FeedbackLog


def run_all_tests() -> None:
    orch = FoundryAgentOrchestrator(agent_mode="demo", db_mode="sqlite_mock", kb_mode="local")
    orch.kb_tool.reset_kb_files()
    orch.oracle_tool.reset_demo_db()

    print("=== 1. Testing Knowledge Base Q&A ===")
    res1 = orch.handle_user_message("What are our P1 and P2 response SLAs, and how do we handle ORA-00060?")
    assert "P1 (Critical)" in res1.reply_markdown
    assert "ORA-00060" in res1.reply_markdown
    print("PASS: Knowledge Base Q&A retrieved SLA & ORA-00060 runbook details.\n")

    print("=== 2. Testing Guarded Oracle DB SELECT Query ===")
    res2 = orch.handle_user_message("Check the Oracle database for open P1/P2 incidents and department owners.")
    assert "INC-1041" in res2.reply_markdown
    assert "INC-1042" in res2.reply_markdown
    assert res2.tool_traces[0]["guardrail_status"] == "PASSED"
    print("PASS: Oracle DB query passed guardrails and returned SYSTEM_INCIDENTS joined with DEPARTMENTS.\n")

    print("=== 3. Testing SQL Guardrail (Block DROP TABLE & Restricted SSN Column) ===")
    res3a = orch.handle_user_message("Run `DROP TABLE SYSTEM_INCIDENTS` on Oracle DB.")
    assert res3a.tool_traces[0]["guardrail_status"] == "BLOCKED"
    assert "DESTRUCTIVE_SQL_KEYWORD" in res3a.tool_traces[0]["rule"]
    print("PASS: Blocked DROP TABLE statement:", res3a.tool_traces[0]["message"])

    res3b = orch.handle_user_message("Run `SELECT EMP_ID, FULL_NAME, SSN FROM EMPLOYEES` on Oracle DB.")
    assert res3b.tool_traces[0]["guardrail_status"] == "BLOCKED"
    assert "RESTRICTED_PII_COLUMN" in res3b.tool_traces[0]["rule"]
    print("PASS: Blocked SSN PII column query:", res3b.tool_traces[0]["message"], "\n")

    print("=== 4. Testing KB Update Guardrail (Block Secret / SSN Leak) ===")
    res4 = orch.handle_user_message(
        "Update `oracle_db_runbook.md` to include admin SSN 123-45-6789 and API key sk-proj1234567890abcdefghijklmnop"
    )
    assert res4.tool_traces[0]["guardrail_status"] == "BLOCKED"
    assert "PII_OR_SECRET_DETECTED" in res4.tool_traces[0]["rule"]
    print("PASS: Blocked PII/Secret in KB update:", res4.tool_traces[0]["message"], "\n")

    print("=== 5. Testing Guarded KB File Update + Incident Resolution with HITL Approval ===")
    res5 = orch.handle_user_message(
        "Update `oracle_db_runbook.md` to add a new section on ORA-12514 listener troubleshooting, "
        "and mark incident INC-1042 as RESOLVED."
    )
    assert len(res5.pending_approvals) == 2
    print(f"PASS: Created {len(res5.pending_approvals)} pending HITL approval actions.")

    for pending in res5.pending_approvals:
        approval = orch.approve_pending_action(pending.action_id)
        print("  ->", approval.reply_markdown.splitlines()[0])

    updated_kb = orch.kb_tool.read_kb_file("oracle_db_runbook.md")
    assert "ORA-12514: TNS:listener Does Not Currently Know of Service" in updated_kb
    inc_check = orch.oracle_tool.execute_guarded_query(
        "SELECT STATUS FROM SYSTEM_INCIDENTS WHERE INCIDENT_ID = 'INC-1042'"
    )
    assert inc_check["rows"][0]["STATUS"] == "RESOLVED"
    print("PASS: Verified KB file updated on disk and INC-1042 status updated to RESOLVED in DB!\n")

    print("=== 6. Testing Feedback Log (Unanswered Question & Problem Report) ===")
    orch.feedback_log = FeedbackLog(Path(tempfile.mkdtemp()) / "feedback_log.jsonl")
    res6a = orch.handle_user_message("What is the capital of France?")
    assert res6a.tool_traces[-1]["tool"] == "log_feedback" and "logged the question" in res6a.reply_markdown
    res6b = orch.handle_user_message("I want to report a problem: the VPN is not working, password: hunter22 rejected")
    assert res6b.tool_traces[0]["tool"] == "log_feedback" and "Problem logged" in res6b.reply_markdown
    entries = orch.feedback_log.read_entries()
    assert [e["kind"] for e in entries] == ["problem_report", "unanswered_question"]
    assert "hunter22" not in orch.feedback_log.path.read_text() and entries[0]["masked"]
    print("PASS: Logged the unanswered question and the problem report, with the password masked.\n")

    # Reset back to clean state for user interactive session
    orch.kb_tool.reset_kb_files()
    orch.oracle_tool.reset_demo_db()
    print("ALL 6 END-TO-END DEMO FLOWS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    run_all_tests()
