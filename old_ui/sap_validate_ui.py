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
body { font-family: sans-serif; margin: 1rem; max-width: 1500px; }
fieldset { margin: 0 0 1rem; }
label { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; margin-top: .5rem; }
th, td { border: 1px solid #bbb; padding: .3rem; text-align: left; vertical-align: top; }
th button { border: 0; background: none; padding: 0; font: inherit; cursor: pointer; }
input[type="search"] { min-width: 18rem; }
.controls { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin: .5rem 0; }
.muted { color: #666; }
.hidden { display: none; }
pre { border: 1px solid #bbb; padding: .6rem; min-height: 12rem; max-height: 34rem; overflow: auto; white-space: pre-wrap; }
details { margin: .5rem 0; }
summary { cursor: pointer; font-weight: bold; }
button { cursor: pointer; }
</style>
</head>
<body>
<fieldset>
<legend>1. Systems</legend>
<div class="controls">
<label>Environment <select id="environmentFilter"><option value="">All</option></select></label>
<label>Landscape <select id="landscapeFilter"><option value="">All</option></select></label>
<label>Component <select id="componentFilter"><option value="">All</option></select></label>
<label>Search <input id="systemSearch" type="search" placeholder="server, host, address"></label>
<button type="button" id="selectVisibleSystems">Select visible</button>
<button type="button" id="clearVisibleSystems">Clear visible</button>
<span id="systemCount"></span>
</div>
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
</fieldset>

<fieldset>
<legend>2. Validation checks</legend>
<div class="controls">
<label>Profile <select id="profile"><option value="">Custom</option></select></label>
<label>Search <input id="checkSearch" type="search" placeholder="category, ID, tag, description"></label>
<label><input id="showUnavailable" type="checkbox"> Show unavailable checks</label>
<button type="button" id="selectVisibleChecks">Select visible</button>
<button type="button" id="clearVisibleChecks">Clear visible</button>
<span id="checkCount"></span>
</div>
<div id="checks"></div>
</fieldset>

<fieldset>
<legend>3. Parameters</legend>
<div class="controls">
<label>Mode
<select id="mode">
<option value="validate">Validate</option>
<option value="discover_validate">Discover, then validate</option>
<option value="discover_only">Discovery only</option>
<option value="prepare_only">Prepare inventory only</option>
</select>
</label>
<label>Batch size <input id="batchSize" type="number" min="1"></label>
<label>Forks <input id="forks" type="number" min="1"></label>
<label>Verbosity <select id="verbose"><option>0</option><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
</div>
<div class="controls">
<label><input id="saveRawOutputs" type="checkbox"> Save raw outputs</label>
<label><input id="strict" type="checkbox"> Strict validation</label>
<label><input id="enableIncrond" type="checkbox"> Enable incrond validation</label>
<label><input id="enableBackint" type="checkbox"> Enable Backint validation</label>
<label><input id="dryRun" type="checkbox"> Dry run</label>
<label><input id="syntaxCheck" type="checkbox"> Syntax check</label>
<label><input id="checkMode" type="checkbox"> Ansible check mode</label>
</div>
</fieldset>

<div class="controls">
<button type="button" id="run">Run validation</button>
<button type="button" id="stop" disabled>Stop</button>
<strong id="status">Loading…</strong>
</div>
<div><code id="command"></code></div>

<fieldset>
<legend>4. Results</legend>
<div class="controls">
<strong id="quickSummary">No completed run yet.</strong>
<a href="/artifacts/" target="_blank" rel="noopener">Browse all outputs</a>
<a id="browseOutput" class="hidden" target="_blank" rel="noopener">Browse current run</a>
<button type="button" id="openOutput" disabled>Open folder</button>
</div>
<div id="runDirectory" class="muted"></div>
<div id="artifactLinks" class="controls"></div>
<table id="summaryTable" class="hidden">
<thead><tr><th>System</th><th>Environment</th><th>Overall</th><th>Quick summary</th><th>Report</th></tr></thead>
<tbody></tbody>
</table>
</fieldset>

<details open>
<summary>Live controller output</summary>
<pre id="output"></pre>
</details>

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
    checkbox.addEventListener('change', updateCounts);
    const cells = [checkbox, server.server_id, server.environment, server.landscape,
      server.components.join(', '), server.physical_hostname, server.address, server.enabled ? 'yes' : 'no'];
    for (const value of cells) {
      const td = document.createElement('td');
      if (value instanceof Node) td.appendChild(value); else td.textContent = value;
      tr.appendChild(td);
    }
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
    details.dataset.category = category.toLowerCase();
    const summary = document.createElement('summary');
    const categoryBox = document.createElement('input');
    categoryBox.type = 'checkbox'; categoryBox.className = 'categoryChoice';
    categoryBox.addEventListener('click', event => event.stopPropagation());
    categoryBox.addEventListener('change', () => {
      for (const box of details.querySelectorAll('.checkChoice:not(:disabled)')) {
        const row = box.closest('tr');
        if (!row.classList.contains('hidden')) box.checked = categoryBox.checked;
      }
      el('profile').value = '';
      updateCounts();
    });
    summary.appendChild(categoryBox);
    summary.append(' ' + category);
    details.appendChild(summary);

    const table = document.createElement('table');
    table.innerHTML = '<thead><tr><th>Select</th><th>Check ID</th><th>Tag</th><th>Component</th><th>Scope</th><th>Status</th><th>Description</th></tr></thead>';
    const tbody = document.createElement('tbody');
    for (const check of checks) {
      const tr = document.createElement('tr');
      tr.dataset.search = [check.category, check.id, check.ansible_tag, check.component, check.scope, check.task, check.implementation_status].join(' ').toLowerCase();
      tr.dataset.selectable = String(check.selectable);
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.className = 'checkChoice'; checkbox.value = check.id;
      checkbox.disabled = !check.selectable;
      checkbox.addEventListener('change', () => { el('profile').value = ''; updateCounts(); });
      const values = [checkbox, check.id, check.ansible_tag || '—', check.component, check.scope, check.implementation_status, check.task];
      for (const value of values) {
        const td = document.createElement('td');
        if (value instanceof Node) td.appendChild(value); else td.textContent = value;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody); details.appendChild(table); container.appendChild(details);
  }
  applyCheckFilters();
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
  return `${item.pass_count || 0}P/${item.fail_count || 0}F/${item.error_count || 0}E/${item.warn_count || 0}W/${item.skipped_count || 0}S`;
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
  const body = table.querySelector('tbody');
  body.textContent = '';
  const summary = state.summary;
  if (!summary || !summary.totals) {
    table.classList.add('hidden');
    if (state.running) el('quickSummary').textContent = 'Validation is running; summary will appear when aggregation completes.';
    else if (artifacts.run_path) el('quickSummary').textContent = 'Output folder created; no aggregate summary is available for this mode yet.';
    else el('quickSummary').textContent = 'No completed run yet.';
    return;
  }

  const totals = summary.totals;
  el('quickSummary').textContent = `Quick summary: ${totals.pass || 0}P/${totals.fail || 0}F/${totals.error || 0}E/${totals.warn || 0}W/${totals.skipped || 0}S across ${totals.servers || 0} system(s)`;
  for (const item of summary.servers || []) {
    const tr = document.createElement('tr');
    const values = [item.server_id, item.environment || '', item.overall_status || '', quickResultText(item)];
    for (const value of values) {
      const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
    }
    const reportCell = document.createElement('td');
    if (item.report_url) {
      const anchor = document.createElement('a');
      anchor.href = item.report_url; anchor.textContent = 'Open';
      anchor.target = '_blank'; anchor.rel = 'noopener'; reportCell.appendChild(anchor);
    } else {
      reportCell.textContent = '—';
    }
    tr.appendChild(reportCell); body.appendChild(tr);
  }
  table.classList.toggle('hidden', body.children.length === 0);
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status');
    const state = await response.json();
    el('run').disabled = state.running;
    el('stop').disabled = !state.running;
    let status = state.running ? 'Running' : (state.return_code === null ? 'Idle' : `Finished (exit ${state.return_code})`);
    if (state.error) status += ` — ${state.error}`;
    el('status').textContent = status;
    el('command').textContent = state.command.join(' ');
    renderResults(state);
    const output = el('output');
    const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 30;
    output.textContent = state.output;
    if (atBottom) output.scrollTop = output.scrollHeight;
  } catch (error) {
    el('status').textContent = 'UI connection error';
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

init().catch(error => { el('status').textContent = error.message; });
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
        self.send_header("Cache-Control", "no-store")
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
        return _path_within(candidate, self.app.paths.artifact_root)

    def _send_artifact_directory(self, directory: Path) -> None:
        root = self.app.paths.artifact_root.resolve()
        relative = directory.resolve().relative_to(root)
        title = "Artifacts" if not relative.parts else f"Artifacts / {relative.as_posix()}"
        entries: list[str] = []
        if relative.parts:
            parent = directory.parent
            entries.append(
                f'<li><a href="{html.escape(_artifact_url(parent, root, directory=True), quote=True)}">../</a></li>'
            )
        try:
            children = sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError:
            self._send_json({"error": "Could not read artifact directory"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        for child in children:
            resolved = _path_within(child, root)
            if resolved is None:
                continue
            is_directory = resolved.is_dir()
            suffix = "/" if is_directory else ""
            href = _artifact_url(resolved, root, directory=is_directory)
            entries.append(
                f'<li><a href="{html.escape(href, quote=True)}">{html.escape(child.name + suffix)}</a></li>'
            )
        document = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title></head><body>"
            f"<h1>{html.escape(title)}</h1><ul>{''.join(entries)}</ul></body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
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