#!/usr/bin/env python3
"""Minimal localhost UI for the bulk SAP validation runner.

Artifact browser build: expandable GitHub-style tree with folder icons and
a full-height split preview pane.

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
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
TRUTHY = {"1", "true", "yes", "y", "on"}
MAX_BODY_BYTES = 1_000_000
MAX_LOG_LINES = 5_000
MAX_PREVIEW_BYTES = 1_000_000
RUN_DIRECTORY_RE = re.compile(r"^Run directory:\s*(?P<path>.+?)\s*$")
UI_BUILD = "2026.08.06-artifact-tree-v10-vscode-code-blocks"


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
              "group": str(raw.get("group") or "Uncategorized"),
              "contributor": str(raw.get("contributor") or "Unknown"),
              "task": str(raw.get("task") or ""),
              "scope": str(raw.get("scope") or ""),
              "component": str(raw.get("component") or ""),
              "ansible_tag": str(tag or ""),
              "implementation_status": str(raw.get("implementation_status") or ""),
              "selectable": selectable,
          }
      )
    checks.sort(
        key=lambda item: (
            item["category"].lower(),
            item["group"].lower(),
            item["id"].lower(),
        )
    )

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
button { min-height: 36px; }
select, input[type="search"], input[type="number"] { min-height: 32px; }
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
  border-radius: 3px;
  background: #fff;
  color: var(--text);
  padding: .25rem .55rem;
}
select {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  background-color: #fff;
  background-image: url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20viewBox%3D%270%200%2016%2010%27%3E%3Cpath%20d%3D%27M1%201h14L8%209z%27%20fill%3D%27%23475467%27/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right .65rem center;
  background-size: 12px 8px;
  color: var(--text);
  padding: .25rem 1.9rem .25rem .55rem;
}
input[type="search"] { min-width: min(22rem, 100%); }
input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--primary); vertical-align: -2px; }
.app-shell { width: min(1800px, calc(100% - 32px)); margin: 0 auto 2rem; }
.app-header {
  margin: 0 0 .9rem;
  padding: .55rem 0;
  background: var(--surface);
  color: var(--text);
  border-bottom: 1px solid var(--border);
}
.header-row {
  display: flex;
  width: min(1800px, calc(100% - 32px));
  margin: 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.app-header h1 { margin: 0; font-size: 1.05rem; font-weight: 600; letter-spacing: -.01em; }
.app-header p { margin: .15rem 0 0; color: var(--muted); font-size: 12.5px; }
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
.workflow,
.run-bar,
.output-section,
main > .card {
  width: 100%;
}
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
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: .45rem 1rem;
  align-items: start;
}
.params-row .controls {
  flex-wrap: nowrap;
  flex: 0 0 auto;
}
.checkbox-grid {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: .35rem 1.15rem;
  margin: 0 0 .15rem;
  padding: 0;
  border: 0;
  background: none;
}
.checkbox-grid label {
  display: flex;
  align-items: center;
  gap: .35rem;
  min-height: 24px;
  white-space: nowrap;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
  align-items: center;
  justify-content: space-between;
  margin-bottom: .6rem;
}
.toolbar-left, .toolbar-right { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.toolbar-right.visibility-actions { gap: .4rem; }
.system-filter-row {
  display: grid;
  grid-template-columns:
    minmax(145px, 165px)
    minmax(145px, 165px)
    minmax(175px, 195px)
    minmax(280px, 430px)
    1fr
    auto;
  gap: .4rem .55rem;
  align-items: end;
  min-width: 0;
  margin-bottom: .6rem;
}
.system-filter-row .control { min-width: 0; }
.system-filter-row select,
.system-filter-row input[type="search"] { width: 100%; min-width: 0; }
.visibility-actions {
  display: flex;
  gap: .4rem;
  white-space: nowrap;
}
.visibility-actions button {
  min-height: 32px;
  padding: .25rem .6rem;
}
.system-selection-actions {
  grid-column: 6;
  align-items: end;
  justify-content: flex-end;
  margin: 0;
  padding: 0;
}
.count-pill { color: #344054; background: #eef2f6; }
.table-wrap { width: 100%; overflow: auto; border: 1px solid var(--border); border-radius: 0; }
table { border-collapse: separate; border-spacing: 0; width: 100%; min-width: 900px; }
th, td { border-bottom: 1px solid var(--border); padding: .45rem .48rem; text-align: left; vertical-align: top; }
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
.check-tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 0;
  margin: 0 0 .75rem;
  border-bottom: 1px solid var(--border-strong);
  overflow-x: auto;
}

.check-tab {
  min-height: 42px;
  border: 0;
  border-bottom: 3px solid transparent;
  border-radius: 0;
  background: transparent;
  color: var(--muted);
  font-weight: 600;
  white-space: nowrap;
}

.check-tab:hover:not(:disabled) {
  background: #f8fafc;
}

.check-tab.active {
  border-bottom-color: var(--primary);
  color: var(--primary);
  background: #f8fafc;
}

.check-tab-panel {
  display: grid;
  gap: .6rem;
}

.check-tab-empty {
  padding: 1.5rem;
  border: 1px solid var(--border);
  background: var(--surface-muted);
  color: var(--muted);
  text-align: center;
}
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
  width: 100%;
  min-width: 1180px;
  max-width: none;
  table-layout: fixed;
}
.check-table th,
.check-table td {
  overflow-wrap: anywhere;
  word-break: normal;
}
.check-table th:nth-child(1), .check-table td:nth-child(1) { width: 52px; }
.check-table th:nth-child(2), .check-table td:nth-child(2) { width: 210px; }
.check-table th:nth-child(3), .check-table td:nth-child(3) { width: 195px; }
.check-table th:nth-child(4), .check-table td:nth-child(4) { width: 145px; }
.check-table th:nth-child(5), .check-table td:nth-child(5) { width: 135px; }
.check-table th:nth-child(6), .check-table td:nth-child(6) { width: 135px; white-space: nowrap; }
.check-table th:nth-child(7), .check-table td:nth-child(7) { width: auto; }
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
  width: max-content;
  max-width: none;
  flex-shrink: 0;
  border-radius: 999px;
  padding: .1rem .3rem;
  font-size: 9px;
  font-weight: 400;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
  white-space: nowrap;
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
@media (max-width: 1100px) and (min-width: 761px) {
  .system-filter-row {
    grid-template-columns:
      minmax(125px, 145px)
      minmax(125px, 145px)
      minmax(145px, 165px)
      minmax(220px, 320px)
      1fr
      auto;
    gap: .4rem;
    margin-bottom: .55rem;
  }
  .system-selection-actions { gap: .3rem; }
  .system-selection-actions button { padding-left: .45rem; padding-right: .45rem; }
}
@media (max-width: 760px) {
  .app-shell { width: min(100% - 20px, 1280px); }
  .header-row { width: min(100% - 20px, 1280px); }
  .card-header, .card-body { padding-left: .8rem; padding-right: .8rem; }
  .run-bar { grid-template-columns: 1fr; bottom: 6px; }
  .result-overview { grid-template-columns: 1fr; }
  .result-actions { justify-content: flex-start; }
  input[type="search"] { min-width: 100%; width: 100%; }
  .control.grow { flex-basis: 100%; }
  .system-filter-row { grid-template-columns: 1fr; margin-bottom: .55rem; }
  .system-selection-actions { grid-column: 1; justify-content: flex-start; }
  .params-row { grid-template-columns: 1fr; align-items: start; }
  .params-row .controls { flex-wrap: wrap; }
  .checkbox-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, auto));
    margin-top: .5rem;
  }
}
</style>
</head>
<body>
<header class="app-header">
  <div class="header-row">
    <div>
      <h1>SAP Validation</h1>
      <p>Select systems and checks, run validation, and review generated reports.</p>
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
      <div class="system-selection-actions visibility-actions">
        <button type="button" id="selectVisibleSystems">Select visible</button>
        <button type="button" id="clearVisibleSystems">Clear visible</button>
      </div>
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
        <label class="control grow"><span class="control-label">Search checks</span><input id="checkSearch" type="search" placeholder="Group, contributor, check ID, tag, or description"></label>
        <label><input id="showUnavailable" type="checkbox"> Show unavailable checks</label>
      </div>
      <div class="toolbar-right visibility-actions">
        <button type="button" id="selectVisibleChecks">Select visible</button>
        <button type="button" id="clearVisibleChecks">Clear visible</button>
      </div>
    </div>
    <div
      id="checkTabs"
      class="check-tabs"
      role="tablist"
      aria-label="Validation category"
    ></div>

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
        <a href="/artifacts/">Browse all outputs</a>
        <a id="browseOutput" class="hidden">Browse current run</a>
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

const CHECK_CATEGORIES = [
  'Build Validation',
  'Prepatch',
  'Health',
  'Availability'
];

let activeCheckCategory = 'Build Validation';

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

function checksForCategory(category) {
  return config.checks.filter(check => check.category === category);
}

function groupChecks(checks) {
  const groups = new Map();

  for (const check of checks) {
    const group = check.group || 'Uncategorized';

    if (!groups.has(group)) {
      groups.set(group, []);
    }

    groups.get(group).push(check);
  }

  return groups;
}

function renderCheckTabs() {
  const tabs = el('checkTabs');
  tabs.textContent = '';

  for (const category of CHECK_CATEGORIES) {
    const button = document.createElement('button');

    button.type = 'button';
    button.className = 'check-tab';
    button.dataset.category = category;
    button.setAttribute('role', 'tab');

    const available = checksForCategory(category)
      .filter(check => check.selectable)
      .length;

    button.textContent = `${category} (${available})`;

    button.addEventListener('click', () => {
      setActiveCheckCategory(category);
    });

    tabs.appendChild(button);
  }
}

function setActiveCheckCategory(category) {
  activeCheckCategory = category;

  for (const button of el('checkTabs').querySelectorAll('.check-tab')) {
    const active = button.dataset.category === category;

    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  }

  for (const panel of el('checks').querySelectorAll('.check-tab-panel')) {
    panel.classList.toggle(
      'hidden',
      panel.dataset.category !== category
    );
  }

  applyCheckFilters();
}

function renderChecks() {
  renderCheckTabs();

  const container = el('checks');
  container.textContent = '';

  for (const category of CHECK_CATEGORIES) {
    const panel = document.createElement('div');

    panel.className = 'check-tab-panel';
    panel.dataset.category = category;
    panel.setAttribute('role', 'tabpanel');

    const categoryChecks = checksForCategory(category);

    if (categoryChecks.length === 0) {
      const empty = document.createElement('div');

      empty.className = 'check-tab-empty';
      empty.textContent = `No ${category} checks have been added yet.`;

      panel.appendChild(empty);
      container.appendChild(panel);
      continue;
    }

    for (const [group, checks] of groupChecks(categoryChecks)) {
      const details = document.createElement('details');

      details.open = true;
      details.className = 'check-category';
      details.dataset.group = group.toLowerCase();

      const summary = document.createElement('summary');

      const groupBox = document.createElement('input');

      groupBox.type = 'checkbox';
      groupBox.className = 'groupChoice';

      groupBox.setAttribute(
        'aria-label',
        `Select visible checks in ${group}`
      );

      groupBox.addEventListener('click', event => {
        event.stopPropagation();
      });

      groupBox.addEventListener('change', () => {
        for (
          const box
          of details.querySelectorAll('.checkChoice:not(:disabled)')
        ) {
          const row = box.closest('tr');

          if (!row.classList.contains('hidden')) {
            box.checked = groupBox.checked;
          }
        }

        el('profile').value = '';
        updateCounts();
      });

      const groupName = document.createElement('span');
      groupName.textContent = group;

      const groupMeta = document.createElement('span');

      groupMeta.className = 'muted';
      groupMeta.style.fontWeight = '600';

      groupMeta.textContent =
        `${checks.filter(check => check.selectable).length} available`;

      summary.append(
        groupBox,
        groupName,
        groupMeta
      );

      details.appendChild(summary);

      const wrap = document.createElement('div');

      wrap.className = 'table-wrap check-table-wrap';

      const table = document.createElement('table');

      table.className = 'check-table';

      table.innerHTML = `
        <colgroup>
          <col style="width:52px">
          <col style="width:195px">
          <col style="width:195px">
          <col style="width:130px">
          <col style="width:115px">
          <col style="width:125px">
          <col style="width:160px">
          <col>
        </colgroup>
        <thead>
          <tr>
            <th>Select</th>
            <th>Check ID</th>
            <th>Tag</th>
            <th>Component</th>
            <th>Scope</th>
            <th>Contributor</th>
            <th>Status</th>
            <th>Description</th>
          </tr>
        </thead>
      `;

      const tbody = document.createElement('tbody');

      for (const check of checks) {
        const tr = document.createElement('tr');

        tr.dataset.search = [
          check.category,
          check.group,
          check.contributor,
          check.id,
          check.ansible_tag,
          check.component,
          check.scope,
          check.task,
          check.implementation_status
        ].join(' ').toLowerCase();

        tr.dataset.selectable = String(check.selectable);

        const checkbox = document.createElement('input');

        checkbox.type = 'checkbox';
        checkbox.className = 'checkChoice';
        checkbox.value = check.id;
        checkbox.disabled = !check.selectable;

        checkbox.setAttribute(
          'aria-label',
          `Select ${check.id}`
        );

        checkbox.addEventListener('change', () => {
          el('profile').value = '';
          updateCounts();
        });

        const values = [
          checkbox,
          check.id,
          check.ansible_tag || '—',
          check.component || '—',
          check.scope || '—',
          check.contributor || '—'
        ];

        for (const value of values) {
          const td = document.createElement('td');

          if (value instanceof Node) {
            td.appendChild(value);
          } else {
            td.textContent = value;
          }

          tr.appendChild(td);
        }

        const statusCell = document.createElement('td');

        statusCell.appendChild(
          statusBadge(
            check.selectable
              ? 'Available'
              : (check.implementation_status || 'Unavailable')
          )
        );

        tr.appendChild(statusCell);

        const descriptionCell = document.createElement('td');
        descriptionCell.textContent = check.task || '—';

        tr.appendChild(descriptionCell);

        tbody.appendChild(tr);
      }

      table.appendChild(tbody);
      wrap.appendChild(table);
      details.appendChild(wrap);
      panel.appendChild(details);
    }

    container.appendChild(panel);
  }

  installCheckTableScrollSync();

  setActiveCheckCategory(activeCheckCategory);
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
  const selectedChecks = selectedValues('.checkChoice');

  const activePanel = [
    ...document.querySelectorAll('.check-tab-panel')
  ].find(
    panel => panel.dataset.category === activeCheckCategory
  );

  const visibleChecks = activePanel
    ? [...activePanel.querySelectorAll('tbody tr')]
        .filter(row => !row.classList.contains('hidden'))
        .length
    : 0;

  el('checkCount').textContent =
    `${selectedChecks.length} selected / ${visibleChecks} visible`;

  // Update each group's "select all" checkbox.
  for (const details of document.querySelectorAll('#checks details')) {
    const boxes = [
      ...details.querySelectorAll('.checkChoice:not(:disabled)')
    ];

    const visible = boxes.filter(
      box => !box.closest('tr').classList.contains('hidden')
    );

    const chosen = visible.filter(
      box => box.checked
    ).length;

    const group = details.querySelector('.groupChoice');

    if (!group) {
      continue;
    }

    group.checked =
      visible.length > 0 &&
      chosen === visible.length;

    group.indeterminate =
      chosen > 0 &&
      chosen < visible.length;
  }
}

function setVisible(selector, checked) {
  for (const box of document.querySelectorAll(selector)) {
    const row = box.closest('tr');

    if (!row || box.disabled || row.classList.contains('hidden')) {
      continue;
    }

    if (selector === '.checkChoice') {
      const panel = box.closest('.check-tab-panel');

      if (
        panel &&
        panel.dataset.category !== activeCheckCategory
      ) {
        continue;
      }
    }

    box.checked = checked;
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
      reportCell.appendChild(anchor);
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

    def _artifact_target_from_relative(self, relative_value: str) -> Path | None:
        raw_relative = unquote(relative_value or "").lstrip("/")
        candidate = self.app.paths.artifact_root / raw_relative
        target = _path_within(candidate, self.app.paths.artifact_root)
        if target is None or _is_hidden_artifact(target, self.app.paths.artifact_root):
            return None
        return target

    def _artifact_relative(self, target: Path) -> str:
        root = self.app.paths.artifact_root.resolve()
        relative = target.resolve().relative_to(root)
        return relative.as_posix() if relative.parts else ""

    def _artifact_raw_url(self, target: Path, *, download: bool = False) -> str:
        relative = target.resolve().relative_to(self.app.paths.artifact_root.resolve())
        encoded = "/".join(quote(part, safe="") for part in relative.parts)
        prefix = "/artifact-download/" if download else "/artifact-raw/"
        return prefix + encoded

    def _send_artifact_browser(self, initial_target: Path) -> None:
        root = self.app.paths.artifact_root.resolve()
        target = _path_within(initial_target, root)
        if target is None or not target.exists() or _is_hidden_artifact(target, root):
            self._send_json({"error": "Artifact not found"}, HTTPStatus.NOT_FOUND)
            return

        initial_relative = self._artifact_relative(target)
        initial_is_file = target.is_file()
        document = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Validation outputs · SAP Validation</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f8fa;
  --surface: #ffffff;
  --surface-muted: #f6f8fa;
  --border: #d0d7de;
  --border-muted: #d8dee4;
  --text: #1f2328;
  --muted: #656d76;
  --primary: #0969da;
  --primary-hover: #0550ae;
  --selected: #ddf4ff;
  --code-bg: #f6f8fa;
}
* { box-sizing: border-box; }
html, body { height: 100%; min-height: 0; }
body {
  height: 100vh;
  min-height: 0;
  margin: 0;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
button, input { font: inherit; }
button, .button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 32px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-muted);
  color: var(--text);
  padding: .3rem .7rem;
  cursor: pointer;
  font-weight: 500;
  text-decoration: none;
}
button:hover, .button:hover { background: #eef1f4; text-decoration: none; }
button.primary, .button.primary { background: var(--primary); border-color: var(--primary); color: #fff; }
button.primary:hover, .button.primary:hover { background: var(--primary-hover); }
button:focus-visible, a:focus-visible, input:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

header {
  background: var(--surface);
  color: var(--text);
  border-bottom: 1px solid var(--border);
}

.header-row {
  width: min(1800px, calc(100% - 32px));
  min-height: 56px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.header-title {
  display: flex;
  align-items: center;
  gap: .65rem;
  color: var(--text);
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: -.01em;
}

.header-mark {
  color: var(--text);
  font: 700 18px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.header-row .button {
  background: #fff;
  border-color: #8c959f;
  color: var(--text);
}

.header-row .button:hover {
  background: var(--surface-muted);
  border-color: #6e7781;
}

main {
  width: min(1600px, calc(100% - 32px));
  height: calc(100vh - 56px);
  min-height: 0;
  margin: 0 auto;
  padding: .65rem 0 .75rem;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.repo-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: .75rem;
  flex-wrap: wrap;
  margin-bottom: .6rem;
}
.repo-title { margin: 0; font-size: 1.15rem; font-weight: 600; }
.repo-subtitle { color: var(--muted); font-size: 12px; }
.explorer {
  --sidebar-width: 25%;

  flex: 1 1 auto;
  display: grid;


  grid-template-columns:
    minmax(220px, var(--sidebar-width))
    1px
    minmax(320px, 1fr);

  height: auto;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface);
  overflow: hidden;
}

.sidebar {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-right: 0;
  background: var(--surface);
}

.explorer-splitter {
  position: relative;
  z-index: 2;
  justify-self: center;
  align-self: stretch;

  width: 7px;
  min-width: 7px;

  cursor: col-resize;
  touch-action: none;
  background: transparent;
}

.explorer-splitter::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 50%;

  width: 1px;
  background: var(--border);
  transform: translateX(-50%);
  transition: background-color 80ms ease;
}

.explorer-splitter:hover::before,
.explorer-splitter.dragging::before {
  background: #8c959f;
}

.explorer-splitter:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: -2px;
}

body.resizing-sidebar {
  cursor: col-resize;
  user-select: none;
}

body.resizing-sidebar iframe {
  pointer-events: none;
}

.sidebar-toolbar { padding: .7rem; border-bottom: 1px solid var(--border); background: var(--surface-muted); }
.breadcrumbs { display: flex; align-items: center; gap: .28rem; flex-wrap: wrap; margin-bottom: .55rem; }
.breadcrumbs button {
  min-height: 0;
  border: 0;
  background: transparent;
  color: var(--primary);
  padding: 0;
  font-weight: 600;
}
.breadcrumbs button:hover { background: transparent; text-decoration: underline; }
.separator { color: var(--muted); }
.search { width: 100%; min-height: 34px; border: 1px solid var(--border); border-radius: 6px; padding: .35rem .55rem; background: #fff; }
#entryList { flex: 1 1 auto; min-height: 0; overflow: auto; }
.tree-list { margin: 0; padding: 0; list-style: none; }
.tree-list .tree-list {
  margin-left: 17px;
  padding-left: 7px;
  border-left: 1px solid var(--border-muted);
}
.tree-item { min-width: 0; }
.tree-row {
  position: relative;
  width: 100%;
  min-height: 34px;
  display: grid;
  grid-template-columns: 16px 20px minmax(0, 1fr) auto;
  align-items: center;
  gap: .35rem;
  border: 0;
  border-radius: 6px;
  background: transparent;
  text-align: left;
  padding: .24rem .55rem;
  font-weight: 400;
}
.tree-row:hover { background: var(--surface-muted); }
.tree-row.active-folder { background: #f3f6f9; }
.tree-row.selected { background: var(--selected); }
.tree-row.selected::before {
  content: "";
  position: absolute;
  left: 0;
  top: 4px;
  bottom: 4px;
  width: 3px;
  border-radius: 2px;
  background: var(--primary);
}
.tree-chevron {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  color: var(--muted);
}
.tree-chevron svg { width: 12px; height: 12px; fill: currentColor; }
.entry-icon {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  color: var(--muted);
}
.entry-icon svg { width: 18px; height: 18px; display: block; fill: currentColor; }
.entry-icon.folder-icon { color: #54aeff; }
.entry-icon.file-icon { color: #57606a; }
.entry-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-weight: 400; }
.tree-row.folder .entry-name { font-weight: 500; }
.entry-meta { color: var(--muted); font-size: 11px; white-space: nowrap; }
.tree-loading { padding: .35rem .7rem .35rem 3rem; color: var(--muted); font-size: 12px; }
.root-tree-row { margin: .3rem .35rem .1rem; width: calc(100% - .7rem); }
.root-tree-row .entry-name { font-weight: 600; }
.root-children { margin-left: 9px !important; padding-left: 8px !important; }
.empty-list { padding: 2rem 1rem; color: var(--muted); text-align: center; }
.preview {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
}
.preview-header {
  min-height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: .7rem;
  padding: .65rem .8rem;
  border-bottom: 1px solid var(--border);
  background: var(--surface-muted);
}
.preview-title { min-width: 0; }
.preview-title strong { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-title span { color: var(--muted); font-size: 12px; }
.preview-title span.truncated { color: #9a6700; }
.preview-actions { display: flex; gap: .4rem; flex-wrap: wrap; justify-content: flex-end; }
.preview-body { flex: 1 1 auto; min-height: 0; overflow: auto; }
.welcome { display: grid; place-items: center; min-height: 100%; padding: 2rem; text-align: center; color: var(--muted); }
.welcome strong { display: block; margin-bottom: .35rem; color: var(--text); font-size: 1.05rem; }
.code-view {
  margin: 0;
  min-height: 100%;
  padding: .8rem 0;
  background: var(--code-bg);
  overflow: auto;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre;
}
.code-line { display: grid; grid-template-columns: 54px minmax(max-content, 1fr); padding-right: 1rem; }
.line-number { color: #8c959f; text-align: right; user-select: none; padding-right: .8rem; }
.line-text { min-width: 0; }
.table-scroll { overflow: auto; }
.csv-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.csv-table th, .csv-table td { padding: .45rem .55rem; border-right: 1px solid var(--border-muted); border-bottom: 1px solid var(--border-muted); text-align: left; white-space: pre-wrap; vertical-align: top; }
.csv-table th { position: sticky; top: 0; background: var(--surface-muted); font-weight: 600; z-index: 1; }
.media-preview { display: grid; place-items: start center; min-height: 100%; padding: 1rem; background: var(--surface-muted); }
.media-preview img { max-width: 100%; height: auto; border: 1px solid var(--border); background: #fff; }
.media-preview iframe { width: 100%; height: 760px; border: 1px solid var(--border); background: #fff; }
.notice { margin: 1rem; padding: .85rem; border: 1px solid var(--border); border-radius: 6px; background: var(--surface-muted); color: var(--muted); }

.preview-empty {
  display: grid;
  place-items: center;
  align-content: center;
  gap: .35rem;
  min-height: 14rem;
  padding: 2rem;
  color: var(--muted);
  text-align: center;
}
.preview-empty strong { color: var(--text); font-weight: 600; }
.markdown-view {
  max-width: 980px;
  margin: 0 auto;
  padding: 1.4rem 1.6rem 3rem;
  color: var(--text);
  font-size: 14px;
  line-height: 1.6;
}
.markdown-view > :first-child { margin-top: 0; }
.markdown-view > :last-child { margin-bottom: 0; }
.markdown-view h1, .markdown-view h2, .markdown-view h3,
.markdown-view h4, .markdown-view h5, .markdown-view h6 {
  margin: 1.35rem 0 .65rem;
  line-height: 1.25;
  font-weight: 600;
}
.markdown-view h1 { padding-bottom: .35rem; border-bottom: 1px solid var(--border); font-size: 1.8rem; }
.markdown-view h2 { padding-bottom: .3rem; border-bottom: 1px solid var(--border); font-size: 1.45rem; }
.markdown-view h3 { font-size: 1.2rem; }
.markdown-view h4 { font-size: 1.05rem; }
.markdown-view p { margin: 0 0 1rem; }
.markdown-view ul, .markdown-view ol { margin: 0 0 1rem; padding-left: 2rem; }
.markdown-view li + li { margin-top: .2rem; }
.markdown-view blockquote {
  margin: 0 0 1rem;
  padding: .15rem 1rem;
  border-left: 4px solid var(--border-strong);
  color: var(--muted);
}
.markdown-view pre {
  margin: .65rem 0 1.15rem;
  overflow: auto;
  padding: .95rem 1.05rem;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  background: #f6f8fa;
  color: #24292f;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre;
}
.markdown-view code {
  border-radius: 4px;
  padding: .12rem .28rem;
  background: #eff1f3;
  font: .9em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.markdown-view pre code { padding: 0; background: transparent; color: inherit; font: inherit; }
.markdown-view hr { height: 1px; margin: 1.4rem 0; border: 0; background: var(--border); }
.markdown-view a { color: var(--primary); text-decoration: underline; text-underline-offset: 2px; }
.markdown-view img { max-width: 100%; height: auto; }
.markdown-view table { display: block; width: max-content; max-width: 100%; margin: 0 0 1rem; overflow: auto; border-collapse: collapse; }
.markdown-view th, .markdown-view td { padding: .4rem .65rem; border: 1px solid var(--border); text-align: left; vertical-align: top; }
.markdown-view th { background: var(--surface-muted); font-weight: 600; }
.markdown-view .task-item { list-style: none; margin-left: -1.4rem; }
.markdown-view .task-item input { margin-right: .45rem; }
.view-toggle { display: inline-flex; align-items: center; border: 1px solid var(--border-strong); border-radius: 7px; overflow: hidden; background: #fff; }
.view-toggle button {
  min-height: 32px;
  border: 0;
  border-radius: 0;
  padding: .3rem .65rem;
  background: #fff;
  color: var(--text);
  cursor: pointer;
}
.view-toggle button + button { border-left: 1px solid var(--border-strong); }
.view-toggle button:hover { background: #f3f6f9; }
.view-toggle button.active { background: #e8f0fe; color: var(--primary); font-weight: 600; }
@media (max-width: 850px) {
  html, body { height: auto; min-height: 100%; }
  body { height: auto; min-height: 100vh; overflow: auto; }
  main { height: auto; min-height: 0; overflow: visible; padding: .65rem 0 1rem; }
  .explorer { grid-template-columns: 1fr; height: auto; min-height: 0; }
  .explorer-splitter { display: none; }
  .sidebar { max-height: 520px; border-right: 0; border-bottom: 1px solid var(--border); }
  .preview-body, .welcome, .code-view { min-height: 520px; }
}
@media (max-width: 540px) {
  .header-row, main { width: min(100% - 20px, 1440px); }
  .entry-meta { display: none; }
  .preview-header { align-items: flex-start; flex-direction: column; }
  .preview-actions { justify-content: flex-start; }
}
</style>
</head>
<body>
<header>
  <div class="header-row">
    <div class="header-title"><span class="header-mark">&lt;/&gt;</span><span>SAP Validation Outputs</span></div>
    <a class="button" href="/">Return to validation UI</a>
  </div>
</header>
<main>
  <div class="repo-bar">
    <div>
      <h1 class="repo-title">artifacts</h1>
      <div class="repo-subtitle">Expand folders into a file tree on the left and preview files on the right.</div>
    </div>
    <div>
      <button id="allFiles" type="button">All files</button>
    </div>
  </div>
  <section class="explorer" aria-label="Artifact explorer">
    <aside class="sidebar">
      <div class="sidebar-toolbar">
        <nav id="breadcrumbs" class="breadcrumbs" aria-label="Artifact path"></nav>
        <input id="fileSearch" class="search" type="search" placeholder="Filter expanded tree" aria-label="Filter expanded file tree">
      </div>
      <div id="entryList" aria-live="polite"></div>
    </aside>

    <div
      id="explorerSplitter"
      class="explorer-splitter"
      role="separator"
      aria-label="Resize folder browser"
      aria-orientation="vertical"
      aria-valuemin="220"
      tabindex="0"
      title="Drag to resize the folder browser. Double-click to reset."
    ></div>

    <section class="preview">
      <div class="preview-header">
        <div class="preview-title"><strong id="previewName">Select a file</strong><span id="previewMeta">File contents will appear here.</span></div>
        <div id="previewActions" class="preview-actions"></div>
      </div>
      <div id="previewBody" class="preview-body">
        <div class="welcome"><div><strong>Artifact preview</strong>Select a file from the folder browser.</div></div>
      </div>
    </section>
  </section>
</main>
<script>
'use strict';
const initialPath = __INITIAL_PATH__;
const initialIsFile = __INITIAL_IS_FILE__;
const directoryCache = new Map();
const directoryRequests = new Map();
const expandedPaths = new Set();
let rootExpanded = true;
let selectedPath = '';
let activeDirectory = '';

const breadcrumbs = document.getElementById('breadcrumbs');
const entryList = document.getElementById('entryList');
const search = document.getElementById('fileSearch');
const previewName = document.getElementById('previewName');
const previewMeta = document.getElementById('previewMeta');
const previewActions = document.getElementById('previewActions');
const previewBody = document.getElementById('previewBody');

const explorer = document.querySelector('.explorer');
const sidebar = explorer.querySelector('.sidebar');
const explorerSplitter = document.getElementById('explorerSplitter');

const MIN_SIDEBAR_WIDTH = 220;
const MIN_PREVIEW_WIDTH = 320;
const DIVIDER_LAYOUT_WIDTH = 1;

let resizedSidebarWidth = null;
let dragStartX = 0;
let dragStartWidth = 0;

function explorerResizeEnabled() {
  return window.matchMedia('(min-width: 851px)').matches;
}

function setSidebarWidth(width) {
  if (!explorerResizeEnabled()) return;

  const explorerWidth = explorer.getBoundingClientRect().width;
  const maximumWidth = Math.max(
    MIN_SIDEBAR_WIDTH,
    explorerWidth - MIN_PREVIEW_WIDTH - DIVIDER_LAYOUT_WIDTH
  );

  const clampedWidth = Math.min(
    Math.max(width, MIN_SIDEBAR_WIDTH),
    maximumWidth
  );

  resizedSidebarWidth = clampedWidth;

  explorer.style.setProperty(
    '--sidebar-width',
    `${clampedWidth}px`
  );

  explorerSplitter.setAttribute(
    'aria-valuenow',
    String(Math.round(clampedWidth))
  );

  explorerSplitter.setAttribute(
    'aria-valuemax',
    String(Math.round(maximumWidth))
  );
}

function finishSidebarResize(event) {
  explorerSplitter.classList.remove('dragging');
  document.body.classList.remove('resizing-sidebar');

  if (
    event &&
    explorerSplitter.hasPointerCapture(event.pointerId)
  ) {
    explorerSplitter.releasePointerCapture(event.pointerId);
  }
}

explorerSplitter.addEventListener('pointerdown', event => {
  if (!explorerResizeEnabled()) return;

  event.preventDefault();

  dragStartX = event.clientX;
  dragStartWidth = sidebar.getBoundingClientRect().width;

  explorerSplitter.classList.add('dragging');
  document.body.classList.add('resizing-sidebar');
  explorerSplitter.setPointerCapture(event.pointerId);
});

explorerSplitter.addEventListener('pointermove', event => {
  if (!explorerSplitter.hasPointerCapture(event.pointerId)) return;

  const movement = event.clientX - dragStartX;
  setSidebarWidth(dragStartWidth + movement);
});

explorerSplitter.addEventListener(
  'pointerup',
  finishSidebarResize
);

explorerSplitter.addEventListener(
  'pointercancel',
  finishSidebarResize
);

explorerSplitter.addEventListener(
  'lostpointercapture',
  () => {
    explorerSplitter.classList.remove('dragging');
    document.body.classList.remove('resizing-sidebar');
  }
);

explorerSplitter.addEventListener('dblclick', () => {
  resizedSidebarWidth = null;
  explorer.style.removeProperty('--sidebar-width');
});

explorerSplitter.addEventListener('keydown', event => {
  if (!explorerResizeEnabled()) return;

  const currentWidth =
    resizedSidebarWidth ??
    sidebar.getBoundingClientRect().width;

  const increment = event.shiftKey ? 50 : 10;

  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    setSidebarWidth(currentWidth - increment);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    setSidebarWidth(currentWidth + increment);
  } else if (event.key === 'Home') {
    event.preventDefault();
    setSidebarWidth(MIN_SIDEBAR_WIDTH);
  } else if (event.key === 'End') {
    event.preventDefault();
    setSidebarWidth(Number.MAX_SAFE_INTEGER);
  }
});

window.addEventListener('resize', () => {
  if (
    explorerResizeEnabled() &&
    resizedSidebarWidth !== null
  ) {
    setSidebarWidth(resizedSidebarWidth);
  }
});

let currentPreviewData = null;
let markdownViewMode = 'preview';

function normalizePath(path) {
  return String(path || '').split('/').filter(Boolean).join('/');
}
function encodeArtifactPath(path) {
  return normalizePath(path).split('/').filter(Boolean).map(encodeURIComponent).join('/');
}
function browserUrl(path, isDirectory) {
  const encoded = encodeArtifactPath(path);
  if (!encoded) return '/artifacts/';
  return '/artifacts/' + encoded + (isDirectory ? '/' : '');
}
function parentPath(path) {
  const parts = normalizePath(path).split('/').filter(Boolean);
  parts.pop();
  return parts.join('/');
}
function setHistory(path, isDirectory) {
  history.pushState({path: normalizePath(path), isDirectory}, '', browserUrl(path, isDirectory));
}
async function getJson(url) {
  const response = await fetch(url, {headers: {'Accept': 'application/json'}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
}
function setChevronIcon(container, expanded, loading = false) {
  if (loading) {
    container.textContent = '…';
    return;
  }
  container.innerHTML = expanded
    ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M12.78 5.22a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06 0L3.22 6.28a.75.75 0 1 1 1.06-1.06L8 8.94l3.72-3.72a.75.75 0 0 1 1.06 0Z"/></svg>'
    : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.22 3.22a.75.75 0 0 1 1.06 0l4.25 4.25a.75.75 0 0 1 0 1.06l-4.25 4.25a.75.75 0 0 1-1.06-1.06L9.94 8 6.22 4.28a.75.75 0 0 1 0-1.06Z"/></svg>';
}
function setEntryIcon(container, isDirectory, expanded = false) {
  container.className = 'entry-icon ' + (isDirectory ? 'folder-icon' : 'file-icon');
  if (isDirectory) {
    container.innerHTML = expanded
      ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.75 2.5h4.19c.4 0 .78.16 1.06.44l.81.81h6.44c.97 0 1.75.78 1.75 1.75v.69a.75.75 0 0 1-.03.21l-1.52 5.32A1.75 1.75 0 0 1 12.77 13H2.23a1.75 1.75 0 0 1-1.68-1.28L.03 9.9A.75.75 0 0 1 0 9.69V4.25C0 3.28.78 2.5 1.75 2.5Zm-.25 4.75v2.33l.49 1.73c.09.41.45.69.87.69h9.91c.4 0 .75-.27.86-.65L15 6.55a.25.25 0 0 0-.24-.32H2.49c-.45 0-.85.3-.96.74l-.03.28Z"/></svg>'
      : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.75 2.5h4.19c.4 0 .78.16 1.06.44l.81.81h6.44c.97 0 1.75.78 1.75 1.75v6.25c0 .97-.78 1.75-1.75 1.75H1.75A1.75 1.75 0 0 1 0 11.75v-7.5C0 3.28.78 2.5 1.75 2.5Zm0 1.5a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h12.5c.14 0 .25-.11.25-.25V5.5a.25.25 0 0 0-.25-.25H7.5a.75.75 0 0 1-.53-.22l-1.03-1.03H1.75Z"/></svg>';
  } else {
    container.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.75 1.5A1.75 1.75 0 0 0 2 3.25v9.5c0 .97.78 1.75 1.75 1.75h8.5A1.75 1.75 0 0 0 14 12.75V5.56c0-.46-.18-.9-.51-1.23L11.17 2a1.75 1.75 0 0 0-1.24-.51H3.75Zm0 1.5h5.5v2.5c0 .41.34.75.75.75h2.5v6.5c0 .14-.11.25-.25.25h-8.5a.25.25 0 0 1-.25-.25v-9.5c0-.14.11-.25.25-.25Zm7 .31 1.44 1.44h-1.44V3.31Z"/></svg>';
  }
}
function breadcrumbParts(path) {
  const result = [{name: 'artifacts', path: ''}];
  let current = '';
  for (const part of normalizePath(path).split('/').filter(Boolean)) {
    current = current ? `${current}/${part}` : part;
    result.push({name: part, path: current});
  }
  return result;
}
function clearPreview(message = 'Select a file from the folder browser.') {
  selectedPath = '';
  previewName.textContent = 'Select a file';
  previewMeta.textContent = 'File contents will appear here.';
  previewMeta.className = '';
  previewActions.textContent = '';
  currentPreviewData = null;
  markdownViewMode = 'preview';
  previewBody.innerHTML = '';
  const welcome = document.createElement('div');
  welcome.className = 'welcome';
  const content = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = 'Artifact preview';
  content.append(strong, document.createTextNode(message));
  welcome.appendChild(content);
  previewBody.appendChild(welcome);
}
function renderBreadcrumbs(path = activeDirectory) {
  breadcrumbs.textContent = '';
  breadcrumbParts(path).forEach((item, index) => {
    if (index) {
      const separator = document.createElement('span');
      separator.className = 'separator';
      separator.textContent = '/';
      breadcrumbs.appendChild(separator);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.name;
    button.addEventListener('click', () => {
      revealDirectory(item.path, true).catch(showTreeError);
    });
    breadcrumbs.appendChild(button);
  });
}
function showTreeError(error) {
  entryList.innerHTML = `<div class="empty-list">${escapeHtml(error.message)}</div>`;
}
async function loadDirectoryData(path) {
  const normalized = normalizePath(path);
  if (directoryCache.has(normalized)) return directoryCache.get(normalized);
  if (directoryRequests.has(normalized)) return directoryRequests.get(normalized);

  const request = (async () => {
    const data = await getJson('/api/artifacts?path=' + encodeURIComponent(normalized));
    directoryCache.set(data.path, data);
    return data;
  })();
  directoryRequests.set(normalized, request);
  renderTree();
  try {
    return await request;
  } finally {
    directoryRequests.delete(normalized);
    renderTree();
  }
}
function entryMatches(entry, query) {
  return `${entry.name} ${entry.type} ${entry.path}`.toLowerCase().includes(query);
}
function entryOrDescendantMatches(entry, query) {
  if (!query || entryMatches(entry, query)) return true;
  if (!entry.is_dir) return false;
  const childData = directoryCache.get(entry.path);
  return Boolean(childData && childData.entries.some(child => entryOrDescendantMatches(child, query)));
}
function createTreeList(entries, query) {
  const list = document.createElement('ul');
  list.className = 'tree-list';
  for (const entry of entries) {
    if (!entryOrDescendantMatches(entry, query)) continue;
    const item = document.createElement('li');
    item.className = 'tree-item';
    const isExpanded = entry.is_dir && expandedPaths.has(entry.path);
    const isLoading = entry.is_dir && directoryRequests.has(entry.path);
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'tree-row';
    if (entry.is_dir) row.classList.add('folder');
    if (entry.is_dir && entry.path === activeDirectory) row.classList.add('active-folder');
    if (!entry.is_dir && entry.path === selectedPath) row.classList.add('selected');
    row.title = entry.path || entry.name;
    if (entry.is_dir) row.setAttribute('aria-expanded', String(isExpanded));

    const chevron = document.createElement('span');
    chevron.className = 'tree-chevron';
    if (entry.is_dir) setChevronIcon(chevron, isExpanded, isLoading);
    const icon = document.createElement('span');
    setEntryIcon(icon, entry.is_dir, isExpanded);
    const name = document.createElement('span');
    name.className = 'entry-name';
    name.textContent = entry.name;
    const meta = document.createElement('span');
    meta.className = 'entry-meta';
    meta.textContent = entry.is_dir ? entry.modified : `${entry.size} · ${entry.modified}`;
    row.append(chevron, icon, name, meta);

    row.addEventListener('click', () => {
      if (!entry.is_dir) {
        loadPreview(entry.path, true).catch(error => {
          previewBody.innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`;
        });
        return;
      }
      activeDirectory = entry.path;
      renderBreadcrumbs();
      if (expandedPaths.has(entry.path)) {
        expandedPaths.delete(entry.path);
        renderTree();
      } else {
        expandDirectory(entry.path, true).catch(showTreeError);
      }
    });
    item.appendChild(row);

    const childData = entry.is_dir ? directoryCache.get(entry.path) : null;
    const showChildren = childData && (isExpanded || Boolean(query));
    if (showChildren) {
      const childList = createTreeList(childData.entries, query);
      if (childList.childElementCount) item.appendChild(childList);
      else if (!query) {
        const empty = document.createElement('div');
        empty.className = 'tree-loading';
        empty.textContent = 'Empty folder';
        item.appendChild(empty);
      }
    } else if (isExpanded && isLoading) {
      const loading = document.createElement('div');
      loading.className = 'tree-loading';
      loading.textContent = 'Loading…';
      item.appendChild(loading);
    }
    list.appendChild(item);
  }
  return list;
}
function renderTree() {
  const rootData = directoryCache.get('');
  entryList.textContent = '';
  if (!rootData) {
    const loading = document.createElement('div');
    loading.className = 'empty-list';
    loading.textContent = directoryRequests.has('') ? 'Loading artifacts…' : 'No artifacts loaded.';
    entryList.appendChild(loading);
    return;
  }

  const query = search.value.trim().toLowerCase();
  const rootList = document.createElement('ul');
  rootList.className = 'tree-list';
  const rootItem = document.createElement('li');
  rootItem.className = 'tree-item';
  const rootRow = document.createElement('button');
  rootRow.type = 'button';
  rootRow.className = 'tree-row folder root-tree-row';
  if (!activeDirectory) rootRow.classList.add('active-folder');
  rootRow.setAttribute('aria-expanded', String(rootExpanded));
  rootRow.title = 'artifacts';

  const rootChevron = document.createElement('span');
  rootChevron.className = 'tree-chevron';
  setChevronIcon(rootChevron, rootExpanded, directoryRequests.has(''));
  const rootIcon = document.createElement('span');
  setEntryIcon(rootIcon, true, rootExpanded);
  const rootName = document.createElement('span');
  rootName.className = 'entry-name';
  rootName.textContent = 'artifacts';
  const rootMeta = document.createElement('span');
  rootMeta.className = 'entry-meta';
  rootMeta.textContent = `${rootData.entries.length} item${rootData.entries.length === 1 ? '' : 's'}`;
  rootRow.append(rootChevron, rootIcon, rootName, rootMeta);
  rootRow.addEventListener('click', () => {
    rootExpanded = !rootExpanded;
    activeDirectory = '';
    renderBreadcrumbs();
    renderTree();
  });
  rootItem.appendChild(rootRow);

  const tree = createTreeList(rootData.entries, query);
  tree.classList.add('root-children');
  if (rootExpanded || query) {
    if (tree.childElementCount) rootItem.appendChild(tree);
    else {
      const empty = document.createElement('div');
      empty.className = 'empty-list';
      empty.textContent = query ? 'No matching loaded files or folders.' : 'The artifact folder is empty.';
      rootItem.appendChild(empty);
    }
  }
  rootList.appendChild(rootItem);
  entryList.appendChild(rootList);
}
async function expandDirectory(path, updateHistory = false) {
  const normalized = normalizePath(path);
  activeDirectory = normalized;
  expandedPaths.add(normalized);
  renderBreadcrumbs();
  renderTree();
  await loadDirectoryData(normalized);
  renderTree();
  if (updateHistory) setHistory(normalized, true);
}
async function revealDirectory(path, updateHistory = false) {
  const normalized = normalizePath(path);
  await loadDirectoryData('');
  let current = '';
  for (const part of normalized.split('/').filter(Boolean)) {
    current = current ? `${current}/${part}` : part;
    expandedPaths.add(current);
    await loadDirectoryData(current);
  }
  activeDirectory = normalized;
  renderBreadcrumbs();
  renderTree();
  if (updateHistory) setHistory(normalized, true);
}
function addPreviewAction(label, href, primary = false) {
  const anchor = document.createElement('a');
  anchor.className = 'button' + (primary ? ' primary' : '');
  anchor.href = href;
  anchor.textContent = label;
  if (label === 'Open raw') { anchor.target = '_blank'; anchor.rel = 'noopener'; }
  previewActions.appendChild(anchor);
}
function renderCode(content) {
  const pre = document.createElement('pre');
  pre.className = 'code-view';
  const lines = content.split('\n');
  lines.forEach((line, index) => {
    const row = document.createElement('span');
    row.className = 'code-line';
    const number = document.createElement('span');
    number.className = 'line-number';
    number.textContent = String(index + 1);
    const value = document.createElement('span');
    value.className = 'line-text';
    value.textContent = line || ' ';
    row.append(number, value);
    pre.appendChild(row);
  });
  previewBody.appendChild(pre);
}
function safeUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (raw.startsWith('#') || raw.startsWith('/') || raw.startsWith('./') || raw.startsWith('../')) return raw;
  try {
    const parsed = new URL(raw, window.location.href);
    return ['http:', 'https:', 'mailto:'].includes(parsed.protocol) ? raw : '';
  } catch (_) {
    return '';
  }
}
function renderMarkdownInline(text) {
  let value = escapeHtml(text);
  const codeTokens = [];
  value = value.replace(/`([^`\n]+)`/g, (_, code) => {
    // Use private-use Unicode sentinels so later Markdown substitutions cannot
    // accidentally interpret the placeholder itself as emphasis.
    const token = `\uE000${codeTokens.length}\uE001`;
    codeTokens.push(`<code>${code}</code>`);
    return token;
  });
  value = value.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+[&quot;'][^)]*[&quot;'])?\)/g, (_, alt, href) => {
    const safe = safeUrl(href.replaceAll('&amp;', '&'));
    return safe ? `<img src="${escapeHtml(safe)}" alt="${alt}">` : `![${alt}](${href})`;
  });
  value = value.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+[&quot;'][^)]*[&quot;'])?\)/g, (_, label, href) => {
    const safe = safeUrl(href.replaceAll('&amp;', '&'));
    return safe ? `<a href="${escapeHtml(safe)}" target="_blank" rel="noopener">${label}</a>` : `[${label}](${href})`;
  });
  value = value.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  // CommonMark does not treat underscores inside words (for example
  // component_abap or hana_db) as emphasis delimiters.
  value = value.replace(/(^|[^A-Za-z0-9])__([^_\n]+)__(?=$|[^A-Za-z0-9])/g, '$1<strong>$2</strong>');
  value = value.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
  value = value.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  value = value.replace(/(^|[^A-Za-z0-9])_([^_\n]+)_(?=$|[^A-Za-z0-9])/g, '$1<em>$2</em>');
  codeTokens.forEach((htmlValue, index) => {
    value = value.split(`\uE000${index}\uE001`).join(htmlValue);
  });
  return value;
}
function isMarkdownTableSeparator(line) {
  const cells = line.trim().replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}
function splitMarkdownTableRow(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
}
function markdownToHtml(content) {
  const lines = String(content || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let index = 0;
  let paragraph = [];
  let listType = null;
  let blockquote = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${renderMarkdownInline(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (!listType) return;
    out.push(`</${listType}>`);
    listType = null;
  };
  const flushBlockquote = () => {
    if (!blockquote.length) return;
    out.push(`<blockquote>${markdownToHtml(blockquote.join('\n'))}</blockquote>`);
    blockquote = [];
  };
  const flushOpen = () => { flushParagraph(); closeList(); flushBlockquote(); };

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    // Support CommonMark-style fenced code blocks, including fences longer
    // than three characters and the compact one-line form emitted by some
    // validation report generators (for example: ```text value ```).
    const inlineFence = line.match(/^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+.-]*)\s+(.+?)\s*\1\s*$/);
    if (inlineFence) {
      flushOpen();
      const language = inlineFence[2].trim();
      const className = language ? ` class="language-${escapeHtml(language.replace(/[^A-Za-z0-9_+.-]/g, ''))}"` : '';
      out.push(`<pre><code${className}>${escapeHtml(inlineFence[3])}</code></pre>`);
      index++;
      continue;
    }
    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([^`~]*)$/);
    if (fence) {
      flushOpen();
      const marker = fence[1];
      const fenceChar = marker[0];
      const fenceLength = marker.length;
      const language = fence[2].trim().split(/\s+/, 1)[0] || '';
      const closingFence = new RegExp(`^\\s*${fenceChar === '`' ? '`' : '~'}{${fenceLength},}\\s*$`);
      const code = [];
      index++;
      while (index < lines.length && !closingFence.test(lines[index])) {
        code.push(lines[index]);
        index++;
      }
      const className = language ? ` class="language-${escapeHtml(language.replace(/[^A-Za-z0-9_+.-]/g, ''))}"` : '';
      out.push(`<pre><code${className}>${escapeHtml(code.join('\n'))}</code></pre>`);
      if (index < lines.length) index++;
      continue;
    }
    if (!trimmed) { flushOpen(); index++; continue; }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flushOpen(); out.push('<hr>'); index++; continue; }
    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      flushOpen();
      const level = heading[1].length;
      out.push(`<h${level}>${renderMarkdownInline(heading[2])}</h${level}>`);
      index++;
      continue;
    }
    if (line.startsWith('>')) {
      flushParagraph(); closeList();
      blockquote.push(line.replace(/^>\s?/, ''));
      index++;
      continue;
    }
    if (blockquote.length) flushBlockquote();
    if (index + 1 < lines.length && line.includes('|') && isMarkdownTableSeparator(lines[index + 1])) {
      flushParagraph(); closeList();
      const headers = splitMarkdownTableRow(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(splitMarkdownTableRow(lines[index]));
        index++;
      }
      out.push('<table><thead><tr>' + headers.map(cell => `<th>${renderMarkdownInline(cell)}</th>`).join('') + '</tr></thead><tbody>');
      for (const row of rows) out.push('<tr>' + headers.map((_, cellIndex) => `<td>${renderMarkdownInline(row[cellIndex] || '')}</td>`).join('') + '</tr>');
      out.push('</tbody></table>');
      continue;
    }
    const unordered = line.match(/^\s{0,3}[-+*]\s+(.+)$/);
    const ordered = line.match(/^\s{0,3}\d+[.)]\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const nextType = ordered ? 'ol' : 'ul';
      if (listType && listType !== nextType) closeList();
      if (!listType) { listType = nextType; out.push(`<${listType}>`); }
      let item = (ordered || unordered)[1];
      const task = item.match(/^\[([ xX])\]\s+(.+)$/);
      if (task) {
        out.push(`<li class="task-item"><input type="checkbox" disabled${task[1].toLowerCase() === 'x' ? ' checked' : ''}>${renderMarkdownInline(task[2])}</li>`);
      } else {
        out.push(`<li>${renderMarkdownInline(item)}</li>`);
      }
      index++;
      continue;
    }
    if (listType) closeList();
    paragraph.push(trimmed);
    index++;
  }
  flushOpen();
  return out.join('');
}
function renderMarkdown(content) {
  const article = document.createElement('article');
  article.className = 'markdown-view';
  article.innerHTML = markdownToHtml(content);
  previewBody.appendChild(article);
}
function addMarkdownViewToggle(data) {
  const group = document.createElement('div');
  group.className = 'view-toggle';
  const previewButton = document.createElement('button');
  previewButton.type = 'button';
  previewButton.textContent = 'Preview';
  const codeButton = document.createElement('button');
  codeButton.type = 'button';
  codeButton.textContent = 'Code';
  function activate(mode) {
    markdownViewMode = mode;
    previewButton.classList.toggle('active', mode === 'preview');
    codeButton.classList.toggle('active', mode === 'code');
    previewBody.textContent = '';
    if (mode === 'preview') renderMarkdown(data.content || '');
    else renderCode(data.content || '');
  }
  previewButton.addEventListener('click', () => activate('preview'));
  codeButton.addEventListener('click', () => activate('code'));
  group.append(previewButton, codeButton);
  previewActions.appendChild(group);
  activate(markdownViewMode);
}
function parseCsv(text) {
  const rows = [];
  let row = [], field = '', quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index++; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ',') { row.push(field); field = ''; }
    else if (char === '\n') { row.push(field.replace(/\r$/, '')); rows.push(row); row = []; field = ''; }
    else field += char;
  }
  if (field || row.length) { row.push(field.replace(/\r$/, '')); rows.push(row); }
  return rows;
}
function renderCsv(content) {
  const rows = parseCsv(content);
  if (!rows.length) { renderCode(content); return; }
  const wrap = document.createElement('div');
  wrap.className = 'table-scroll';
  const table = document.createElement('table');
  table.className = 'csv-table';
  const thead = document.createElement('thead');
  const header = document.createElement('tr');
  for (const value of rows[0]) { const th = document.createElement('th'); th.textContent = value; header.appendChild(th); }
  thead.appendChild(header);
  const tbody = document.createElement('tbody');
  for (const values of rows.slice(1)) {
    const tr = document.createElement('tr');
    for (let index = 0; index < rows[0].length; index++) {
      const td = document.createElement('td'); td.textContent = values[index] ?? ''; tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.append(thead, tbody); wrap.appendChild(table); previewBody.appendChild(wrap);
}
async function loadPreview(path, updateHistory = false) {
  try {
    const normalized = normalizePath(path);
    await revealDirectory(parentPath(normalized), false);
    const data = await getJson('/api/artifact-preview?path=' + encodeURIComponent(normalized));
    selectedPath = data.path;
    activeDirectory = data.parent;
    renderBreadcrumbs();
    renderTree();
    previewName.textContent = data.name;
    previewMeta.textContent = `${data.type} · ${data.size} · modified ${data.modified}` + (data.truncated ? ' · preview truncated' : '');
    previewMeta.className = data.truncated ? 'truncated' : '';
    currentPreviewData = data;
    previewActions.textContent = '';
    if (data.kind === 'markdown') { markdownViewMode = 'preview'; addMarkdownViewToggle(data); }
    addPreviewAction('Download', data.download_url, false);
    if (data.raw_url && data.kind === 'pdf') addPreviewAction('Open raw', data.raw_url, true);
    if (data.kind !== 'markdown') previewBody.textContent = '';
    if (data.kind === 'markdown') {
      // addMarkdownViewToggle() already rendered the initial Markdown preview.
      // Do not fall through to the unsupported-file fallback below.
    } else if (data.kind === 'csv') renderCsv(data.content || '');
    else if (['text', 'json', 'html'].includes(data.kind)) renderCode(data.content || '');
    else if (data.kind === 'image') {
      const wrap = document.createElement('div'); wrap.className = 'media-preview';
      const image = document.createElement('img'); image.src = data.raw_url; image.alt = data.name; wrap.appendChild(image); previewBody.appendChild(wrap);
    } else if (data.kind === 'pdf') {
      const wrap = document.createElement('div'); wrap.className = 'media-preview';
      const frame = document.createElement('iframe'); frame.src = data.raw_url; frame.title = data.name; wrap.appendChild(frame); previewBody.appendChild(wrap);
    } else {
      const empty = document.createElement('div');
      empty.className = 'preview-empty';
      const strong = document.createElement('strong');
      strong.textContent = 'No inline preview available';
      const detail = document.createElement('span');
      detail.textContent = 'Use Download to open this file with its native application.';
      empty.append(strong, detail);
      previewBody.appendChild(empty);
    }
    if (updateHistory) setHistory(data.path, false);
  } catch (error) {
    previewBody.innerHTML = `<div class="notice">${escapeHtml(error.message)}</div>`;
    throw error;
  }
}
search.addEventListener('input', renderTree);
document.getElementById('allFiles').addEventListener('click', () => {
  expandedPaths.clear();
  rootExpanded = true;
  activeDirectory = '';
  clearPreview();
  renderBreadcrumbs();
  loadDirectoryData('').then(() => {
    renderTree();
    setHistory('', true);
  }).catch(showTreeError);
});
window.addEventListener('popstate', event => {
  const state = event.state;
  if (state && state.isDirectory) {
    clearPreview();
    revealDirectory(state.path, false).catch(showTreeError);
  } else if (state) {
    loadPreview(state.path, false).catch(() => {});
  } else {
    location.reload();
  }
});

(async function init() {
  await loadDirectoryData('');
  if (initialIsFile) {
    await loadPreview(initialPath, false);
  } else {
    clearPreview();
    await revealDirectory(initialPath, false);
  }
  history.replaceState({path: initialPath, isDirectory: !initialIsFile}, '', browserUrl(initialPath, !initialIsFile));
})().catch(showTreeError);
</script>
</body>
</html>'''
        document = document.replace("__UI_BUILD__", html.escape(UI_BUILD))
        document = document.replace("__INITIAL_PATH__", json.dumps(initial_relative))
        document = document.replace("__INITIAL_IS_FILE__", "true" if initial_is_file else "false")
        body = document.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_artifact_directory_json(self, relative_value: str) -> None:
        root = self.app.paths.artifact_root.resolve()
        directory = self._artifact_target_from_relative(relative_value)
        if directory is None or not directory.is_dir():
            self._send_json({"error": "Artifact directory not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            children = sorted(
                (child for child in directory.iterdir() if not _is_hidden_artifact(child, root)),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError:
            self._send_json({"error": "Could not read artifact directory"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        entries: list[dict[str, Any]] = []
        for child in children:
            resolved = _path_within(child, root)
            if resolved is None or _is_hidden_artifact(resolved, root):
                continue
            try:
                metadata = resolved.stat()
                is_directory = resolved.is_dir()
            except OSError:
                continue
            extension = resolved.suffix.lstrip(".").upper()
            entries.append(
                {
                    "name": resolved.name,
                    "path": self._artifact_relative(resolved),
                    "is_dir": is_directory,
                    "type": "Folder" if is_directory else (extension or "File"),
                    "size": "—" if is_directory else _format_file_size(metadata.st_size),
                    "size_bytes": 0 if is_directory else metadata.st_size,
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(metadata.st_mtime)),
                }
            )

        relative = directory.relative_to(root)
        breadcrumb_parts = [{"name": "artifacts", "path": ""}]
        current = Path()
        for part in relative.parts:
            current /= part
            breadcrumb_parts.append({"name": part, "path": current.as_posix()})
        self._send_json(
            {
                "path": self._artifact_relative(directory),
                "parent": self._artifact_relative(directory.parent) if relative.parts else None,
                "breadcrumbs": breadcrumb_parts,
                "entries": entries,
            }
        )

    def _send_artifact_preview_json(self, relative_value: str) -> None:
        target = self._artifact_target_from_relative(relative_value)
        if target is None or not target.is_file():
            self._send_json({"error": "Artifact file not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            metadata = target.stat()
        except OSError:
            self._send_json({"error": "Could not read artifact metadata"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        suffix = target.suffix.lower()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        text_suffixes = {
            ".txt", ".log", ".out", ".md", ".markdown", ".json", ".csv", ".tsv",
            ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml", ".html", ".htm",
            ".py", ".sh", ".j2", ".sql", ".properties",
        }
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
            kind = "image"
        elif suffix == ".pdf":
            kind = "pdf"
        elif suffix == ".csv":
            kind = "csv"
        elif suffix == ".json":
            kind = "json"
        elif suffix in {".md", ".markdown"}:
            kind = "markdown"
        elif suffix in {".html", ".htm"}:
            kind = "html"
        elif content_type.startswith("text/") or suffix in text_suffixes:
            kind = "text"
        else:
            kind = "binary"

        content: str | None = None
        truncated = False
        if kind in {"csv", "json", "markdown", "html", "text", "binary"}:
            try:
                with target.open("rb") as handle:
                    raw = handle.read(MAX_PREVIEW_BYTES + 1)
            except OSError:
                self._send_json({"error": "Could not read artifact"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            truncated = len(raw) > MAX_PREVIEW_BYTES
            raw = raw[:MAX_PREVIEW_BYTES]

            if kind == "binary":
                # Unknown extensions are often controller output or configuration files.
                # Preview them as source when they are valid UTF-8 text and contain no NULs.
                if b"\x00" not in raw:
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        content = None
                    else:
                        kind = "text"
            else:
                content = raw.decode("utf-8", errors="replace")

            if kind == "json" and content is not None:
                try:
                    content = json.dumps(json.loads(content), indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass

        self._send_json(
            {
                "name": target.name,
                "path": self._artifact_relative(target),
                "parent": self._artifact_relative(target.parent),
                "kind": kind,
                "type": suffix.lstrip(".").upper() or content_type,
                "size": _format_file_size(metadata.st_size),
                "size_bytes": metadata.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(metadata.st_mtime)),
                "content": content,
                "truncated": truncated,
                "raw_url": self._artifact_raw_url(target),
                "download_url": self._artifact_raw_url(target, download=True),
            }
        )

    def _send_artifact_bytes(self, request_path: str, *, download: bool) -> None:
        prefix = "/artifact-download/" if download else "/artifact-raw/"
        raw_relative = unquote(request_path.removeprefix(prefix)).lstrip("/")
        target = self._artifact_target_from_relative(raw_relative)
        if target is None or not target.is_file():
            self._send_json({"error": "Artifact not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        try:
            size = target.stat().st_size
            handle = target.open("rb")
        except OSError:
            self._send_json({"error": "Could not read artifact"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        disposition = "attachment" if download else "inline"
        safe_name = target.name.replace('"', "")
        with handle:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'{disposition}; filename="{safe_name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if not download and content_type in {"text/html", "image/svg+xml"}:
                self.send_header(
                    "Content-Security-Policy",
                    "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'",
                )
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
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
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
        elif path == "/api/artifacts":
            self._send_artifact_directory_json(query.get("path", [""])[0])
        elif path == "/api/artifact-preview":
            self._send_artifact_preview_json(query.get("path", [""])[0])
        elif path.startswith("/artifact-raw/"):
            self._send_artifact_bytes(path, download=False)
        elif path.startswith("/artifact-download/"):
            self._send_artifact_bytes(path, download=True)
        elif path == "/artifacts":
            self.send_response(HTTPStatus.PERMANENT_REDIRECT)
            self.send_header("Location", "/artifacts/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path.startswith("/artifacts/"):
            target = self._artifact_target(path)
            if target is None or not target.exists():
                self._send_json({"error": "Artifact not found"}, HTTPStatus.NOT_FOUND)
            else:
                self._send_artifact_browser(target)
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
    print(f"UI source: {Path(__file__).resolve()}", flush=True)
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