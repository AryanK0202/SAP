"""Aggregate per-server Ansible artifacts into run-level summaries."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)\s+"
    r"skipped=(?P<skipped>\d+)\s+rescued=(?P<rescued>\d+)\s+ignored=(?P<ignored>\d+)\s*$"
)


def parse_recap(log_path: Path) -> dict[str, dict[str, int]]:
    recap: dict[str, dict[str, int]] = {}
    if not log_path.exists():
        return recap
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = RECAP_RE.match(line.strip())
        if match:
            values = match.groupdict()
            host = values.pop("host")
            recap[host] = {key: int(value) for key, value in values.items()}
    return recap


def _read_json(path: Path, default: Any):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _result_counts(results: list[dict[str, Any]]) -> Counter:
    return Counter(str(item.get("status", "ERROR")).upper() for item in results)


def aggregate_run(
    *,
    run_dir: Path,
    normalized: dict[str, Any],
    selected_hosts: list[str] | None,
    ansible_return_code: int,
    command: str,
    run_timestamp: str,
    mode: str = "validation",
) -> dict[str, Any]:
    recap = parse_recap(run_dir / "_controller.log")
    known_servers = normalized.get("servers", {})
    if selected_hosts is None:
        discovered_hosts = {
            path.name
            for path in run_dir.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        }
        discovered_hosts.update(host for host in recap if host != "localhost")
        selected_hosts = sorted(discovered_hosts)

    server_summaries: list[dict[str, Any]] = []
    flattened_results: list[dict[str, Any]] = []

    for server_id in sorted(set(selected_hosts)):
        server_dir = run_dir / server_id
        server_dir.mkdir(parents=True, exist_ok=True)
        source = known_servers.get(server_id, {})
        metadata_path = server_dir / "metadata.json"
        metadata = _read_json(metadata_path, {})
        if not metadata:
            metadata = {
                "server_id": server_id,
                "ansible_host": source.get("address", ""),
                "physical_hostname": source.get("physical_hostname", ""),
                "physical_ip": source.get("physical_ip", ""),
                "environment": source.get("environment", ""),
                "customer": source.get("customer", ""),
                "components": source.get("components", []),
                "sap_instances": source.get("instances", []),
            }
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        results = _read_json(server_dir / "results.json", [])
        if not isinstance(results, list):
            results = []
        counts = _result_counts(results)
        host_recap = recap.get(server_id, {})

        if host_recap.get("unreachable", 0):
            execution_status = "UNREACHABLE"
        elif host_recap.get("failed", 0):
            execution_status = "FAILED"
        elif mode == "discovery" and host_recap:
            execution_status = "DISCOVERED"
        elif (server_dir / "results.json").exists():
            execution_status = "COMPLETED"
        else:
            execution_status = "NOT_COMPLETED"

        if execution_status in {"UNREACHABLE", "FAILED", "NOT_COMPLETED", "DISCOVERED"}:
            overall_status = execution_status
        elif counts["FAIL"] or counts["ERROR"]:
            overall_status = "FAIL"
        elif counts["WARN"] or counts["SKIPPED"]:
            overall_status = "WARN"
        else:
            overall_status = "PASS"

        summary = {
            "server_id": server_id,
            "address": metadata.get("ansible_host", source.get("address", "")),
            "environment": metadata.get("environment", source.get("environment", "")),
            "customer": metadata.get("customer", source.get("customer", "")),
            "components": metadata.get("components", source.get("components", [])),
            "execution_status": execution_status,
            "overall_status": overall_status,
            "pass_count": counts["PASS"],
            "fail_count": counts["FAIL"],
            "error_count": counts["ERROR"],
            "warn_count": counts["WARN"],
            "skipped_count": counts["SKIPPED"],
            "result_count": len(results),
            "report_file": f"{server_id}/report.md" if (server_dir / "report.md").exists() else None,
            "recap": host_recap,
        }
        (server_dir / "status.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        server_summaries.append(summary)

        for result in results:
            flattened_results.append(
                {
                    "server_id": server_id,
                    "environment": summary["environment"],
                    "customer": summary["customer"],
                    "check": result.get("check", ""),
                    "status": result.get("status", ""),
                    "details": result.get("details", ""),
                }
            )

    status_counts = Counter(item["overall_status"] for item in server_summaries)
    totals = {
        "servers": len(server_summaries),
        "pass": sum(item["pass_count"] for item in server_summaries),
        "fail": sum(item["fail_count"] for item in server_summaries),
        "error": sum(item["error_count"] for item in server_summaries),
        "warn": sum(item["warn_count"] for item in server_summaries),
        "skipped": sum(item["skipped_count"] for item in server_summaries),
    }
    summary_data = {
        "run_timestamp": run_timestamp,
        "generated_at": datetime.now().astimezone().isoformat(),
        "command": command,
        "ansible_return_code": ansible_return_code,
        "mode": mode,
        "totals": totals,
        "server_status_counts": dict(sorted(status_counts.items())),
        "servers": server_summaries,
    }
    (run_dir / "_summary.json").write_text(
        json.dumps(summary_data, indent=2) + "\n", encoding="utf-8"
    )

    with (run_dir / "_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "server_id", "address", "environment", "customer", "components",
            "execution_status", "overall_status", "pass_count", "fail_count",
            "error_count", "warn_count", "skipped_count", "result_count", "report_file",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in server_summaries:
            export = {key: row.get(key) for key in fieldnames}
            export["components"] = ",".join(row.get("components", []))
            writer.writerow(export)

    with (run_dir / "_results.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["server_id", "environment", "customer", "check", "status", "details"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flattened_results)

    lines = [
        f"# SAP {'Discovery' if mode == 'discovery' else 'Validation'} Run — {run_timestamp}",
        "",
        f"- **Servers selected:** {totals['servers']}",
        f"- **Ansible return code:** {ansible_return_code}",
        f"- **Check results:** {totals['pass']} pass, {totals['fail']} fail, "
        f"{totals['error']} error, {totals['warn']} warn, {totals['skipped']} skipped",
        "",
        "| Server | Environment | Customer | Components | Execution | Overall | Results |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for item in server_summaries:
        server_link = (
            f"[{item['server_id']}]({item['report_file']})"
            if item.get("report_file")
            else item["server_id"]
        )
        result_text = (
            f"{item['pass_count']}P/{item['fail_count']}F/{item['error_count']}E/"
            f"{item['warn_count']}W/{item['skipped_count']}S"
        )
        lines.append(
            f"| {server_link} | {item['environment']} | {item['customer']} | "
            f"{', '.join(item.get('components', []))} | {item['execution_status']} | "
            f"{item['overall_status']} | {result_text} |"
        )
    (run_dir / "_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_data
