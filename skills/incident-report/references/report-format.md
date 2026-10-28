# Incident report format

The report lists each matching incident's ID, service, Oracle error code, severity,
status, department and on-call owner. It counts incidents by severity and status.

An incident is open when its status is OPEN, INVESTIGATING or MITIGATED. RESOLVED
and CLOSED incidents count as completed. P1 is the highest severity, followed by
P2, P3 and P4. A missing incident is reported as no matching rows, not a resolved
incident. All-incident reports include both open and completed incidents.

The report is returned as Markdown with structured JSON summary and incident
records. It is not persisted to storage. Secret-like values are masked before
the script receives data and before its result is returned.
