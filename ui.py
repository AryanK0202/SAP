#!/usr/bin/env python3
"""Minimal localhost UI for the bulk SAP validation runner.

The UI has no third-party Python dependencies. It reads the repository's CSV
inputs and check catalog, opens a browser, and launches ``sap_validate.py``
with a validated argument list.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TRUTHY = {"1", "true", "yes", "y", "on"}
MAX_BODY_BYTES = 1_000_000
MAX_LOG_LINES = 5_000
RUN_DIRECTORY_RE = re.compile(r"^Run directory:\s*(?P<path>.+?)\s*$")
UI_BUILD = "2026.08.04-layout-4-arrows-left"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in TRUTHY


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            return [dict(row) for row in reader]
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {path}") from exc


def _path_within(path: Path, root: Path) -> Path | None:
    """Return the resolved path when it stays below root, otherwise None."""

    try:
        resolved = path.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _artifact_url(path: Path, artifact_root: Path, *, directory: bool = False) -> str:
    resolved = _path_within(path, artifact_root)
    if resolved is None:
        raise ValueError("Artifact path is outside the configured artifact root")
    relative = resolved.relative_to(artifact_root.resolve())
    encoded = "/".join(quote(part, safe="") for part in relative.parts)
    url = "/artifacts/" + encoded
    if directory and not url.endswith("/"):
        url += "/"
    return url


def _is_hidden_artifact(path: Path, artifact_root: Path) -> bool:
    """Return True for dotfiles/dot-directories or OS-hidden artifact entries."""

    resolved = _path_within(path, artifact_root)
    if resolved is None:
        return True
    root = artifact_root.resolve()
    relative = resolved.relative_to(root)
    if not relative.parts:
        return False
    if any(part.startswith(".") for part in relative.parts):
        return True
    try:
        # Windows exposes FILE_ATTRIBUTE_HIDDEN through st_file_attributes.
        return bool(getattr(resolved.stat(), "st_file_attributes", 0) & 0x2)
    except OSError:
        return True


def _format_file_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _read_run_summary(run_dir: Path) -> dict[str, Any] | None:
    summary_path = run_dir / "_summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _artifact_payload(run_dir_value: str | None, artifact_root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "root_path": str(artifact_root),
        "root_url": "/artifacts/",
        "run_path": None,
        "browse_url": None,
        "files": [],
    }
    if not run_dir_value:
        return payload
    run_dir = _path_within(Path(run_dir_value), artifact_root)
    if run_dir is None or not run_dir.is_dir():
        return payload

    payload["run_path"] = str(run_dir)
    payload["browse_url"] = _artifact_url(run_dir, artifact_root, directory=True)
    known_files = [
        ("Summary (Markdown)", "_summary.md"),
        ("Summary (JSON)", "_summary.json"),
        ("Summary (CSV)", "_summary.csv"),
        ("All results (CSV)", "_results.csv"),
        ("Controller log", "_controller.log"),
        ("Run metadata", "_run.json"),
    ]
    for label, filename in known_files:
        path = run_dir / filename
        if path.is_file():
            payload["files"].append(
                {"label": label, "name": filename, "url": _artifact_url(path, artifact_root)}
            )
    return payload


def _open_directory(path: Path) -> None:
    if sys.platform == "darwin":
        command = ["open", str(path)]
    elif os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    else:
        command = ["xdg-open", str(path)]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class TerminalStatus:
    """Render one replaceable terminal line instead of an endless access log."""

    def __init__(self, *, full_access_log: bool = False):
        self.full_access_log = full_access_log
        self.is_tty = sys.stderr.isatty()
        self.lock = threading.Lock()
        self.request_count = 0
        self.last_line = ""
        self.line_visible = False

    def _write_status(self, text: str) -> None:
        width = max(20, shutil.get_terminal_size((120, 24)).columns)
        rendered = text[: max(1, width - 1)]
        sys.stderr.write("\r\033[2K" + rendered)
        sys.stderr.flush()
        self.last_line = text
        self.line_visible = True

    def clear(self) -> None:
        with self.lock:
            if self.is_tty and self.line_visible:
                sys.stderr.write("\r\033[2K")
                sys.stderr.flush()
            self.line_visible = False

    def show(self, text: str) -> None:
        if not self.is_tty or self.full_access_log:
            return
        with self.lock:
            self._write_status(text)

    def request(self, method: str, path: str, status: int, state: "RunState") -> None:
        with self.lock:
            self.request_count += 1
            if self.full_access_log:
                if self.is_tty and self.line_visible:
                    sys.stderr.write("\r\033[2K")
                sys.stderr.write(f'[ui] "{method} {path}" {status}\n')
                sys.stderr.flush()
                self.line_visible = False
                return

            with state.lock:
                running = state.running
                run_number = state.run_number
                return_code = state.return_code
            if running:
                run_text = f"run #{run_number} running"
            elif return_code is None:
                run_text = "idle"
            else:
                run_text = f"run #{run_number} exit {return_code}"
            text = (
                f"UI active | requests {self.request_count} | last {method} {path} {status} | {run_text}"
            )
            if self.is_tty:
                self._write_status(text)
            elif method != "GET" or status >= 400:
                sys.stderr.write(f'[ui] "{method} {path}" {status}\n')
                sys.stderr.flush()


@dataclass(frozen=True)
class UIPaths:
    root: Path
    servers: Path
    instances: Path
    catalog: Path
    defaults: Path
    artifact_root: Path

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "UIPaths":
        root = args.root.resolve()

        def resolve(value: Path) -> Path:
            return value.resolve() if value.is_absolute() else (root / value).resolve()

        return cls(
            root=root,
            servers=resolve(args.servers),
            instances=resolve(args.instances),
            catalog=resolve(args.catalog),
            defaults=resolve(args.defaults),
            artifact_root=resolve(args.artifact_root),
        )


@dataclass
class RepositoryData:
    servers: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    profiles: dict[str, list[str]]
    batch_size: int
    forks: int
    known_server_ids: set[str] = field(repr=False)
    selectable_check_ids: set[str] = field(repr=False)


def load_repository_data(paths: UIPaths) -> RepositoryData:
    server_rows = _read_csv(paths.servers)
    instance_rows = _read_csv(paths.instances)
    catalog = _read_json(paths.catalog)
    defaults = _read_json(paths.defaults)

    components_by_server: dict[str, set[str]] = {}
    for row in instance_rows:
        server_id = (row.get("server_id") or "").strip()
        component = (row.get("component") or "").strip()
        if server_id and component:
            components_by_server.setdefault(server_id, set()).add(component)

    servers: list[dict[str, Any]] = []
    known_server_ids: set[str] = set()
    for row in server_rows:
        server_id = (row.get("server_id") or "").strip()
        if not server_id:
            continue
        if not SERVER_ID_RE.fullmatch(server_id):
            raise ValueError(
                f"Unsupported server_id {server_id!r}; UI-safe IDs may contain letters, "
                "numbers, '.', '_' and '-'."
            )
        servers.append(
            {
                "server_id": server_id,
                "address": (row.get("address") or "").strip(),
                "physical_ip": (row.get("physical_ip") or "").strip(),
                "physical_hostname": (row.get("physical_hostname") or "").strip(),
                "environment": (row.get("environment") or "").strip(),
                "landscape": (row.get("landscape") or "").strip(),
                "credential_profile": (row.get("credential_profile") or "").strip(),
                "components": sorted(components_by_server.get(server_id, set())),
                "enabled": _truthy(row.get("enabled", "true")),
            }
        )
    servers.sort(key=lambda item: item["server_id"].lower())
    known_server_ids = {item["server_id"] for item in servers if item["enabled"]}

    checks_raw = catalog.get("checks")
    profiles_raw = catalog.get("profiles")
    if not isinstance(checks_raw, list) or not isinstance(profiles_raw, dict):
        raise ValueError(f"Catalog {paths.catalog} must contain checks[] and profiles{{}}")

    checks: list[dict[str, Any]] = []
    selectable_check_ids: set[str] = set()
    tag_to_ids: dict[str, list[str]] = {}
    for raw in checks_raw:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        check_id = str(raw["id"])
        tag = raw.get("ansible_tag")
        selectable = bool(tag) and raw.get("implementation_status") == "runs_today"
        if selectable:
            selectable_check_ids.add(check_id)
            tag_to_ids.setdefault(str(tag), []).append(check_id)
        checks.append(
            {
                "id": check_id,
                "category": str(raw.get("category") or "Uncategorized"),
                "task": str(raw.get("task") or ""),
                "scope": str(raw.get("scope") or ""),
                "component": str(raw.get("component") or ""),
                "ansible_tag": str(tag or ""),
                "implementation_status": str(raw.get("implementation_status") or ""),
                "selectable": selectable,
            }
        )
    checks.sort(key=lambda item: (item["category"].lower(), item["id"].lower()))

    profiles: dict[str, list[str]] = {}
    for name, profile in profiles_raw.items():
        if not isinstance(profile, dict):
            continue
        selected_ids: list[str] = []
        for tag in profile.get("tags", []):
            selected_ids.extend(tag_to_ids.get(str(tag), []))
        profiles[str(name)] = sorted(set(selected_ids))

    execution = defaults.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    batch_size = int(execution.get("batch_size", 50))
    forks = int(execution.get("forks", 50))

    return RepositoryData(
        servers=servers,
        checks=checks,
        profiles=profiles,
        batch_size=batch_size,
        forks=forks,
        known_server_ids=known_server_ids,
        selectable_check_ids=selectable_check_ids,
    )


@dataclass
class RunState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: subprocess.Popen[str] | None = None
    running: bool = False
    command: list[str] = field(default_factory=list)
    output: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    started_at: float | None = None
    finished_at: float | None = None
    return_code: int | None = None
    error: str | None = None
    run_number: int = 0
    run_dir: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "command": self.command,
                "output": "".join(self.output),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "return_code": self.return_code,
                "error": self.error,
                "run_number": self.run_number,
                "run_dir": self.run_dir,
            }


def _bounded_int(payload: dict[str, Any], name: str, default: int, maximum: int = 10_000) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 1 or result > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return result


def build_validation_command(
    payload: dict[str, Any],
    *,
    paths: UIPaths,
    data: RepositoryData,
) -> list[str]:
    systems = payload.get("systems", [])
    checks = payload.get("checks", [])
    if not isinstance(systems, list) or not all(isinstance(item, str) for item in systems):
        raise ValueError("systems must be a list of server IDs")
    if not isinstance(checks, list) or not all(isinstance(item, str) for item in checks):
        raise ValueError("checks must be a list of check IDs")

    systems = sorted(set(systems))
    checks = sorted(set(checks))
    profile = str(payload.get("profile", "")).strip()
    unknown_systems = set(systems) - data.known_server_ids
    unknown_checks = set(checks) - data.selectable_check_ids
    if unknown_systems:
        raise ValueError(f"Unknown or disabled server IDs: {', '.join(sorted(unknown_systems))}")
    if unknown_checks:
        raise ValueError(f"Unknown or unavailable check IDs: {', '.join(sorted(unknown_checks))}")
    if profile:
        if profile not in data.profiles:
            raise ValueError(f"Unknown profile: {profile}")
        if checks != sorted(data.profiles[profile]):
            raise ValueError("The selected checks no longer match the chosen profile; use Custom")

    mode = str(payload.get("mode", "validate"))
    valid_modes = {"validate", "discover_validate", "discover_only", "prepare_only"}
    if mode not in valid_modes:
        raise ValueError(f"Unknown mode: {mode}")
    if not systems:
        raise ValueError("Select at least one enabled system")
    if mode in {"validate", "discover_validate"} and not checks:
        raise ValueError("Select at least one automated validation check")

    command = [
        sys.executable,
        str(paths.root / "sap_validate.py"),
        "--servers",
        str(paths.servers),
        "--instances",
        str(paths.instances),
        "--catalog",
        str(paths.catalog),
        "--defaults",
        str(paths.defaults),
        "--artifact-root",
        str(paths.artifact_root),
        "--limit",
        ",".join(systems),
        "--batch-size",
        str(_bounded_int(payload, "batch_size", data.batch_size)),
        "--forks",
        str(_bounded_int(payload, "forks", data.forks)),
    ]

    if profile:
        command += ["--profile", profile]
    elif checks:
        command += ["--checks", ",".join(checks)]
    if mode == "discover_validate":
        command.append("--discover")
    elif mode == "discover_only":
        command.append("--discover-only")
    elif mode == "prepare_only":
        command.append("--prepare-only")

    boolean_flags = {
        "save_raw_outputs": "--save-raw-outputs",
        "strict": "--strict",
        "enable_incrond": "--enable-incrond",
        "enable_backint": "--enable-backint",
        "dry_run": "--dry-run",
        "syntax_check": "--syntax-check",
        "check_mode": "--check-mode",
    }
    for field_name, flag in boolean_flags.items():
        if payload.get(field_name) is True:
            command.append(flag)

    verbose = payload.get("verbose", 0)
    try:
        verbose_int = int(verbose)
    except (TypeError, ValueError) as exc:
        raise ValueError("verbose must be between 0 and 4") from exc
    if verbose_int < 0 or verbose_int > 4:
        raise ValueError("verbose must be between 0 and 4")
    if verbose_int:
        command.append("-" + "v" * verbose_int)

    return command


def _run_process(
    state: RunState,
    command: list[str],
    cwd: Path,
    artifact_root: Path,
) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=(os.name != "nt"),
        )
        with state.lock:
            state.process = process
        assert process.stdout is not None
        for line in process.stdout:
            run_dir_match = RUN_DIRECTORY_RE.match(line.strip())
            if run_dir_match:
                candidate = Path(run_dir_match.group("path")).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                resolved = _path_within(candidate, artifact_root)
                if resolved is not None:
                    with state.lock:
                        state.run_dir = str(resolved)
            with state.lock:
                state.output.append(line)
        return_code = process.wait()
        with state.lock:
            state.return_code = return_code
    except Exception as exc:  # Last-resort visibility for controller failures.
        with state.lock:
            state.error = str(exc)
            state.output.append(f"UI controller error: {exc}\n")
            state.return_code = 1
    finally:
        with state.lock:
            state.running = False
            state.process = None
            state.finished_at = time.time()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.terminate()


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAP Validation</title>
<style>
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --surface: #ffffff;
  --surface-muted: #f8fafc;
  --border: #d8dee6;
  --border-strong: #b8c2cf;
  --text: #17202a;
  --muted: #5f6b7a;
  --primary: #175cd3;
  --primary-hover: #1249aa;
  --danger: #b42318;
  --danger-bg: #fff1f0;
  --success: #067647;
  --warning: #a15c00;
  --shadow: none;
  --radius: 0;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  line-height: 1.45;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
button, input, select { font: inherit; }
button, select, input[type="search"], input[type="number"] { min-height: 36px; }
button {
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: var(--surface);
  color: var(--text);
  padding: .45rem .8rem;
  cursor: pointer;
  font-weight: 500;
}
button:hover:not(:disabled) { background: #eef2f6; }
button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible, a:focus-visible {
  outline: 3px solid rgba(23, 92, 211, .22);
  outline-offset: 2px;
}
button:disabled { cursor: not-allowed; opacity: .5; }
button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:hover:not(:disabled) { background: var(--primary-hover); }
button.danger { border-color: #f1b4ae; color: var(--danger); background: var(--danger-bg); }
input[type="search"], input[type="number"] {
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: #fff;
  color: var(--text);
  padding: .4rem .6rem;
}
select {
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  background: #fff;
  color: var(--text);
  padding: .4rem .6rem;
}
.workflow section:nth-of-type(2) select,
.workflow section:nth-of-type(3) select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20viewBox%3D%270%200%2024%2024%27%20fill%3D%27none%27%20stroke%3D%27%23475467%27%20stroke-width%3D%272.5%27%20stroke-linecap%3D%27round%27%20stroke-linejoin%3D%27round%27%3E%3Cpolyline%20points%3D%276%209%2012%2015%2018%209%27/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: .5rem center;
  background-size: 18px 18px;
  padding-left: 2.2rem;
}
input[type="search"] { min-width: min(22rem, 100%); }
input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--primary); vertical-align: -2px; }
.app-shell { width: min(1600px, calc(100% - 32px)); margin: 0 auto 2rem; }
.app-header {
  margin: 0 -16px .9rem;
  padding: .6rem max(16px, calc((100vw - 1600px) / 2));
  background: var(--surface);
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.header-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.app-header h1 { margin: 0; font-size: 1.05rem; font-weight: 600; letter-spacing: -.01em; }
.app-header p { margin: .15rem 0 0; color: var(--muted); font-size: 12.5px; }
.ui-build { display: inline-block; margin-left: .45rem; color: var(--muted); font-size: 11px; font-weight: 500; letter-spacing: .03em; }
.header-status { display: flex; gap: .5rem; align-items: center; }
.status-pill, .count-pill {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  border-radius: 999px;
  padding: .15rem .6rem;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}
.status-pill { color: #175cd3; background: #eef4fe; border: 1px solid #c7d7f5; }
.status-pill.running { color: #a15c00; background: #fef6e6; border-color: #f5d9a6; }
.status-pill.success { color: #067647; background: #eafaf1; border-color: #a9e6c6; }
.status-pill.failed { color: #b42318; background: #fdeeed; border-color: #f1b4ae; }
.workflow { display: grid; gap: .75rem; }
.card {
  margin: 0;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  overflow: hidden;
}
.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: .7rem .9rem .6rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.card-title { display: flex; gap: .75rem; align-items: flex-start; }
.step-number {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  flex: 0 0 24px;
  border-radius: 3px;
  background: #e8f0fe;
  color: var(--primary);
  font-weight: 600;
}
.card h2 { margin: 0; font-size: 1rem; font-weight: 600; }
.card-subtitle { margin: .15rem 0 0; color: var(--muted); font-size: 12.5px; }
.card-body { padding: .75rem .9rem .85rem; }
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: .4rem .6rem;
  align-items: end;
}
.controls + .controls { margin-top: .4rem; }
.control { display: grid; gap: .2rem; min-width: 8.5rem; }
.control.grow { flex: 1 1 20rem; }
.control-label { color: #344054; font-size: 12px; font-weight: 600; }
.params-row {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem 1.5rem;
  align-items: start;
  justify-content: flex-start;
}
.params-row .controls { flex: 0 1 auto; }
.checkbox-grid {
  display: grid;
  grid-template-columns: repeat(4, auto);
  gap: .35rem 1.75rem;
  margin-top: 1.3rem;
  padding: 0;
  border: 0;
  background: none;
}
.checkbox-grid label { display: flex; align-items: center; gap: .4rem; min-height: 24px; white-space: nowrap; }
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
  align-items: center;
  justify-content: space-between;
  margin-bottom: .6rem;
}
.toolbar-left, .toolbar-right { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.system-filter-row {
  display: grid;
  grid-template-columns: minmax(145px, .8fr) minmax(145px, .8fr) minmax(165px, .9fr) minmax(280px, 1.7fr);
  gap: .4rem .6rem;
  align-items: end;
  min-width: 0;
}
.system-filter-row .control { min-width: 0; }
.system-filter-row select,
.system-filter-row input[type="search"] { width: 100%; min-width: 0; }
.system-selection-actions {
  display: flex;
  justify-content: flex-end;
  gap: .5rem;
  margin-top: .5rem;
  margin-bottom: .8rem;
  padding-bottom: .1rem;
}
.count-pill { color: #344054; background: #eef2f6; }
.table-wrap { width: 100%; overflow: auto; border: 1px solid var(--border); border-radius: 0; }
table { border-collapse: separate; border-spacing: 0; width: 100%; min-width: 900px; }
th, td { border-bottom: 1px solid var(--border); padding: .45rem .55rem; text-align: left; vertical-align: top; }
th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #f3f6f9;
  color: #344054;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .01em;
  white-space: nowrap;
}
th button { min-height: 0; border: 0; background: transparent; padding: 0; font: inherit; color: inherit; }
th button:hover:not(:disabled) { background: transparent; color: var(--primary); }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:nth-child(even) { background: #fbfcfd; }
tbody tr:hover { background: #f1f6ff; }
td:first-child, th:first-child { text-align: center; width: 64px; }
.muted { color: var(--muted); }
.hidden { display: none !important; }
.check-groups {
  display: grid;
  gap: .6rem;
  margin-top: .7rem;
}
.check-table-wrap {
  overflow-x: auto;
  scrollbar-gutter: auto;
}
.check-table {
  width: max(100%, 1380px);
  min-width: 1380px;
  max-width: none;
  table-layout: fixed;
}
.check-table th,
.check-table td {
  overflow-wrap: anywhere;
  word-break: normal;
}
.check-table th:nth-child(1), .check-table td:nth-child(1) { width: 4.64%; }
.check-table th:nth-child(2), .check-table td:nth-child(2) { width: 15.22%; }
.check-table th:nth-child(3), .check-table td:nth-child(3) { width: 14.13%; }
.check-table th:nth-child(4), .check-table td:nth-child(4) { width: 11.59%; }
.check-table th:nth-child(5), .check-table td:nth-child(5) { width: 10.51%; }
.check-table th:nth-child(6), .check-table td:nth-child(6) { width: 10.51%; }
.check-table th:nth-child(7), .check-table td:nth-child(7) { width: 33.40%; }
details.check-category, details.output-panel {
  border: 1px solid var(--border);
  border-radius: 0;
  background: #fff;
  overflow: hidden;
}
details.check-category > summary, details.output-panel > summary {
  display: flex;
  align-items: center;
  gap: .5rem;
  cursor: pointer;
  list-style: none;
  padding: .5rem .7rem;
  font-weight: 600;
  background: #f8fafc;
}
details.check-category > summary::-webkit-details-marker, details.output-panel > summary::-webkit-details-marker { display: none; }
details.check-category > summary::before, details.output-panel > summary::before {
  content: "▾";
  display: inline-grid;
  place-items: center;
  flex: 0 0 1.45rem;
  width: 1.45rem;
  height: 1.45rem;
  margin-right: .15rem;
  color: var(--muted);
  font-size: 1.45rem;
  font-weight: 400;
  line-height: 1;
  transform-origin: center;
  transition: transform .15s ease;
}
details:not([open]) > summary::before { transform: rotate(-90deg); }
details.check-category > summary::after, details.output-panel > summary::after {
  content: none;
}
details.check-category .table-wrap { border: 0; border-top: 1px solid var(--border); border-radius: 0; }
.run-bar {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: .8rem;
  margin: .6rem 0;
  padding: .6rem .8rem;
  border: 1px solid #c7d7f5;
  border-radius: 0;
  background: #fff;
}
.run-actions { display: flex; gap: .5rem; }
.command-panel { min-width: 0; }
.command-label { display: block; margin-bottom: .15rem; color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
#command {
  display: block;
  max-width: 100%;
  overflow: hidden;
  color: #344054;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.result-overview {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: .8rem;
  align-items: center;
  padding: .6rem .7rem;
  border: 1px solid var(--border);
  border-radius: 0;
  background: var(--surface-muted);
}
.result-actions { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; justify-content: flex-end; }
#quickSummary { font-size: 15px; }
#runDirectory { margin-top: .4rem; overflow-wrap: anywhere; }
.artifact-links { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }
.artifact-links a {
  display: inline-flex;
  align-items: center;
  min-height: 34px;
  padding: .35rem .65rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #fff;
  font-weight: 500;
}
.badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: .15rem .5rem;
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: .035em;
}
.badge.pass, .badge.success, .badge.green { background: #dcfae6; color: var(--success); }
.badge.fail, .badge.error, .badge.failed, .badge.red { background: #fee4e2; color: var(--danger); }
.badge.warn, .badge.warning, .badge.yellow { background: #fef0c7; color: var(--warning); }
.badge.neutral { background: #eef2f6; color: #475467; }
.output-section { margin-top: .6rem; }
pre {
  margin: 0;
  min-height: 15rem;
  max-height: 38rem;
  overflow: auto;
  border-top: 1px solid #263244;
  background: #101828;
  color: #d1e0ff;
  padding: 1rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.empty-note { padding: 1rem; text-align: center; color: var(--muted); }
@media (max-width: 760px) {
  .app-shell { width: min(100% - 20px, 1600px); }
  .app-header { margin-left: -10px; margin-right: -10px; }
  .card-header, .card-body { padding-left: .8rem; padding-right: .8rem; }
  .run-bar { grid-template-columns: 1fr; bottom: 6px; }
  .result-overview { grid-template-columns: 1fr; }
  .result-actions { justify-content: flex-start; }
  input[type="search"] { min-width: 100%; width: 100%; }
  .control.grow { flex-basis: 100%; }
  .system-filter-row { grid-template-columns: 1fr; }
  .system-selection-actions { justify-content: flex-start; }
  .checkbox-grid { grid-template-columns: repeat(2, auto); margin-top: .6rem; }
}
</style>
</head>
<body>
<header class="app-header">
  <div class="header-row">
    <div>
      <h1>SAP Validation</h1>
      <p>Select systems and checks, run validation, and review generated reports. <span class="ui-build">Build 2026.08.04-layout-4-arrows-left</span></p>
    </div>
    <div class="header-status"><span id="headerStatus" class="status-pill">Loading</span></div>
  </div>
</header>

<main class="app-shell">
<div class="workflow">
<section class="card" aria-labelledby="systemsHeading">
  <div class="card-header">
    <div class="card-title">
      <span class="step-number">1</span>
      <div><h2 id="systemsHeading">Systems</h2><p class="card-subtitle">Filter the inventory and select the target systems.</p></div>
    </div>
    <span id="systemCount" class="count-pill">0 selected</span>
  </div>
  <div class="card-body">
    <div class="system-filter-row">
      <label class="control"><span class="control-label">Environment</span><select id="environmentFilter"><option value="">All environments</option></select></label>
      <label class="control"><span class="control-label">Landscape</span><select id="landscapeFilter"><option value="">All landscapes</option></select></label>
      <label class="control"><span class="control-label">Component</span><select id="componentFilter"><option value="">All components</option></select></label>
      <label class="control"><span class="control-label">Search systems</span><input id="systemSearch" type="search" placeholder="Server, hostname, address, component"></label>
    </div>
    <div class="system-selection-actions">
      <button type="button" id="selectVisibleSystems">Select visible</button>
      <button type="button" id="clearVisibleSystems">Clear visible</button>
    </div>
    <div class="table-wrap">
      <table id="systemsTable">
      <thead><tr>
      <th>Select</th>
      <th><button type="button" data-sort="server_id">System</button></th>
      <th><button type="button" data-sort="environment">Environment</button></th>
      <th><button type="button" data-sort="landscape">Landscape</button></th>
      <th><button type="button" data-sort="components">Components</button></th>
      <th><button type="button" data-sort="physical_hostname">Hostname</button></th>
      <th><button type="button" data-sort="address">Address</button></th>
      <th><button type="button" data-sort="enabled">Enabled</button></th>
      </tr></thead>
      <tbody></tbody>
      </table>
    </div>
  </div>
</section>

<section class="card" aria-labelledby="checksHeading">
  <div class="card-header">
    <div class="card-title">
      <span class="step-number">2</span>
      <div><h2 id="checksHeading">Validation checks</h2><p class="card-subtitle">Use a profile or build a custom set of automated checks.</p></div>
    </div>
    <span id="checkCount" class="count-pill">0 selected</span>
  </div>
  <div class="card-body">
    <div class="toolbar">
      <div class="toolbar-left">
        <label class="control"><span class="control-label">Profile</span><select id="profile"><option value="">Custom selection</option></select></label>
        <label class="control grow"><span class="control-label">Search checks</span><input id="checkSearch" type="search" placeholder="Category, check ID, tag, or description"></label>
        <label><input id="showUnavailable" type="checkbox"> Show unavailable checks</label>
      </div>
      <div class="toolbar-right">
        <button type="button" id="selectVisibleChecks">Select visible</button>
        <button type="button" id="clearVisibleChecks">Clear visible</button>
      </div>
    </div>
    <div id="checks" class="check-groups"></div>
  </div>
</section>

<section class="card" aria-labelledby="parametersHeading">
  <div class="card-header">
    <div class="card-title">
      <span class="step-number">3</span>
      <div><h2 id="parametersHeading">Run parameters</h2><p class="card-subtitle">Control discovery, concurrency, diagnostics, and optional validations.</p></div>
    </div>
  </div>
  <div class="card-body">
    <div class="params-row">
      <div class="controls">
        <label class="control"><span class="control-label">Mode</span>
          <select id="mode">
            <option value="validate">Validate</option>
            <option value="discover_validate">Discover, then validate</option>
            <option value="discover_only">Discovery only</option>
            <option value="prepare_only">Prepare inventory only</option>
          </select>
        </label>
        <label class="control"><span class="control-label">Batch size</span><input id="batchSize" type="number" min="1"></label>
        <label class="control"><span class="control-label">Forks</span><input id="forks" type="number" min="1"></label>
        <label class="control"><span class="control-label">Verbosity</span><select id="verbose"><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
      </div>
      <div class="checkbox-grid">
        <label><input id="saveRawOutputs" type="checkbox"> Save raw outputs</label>
        <label><input id="strict" type="checkbox"> Strict validation</label>
        <label><input id="enableIncrond" type="checkbox"> Enable incrond validation</label>
        <label><input id="enableBackint" type="checkbox"> Enable Backint validation</label>
        <label><input id="dryRun" type="checkbox"> Dry run</label>
        <label><input id="syntaxCheck" type="checkbox"> Syntax check</label>
        <label><input id="checkMode" type="checkbox"> Ansible check mode</label>
      </div>
    </div>
  </div>
</section>
</div>

<div class="run-bar">
  <div class="run-actions">
    <button class="primary" type="button" id="run">Run validation</button>
    <button class="danger" type="button" id="stop" disabled>Stop</button>
  </div>
  <div class="command-panel">
    <span class="command-label">Command</span>
    <code id="command">Command will appear after a run starts.</code>
  </div>
</div>

<section class="card" aria-labelledby="resultsHeading">
  <div class="card-header">
    <div class="card-title">
      <span class="step-number">4</span>
      <div><h2 id="resultsHeading">Results</h2><p class="card-subtitle">Open reports, browse raw outputs, or inspect the aggregate summary.</p></div>
    </div>
    <strong id="status" class="muted">Loading…</strong>
  </div>
  <div class="card-body">
    <div class="result-overview">
      <div>
        <strong id="quickSummary">No completed run yet.</strong>
        <div id="runDirectory" class="muted"></div>
      </div>
      <div class="result-actions">
        <a href="/artifacts/" target="_blank" rel="noopener">Browse all outputs</a>
        <a id="browseOutput" class="hidden" target="_blank" rel="noopener">Browse current run</a>
        <button type="button" id="openOutput" disabled>Open folder</button>
      </div>
    </div>
    <div id="artifactLinks" class="artifact-links"></div>
    <div class="table-wrap hidden" id="summaryTableWrap" style="margin-top:.9rem">
      <table id="summaryTable">
      <thead><tr><th>System</th><th>Environment</th><th>Overall</th><th>Quick summary</th><th>Report</th></tr></thead>
      <tbody></tbody>
      </table>
    </div>
  </div>
</section>

<section class="output-section">
  <details class="output-panel" open>
    <summary>Live controller output</summary>
    <pre id="output" aria-live="polite"></pre>
  </details>
</section>
</main>

<script>
'use strict';
let config = null;
let systemSort = {key: 'server_id', asc: true};

function el(id) { return document.getElementById(id); }
function selectedValues(selector) { return [...document.querySelectorAll(selector + ':checked')].map(x => x.value); }
function uniqueSorted(values) { return [...new Set(values.filter(Boolean))].sort((a,b) => a.localeCompare(b)); }
function addOptions(select, values) {
  for (const value of values) {
    const option = document.createElement('option');
    option.value = value; option.textContent = value; select.appendChild(option);
  }
}
function statusClass(value) {
  const normalized = String(value || '').trim().toLowerCase();
  if (['pass','passed','success','green','ok'].some(x => normalized.includes(x))) return 'success';
  if (['fail','failed','error','red'].some(x => normalized.includes(x))) return 'failed';
  if (['warn','warning','yellow'].some(x => normalized.includes(x))) return 'warning';
  return 'neutral';
}
function statusBadge(value) {
  const span = document.createElement('span');
  span.className = `badge ${statusClass(value)}`;
  span.textContent = value || '—';
  return span;
}

function renderSystems() {
  const selected = new Set(selectedValues('.systemChoice'));
  const body = el('systemsTable').querySelector('tbody');
  body.textContent = '';
  const rows = [...config.servers].sort((a,b) => {
    let av = a[systemSort.key], bv = b[systemSort.key];
    if (Array.isArray(av)) av = av.join(', ');
    if (Array.isArray(bv)) bv = bv.join(', ');
    const result = String(av ?? '').localeCompare(String(bv ?? ''), undefined, {numeric: true, sensitivity: 'base'});
    return systemSort.asc ? result : -result;
  });
  for (const server of rows) {
    const tr = document.createElement('tr');
    tr.dataset.environment = server.environment;
    tr.dataset.landscape = server.landscape;
    tr.dataset.components = server.components.join(' ');
    tr.dataset.search = Object.values(server).flat().join(' ').toLowerCase();
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.className = 'systemChoice'; checkbox.value = server.server_id;
    checkbox.disabled = !server.enabled; checkbox.checked = selected.has(server.server_id);
    checkbox.setAttribute('aria-label', `Select ${server.server_id}`);
    checkbox.addEventListener('change', updateCounts);
    const cells = [checkbox, server.server_id, server.environment || '—', server.landscape || '—',
      server.components.join(', ') || '—', server.physical_hostname || '—', server.address || '—', server.enabled ? 'Yes' : 'No'];
    for (const value of cells) {
      const td = document.createElement('td');
      if (value instanceof Node) td.appendChild(value); else td.textContent = value;
      tr.appendChild(td);
    }
    if (!server.enabled) tr.classList.add('muted');
    body.appendChild(tr);
  }
  applySystemFilters();
}

function applySystemFilters() {
  const environment = el('environmentFilter').value;
  const landscape = el('landscapeFilter').value;
  const component = el('componentFilter').value;
  const search = el('systemSearch').value.trim().toLowerCase();
  for (const row of el('systemsTable').querySelectorAll('tbody tr')) {
    const visible = (!environment || row.dataset.environment === environment)
      && (!landscape || row.dataset.landscape === landscape)
      && (!component || row.dataset.components.split(' ').includes(component))
      && (!search || row.dataset.search.includes(search));
    row.classList.toggle('hidden', !visible);
  }
  updateCounts();
}

function groupChecks() {
  const groups = new Map();
  for (const check of config.checks) {
    if (!groups.has(check.category)) groups.set(check.category, []);
    groups.get(check.category).push(check);
  }
  return groups;
}

function renderChecks() {
  const container = el('checks');
  container.textContent = '';
  for (const [category, checks] of groupChecks()) {
    const details = document.createElement('details');
    details.open = true;
    details.className = 'check-category';
    details.dataset.category = category.toLowerCase();
    const summary = document.createElement('summary');
    const categoryBox = document.createElement('input');
    categoryBox.type = 'checkbox'; categoryBox.className = 'categoryChoice';
    categoryBox.setAttribute('aria-label', `Select visible checks in ${category}`);
    categoryBox.addEventListener('click', event => event.stopPropagation());
    categoryBox.addEventListener('change', () => {
      for (const box of details.querySelectorAll('.checkChoice:not(:disabled)')) {
        const row = box.closest('tr');
        if (!row.classList.contains('hidden')) box.checked = categoryBox.checked;
      }
      el('profile').value = '';
      updateCounts();
    });
    const categoryName = document.createElement('span');
    categoryName.textContent = category;
    const categoryMeta = document.createElement('span');
    categoryMeta.className = 'muted';
    categoryMeta.style.fontWeight = '600';
    categoryMeta.textContent = `${checks.filter(x => x.selectable).length} available`;
    summary.append(categoryBox, categoryName, categoryMeta);
    details.appendChild(summary);

    const wrap = document.createElement('div');
    wrap.className = 'table-wrap check-table-wrap';
    const table = document.createElement('table');
    table.className = 'check-table';
    table.innerHTML = '<colgroup><col style="width:4.64%"><col style="width:15.22%"><col style="width:14.13%"><col style="width:11.59%"><col style="width:10.51%"><col style="width:10.51%"><col style="width:33.40%"></colgroup><thead><tr><th>Select</th><th>Check ID</th><th>Tag</th><th>Component</th><th>Scope</th><th>Status</th><th>Description</th></tr></thead>';
    const tbody = document.createElement('tbody');
    for (const check of checks) {
      const tr = document.createElement('tr');
      tr.dataset.search = [check.category, check.id, check.ansible_tag, check.component, check.scope, check.task, check.implementation_status].join(' ').toLowerCase();
      tr.dataset.selectable = String(check.selectable);
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.className = 'checkChoice'; checkbox.value = check.id;
      checkbox.disabled = !check.selectable;
      checkbox.setAttribute('aria-label', `Select ${check.id}`);
      checkbox.addEventListener('change', () => { el('profile').value = ''; updateCounts(); });
      const values = [checkbox, check.id, check.ansible_tag || '—', check.component || '—', check.scope || '—'];
      for (const value of values) {
        const td = document.createElement('td');
        if (value instanceof Node) td.appendChild(value); else td.textContent = value;
        tr.appendChild(td);
      }
      const statusCell = document.createElement('td');
      statusCell.appendChild(statusBadge(check.selectable ? 'Available' : (check.implementation_status || 'Unavailable')));
      tr.appendChild(statusCell);
      const descriptionCell = document.createElement('td');
      descriptionCell.textContent = check.task || '—';
      tr.appendChild(descriptionCell);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody); wrap.appendChild(table); details.appendChild(wrap); container.appendChild(details);
  }
  installCheckTableScrollSync();
  applyCheckFilters();
}

let checkScrollSyncActive = false;
function installCheckTableScrollSync() {
  for (const wrap of document.querySelectorAll('.check-table-wrap')) {
    wrap.addEventListener('scroll', () => {
      if (checkScrollSyncActive) return;
      checkScrollSyncActive = true;
      const left = wrap.scrollLeft;
      for (const other of document.querySelectorAll('.check-table-wrap')) {
        if (other !== wrap && other.scrollLeft !== left) other.scrollLeft = left;
      }
      requestAnimationFrame(() => { checkScrollSyncActive = false; });
    }, {passive: true});
  }
}

function applyCheckFilters() {
  const search = el('checkSearch').value.trim().toLowerCase();
  const showUnavailable = el('showUnavailable').checked;
  for (const details of el('checks').querySelectorAll('details')) {
    let visibleCount = 0;
    for (const row of details.querySelectorAll('tbody tr')) {
      const visible = (showUnavailable || row.dataset.selectable === 'true') && (!search || row.dataset.search.includes(search));
      row.classList.toggle('hidden', !visible);
      if (visible) visibleCount++;
    }
    details.classList.toggle('hidden', visibleCount === 0);
  }
  updateCounts();
}

function setProfile(name) {
  const selected = new Set(config.profiles[name] || []);
  for (const box of document.querySelectorAll('.checkChoice')) box.checked = selected.has(box.value);
  updateCounts();
}

function updateCounts() {
  const selectedSystems = selectedValues('.systemChoice');
  const visibleSystems = [...document.querySelectorAll('#systemsTable tbody tr:not(.hidden)')].length;
  el('systemCount').textContent = `${selectedSystems.length} selected / ${visibleSystems} visible`;
  const selectedChecks = selectedValues('.checkChoice');
  const visibleChecks = [...document.querySelectorAll('#checks tbody tr:not(.hidden)')].length;
  el('checkCount').textContent = `${selectedChecks.length} selected / ${visibleChecks} visible`;
  for (const details of document.querySelectorAll('#checks details')) {
    const boxes = [...details.querySelectorAll('.checkChoice:not(:disabled)')];
    const visible = boxes.filter(box => !box.closest('tr').classList.contains('hidden'));
    const chosen = visible.filter(box => box.checked).length;
    const category = details.querySelector('.categoryChoice');
    category.checked = visible.length > 0 && chosen === visible.length;
    category.indeterminate = chosen > 0 && chosen < visible.length;
  }
}

function setVisible(selector, checked) {
  for (const box of document.querySelectorAll(selector)) {
    const row = box.closest('tr');
    if (!box.disabled && row && !row.classList.contains('hidden')) box.checked = checked;
  }
  updateCounts();
}

function payload() {
  return {
    systems: selectedValues('.systemChoice'),
    checks: selectedValues('.checkChoice'),
    profile: el('profile').value,
    mode: el('mode').value,
    batch_size: Number(el('batchSize').value),
    forks: Number(el('forks').value),
    verbose: Number(el('verbose').value),
    save_raw_outputs: el('saveRawOutputs').checked,
    strict: el('strict').checked,
    enable_incrond: el('enableIncrond').checked,
    enable_backint: el('enableBackint').checked,
    dry_run: el('dryRun').checked,
    syntax_check: el('syntaxCheck').checked,
    check_mode: el('checkMode').checked
  };
}

async function post(path, body = {}) {
  const response = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-SAP-UI-Token': config.ui_token}, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function run() {
  try {
    await post('/api/run', payload());
    await refreshStatus();
  } catch (error) {
    alert(error.message);
  }
}

async function stop() {
  try { await post('/api/stop'); await refreshStatus(); } catch (error) { alert(error.message); }
}

async function openOutputFolder() {
  try { await post('/api/open-output'); } catch (error) { alert(error.message); }
}

function quickResultText(item) {
  return `${item.pass_count || 0} pass · ${item.fail_count || 0} fail · ${item.error_count || 0} error · ${item.warn_count || 0} warn · ${item.skipped_count || 0} skipped`;
}

function renderResults(state) {
  const artifacts = state.artifacts || {};
  const browse = el('browseOutput');
  browse.classList.toggle('hidden', !artifacts.browse_url);
  if (artifacts.browse_url) browse.href = artifacts.browse_url;
  el('openOutput').disabled = !artifacts.run_path;
  el('runDirectory').textContent = artifacts.run_path ? `Output folder: ${artifacts.run_path}` : '';

  const links = el('artifactLinks');
  links.textContent = '';
  for (const file of artifacts.files || []) {
    const anchor = document.createElement('a');
    anchor.href = file.url; anchor.textContent = file.label;
    anchor.target = '_blank'; anchor.rel = 'noopener';
    links.appendChild(anchor);
  }

  const table = el('summaryTable');
  const tableWrap = el('summaryTableWrap');
  const body = table.querySelector('tbody');
  body.textContent = '';
  const summary = state.summary;
  if (!summary || !summary.totals) {
    tableWrap.classList.add('hidden');
    if (state.running) el('quickSummary').textContent = 'Validation is running. The summary will appear when aggregation completes.';
    else if (artifacts.run_path) el('quickSummary').textContent = 'The output folder was created, but this mode has no aggregate summary yet.';
    else el('quickSummary').textContent = 'No completed run yet.';
    return;
  }

  const totals = summary.totals;
  el('quickSummary').textContent = `${totals.pass || 0} pass · ${totals.fail || 0} fail · ${totals.error || 0} error · ${totals.warn || 0} warn · ${totals.skipped || 0} skipped across ${totals.servers || 0} system(s)`;
  for (const item of summary.servers || []) {
    const tr = document.createElement('tr');
    for (const value of [item.server_id, item.environment || '—']) {
      const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
    }
    const statusCell = document.createElement('td');
    statusCell.appendChild(statusBadge(item.overall_status || 'Unknown'));
    tr.appendChild(statusCell);
    const quickCell = document.createElement('td'); quickCell.textContent = quickResultText(item); tr.appendChild(quickCell);
    const reportCell = document.createElement('td');
    if (item.report_url) {
      const anchor = document.createElement('a');
      anchor.href = item.report_url; anchor.textContent = 'Open report';
      anchor.target = '_blank'; anchor.rel = 'noopener'; reportCell.appendChild(anchor);
    } else {
      reportCell.textContent = '—';
    }
    tr.appendChild(reportCell); body.appendChild(tr);
  }
  tableWrap.classList.toggle('hidden', body.children.length === 0);
}

function renderRunState(state) {
  el('run').disabled = state.running;
  el('stop').disabled = !state.running;
  const headerStatus = el('headerStatus');
  let status;
  if (state.running) {
    status = `Run #${state.run_number} running`;
    headerStatus.className = 'status-pill running';
  } else if (state.return_code === null) {
    status = 'Idle';
    headerStatus.className = 'status-pill';
  } else if (state.return_code === 0) {
    status = `Run #${state.run_number} completed`;
    headerStatus.className = 'status-pill success';
  } else {
    status = `Run #${state.run_number} failed (exit ${state.return_code})`;
    headerStatus.className = 'status-pill failed';
  }
  if (state.error) status += ` — ${state.error}`;
  headerStatus.textContent = status;
  el('status').textContent = status;
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status');
    const state = await response.json();
    renderRunState(state);
    el('command').textContent = state.command.length ? state.command.join(' ') : 'Command will appear after a run starts.';
    el('command').title = state.command.join(' ');
    renderResults(state);
    const output = el('output');
    const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 30;
    output.textContent = state.output || 'Controller output will appear here.';
    if (atBottom) output.scrollTop = output.scrollHeight;
  } catch (error) {
    el('status').textContent = 'UI connection error';
    el('headerStatus').textContent = 'Connection error';
    el('headerStatus').className = 'status-pill failed';
  }
}

async function init() {
  const response = await fetch('/api/config');
  config = await response.json();
  addOptions(el('environmentFilter'), uniqueSorted(config.servers.map(x => x.environment)));
  addOptions(el('landscapeFilter'), uniqueSorted(config.servers.map(x => x.landscape)));
  addOptions(el('componentFilter'), uniqueSorted(config.servers.flatMap(x => x.components)));
  addOptions(el('profile'), Object.keys(config.profiles).sort());
  el('batchSize').value = config.defaults.batch_size;
  el('forks').value = config.defaults.forks;
  renderSystems(); renderChecks();

  for (const id of ['environmentFilter','landscapeFilter','componentFilter']) el(id).addEventListener('change', applySystemFilters);
  el('systemSearch').addEventListener('input', applySystemFilters);
  el('checkSearch').addEventListener('input', applyCheckFilters);
  el('showUnavailable').addEventListener('change', applyCheckFilters);
  el('profile').addEventListener('change', event => { if (event.target.value) setProfile(event.target.value); });
  el('selectVisibleSystems').addEventListener('click', () => setVisible('.systemChoice', true));
  el('clearVisibleSystems').addEventListener('click', () => setVisible('.systemChoice', false));
  el('selectVisibleChecks').addEventListener('click', () => { setVisible('.checkChoice', true); el('profile').value = ''; });
  el('clearVisibleChecks').addEventListener('click', () => { setVisible('.checkChoice', false); el('profile').value = ''; });
  el('run').addEventListener('click', run); el('stop').addEventListener('click', stop);
  el('openOutput').addEventListener('click', openOutputFolder);
  for (const button of document.querySelectorAll('#systemsTable th button')) {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      if (systemSort.key === key) systemSort.asc = !systemSort.asc; else systemSort = {key, asc: true};
      renderSystems();
    });
  }
  updateCounts(); await refreshStatus(); setInterval(refreshStatus, 1000);
}

init().catch(error => {
  el('status').textContent = error.message;
  el('headerStatus').textContent = 'Configuration error';
  el('headerStatus').className = 'status-pill failed';
});
</script>
</body>
</html>
'''


class SAPUIHandler(BaseHTTPRequestHandler):
    server_version = "SAPValidationUI/1.0"

    @property
    def app(self) -> "SAPUIServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        try:
            status = int(args[1])
        except (IndexError, TypeError, ValueError):
            status = 0
        self.app.terminal.request(self.command, urlparse(self.path).path, status, self.app.state)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self) -> None:
        body = HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: HTTPStatus = HTTPStatus.NO_CONTENT) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _artifact_target(self, request_path: str) -> Path | None:
        raw_relative = unquote(request_path.removeprefix("/artifacts/")).lstrip("/")
        candidate = self.app.paths.artifact_root / raw_relative
        target = _path_within(candidate, self.app.paths.artifact_root)
        if target is None or _is_hidden_artifact(target, self.app.paths.artifact_root):
            return None
        return target

    def _send_artifact_directory(self, directory: Path) -> None:
        root = self.app.paths.artifact_root.resolve()
        relative = directory.resolve().relative_to(root)
        page_title = "Validation outputs" if not relative.parts else relative.name
        location = "Artifact root" if not relative.parts else relative.as_posix()

        breadcrumb_items = ['<a href="/artifacts/">Outputs</a>']
        current = root
        for part in relative.parts:
            current /= part
            href = _artifact_url(current, root, directory=True)
            breadcrumb_items.append(
                f'<a href="{html.escape(href, quote=True)}">{html.escape(part)}</a>'
            )
        breadcrumbs = '<span class="separator">/</span>'.join(breadcrumb_items)

        try:
            children = sorted(
                (
                    child
                    for child in directory.iterdir()
                    if not _is_hidden_artifact(child, root)
                ),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError:
            self._send_json(
                {"error": "Could not read artifact directory"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        rows: list[str] = []
        for child in children:
            resolved = _path_within(child, root)
            if resolved is None or _is_hidden_artifact(resolved, root):
                continue
            try:
                metadata = resolved.stat()
                is_directory = resolved.is_dir()
            except OSError:
                continue
            href = _artifact_url(resolved, root, directory=is_directory)
            display_name = child.name + ("/" if is_directory else "")
            extension = child.suffix.lstrip(".").upper()
            kind = "Folder" if is_directory else (extension or "File")
            size = "—" if is_directory else _format_file_size(metadata.st_size)
            modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(metadata.st_mtime))
            icon = "DIR" if is_directory else "FILE"
            search_value = f"{child.name} {kind}".lower()
            rows.append(
                '<tr data-search="{}">'
                '<td class="name-cell"><span class="file-icon" aria-hidden="true">{}</span>'
                '<a href="{}">{}</a></td>'
                '<td><span class="type-badge">{}</span></td>'
                '<td class="size-cell">{}</td>'
                '<td class="modified-cell">{}</td>'
                '</tr>'.format(
                    html.escape(search_value, quote=True),
                    icon,
                    html.escape(href, quote=True),
                    html.escape(display_name),
                    html.escape(kind),
                    html.escape(size),
                    html.escape(modified),
                )
            )

        parent_link = ""
        if relative.parts:
            parent_link = (
                f'<a class="button secondary" href="{html.escape(_artifact_url(directory.parent, root, directory=True), quote=True)}">'
                '← Parent folder</a>'
            )

        empty_row = (
            '<tr id="emptyRow"><td colspan="4" class="empty">No visible files or folders in this directory.</td></tr>'
            if not rows
            else '<tr id="emptyRow" hidden><td colspan="4" class="empty">No matching files or folders.</td></tr>'
        )
        document = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)} · SAP Validation</title>
<style>
:root {{
  --bg: #f4f6f8; --surface: #fff; --border: #d8dee6; --text: #17202a;
  --muted: #667085; --primary: #175cd3; --shadow: none;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }}
a {{ color: var(--primary); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
header {{ background: var(--surface); color: var(--text); padding: .6rem max(20px, calc((100vw - 1200px) / 2)); border-bottom: 1px solid var(--border); }}
.header-row {{ display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }}
header strong {{ font-size: 1rem; font-weight: 600; }}
header a {{ color: var(--primary); }}
main {{ width: min(1200px, calc(100% - 32px)); margin: 1rem auto 2rem; }}
.breadcrumbs {{ display: flex; flex-wrap: wrap; gap: .4rem; color: var(--muted); margin-bottom: .6rem; }}
.separator {{ color: #98a2b3; }}
.page-actions {{ display: flex; align-items: center; gap: .55rem; margin: 0 0 .6rem; }}
.card {{ border: 1px solid var(--border); border-radius: 0; background: var(--surface); overflow: hidden; }}
.card-header {{ padding: .7rem .9rem; border-bottom: 1px solid var(--border); background: var(--surface); }}
h1 {{ margin: 0; font-size: 1.1rem; font-weight: 600; }}
.subtitle {{ margin: .2rem 0 0; color: var(--muted); overflow-wrap: anywhere; }}
.toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: .6rem; flex-wrap: wrap; padding: .6rem .9rem; border-bottom: 1px solid var(--border); }}
.toolbar-left, .toolbar-right {{ display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }}
input[type="search"] {{ width: min(28rem, 75vw); min-height: 36px; border: 1px solid #b8c2cf; border-radius: 3px; padding: .4rem .65rem; font: inherit; }}
.button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 38px; padding: .46rem .8rem; border: 1px solid #b8c2cf; border-radius: 8px; font-weight: 500; }}
.button.secondary {{ color: var(--text); background: #fff; }}
.button.primary {{ color: #fff; background: var(--primary); border-color: var(--primary); }}
.button.primary:hover {{ background: #1249aa; text-decoration: none; }}
.return-button {{ min-width: 220px; }}
.count {{ color: var(--muted); font-weight: 500; }}
.table-wrap {{ overflow: auto; }}
table {{ width: 100%; border-collapse: collapse; min-width: 650px; }}
th, td {{ padding: .5rem .65rem; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ background: #f3f6f9; color: #344054; font-size: 12px; font-weight: 600; white-space: nowrap; }}
tbody tr:nth-child(even) {{ background: #fbfcfd; }}
tbody tr:hover {{ background: #f1f6ff; }}
tbody tr:last-child td {{ border-bottom: 0; }}
.name-cell {{ width: 55%; font-weight: 500; }}
.file-icon {{ display: inline-grid; place-items: center; min-width: 2.8rem; margin-right: .5rem; border-radius: 5px; padding: .08rem .3rem; background: #eef2f6; color: #475467; font-size: 10px; font-weight: 500; }}
.type-badge {{ display: inline-flex; border-radius: 999px; padding: .14rem .48rem; background: #eef2f6; color: #475467; font-size: 11px; font-weight: 500; }}
.size-cell, .modified-cell {{ color: var(--muted); white-space: nowrap; }}
.empty {{ padding: 2.5rem; text-align: center; color: var(--muted); }}
.footer-note {{ padding: .75rem 1.1rem; border-top: 1px solid var(--border); color: var(--muted); font-size: 12px; background: #fbfcfd; }}
@media (max-width: 650px) {{ main {{ width: min(100% - 20px, 1200px); }} .modified-cell {{ white-space: normal; }} }}
</style>
</head>
<body>
<header><div class="header-row"><strong>SAP Validation Outputs</strong></div></header>
<main>
  <nav class="breadcrumbs" aria-label="Breadcrumb">{breadcrumbs}</nav>
  <div class="page-actions">
    <a class="button primary return-button" href="/">← Return to validation UI</a>
  </div>
  <section class="card">
    <div class="card-header">
      <h1>{html.escape(page_title)}</h1>
      <p class="subtitle">{html.escape(location)}</p>
    </div>
    <div class="toolbar">
      <div class="toolbar-left">
        <input id="fileSearch" type="search" placeholder="Filter files and folders" aria-label="Filter files and folders">
        <span id="entryCount" class="count">{len(rows)} item(s)</span>
      </div>
      <div class="toolbar-right">{parent_link}</div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th></tr></thead>
        <tbody>{''.join(rows)}{empty_row}</tbody>
      </table>
    </div>
    <div class="footer-note">Hidden files and hidden directories are excluded from this browser.</div>
  </section>
</main>
<script>
'use strict';
const search = document.getElementById('fileSearch');
const count = document.getElementById('entryCount');
const empty = document.getElementById('emptyRow');
const rows = [...document.querySelectorAll('tbody tr[data-search]')];
search.addEventListener('input', () => {{
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  for (const row of rows) {{
    const show = !query || row.dataset.search.includes(query);
    row.hidden = !show;
    if (show) visible++;
  }}
  count.textContent = `${{visible}} item(s)`;
  empty.hidden = visible !== 0;
}});
</script>
</body>
</html>'''.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(document)

    def _send_artifact(self, request_path: str) -> None:
        target = self._artifact_target(request_path)
        if target is None or not target.exists():
            self._send_json({"error": "Artifact not found"}, HTTPStatus.NOT_FOUND)
            return
        if target.is_dir():
            self._send_artifact_directory(target)
            return
        if not target.is_file():
            self._send_json({"error": "Artifact is not a regular file"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            size = target.stat().st_size
            handle = target.open("rb")
        except OSError:
            self._send_json({"error": "Could not read artifact"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        with handle:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile)

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("X-SAP-UI-Token") != self.app.ui_token:
            raise PermissionError("Invalid UI token")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body is too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html()
        elif path == "/favicon.ico":
            self._send_empty()
        elif path == "/api/config":
            self._send_json(
                {
                    "servers": self.app.data.servers,
                    "checks": self.app.data.checks,
                    "profiles": self.app.data.profiles,
                    "defaults": {"batch_size": self.app.data.batch_size, "forks": self.app.data.forks},
                    "ui_token": self.app.ui_token,
                }
            )
        elif path == "/api/status":
            self._send_json(self.app.status_payload())
        elif path == "/artifacts":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", "/artifacts/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/artifacts/"):
            self._send_artifact(path)
        else:
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json_body()
            if path == "/api/run":
                self._handle_run(payload)
            elif path == "/api/stop":
                self._handle_stop()
            elif path == "/api/open-output":
                self._handle_open_output()
            else:
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _handle_run(self, payload: dict[str, Any]) -> None:
        command = build_validation_command(payload, paths=self.app.paths, data=self.app.data)
        with self.app.state.lock:
            if self.app.state.running:
                raise ValueError("A validation run is already active")
            self.app.state.running = True
            self.app.state.command = command
            self.app.state.output.clear()
            self.app.state.output.append("$ " + " ".join(command) + "\n")
            self.app.state.started_at = time.time()
            self.app.state.finished_at = None
            self.app.state.return_code = None
            self.app.state.error = None
            self.app.state.run_dir = None
            self.app.state.run_number += 1
        thread = threading.Thread(
            target=_run_process,
            args=(
                self.app.state,
                command,
                self.app.paths.root,
                self.app.paths.artifact_root,
            ),
            daemon=True,
        )
        thread.start()
        self._send_json({"ok": True, "command": command}, HTTPStatus.ACCEPTED)

    def _handle_stop(self) -> None:
        with self.app.state.lock:
            process = self.app.state.process
            running = self.app.state.running
        if not running or process is None:
            raise ValueError("No validation run is active")
        _terminate_process(process)
        self._send_json({"ok": True}, HTTPStatus.ACCEPTED)

    def _handle_open_output(self) -> None:
        with self.app.state.lock:
            run_dir_value = self.app.state.run_dir
        if not run_dir_value:
            raise ValueError("No output folder is available for the current run")
        run_dir = _path_within(Path(run_dir_value), self.app.paths.artifact_root)
        if run_dir is None or not run_dir.is_dir():
            raise ValueError("The output folder is no longer available")
        try:
            _open_directory(run_dir)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"Could not open the output folder: {exc}") from exc
        self._send_json({"ok": True, "path": str(run_dir)})


class SAPUIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        paths: UIPaths,
        data: RepositoryData,
        *,
        full_access_log: bool = False,
    ):
        super().__init__(address, SAPUIHandler)
        self.paths = paths
        self.data = data
        self.state = RunState()
        self.ui_token = os.urandom(24).hex()
        self.terminal = TerminalStatus(full_access_log=full_access_log)

    def status_payload(self) -> dict[str, Any]:
        snapshot = self.state.snapshot()
        artifacts = _artifact_payload(snapshot.get("run_dir"), self.paths.artifact_root)
        summary = None
        run_path = artifacts.get("run_path")
        if run_path:
            run_dir = Path(str(run_path))
            summary = _read_run_summary(run_dir)
            if summary:
                for item in summary.get("servers", []):
                    if not isinstance(item, dict):
                        continue
                    report_file = item.get("report_file")
                    if isinstance(report_file, str) and report_file:
                        report_path = _path_within(run_dir / report_file, self.paths.artifact_root)
                        if report_path is not None and report_path.is_file():
                            item["report_url"] = _artifact_url(report_path, self.paths.artifact_root)
        snapshot["summary"] = summary
        snapshot["artifacts"] = artifacts
        return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open a minimal browser UI for sap_validate.py")
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root")
    parser.add_argument("--servers", type=Path, default=Path("inputs/servers.csv"))
    parser.add_argument("--instances", type=Path, default=Path("inputs/instances.csv"))
    parser.add_argument("--catalog", type=Path, default=Path("checks_catalog.json"))
    parser.add_argument("--defaults", type=Path, default=Path("config/defaults.json"))
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts"))
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; localhost is recommended")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Print every HTTP request instead of using one replaceable status line",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.port < 0 or args.port > 65535:
        raise SystemExit("--port must be between 0 and 65535")
    paths = UIPaths.from_args(args)
    runner = paths.root / "sap_validate.py"
    if not runner.exists():
        raise SystemExit(f"sap_validate.py not found below repository root: {runner}")
    try:
        data = load_repository_data(paths)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        paths.artifact_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"Could not create artifact root {paths.artifact_root}: {exc}") from exc

    try:
        server = SAPUIServer(
            (args.host, args.port),
            paths,
            data,
            full_access_log=args.access_log,
        )
    except OSError as exc:
        raise SystemExit(f"Could not start UI on {args.host}:{args.port}: {exc}") from exc
    actual_host, actual_port = server.server_address[:2]
    browser_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    url = f"http://{browser_host}:{actual_port}/"
    print(f"SAP Validation UI: {url}", flush=True)
    print(f"UI build: {UI_BUILD}", flush=True)
    print("Press Ctrl+C to stop the UI server.", flush=True)
    server.terminal.show("UI active | requests 0 | idle")
    if not args.no_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        server.terminal.clear()
        print("Stopping UI.")
    finally:
        server.terminal.clear()
        with server.state.lock:
            process = server.state.process
        if process is not None and process.poll() is None:
            _terminate_process(process)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())