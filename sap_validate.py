#!/usr/bin/env python3
"""Bulk SAP validation runner for one to thousands of servers.

The runner compiles CSV source data into an Ansible inventory, creates one
shared run directory, executes the existing validation tasks in serial batches,
and aggregates all per-server artifacts into run-level JSON/CSV/Markdown files.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from inventory_compiler import CompileError, compile_inventory, write_inventory
from runner.catalog import CatalogError, load_catalog, print_checks, resolve_tags
from runner.reporting import aggregate_run

ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "checks_catalog.json"
DEFAULT_PLAYBOOK = ROOT / "site.yml"
DEFAULT_DISCOVERY_PLAYBOOK = ROOT / "playbooks" / "discover.yml"


def _default_path(relative: str) -> Path:
    return ROOT / relative


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _copy_snapshot(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _slug(value: str) -> str:
    import re

    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", value)).strip("_").lower()


def _build_limit(args: argparse.Namespace) -> str | None:
    parts: list[str] = []
    if args.environment:
        parts.append(f"environment_{_slug(args.environment)}")
    if args.customer:
        parts.append(f"customer_{_slug(args.customer)}")
    for component in args.component or []:
        parts.append(f"component_{component}")
    if args.limit:
        parts.append(args.limit)
    if not parts:
        return None
    pattern = parts[0]
    for item in parts[1:]:
        pattern += f":&{item}"
    return pattern


def _selected_hosts(
    normalized: dict[str, Any], args: argparse.Namespace
) -> list[str] | None:
    servers = normalized.get("servers", {})
    if not servers:
        return None

    explicit: set[str] | None = None
    if args.limit:
        if any(token in args.limit for token in (":", "&", "!", "*", "?", "[")):
            return None
        explicit = {item.strip() for item in args.limit.split(",") if item.strip()}

    target_members: set[str] | None = None
    if args.target_group:
        groups = normalized.get("groups", {})
        if args.target_group not in groups:
            return []
        target_members = set(groups[args.target_group])

    selected = []
    for server_id, server in servers.items():
        if args.environment and _slug(str(server.get("environment", ""))) != _slug(args.environment):
            continue
        if args.customer and _slug(str(server.get("customer", ""))) != _slug(args.customer):
            continue
        if args.component and not set(args.component).issubset(set(server.get("components", []))):
            continue
        if explicit is not None and server_id not in explicit:
            continue
        if target_members is not None and server_id not in target_members:
            continue
        selected.append(server_id)
    return sorted(selected)


def _filter_tags_for_components(
    catalog: dict[str, Any], tags: list[str] | None, components: list[str] | None
) -> list[str] | None:
    """Keep host checks plus checks for the selected SAP component types."""
    if not components:
        return tags
    allowed = {
        check.get("ansible_tag")
        for check in catalog.get("checks", [])
        if check.get("ansible_tag")
        and (
            check.get("scope") == "host"
            or check.get("component") in set(components)
        )
    }
    if tags is None:
        return sorted(allowed)
    return [tag for tag in tags if tag in allowed]


def _retry_hosts(run_dir: Path, statuses: set[str]) -> list[str]:
    summary = _load_json(run_dir / "_summary.json")
    return sorted(
        item["server_id"]
        for item in summary.get("servers", [])
        if str(item.get("overall_status", "")).upper() in statuses
        or str(item.get("execution_status", "")).upper() in statuses
    )


def _run_command(cmd: list[str], log_path: Path) -> int:
    print("+ " + " ".join(shlex.quote(part) for part in cmd))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="")
                log.write(line)
            return process.wait()
    except FileNotFoundError:
        message = f"Executable not found: {cmd[0]}\n"
        print(message, file=sys.stderr, end="")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(message)
        return 127


def _write_run_metadata(
    *,
    run_dir: Path,
    timestamp: str,
    command: str,
    args: argparse.Namespace,
    selected_hosts: list[str] | None,
) -> None:
    git_commit = ""
    try:
        git_commit = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    payload = {
        "run_timestamp": timestamp,
        "created_at": datetime.now().astimezone().isoformat(),
        "command": command,
        "git_commit": git_commit,
        "selected_hosts": selected_hosts,
        "selection_is_exact": selected_hosts is not None,
        "profile": args.profile,
        "checks": args.checks,
        "components": args.component or [],
        "environment": args.environment,
        "customer": args.customer,
        "batch_size": args.batch_size,
        "forks": args.forks,
        "save_raw_outputs": args.save_raw_outputs,
        "strict": args.strict,
        "enable_incrond": args.enable_incrond,
        "enable_backint": args.enable_backint,
        "target_group": args.target_group,
        "resume": bool(args.resume),
    }
    run_metadata_path = run_dir / "_run.json"
    if not run_metadata_path.exists():
        run_metadata_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    attempts_path = run_dir / "_attempts.json"
    attempts = _load_json(attempts_path).get("attempts", [])
    attempts.append(payload)
    attempts_path.write_text(
        json.dumps({"attempts": attempts}, indent=2) + "\n", encoding="utf-8"
    )
    with (run_dir / "_invocation.txt").open("a", encoding="utf-8") as handle:
        handle.write(f"[{payload['created_at']}] {command}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile inventory and run bulk SAP validations.")
    parser.add_argument("--servers", type=Path, default=_default_path("inputs/servers.csv"))
    parser.add_argument("--instances", type=Path, default=_default_path("inputs/instances.csv"))
    parser.add_argument("--overrides", type=Path, default=_default_path("inputs/overrides.json"))
    parser.add_argument("--defaults", type=Path, default=_default_path("config/defaults.json"))
    parser.add_argument("--credentials", type=Path, default=_default_path("config/credentials.json"))
    parser.add_argument("-i", "--inventory", type=Path, help="Use an existing inventory instead of compiling CSV input")
    parser.add_argument("--playbook", type=Path, default=DEFAULT_PLAYBOOK)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--artifact-root", type=Path, default=_default_path("artifacts"))
    parser.add_argument("--generated-dir", type=Path, default=_default_path("generated"))
    parser.add_argument("--limit", help="Ansible host pattern or comma-separated server IDs")
    parser.add_argument("--environment")
    parser.add_argument("--customer")
    parser.add_argument(
        "--component",
        action="append",
        choices=["hana", "abap", "ascs", "webdispatcher", "host_agent"],
        help="Restrict to servers containing this component; repeat for intersections",
    )
    parser.add_argument("--target-group", help="Legacy play target override")
    parser.add_argument("--checks", help="Comma-separated catalog check IDs")
    parser.add_argument("--profile", help="Named catalog profile")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--enable-incrond", action="store_true")
    parser.add_argument("--enable-backint", action="store_true")
    parser.add_argument("--save-raw-outputs", action="store_true")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--forks", type=int)
    parser.add_argument("--discover", action="store_true", help="Run read-only SAP discovery before validation")
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="Compile and snapshot inventory without Ansible")
    parser.add_argument("--resume", type=Path, help="Reuse an existing run directory")
    parser.add_argument(
        "--retry",
        default="FAILED,UNREACHABLE,NOT_COMPLETED,FAIL",
        help="Statuses retried with --resume",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--syntax-check", action="store_true")
    parser.add_argument("--check-mode", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--category")
    parser.add_argument("--automated-only", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        catalog = load_catalog(args.catalog)
    except CatalogError as exc:
        parser.error(str(exc))

    if args.list:
        print_checks(catalog, args.category, args.automated_only)
        return 0

    prior_run: dict[str, Any] = {}
    if args.resume:
        prior_run = _load_json(args.resume.resolve() / "_run.json")
        if not args.profile and not args.checks:
            args.profile = prior_run.get("profile")
            args.checks = prior_run.get("checks")
        if not args.component:
            args.component = prior_run.get("components") or None
        if not args.environment:
            args.environment = prior_run.get("environment")
        if not args.customer:
            args.customer = prior_run.get("customer")
        if not args.save_raw_outputs:
            args.save_raw_outputs = bool(prior_run.get("save_raw_outputs", False))
        if not args.strict:
            args.strict = bool(prior_run.get("strict", False))
        if not args.enable_incrond:
            args.enable_incrond = bool(prior_run.get("enable_incrond", False))
        if not args.enable_backint:
            args.enable_backint = bool(prior_run.get("enable_backint", False))
        if not args.target_group:
            args.target_group = prior_run.get("target_group")

    execution_defaults = _load_json(args.defaults).get("execution", {})
    if args.batch_size is None:
        args.batch_size = int(prior_run.get("batch_size") or execution_defaults.get("batch_size", 50))
    if args.forks is None:
        args.forks = int(prior_run.get("forks") or execution_defaults.get("forks", 50))

    try:
        tags, profile_extra_vars = resolve_tags(catalog, args.profile, args.checks)
    except CatalogError as exc:
        parser.error(str(exc))
    if not args.checks:
        tags = _filter_tags_for_components(catalog, tags, args.component)

    if args.batch_size < 1 or args.forks < 1:
        parser.error("--batch-size and --forks must be positive integers")

    if args.resume:
        run_dir = args.resume.resolve()
        if not run_dir.exists():
            parser.error(f"Resume directory does not exist: {run_dir}")
        timestamp = run_dir.name
        inventory_path = run_dir / "_input" / "generated_inventory.yml"
        normalized_path = run_dir / "_input" / "normalized_inventory.json"
        normalized = _load_json(normalized_path)
        retry_statuses = {item.strip().upper() for item in args.retry.split(",") if item.strip()}
        retry_hosts = _retry_hosts(run_dir, retry_statuses)
        if not retry_hosts:
            print("No servers in the requested retry states.")
            return 0
        args.limit = ",".join(retry_hosts)
    else:
        base_timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        timestamp = base_timestamp
        run_dir = (args.artifact_root / timestamp).resolve()
        suffix = 1
        while run_dir.exists():
            timestamp = f"{base_timestamp}_{suffix:02d}"
            run_dir = (args.artifact_root / timestamp).resolve()
            suffix += 1
        run_dir.mkdir(parents=True, exist_ok=False)
        if args.inventory:
            inventory_path = args.inventory.resolve()
            normalized_path = args.generated_dir / "normalized_inventory.json"
            normalized = _load_json(normalized_path)
        else:
            inventory_path = (args.generated_dir / "inventory.yml").resolve()
            normalized_path = (args.generated_dir / "normalized_inventory.json").resolve()
            try:
                inventory, normalized = compile_inventory(
                    servers_path=args.servers.resolve(),
                    instances_path=args.instances.resolve(),
                    defaults_path=args.defaults.resolve(),
                    credentials_path=args.credentials.resolve(),
                    overrides_path=args.overrides.resolve(),
                )
                write_inventory(
                    inventory,
                    normalized,
                    inventory_path=inventory_path,
                    normalized_path=normalized_path,
                )
            except CompileError as exc:
                print(f"Inventory compilation failed: {exc}", file=sys.stderr)
                return 2

        input_dir = run_dir / "_input"
        for source, name in [
            (args.servers, "servers.csv"),
            (args.instances, "instances.csv"),
            (args.overrides, "overrides.json"),
            (args.defaults, "defaults.json"),
            (args.catalog, "checks_catalog.json"),
            (inventory_path, "generated_inventory.yml"),
            (normalized_path, "normalized_inventory.json"),
        ]:
            _copy_snapshot(source.resolve(), input_dir / name)

    selected_hosts = _selected_hosts(normalized, args)
    if selected_hosts == [] and normalized.get("servers"):
        print("No enabled servers matched the requested filters.", file=sys.stderr)
        return 2
    limit_pattern = _build_limit(args)

    extra_vars = dict(profile_extra_vars)
    if args.strict:
        extra_vars["hana_strict_validation"] = True
    if args.enable_incrond:
        extra_vars["hana_validate_incrond"] = True
    if args.enable_backint:
        extra_vars["hana_validate_backint"] = True
    if args.save_raw_outputs:
        extra_vars["save_raw_outputs"] = True
    if args.target_group:
        extra_vars["target_group"] = args.target_group
    extra_vars.update(
        {
            "run_timestamp": timestamp,
            "validation_run_dir": str(run_dir),
            "validation_batch_size": args.batch_size,
        }
    )

    def ansible_command(playbook: Path) -> list[str]:
        command = ["ansible-playbook", "-i", str(inventory_path), str(playbook), "--forks", str(args.forks)]
        if limit_pattern:
            command += ["--limit", limit_pattern]
        if tags and playbook == args.playbook:
            command += ["--tags", ",".join(sorted(set(tags)))]
        if args.syntax_check:
            command.append("--syntax-check")
        if args.check_mode:
            command.append("--check")
        if args.verbose:
            command.append("-" + "v" * min(args.verbose, 4))
        for key, value in extra_vars.items():
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            command += ["-e", f"{key}={rendered}"]
        return command

    validation_cmd = ansible_command(args.playbook.resolve())
    invoked_command = " ".join(shlex.quote(part) for part in validation_cmd)
    validation_cmd += ["-e", f"invoked_command={shlex.quote(invoked_command)}"]
    _write_run_metadata(
        run_dir=run_dir,
        timestamp=timestamp,
        command=invoked_command,
        args=args,
        selected_hosts=selected_hosts,
    )

    print(f"Prepared inventory for {len(normalized.get('servers', {}))} enabled server(s).")
    selected_label = len(selected_hosts) if selected_hosts is not None else "Ansible-pattern-defined"
    print(f"Selected {selected_label} server(s).")
    print(f"Run directory: {run_dir}")

    if args.prepare_only:
        return 0
    if args.dry_run:
        if args.discover or args.discover_only:
            print("+ " + " ".join(shlex.quote(part) for part in ansible_command(DEFAULT_DISCOVERY_PLAYBOOK)))
        print("+ " + " ".join(shlex.quote(part) for part in validation_cmd))
        return 0

    return_code = 0
    if args.discover or args.discover_only:
        discovery_cmd = ansible_command(DEFAULT_DISCOVERY_PLAYBOOK)
        return_code = _run_command(discovery_cmd, run_dir / "_controller.log")
        if args.discover_only:
            aggregate_run(
                run_dir=run_dir,
                normalized=normalized,
                selected_hosts=selected_hosts,
                ansible_return_code=return_code,
                command=" ".join(shlex.quote(part) for part in discovery_cmd),
                run_timestamp=timestamp,
                mode="discovery",
            )
            return return_code

    if return_code == 0:
        return_code = _run_command(validation_cmd, run_dir / "_controller.log")

    if args.syntax_check:
        return return_code

    aggregate_run(
        run_dir=run_dir,
        normalized=normalized,
        selected_hosts=selected_hosts,
        ansible_return_code=return_code,
        command=invoked_command,
        run_timestamp=timestamp,
    )
    print(f"Summary: {run_dir / '_summary.md'}")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
