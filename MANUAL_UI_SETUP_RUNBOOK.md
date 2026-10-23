# Manual UI Runbook: Configuring & Operating the Azure AI Foundry Agent (Click-by-Click Portal Guide)

**Purpose:** This runbook is designed for engineers who need to configure, connect, and test the **Azure AI Foundry Agent**, **Knowledge Base**, **Oracle Database Action**, **KB Update Action**, and **Guardrails** **manually through the Web UI** (without relying on automated provisioning scripts).

---

## Part 1: Setting Up the Agent Manually in the Azure AI Foundry Portal UI

### Step 1: Create the Agent in Azure AI Foundry Portal
1. Open your browser and sign in to the **[Azure AI Foundry Portal](https://ai.azure.com)**.
2. Select your **AI Foundry Project** from the home screen.
3. In the left-hand navigation menu under **Build and customize**, click **Agents**.
4. Click the **+ New agent** button at the top left.
5. In the **Agent setup** right-hand pane, configure:
   * **Name:** `Oracle-And-KB-Guarded-Agent`
   * **Deployment (Model):** Select `gpt-4o` (or `gpt-4o-mini`).
   * **Instructions:** Paste the following system prompt into the **Instructions** box:
     ```text
     You are an Enterprise Operations AI Agent.
     1. Use your Knowledge Base (File Search) to answer questions about SLAs, escalation paths, and Oracle DB UI troubleshooting runbooks.
     2. When asked to check live incidents, departments, or employees, call the `QueryOracleDB` action tool with a read-only SELECT statement on approved tables (SYSTEM_INCIDENTS, DEPARTMENTS, EMPLOYEES). Never query restricted PII columns (SSN, PASSWORD).
     3. When asked to update a Knowledge Base file or update an incident status, always summarize the proposed change clearly and invoke the guarded approval action so a human operator can review and approve it in the UI.
     ```

---

### Step 2: Attach the Knowledge Base Files Manually via Portal UI
1. Still in the **Agent setup** pane in Azure AI Foundry Portal, scroll down to the **Knowledge** section.
2. Click **+ Add** next to **Knowledge**.
3. Select **File search (Vector store)** from the dropdown menu.
4. Choose **Upload files** -> select **Local files**.
5. Browse to `/home/mli/projects/foundry-agent-demo/knowledge_base/` and select:
   * `oracle_db_runbook.md`
   * `sla_and_escalation_guide.md`
6. Click **Upload and save**.
   * *What happens in UI:* The portal creates a managed **Vector Store** and displays the **Vector store ID** (e.g., `vs_abc123...`) under the **Knowledge** section.

---

### Step 3: Attach the Oracle DB & KB Update Actions with Guardrails via UI (Azure Logic Apps)
In Azure AI Foundry Portal, the easiest manual UI way to connect **Oracle DB** and **Knowledge Base File Updates** with a **Human-in-the-Loop (HITL) approval guardrail**—without writing custom code—is using **Azure Logic Apps Actions** directly from the Agent UI:

#### 3A. Add the Guarded Oracle DB Query Action in UI
1. In the **Agent setup** pane, scroll down to the **Actions** section and click **+ Add**.
2. Select **Azure Logic Apps** from the tool catalog.
3. Click **Create a new Logic App workflow** -> choose **Request / Response** template:
   * **Workflow Name:** `QueryOracleDB_Guarded`
   * **Step 1 (Trigger):** **When an HTTP request is received** (accepts JSON `{ "table_name": "SYSTEM_INCIDENTS", "severity_filter": "P1" }`).
     > **UI Guardrail Tip:** Instead of passing raw SQL strings from the LLM, define fixed dropdown inputs (`table_name`, `severity_filter`, `status_filter`) in the trigger schema so the agent **cannot** perform `DROP`/`DELETE` or SQL injection.
   * **Step 2 (Action):** Click **+ Add an action** -> search for **Oracle Database** -> select **Get rows (V2)**.
   * **Step 3 (Connection):** Enter your Oracle Server (`host:1521/service_name`), authentication type, username, and password in the UI connection dialog, then select table `SYSTEM_INCIDENTS`.
   * **Step 4 (Response):** Add a **Response** action returning the rows as JSON (`Body:Outputs('Get_rows')`).
4. Click **Save** and attach the Logic App to your Agent in the Foundry Portal.

#### 3B. Add the Knowledge Base File Update Action with Human Approval (HITL) in UI
1. In the **Agent setup** pane under **Actions**, click **+ Add** -> **Azure Logic Apps**.
2. Create a workflow named `UpdateKBFile_WithHumanApproval`:
   * **Step 1 (Trigger):** **When an HTTP request is received** with JSON schema:
     ```json
     {
       "type": "object",
       "properties": {
         "filename": { "type": "string" },
         "proposed_markdown_section": { "type": "string" },
         "change_summary": { "type": "string" }
       },
       "required": ["filename", "proposed_markdown_section", "change_summary"]
     }
     ```
   * **Step 2 (Automated Guardrail Condition in UI):** Click **+ Add an action** -> **Condition** control:
     * Check that `filename` ends with `.md` AND `proposed_markdown_section` does **not** contain `password` or `sk-`.
     * In the **False** branch: Add a **Response** step returning `400 Bad Request - Blocked by KB Content Guardrail`.
   * **Step 3 (Human-in-the-Loop Approval Gate in UI):** Inside the **True** branch, click **+ Add an action** -> search for **Approvals (Teams / Outlook)** -> select **Start and wait for an approval**:
     * **Approval Type:** `Approve/Reject - First to respond`
     * **Title:** `AI Agent KB Update Request: @{triggerBody()?['filename']}`
     * **Details:** Show `change_summary` and `proposed_markdown_section` so the engineer can inspect the exact diff in Teams/Email.
   * **Step 4 (Condition on Outcome):** Add a **Condition** checking if `Outcome` is equal to `Approve`:
     * **If Approved:** Add **Azure Blob Storage -> Update blob** (or **SharePoint -> Update file**) to write the new Markdown content to your Knowledge Base source folder, followed by **Azure AI Search -> Run indexer** to refresh the KB.
     * **If Rejected:** Return `Operation cancelled by human reviewer`.
3. Click **Save** and attach the tool to your Agent.

---

### Step 4: Configure Content Safety Guardrails in Azure AI Foundry Portal UI
1. In the left navigation menu of Azure AI Foundry Portal, go to **Guardrails + controls** (under **Protect and govern**).
2. Click **+ Create content filter** (or select your existing filter).
3. In the UI sliders:
   * Enable **Prompt Shields (Jailbreak & Indirect Prompt Injection detection)** -> set Action to **Annotate and block**.
   * Enable **Protected Material detection** and **PII filter** -> set Action to **Block**.
4. Under **Apply to deployments**, check your `gpt-4o` deployment used by the Agent and click **Save**.

---

## Part 2: Manual Engineer Walkthrough in the Local Demo Web UI

If you are presenting or testing using the included local **Streamlit Web UI** (`app.py`), an engineer can walk through the entire workflow manually in the browser without writing any commands or code during the demo:

### Step 1: Open the Web UI
1. Start the UI server once from terminal:
   ```bash
   cd /home/mli/projects/foundry-agent-demo
   streamlit run app.py
   ```
2. Open the local URL shown in your browser (typically `http://localhost:8501`).

### Step 2: Inspect the Initial State via UI Tabs
1. Click the **`🗄️ Live Oracle DB Inspector`** tab at the top of the page.
   * Verify that incident **`INC-1042`** (`ORA-12514` on `PROD-ORA-WEST`) currently has status **`INVESTIGATING`**.
   * Notice that the `EMPLOYEES` table hides the restricted `SSN` column.
2. Click the **`📚 Live Knowledge Base Files`** tab.
   * Select `oracle_db_runbook.md` in the dropdown.
   * Notice that it currently documents `ORA-00060`, `ORA-01555`, and `ORA-04031`, **not** `ORA-12514`.

### Step 3: Walk Through the 5 Guarded Scenarios by Clicking UI Buttons
Switch back to the **`💬 Agent Chat & HITL Approvals`** tab and click the top buttons in order:

1. **Click Button `1️⃣ Ask KB Question (SLAs & ORA-00060)`**
   * **What to observe in UI:** The agent retrieves the P1/P2 response SLA table from `sla_and_escalation_guide.md` and the manual UI troubleshooting steps for `ORA-00060` from `oracle_db_runbook.md`.
2. **Click Button `2️⃣ Query Oracle DB (Open Incidents)`**
   * **What to observe in UI:** Expand the green **`🟢 Tool Call: query_oracle_db — Guardrail: PASSED`** trace card to show the audience that the SQL query was inspected before running, and view the live Oracle DB table returned in chat.
3. **Click Button `3️⃣ Test SQL Guardrail (Block DROP/SSN)`**
   * **What to observe in UI:** A red **`🔴 Tool Call: query_oracle_db — Guardrail: BLOCKED`** card appears, showing that `DROP TABLE SYSTEM_INCIDENTS` was intercepted and blocked before touching the Oracle database.
   * Check the **Sidebar -> `📋 Guardrail Audit Log`** on the left to see the red `BLOCKED` entry logged with timestamp.
4. **Click Button `5️⃣ Test KB PII Guardrail (Block Secret)`**
   * **What to observe in UI:** A red **`🔴 Tool Call: propose_kb_file_update — Guardrail: BLOCKED`** card appears because the proposed KB update contained an SSN (`123-45-6789`) and API key (`sk-...`).
5. **Click Button `4️⃣ Update KB File + DB (HITL Approval)`**
   * **What to observe in UI:**
     1. The agent pauses execution (`PENDING_HITL`) and displays yellow **`⏸️ Approval Required`** cards at the bottom of the chat.
     2. Review the **Unified Diff Preview (Before vs. After)** right in the UI showing the new `+ ### ORA-12514` manual UI troubleshooting steps being added to `oracle_db_runbook.md`.
     3. Click the **`✅ Approve & Execute`** button on both cards.
     4. Click the **`📚 Live Knowledge Base Files`** tab and **`🗄️ Live Oracle DB Inspector`** tab to show that `oracle_db_runbook.md` now includes the new `ORA-12514` section and `INC-1042` is now **`RESOLVED`**!
     5. To reset everything for the next demo session, click **`🔄 Reset KB Files`** and **`🔄 Reset Oracle DB`** in the left sidebar.

---

## Part 3: Sharing the Agent Outside Your Machine

Neither option below needs this machine or the Streamlit app running.

### Option A: Foundry's preview link (sign-in required)
1. In the **[Foundry portal](https://ai.azure.com)**, open the agent `Oracle-And-KB-Guarded-Agent` and select **Publish**.
2. Choose the preview link. It looks like `https://ai.azure.com/nextgen/agents/<id>/preview?version=<n>`.
3. Anyone opening it signs in with an Azure account that has access to the agent. This is the full agent: Foundry holds incident and Knowledge Base updates until they're approved, so try one update there and check the page shows an approve/deny prompt before relying on it.
4. The link names one agent version. When the app or `deploy_mcp_tools.sh` creates a new version (for example after the instructions change), select **Publish Updates** and share the link it gives.

### Option B: Public chat page (no sign-in)
1. Run `./deploy_mcp_tools.sh` (or `./az_tui.sh` → 10 → 6). It ends with `Public chat page (no sign-in): https://agent-tools-<suffix>.azurewebsites.net/api/chat`.
2. Share that address. Visitors chat with `Oracle-And-KB-Public-Agent`, a read-only copy: it answers from the Knowledge Base, runs read-only database queries and logs questions it can't answer, but can't change anything.
3. Visitors spend your subscription's credit. Each visitor gets 20 messages an hour and everyone together 100 replies a day. `./az_tui.sh` → 10 → 8 shows the limits and how much of them is used; → 9 changes one. In the Azure portal they're under the Function App → **Settings → Environment variables** (`CHAT_PER_VISITOR_PER_HOUR`, `CHAT_DAILY_LIMIT`, `CHAT_MAX_MESSAGE_CHARS`).
   * To find the Function App in the portal, sign in at [portal.azure.com](https://portal.azure.com) with the account that owns the subscription; another account sees nothing, or gets "account does not exist in tenant". `./az_tui.sh` → 10 → 7 prints a direct link that opens in the right directory.
4. Questions the page couldn't answer show up in the Streamlit app's **Feedback Log** tab like any other entry.
