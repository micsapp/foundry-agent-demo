# Oracle Database Operations & Manual UI Troubleshooting Runbook

**Document ID:** KB-ORA-001  
**Last Updated:** 2026-10-01  
**Owner:** Enterprise Database Reliability Engineering (DBRE)  
**Execution Mode:** Manual Engineer UI Walkthrough (Oracle Enterprise Manager Cloud Control & Azure Portal UI)

---

## 1. Overview
This runbook provides step-by-step **manual UI procedures** for on-call engineers diagnosing and resolving production incidents on our core **Oracle Database 23ai / 19c** clusters (`PROD-ORA-EAST` and `PROD-ORA-WEST`).

> **Important Policy:** Automated remediation scripts are disabled in production. Engineers must perform all diagnostic and recovery actions manually through the **Oracle Enterprise Manager (OEM) Cloud Control Web UI** or the **Azure Portal (Oracle Database@Azure) UI** following the click-by-click steps below.

---

## 2. Common Oracle Error Codes & Manual UI Remediation Steps

### ORA-00060: Deadlock Detected While Waiting for Resource
- **Symptoms:** Transactions fail with `ORA-00060`; application alerts show row-level lock contention.
- **Manual UI Diagnostic & Resolution Steps (OEM Cloud Control UI):**
  1. Log in to the **Oracle Enterprise Manager (OEM) Cloud Control UI** in your browser.
  2. Navigate to **Targets** -> **Databases** -> click **`PROD-ORA-EAST`**.
  3. From the top **Performance** dropdown menu, select **Blocking Sessions**.
  4. In the **Blocking Tree** table, identify the root blocking session ID (`SID`) and verify the waiting application module.
  5. Click the **Alert Log / Incident Manager** tab -> download the deadlock graph trace file and attach it to the ticket in `SYSTEM_INCIDENTS`.
  6. **Escalation Check:** Coordinate with the owning department lead before clicking **Kill Session** in the OEM UI (requires DBA Lead approval).

### ORA-01555: Snapshot Too Old
- **Symptoms:** Long-running batch analytics queries fail mid-execution.
- **Manual UI Diagnostic & Resolution Steps (OEM Cloud Control UI):**
  1. In **OEM Cloud Control UI**, open the target database -> click **Administration** -> **Storage** -> **Undo Management**.
  2. Inspect the **Undo Advisor** chart to view the current **Undo Retention Period** and tablespace utilization.
  3. In the **Undo Configuration** panel, manually change **Undo Retention (Seconds)** from `1800` to `7200` in the input box.
  4. Click **Apply** in the top-right corner of the UI and confirm the change in the confirmation modal.

### ORA-04031: Unable to Allocate Bytes of Shared Memory
- **Symptoms:** Intermittent connection failures or PL/SQL package compilation errors in the Shared Pool.
- **Manual UI Diagnostic & Resolution Steps (OEM Cloud Control UI):**
  1. In **OEM Cloud Control UI**, navigate to **Server** -> **Memory Advisors** (under Database Configuration).
  2. Click the **SGA** tab and inspect the **Shared Pool Size Advice** graph.
  3. If immediate emergency mitigation is approved, go to **Administration** -> **Initialization Parameters**, verify current memory allocation, and schedule a **Maximum SGA Size** increase via the **Change Plan UI** for the Sunday maintenance window.

---

## 3. Approved Monitoring Tables for AI Agent Read-Only Checks
The Azure AI Foundry Agent may run guarded read-only `SELECT` queries against the following tables to assist the engineer:
- `SYSTEM_INCIDENTS`: Tracks active and historical infrastructure & database incidents.
- `DEPARTMENTS`: Maps department IDs, cost centers, and on-call escalation owners.
- `EMPLOYEES`: Directory of engineering and operations staff (PII columns such as `SSN` are blocked by guardrail).
