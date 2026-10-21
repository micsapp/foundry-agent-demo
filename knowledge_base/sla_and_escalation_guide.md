# Enterprise Incident SLA & Escalation Guide

**Document ID:** KB-SLA-002  
**Last Updated:** 2026-09-15  
**Owner:** Site Reliability Engineering (SRE)

---

## 1. Severity Definitions & Response SLAs

| Severity | Classification | Initial Response SLA | Resolution Target | Escalation Path |
| :--- | :--- | :--- | :--- | :--- |
| **P1 (Critical)** | Production outage, Oracle DB unreachable, or payment processing halted | **15 minutes** | **2 hours** | On-Call DBA -> Director of Infrastructure -> VP of Engineering |
| **P2 (High)** | Severe latency degradation (>3s query latency) or partial service failure | **30 minutes** | **4 hours** | On-Call DBA -> Team Lead |
| **P3 (Medium)** | Non-critical job failure, staging DB issue, or single-node warning | **2 hours** | **24 hours** | Assigned Engineering Squad |
| **P4 (Low)** | Minor documentation update, schema request, or performance tuning | **1 business day** | **5 business days** | Backlog Triage |

---

## 2. Post-Incident Knowledge Base Update Policy
1. Whenever a **P1 or P2 database incident** is mitigated, the responding engineer or AI Agent must update `oracle_db_runbook.md` with the root cause and troubleshooting steps.
2. **Guardrail Requirement:** Any automated AI Agent update to a Knowledge Base file **must** pass PII/Secret scanning and receive **Human-in-the-Loop (HITL) approval** before being committed to the production Azure AI Foundry Vector Store.
