"""Read an incident snapshot as JSON on stdin and write a report as JSON on stdout.

Pure standard-library computation: no database credentials, network calls, or writes.
The Azure Function runner fetches the guarded snapshot before starting this process.
"""

import json
import sys
from collections import Counter


def markdown_cell(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def generate_report(payload: dict) -> dict:
    incidents = payload["incidents"]
    severity = Counter(row["SEVERITY"] for row in incidents)
    status = Counter(row["STATUS"] for row in incidents)
    open_count = sum(row["STATUS"] not in {"RESOLVED", "CLOSED"} for row in incidents)
    incident_id = payload["arguments"].get("incident_id")
    title = f"Incident report: {incident_id}" if incident_id else "Incident report: all incidents"
    lines = [
        f"## {title}",
        "",
        f"**Total:** {len(incidents)} · **Open:** {open_count} · **Completed:** {len(incidents) - open_count}",
        "",
    ]
    if incidents:
        lines += [
            "| Incident | Service | Oracle error | Severity | Status | Department | On-call owner |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        fields = ("INCIDENT_ID", "SERVICE_NAME", "ORACLE_ERROR_CODE", "SEVERITY", "STATUS", "DEPT_NAME", "ONCALL_LEAD")
        lines += ["| " + " | ".join(markdown_cell(row.get(field)) for field in fields) + " |" for row in incidents]
        lines += ["", "**Severity counts:** " + ", ".join(f"{key}: {count}" for key, count in sorted(severity.items()))]
    else:
        lines.append("No matching incidents were found in the current database.")
    lines += ["", "Source: current demo incident database. This report made no database changes."]
    return {
        "report_markdown": "\n".join(lines),
        "summary": {"total": len(incidents), "open": open_count, "completed": len(incidents) - open_count,
                    "by_severity": dict(sorted(severity.items())), "by_status": dict(sorted(status.items()))},
        "incidents": incidents,
    }


if __name__ == "__main__":
    print(json.dumps(generate_report(json.load(sys.stdin)), ensure_ascii=False))
