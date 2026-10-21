"""
Streamlit Interactive Demo UI for Microsoft Azure AI Foundry Agent:
- Knowledge Base Question Answering (Vector Store / File Search)
- Guarded Oracle Database Queries & Incident Updates
- Guarded Knowledge Base Source File Updates with Unified Diff & Human-in-the-Loop Approval
"""

from __future__ import annotations

import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent_service import AgentTurnResponse, FoundryAgentOrchestrator
from feedback_log import FEEDBACK_KINDS
from oracle_tool import DB_FILE_PATH, seed_mock_db

load_dotenv()

st.set_page_config(
    page_title="Azure AI Foundry Agent — Oracle DB & KB Guardrails Demo",
    page_icon="🛡️",
    layout="wide",
)

# ============================================================================
# Session State Initialization
# ============================================================================
if "orchestrator" not in st.session_state:
    st.session_state.orchestrator = FoundryAgentOrchestrator(
        agent_mode=os.getenv("AGENT_MODE", "demo"),
        db_mode=os.getenv("DB_MODE", "sqlite_mock"),
        kb_mode=os.getenv("KB_MODE", "local"),
    )

if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "👋 **Welcome to the Microsoft AI Foundry Agent Demo!**\n\n"
                "I am configured with:\n"
                "1. **Knowledge Base Grounding (`FileSearchTool` / Vector Store):** Ask me about database runbooks, "
                "`ORA-*` error codes, incident response SLAs, or the product manuals (Quorvia MQ, Tallybrook Ledger, "
                "Fernwick Field Service).\n"
                "2. **Guarded Oracle Database Tool (`query_oracle_db` & `update_oracle_incident`):** I can query "
                "`SYSTEM_INCIDENTS`, `DEPARTMENTS`, and `EMPLOYEES` with strict read-only & column-privacy guardrails.\n"
                "3. **Guarded KB Source File Updater (`propose_kb_file_update`):** Ask me to update `oracle_db_runbook.md` "
                "or `sla_and_escalation_guide.md` — I will scan for PII/secrets, generate a unified diff, and pause for "
                "your **Human-in-the-Loop (HITL)** approval before writing and re-indexing the Vector Store.\n"
                "4. **Feedback Log (`log_feedback`):** Questions I can't answer from the Knowledge Base, and problems "
                "you report, are written to the team's feedback log (see the **Feedback Log** tab)."
            ),
            "traces": [],
        }
    ]

orch: FoundryAgentOrchestrator = st.session_state.orchestrator


# ============================================================================
# Sidebar: Configuration, Guardrail Toggles, Live DB & KB Viewers
# ============================================================================
with st.sidebar:
    st.title("⚙️ Foundry & Guardrail Control")

    with st.expander("🔌 Agent & Oracle DB Mode", expanded=False):
        agent_mode = st.selectbox(
            "Agent Execution Mode",
            options=["demo", "foundry"],
            index=0 if orch.agent_mode == "demo" else 1,
            help="Use 'demo' for instant local testing, or 'foundry' to connect to the project at PROJECT_ENDPOINT.",
        )
        db_modes = ["sqlite_mock", "azure_blob", "oracle_live"]
        db_mode = st.selectbox(
            "Oracle Database Mode",
            options=db_modes,
            index=db_modes.index(orch.oracle_tool.db_mode) if orch.oracle_tool.db_mode in db_modes else 0,
            help=(
                "'sqlite_mock': pre-seeded Oracle tables in a local file. 'azure_blob': the same tables in Azure "
                "Storage, shared with the Azure Function that runs the DB tools for Foundry (also in the portal "
                "playground). 'oracle_live': a real Oracle database through python-oracledb."
            ),
        )
        if db_mode == "oracle_live":
            oracle_dsn = st.text_input("Oracle DSN", value=orch.oracle_tool.oracle_dsn)
            oracle_user = st.text_input("Oracle User", value=orch.oracle_tool.oracle_user)
            oracle_pwd = st.text_input("Oracle Password", value=orch.oracle_tool.oracle_password, type="password")
            orch.oracle_tool.oracle_dsn = oracle_dsn
            orch.oracle_tool.oracle_user = oracle_user
            orch.oracle_tool.oracle_password = oracle_pwd

        if agent_mode != orch.agent_mode or db_mode != orch.oracle_tool.db_mode:
            orch.agent_mode = agent_mode
            orch.oracle_tool.db_mode = db_mode
            if db_mode == "sqlite_mock":
                seed_mock_db(DB_FILE_PATH)
            if agent_mode == "foundry":
                orch._init_foundry_client()

        if orch.agent_mode == "foundry":
            st.caption(f"Foundry: {orch.foundry_status}")

    st.subheader("🛡️ Active Guardrail Policies")
    orch.guardrails.enforce_sql_readonly = st.checkbox(
        "SQL Guardrail: Enforce Read-Only SELECT",
        value=orch.guardrails.enforce_sql_readonly,
        help="Blocks DROP, DELETE, UPDATE, INSERT, ALTER, TRUNCATE, and multi-statement queries.",
    )
    orch.guardrails.enforce_table_allowlist = st.checkbox(
        "SQL Guardrail: Table & PII Column Allowlist",
        value=orch.guardrails.enforce_table_allowlist,
        help="Restricts queries to SYSTEM_INCIDENTS, DEPARTMENTS, EMPLOYEES and blocks SSN/PASSWORD columns.",
    )
    orch.guardrails.enforce_pii_scan = st.checkbox(
        "KB Guardrail: Block PII & API Secrets",
        value=orch.guardrails.enforce_pii_scan,
        help="Scans proposed KB file updates for SSNs, API keys (sk-...), and hardcoded passwords.",
    )
    orch.guardrails.require_hitl_for_kb_update = st.checkbox(
        "HITL Gate: Require Approval for KB File Updates",
        value=orch.guardrails.require_hitl_for_kb_update,
        help="Pauses execution and displays a Unified Diff for human approval before modifying KB files.",
    )
    orch.guardrails.require_hitl_for_db_write = st.checkbox(
        "HITL Gate: Require Approval for Oracle DB Writes",
        value=orch.guardrails.require_hitl_for_db_write,
        help="Requires human operator approval before updating incident status in Oracle DB.",
    )

    st.divider()
    col_reset1, col_reset2 = st.columns(2)
    with col_reset1:
        if st.button("🔄 Reset KB Files", width="stretch"):
            try:
                orch.kb_tool.reset_kb_files()
            except Exception as exc:
                st.error(f"Couldn't reset {orch.kb_tool.location}: `{type(exc).__name__}: {str(exc)[:200]}`")
            else:
                st.toast("Knowledge Base files restored to initial state!", icon="📄")
                st.rerun()
    with col_reset2:
        if st.button("🔄 Reset Oracle DB", width="stretch"):
            try:
                orch.oracle_tool.reset_demo_db()
            except Exception as exc:
                st.error(f"Couldn't reset {orch.oracle_tool.location}: `{type(exc).__name__}: {str(exc)[:200]}`")
            else:
                st.toast("Oracle demo tables restored to initial state!", icon="🗄️")
                st.rerun()

    st.divider()
    st.subheader("📋 Guardrail Audit Log")
    if not orch.guardrails.audit_log:
        st.caption("No tool calls evaluated yet.")
    else:
        for entry in orch.guardrails.audit_log[:8]:
            badge = (
                "🟢"
                if entry.status in ("PASSED", "APPROVED")
                else ("🟡" if entry.status == "PENDING_HITL" else "🔴")
            )
            st.markdown(
                f"{badge} **`{entry.status}`** | `{entry.action_type}`  \n"
                f"<small>{entry.timestamp} — *{entry.rule_triggered}*</small>",
                unsafe_allow_html=True,
            )


# ============================================================================
# Main Page Header & Live Data Inspector Tabs
# ============================================================================
st.title("🛡️ Microsoft AI Foundry Agent — Oracle DB & Knowledge Base Guardrails")
st.caption(
    "Demonstrates an Azure AI Foundry Agent that answers questions from a Vector Store Knowledge Base, "
    "queries an Oracle Database, and updates KB source files with deterministic guardrails & Human-in-the-Loop approval."
)

tab_chat, tab_db, tab_kb, tab_feedback, tab_arch = st.tabs(
    [
        "💬 Agent Chat & HITL Approvals",
        "🗄️ Live Oracle DB Inspector",
        "📚 Live Knowledge Base Files",
        "📝 Feedback Log",
        "🏗️ Azure AI Foundry Architecture",
    ]
)

# ----------------------------------------------------------------------------
# Tab 2: Live Oracle DB Inspector
# ----------------------------------------------------------------------------
with tab_db:
    st.subheader("🗄️ Oracle Database Tables (`SYSTEM_INCIDENTS`, `DEPARTMENTS`, `EMPLOYEES`)")
    st.caption(
        f"Live view of the database tables ({orch.oracle_tool.location}). When you approve an incident update "
        "in the chat, this table updates immediately."
        + (
            " Foundry runs the database tools on the Azure Function (an MCP server), which uses this same "
            "database, so changes approved in the portal playground show up here too."
            if orch.db_via_function
            else ""
        )
    )
    inc_res = orch.oracle_tool.execute_guarded_query("SELECT * FROM SYSTEM_INCIDENTS")
    if not inc_res["success"]:
        st.error(f"Couldn't read {orch.oracle_tool.location}: {inc_res.get('error')}")
    if inc_res["rows"]:
        st.markdown("#### `SYSTEM_INCIDENTS`")
        st.dataframe(pd.DataFrame(inc_res["rows"]), width="stretch", hide_index=True)

    col_d, col_e = st.columns(2)
    with col_d:
        dept_res = orch.oracle_tool.execute_guarded_query("SELECT * FROM DEPARTMENTS")
        if dept_res["rows"]:
            st.markdown("#### `DEPARTMENTS`")
            st.dataframe(pd.DataFrame(dept_res["rows"]), width="stretch", hide_index=True)
    with col_e:
        emp_res = orch.oracle_tool.execute_guarded_query(
            "SELECT EMP_ID, FULL_NAME, DEPT_ID, ROLE_TITLE, STATUS FROM EMPLOYEES"
        )
        if emp_res["rows"]:
            st.markdown("#### `EMPLOYEES` (PII `SSN` column hidden by Guardrail)")
            st.dataframe(pd.DataFrame(emp_res["rows"]), width="stretch", hide_index=True)

# ----------------------------------------------------------------------------
# Tab 3: Live Knowledge Base Source Files
# ----------------------------------------------------------------------------
with tab_kb:
    st.subheader("📚 Knowledge Base Source Files (`*.md`, `*.pdf`)")
    st.caption(
        f"Source files in {orch.kb_tool.location}. File Search searches the vector store's indexed copy of each "
        "file; an approved update saves the file and re-indexes it."
        + (
            " Foundry runs KB updates on the Azure Function (an MCP server), which edits these same files, so "
            "updates approved in the portal playground show up here too."
            if orch.kb_via_function
            else ""
        )
    )
    try:
        kb_files = orch.kb_tool.list_kb_files()
    except Exception as exc:
        kb_files = []
        st.error(f"Couldn't list {orch.kb_tool.location}: `{type(exc).__name__}: {str(exc)[:200]}`")
    selected_kb = st.selectbox("Select Knowledge Base File to Inspect:", kb_files)
    if selected_kb:
        content = orch.kb_tool.read_kb_file(selected_kb)
        if selected_kb.lower().endswith(".pdf"):
            st.caption("Read-only PDF manual. Text below is what was extracted from it; the agent can't edit it.")
            st.code(content, language="text")
        else:
            st.code(content, language="markdown")

# ----------------------------------------------------------------------------
# Tab 4: Feedback Log (unanswered questions & problem reports)
# ----------------------------------------------------------------------------
with tab_feedback:
    st.subheader(f"📝 Feedback Log ({orch.feedback_log.location})")
    st.caption(
        "Questions the agent couldn't answer from the Knowledge Base, and problems users reported. The agent "
        "adds entries with the `log_feedback` tool; SSNs, API keys and passwords are masked before writing."
        + (
            " Foundry calls the log_feedback Azure Function (an MCP server) itself, so entries also arrive from the "
            "portal playground."
            if orch.feedback_via_function
            else ""
        )
    )
    try:
        fb_entries = orch.feedback_log.read_entries()
        fb_error = None
    except Exception as exc:
        fb_entries, fb_error = [], exc
    if fb_error:
        st.error(
            f"Couldn't read {orch.feedback_log.location}: `{type(fb_error).__name__}`. If the message mentions "
            "security defaults or sign-in, run `az login` again (it asks for MFA), then reload. You can also browse "
            "the table in the Azure portal: storage account → Storage browser → Tables."
        )
        with st.expander("Error details"):
            st.code(str(fb_error)[:2000])
    elif not fb_entries:
        st.caption("No entries yet.")
    else:
        counts = {kind: sum(e.get("kind") == kind for e in fb_entries) for kind in FEEDBACK_KINDS}
        st.markdown(" · ".join(f"**{n}** {kind.replace('_', ' ')}(s)" for kind, n in counts.items()))
        st.dataframe(
            pd.DataFrame(
                fb_entries,
                columns=["time", "kind", "id", "summary", "user_message", "details", "agent", "conversation_id"],
            ),
            width="stretch",
            hide_index=True,
        )
        st.download_button(
            "⬇️ Download as JSON Lines",
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in reversed(fb_entries)),
            file_name="feedback_log.jsonl",
            mime="application/x-ndjson",
        )

# ----------------------------------------------------------------------------
# Tab 5: Architecture Reference
# ----------------------------------------------------------------------------
with tab_arch:
    st.subheader("🏗️ How This Works in Microsoft Azure AI Foundry")
    st.markdown(
        """
        1. **Knowledge Base Grounding (Read-Only):**
           - Uses Microsoft Foundry's `FileSearchTool` backed by a **Vector Store** (`vector_stores.create` + `vector_stores.files.upload_and_poll`).
           - The agent searches `oracle_db_runbook.md` and `sla_and_escalation_guide.md` to answer operational questions, and three read-only PDF product manuals for software questions.
        2. **Oracle Database Action Tool (`query_oracle_db` & `update_oracle_incident`):**
           - With `DB_MODE=azure_blob` and the Azure Function deployed, both are **MCP tools** that Foundry calls on the function, so they work in the portal playground too. The mock database is a SQLite file in Azure Storage that the function and this app share. Foundry holds every `update_oracle_incident` call until someone approves it here (or in the playground's approve prompt).
           - Otherwise they're `FunctionTool`s that this app runs.
           - **SQL Guardrail** intercepts every query before `python-oracledb` executes it: blocking `DROP`/`DELETE`/`UPDATE`, preventing multi-statement `;` injection, and enforcing table/column allowlists.
        3. **Knowledge Base Update Action Tool (`propose_kb_file_update`):**
           - Because Vector Store files are read-only to the model, updating a KB file is exposed as an **Action Tool**.
           - With `KB_MODE=azure_blob` and the Azure Function deployed, it's an **MCP tool** that Foundry calls on the function after the update is approved; the KB files live in an Azure blob container that the function and this app share. Otherwise it's a `FunctionTool` that this app runs on `knowledge_base/`.
           - **KB Guardrail** checks for path traversal, scans for accidental PII/secrets (`SSN`, `sk-...` API keys), generates a `difflib` unified diff, and answers the agent's `function_call` with `PENDING_HITL` until a human clicks **Approve**.
           - Upon approval, the tool writes the updated Markdown file, uploads it to the agent's vector store via `vector_stores.files.upload_and_poll`, removes the outdated `file_id`, and tells the agent the outcome on the next turn.
        4. **Feedback Log Tool (`log_feedback`):**
           - When the Knowledge Base can't answer a question, or a user reports a problem, the agent calls this tool. With `deploy_feedback_function.sh` set up, it's an **MCP tool** that Foundry calls itself: an Azure Function (MCP server) writes the entry to an Azure Table, so it works in the portal playground too. Otherwise it's a `FunctionTool` and the app appends to `feedback_log.jsonl`.
           - The log is append-only and the agent never reads it back, so no approval is needed; SSNs, API keys and passwords are masked first.
        """
    )

# ----------------------------------------------------------------------------
# Tab 1: Interactive Agent Chat & One-Click Scenarios
# ----------------------------------------------------------------------------
with tab_chat:
    st.markdown("##### ⚡ Try a Demo Scenario (Click any button):")
    sc_col1, sc_col2, sc_col3, sc_col4, sc_col5 = st.columns(5)

    triggered_prompt = None
    with sc_col1:
        if st.button("1️⃣ Ask KB Question (SLAs & ORA-00060)", width="stretch"):
            triggered_prompt = (
                "What are our P1 and P2 response SLAs, and how do we troubleshoot ORA-00060 deadlocks?"
            )
    with sc_col2:
        if st.button("2️⃣ Query Oracle DB (Open Incidents)", width="stretch"):
            triggered_prompt = (
                "Check the Oracle database for active incidents and show which department and on-call lead owns them."
            )
    with sc_col3:
        if st.button("3️⃣ Test SQL Guardrail (Block DROP/SSN)", width="stretch"):
            triggered_prompt = (
                "Run `DROP TABLE SYSTEM_INCIDENTS` on the Oracle database."
            )
    with sc_col4:
        if st.button("4️⃣ Update KB File + DB (HITL Approval)", width="stretch"):
            triggered_prompt = (
                "Update `oracle_db_runbook.md` to add a new section on ORA-12514 listener troubleshooting, "
                "and mark incident INC-1042 as RESOLVED."
            )
    with sc_col5:
        if st.button("5️⃣ Test KB PII Guardrail (Block Secret)", width="stretch"):
            triggered_prompt = (
                "Update `oracle_db_runbook.md` to include emergency admin SSN 123-45-6789 and API key sk-proj1234567890abcdefghijklmnop."
            )

    st.divider()

    # Render conversation history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            for tr in msg.get("traces", []):
                status = tr.get("guardrail_status", "PASSED")
                icon = "🟢" if status == "PASSED" else ("🟡" if status == "PENDING_HITL" else "🔴")
                with st.expander(
                    f"{icon} Tool Call: `{tr['tool']}` — Guardrail: `{status}` ({tr.get('rule', '')})",
                    expanded=(status != "PASSED"),
                ):
                    st.markdown(f"**Guardrail Message:** {tr.get('message', '')}")
                    st.json(tr.get("arguments", {}))
                    if tr.get("diff_preview"):
                        st.markdown("**Proposed Unified Diff:**")
                        st.code(tr["diff_preview"], language="diff")

    # Render any Pending Human-in-the-Loop (HITL) Approval Cards
    if orch.pending_actions:
        st.warning(
            f"⏸️ **{len(orch.pending_actions)} Action(s) Awaiting Human-in-the-Loop (HITL) Approval**"
        )
        for act_id, act in list(orch.pending_actions.items()):
            with st.container(border=True):
                st.markdown(
                    f"#### 🛡️ Approval Required: `{act.tool_name}` (`{act_id}`)\n"
                    f"**Guardrail Status:** `{act.guardrail_result.status}` — {act.guardrail_result.message}"
                )
                if act.tool_name == "propose_kb_file_update":
                    st.markdown(
                        f"- **Target KB File:** `knowledge_base/{act.arguments.get('filename')}`\n"
                        f"- **Change Summary:** {act.arguments.get('change_summary')}"
                    )
                    if act.guardrail_result.diff_preview:
                        st.markdown("**Unified Diff Preview (Before vs. After):**")
                        st.code(act.guardrail_result.diff_preview, language="diff")
                    if act.foundry_approval_request_id:
                        st.caption(
                            "Foundry is holding this call. When you approve, it sends it to the Azure Function, "
                            "which saves the file and re-indexes it in the vector store."
                        )
                elif act.tool_name == "update_oracle_incident":
                    st.markdown(
                        f"- **Oracle Table:** `SYSTEM_INCIDENTS`\n"
                        f"- **Incident ID:** `{act.arguments.get('incident_id')}`\n"
                        f"- **New Status:** `{act.arguments.get('new_status')}`\n"
                        f"- **Resolution Note:** {act.arguments.get('resolution_note')}"
                    )
                    if act.foundry_approval_request_id:
                        st.caption(
                            "Foundry is holding this call. When you approve, it sends it to the Azure Function, "
                            "which updates the shared database."
                        )

                btn_col1, btn_col2, _ = st.columns([1, 1, 3])
                with btn_col1:
                    if st.button(
                        "✅ Approve & Execute",
                        key=f"approve_{act_id}",
                        type="primary",
                        width="stretch",
                    ):
                        outcome = orch.approve_pending_action(act_id)
                        st.session_state.messages.append(
                            {"role": "assistant", "content": outcome.reply_markdown, "traces": outcome.tool_traces}
                        )
                        st.rerun()
                with btn_col2:
                    if st.button(
                        "🛑 Reject Action",
                        key=f"reject_{act_id}",
                        width="stretch",
                    ):
                        outcome = orch.reject_pending_action(act_id)
                        st.session_state.messages.append(
                            {"role": "assistant", "content": outcome.reply_markdown, "traces": outcome.tool_traces}
                        )
                        st.rerun()

    # Chat input box
    user_input = st.chat_input(
        "Ask a KB question, query the Oracle DB, or ask the agent to update a KB file..."
    )
    active_prompt = triggered_prompt or user_input

    if active_prompt:
        st.session_state.messages.append({"role": "user", "content": active_prompt, "traces": []})
        turn_res: AgentTurnResponse = orch.handle_user_message(active_prompt)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": turn_res.reply_markdown,
                "traces": turn_res.tool_traces,
            }
        )
        st.rerun()
