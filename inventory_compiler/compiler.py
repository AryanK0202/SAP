#!/usr/bin/env python3
"""Compile scalable SAP server/instance input files into Ansible inventory.

The compiler intentionally uses only the Python standard library. It creates a
canonical normalized model and a static YAML inventory. Existing validation
files continue to receive their legacy scalar variables (hana_sid, app_sid,
etc.) while new code can consume the sap_instances list and modern component
inventory groups.
"""

from __future__ import annotations

import csv
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


class CompileError(ValueError):
    """Raised when source inventory data is incomplete or inconsistent."""


SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
SUPPORTED_COMPONENTS = {
    "hana",
    "abap",
    "ascs",
    "webdispatcher",
    "host_agent",
}


def _read_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise CompileError(f"Required JSON file not found: {path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompileError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CompileError(f"Expected a JSON object in {path}")
    return data


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise CompileError(f"Required CSV file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise CompileError(f"CSV has no header: {path}")
        return [
            {str(k).strip(): (v or "").strip() for k, v in row.items() if k is not None}
            for row in reader
            if any((v or "").strip() for v in row.values())
        ]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise CompileError(f"Invalid boolean value: {value!r}")


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CompileError(f"Invalid integer value: {value!r}") from exc


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise CompileError(f"Cannot create inventory group from {value!r}")
    return slug.lower()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _clean_dict(row: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in row.items() if v != ""}


def _derive_instance(row: dict[str, str]) -> dict[str, Any]:
    component = row.get("component", "").strip().lower()
    if component not in SUPPORTED_COMPONENTS:
        raise CompileError(
            f"Unsupported component {component!r}; expected one of "
            f"{', '.join(sorted(SUPPORTED_COMPONENTS))}"
        )

    sid = row.get("sid", "").strip().upper()
    raw_instance_number = row.get("instance_number", "").strip()
    instance_number = raw_instance_number.zfill(2) if raw_instance_number else ""
    if component != "host_agent" and not sid:
        raise CompileError(f"Component {component!r} requires a SID")
    if component in {"hana", "abap", "ascs"} and not instance_number:
        raise CompileError(f"Component {component!r} requires an instance_number")

    default_prefix = {"hana": "HDB", "abap": "D", "ascs": "ASCS"}.get(component, "")
    instance_name = row.get("instance_name", "").strip()
    if not instance_name and default_prefix and instance_number:
        instance_name = f"{default_prefix}{instance_number}"

    admin_user = row.get("admin_user", "").strip()
    if not admin_user and sid:
        admin_user = f"{sid.lower()}adm"

    data: dict[str, Any] = {
        "component": component,
        "sid": sid,
        "instance_number": instance_number,
        "instance_name": instance_name,
        "virtual_hostname": row.get("virtual_hostname", "").strip(),
        "admin_user": admin_user,
        "role": row.get("role", "").strip(),
        "tenant": row.get("tenant", "").strip(),
        "userstore_key": row.get("userstore_key", "").strip(),
        "hdbsql_bin": row.get("hdbsql_bin", "").strip(),
        "profile_dir": row.get("profile_dir", "").strip(),
        "default_pfl_path": row.get("default_pfl_path", "").strip(),
        "db_schema": row.get("db_schema", "").strip(),
    }
    return {key: value for key, value in data.items() if value != ""}


def _compatibility_vars(instances: list[dict[str, Any]]) -> dict[str, Any]:
    """Render variables expected by the existing validated task files."""

    result: dict[str, Any] = {}
    hana = next((i for i in instances if i["component"] == "hana"), None)
    abap = next((i for i in instances if i["component"] == "abap"), None)
    ascs = next((i for i in instances if i["component"] == "ascs"), None)

    if hana:
        sid = hana["sid"]
        number = hana["instance_number"]
        admin = hana.get("admin_user", f"{sid.lower()}adm")
        hdbsql_bin = hana.get("hdbsql_bin", f"/usr/sap/{sid}/HDB{number}/exe/hdbsql")
        result.update(
            {
                "hana_sid": sid,
                "hana_instance_number": number,
                "hana_admin_user": admin,
                "hana_admin_home": f"/usr/sap/{sid}/home",
                "hdbsql_userkey": hana.get("userstore_key", "HDB_KEY_CAL"),
                "hana_hdbsql_bin": hdbsql_bin,
                "global_ini_remote_path": f"/usr/sap/{sid}/SYS/global/hdb/custom/config/global.ini",
                "backint_integration_path": f"/usr/sap/{sid}/SYS/global/hdb/opt",
                "hana_mounts_to_validate": [
                    "/",
                    f"/hana/data/{sid}",
                    f"/hana/log/{sid}",
                    f"/hana/shared/{sid}",
                ],
            }
        )

    if abap:
        sid = abap["sid"]
        number = abap["instance_number"]
        admin = abap.get("admin_user", f"{sid.lower()}adm")
        profile_dir = abap.get("profile_dir", f"/sapmnt/{sid}/profile")
        result.update(
            {
                "app_sid": sid,
                "app_instance_number": number,
                "app_admin_user": admin,
                "app_admin_home": f"/home/{admin}",
                "app_profile_dir": profile_dir,
                "app_default_pfl_remote_path": abap.get(
                    "default_pfl_path", f"{profile_dir}/DEFAULT.PFL"
                ),
                "app_hdbsql_userkey": abap.get("userstore_key", "DEFAULT"),
                "app_db_schema": abap.get("db_schema", ""),
                "app_hdbsql_bin": abap.get(
                    "hdbsql_bin",
                    result.get("hana_hdbsql_bin", "/usr/sap/hdbclient/hdbsql"),
                ),
            }
        )

    if ascs:
        result.setdefault("app_sid", ascs["sid"])
        result.setdefault("app_admin_user", ascs.get("admin_user", f"{ascs['sid'].lower()}adm"))
        result["app_ascs_instance_number"] = ascs["instance_number"]

    return result


def compile_inventory(
    *,
    servers_path: Path,
    instances_path: Path,
    defaults_path: Path,
    credentials_path: Path,
    overrides_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(ansible_inventory, normalized_model)``."""

    defaults = _read_json(defaults_path)
    credentials = _read_json(credentials_path)
    overrides = _read_json(overrides_path, required=False) if overrides_path else {}
    server_rows = _read_csv(servers_path)
    instance_rows = _read_csv(instances_path)

    connection_defaults = defaults.get("connection", {})
    credential_profiles = credentials.get("profiles", {})
    host_agent_default = _as_bool(
        defaults.get("inventory", {}).get("host_agent_expected_by_default", True),
        True,
    )

    servers: dict[str, dict[str, Any]] = {}
    for row in server_rows:
        server_id = row.get("server_id", "").strip()
        if not server_id:
            raise CompileError("Every servers.csv row requires server_id")
        if not SAFE_ID.fullmatch(server_id):
            raise CompileError(
                f"Unsafe server_id {server_id!r}; use letters, digits, '.', '_' or '-'"
            )
        if server_id in servers:
            raise CompileError(f"Duplicate server_id in servers.csv: {server_id}")
        if not _as_bool(row.get("enabled", "true"), True):
            continue

        address = row.get("address", "").strip()
        if not address:
            raise CompileError(f"Server {server_id} has no address")

        profile_name = (
            row.get("credential_profile", "").strip()
            or connection_defaults.get("credential_profile", "")
        )
        profile = credential_profiles.get(profile_name, {}) if profile_name else {}
        if profile_name and profile_name not in credential_profiles:
            raise CompileError(
                f"Server {server_id} references unknown credential profile {profile_name!r}"
            )

        ssh_user = row.get("ssh_user", "").strip() or profile.get("ssh_user")
        private_key = (
            row.get("private_key_file", "").strip() or profile.get("private_key_file")
        )
        ssh_port = _as_int(
            row.get("ssh_port", "") or profile.get("ssh_port"),
            int(connection_defaults.get("ssh_port", 22)),
        )
        python_interpreter = (
            row.get("python_interpreter", "").strip()
            or profile.get("python_interpreter")
            or connection_defaults.get("python_interpreter")
        )

        host: dict[str, Any] = {
            "server_id": server_id,
            "address": address,
            "physical_ip": row.get("physical_ip", "").strip(),
            "physical_hostname": row.get("physical_hostname", "").strip(),
            "environment": row.get("environment", "unassigned").strip() or "unassigned",
            "customer": row.get("customer", "unassigned").strip() or "unassigned",
            "credential_profile": profile_name,
            "host_agent_expected": _as_bool(
                row.get("host_agent_expected", ""), host_agent_default
            ),
            "connection": {
                "ssh_user": ssh_user,
                "ssh_port": ssh_port,
                "private_key_file": private_key,
                "python_interpreter": python_interpreter,
            },
            "instances": [],
        }
        servers[server_id] = host

    for row in instance_rows:
        server_id = row.get("server_id", "").strip()
        if server_id not in servers:
            if server_id and any(r.get("server_id", "").strip() == server_id for r in server_rows):
                # The server exists but is disabled.
                continue
            raise CompileError(f"instances.csv references unknown server_id: {server_id!r}")
        servers[server_id]["instances"].append(_derive_instance(row))

    server_overrides = overrides.get("servers", {})
    customer_overrides = overrides.get("customers", {})
    normalized_servers: dict[str, Any] = {}
    groups: dict[str, set[str]] = {"sap_hosts": set()}

    for server_id, server in sorted(servers.items()):
        host_vars: dict[str, Any] = {
            "ansible_host": server["address"],
            "server_id": server_id,
            "physical_ip": server.get("physical_ip", ""),
            "physical_hostname": server.get("physical_hostname", ""),
            "sap_environment": server["environment"],
            "sap_customer": server["customer"],
            "sap_instances": server["instances"],
            "sap_host_agent": {"expected": server["host_agent_expected"]},
        }
        connection = server["connection"]
        if connection.get("ssh_user"):
            host_vars["ansible_user"] = connection["ssh_user"]
        host_vars["ansible_port"] = connection["ssh_port"]
        if connection.get("private_key_file"):
            host_vars["ansible_ssh_private_key_file"] = connection["private_key_file"]
        if connection.get("python_interpreter"):
            host_vars["ansible_python_interpreter"] = connection["python_interpreter"]

        host_vars.update(_compatibility_vars(server["instances"]))
        customer_data = customer_overrides.get(server["customer"], {})
        if isinstance(customer_data, dict):
            host_vars = _deep_merge(host_vars, customer_data)
        specific = server_overrides.get(server_id, {})
        if isinstance(specific, dict):
            host_vars = _deep_merge(host_vars, specific)

        groups["sap_hosts"].add(server_id)
        groups.setdefault(f"environment_{_slug(server['environment'])}", set()).add(server_id)
        groups.setdefault(f"customer_{_slug(server['customer'])}", set()).add(server_id)

        components = {i["component"] for i in server["instances"]}
        if server["host_agent_expected"]:
            components.add("host_agent")
        for component in components:
            groups.setdefault(f"component_{component}", set()).add(server_id)

        # Compatibility aliases used by the existing, validated task files.
        if "hana" in components:
            groups.setdefault("hana_db", set()).add(server_id)
        if "abap" in components:
            groups.setdefault("nw_app", set()).add(server_id)

        normalized_servers[server_id] = {
            **server,
            "host_vars": host_vars,
            "components": sorted(components),
        }

    children: dict[str, Any] = {}
    for group_name, members in sorted(groups.items()):
        children[group_name] = {"hosts": {host: {} for host in sorted(members)}}

    inventory = {
        "all": {
            "children": children,
            "hosts": {
                server_id: data["host_vars"]
                for server_id, data in sorted(normalized_servers.items())
            },
        }
    }
    normalized = {
        "source": {
            "servers": str(servers_path),
            "instances": str(instances_path),
            "defaults": str(defaults_path),
            "credentials": str(credentials_path),
            "overrides": str(overrides_path) if overrides_path else None,
        },
        "servers": normalized_servers,
        "groups": {name: sorted(members) for name, members in sorted(groups.items())},
    }
    return inventory, normalized


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_lines(value: Any, indent: int = 0) -> list[str]:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return [prefix + "{}"]
        lines: list[str] = []
        for key, child in value.items():
            rendered_key = json.dumps(str(key), ensure_ascii=False)
            if isinstance(child, (dict, list)) and child:
                lines.append(f"{prefix}{rendered_key}:")
                lines.extend(_yaml_lines(child, indent + 2))
            elif isinstance(child, (dict, list)):
                lines.append(f"{prefix}{rendered_key}: {'{}' if isinstance(child, dict) else '[]'}")
            else:
                lines.append(f"{prefix}{rendered_key}: {_yaml_scalar(child)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [prefix + "[]"]
        lines = []
        for child in value:
            if isinstance(child, dict):
                if not child:
                    lines.append(prefix + "- {}")
                    continue
                first = True
                for key, item in child.items():
                    rendered_key = json.dumps(str(key), ensure_ascii=False)
                    marker = "- " if first else "  "
                    if isinstance(item, (dict, list)) and item:
                        lines.append(f"{prefix}{marker}{rendered_key}:")
                        lines.extend(_yaml_lines(item, indent + 4))
                    elif isinstance(item, (dict, list)):
                        lines.append(
                            f"{prefix}{marker}{rendered_key}: "
                            f"{'{}' if isinstance(item, dict) else '[]'}"
                        )
                    else:
                        lines.append(f"{prefix}{marker}{rendered_key}: {_yaml_scalar(item)}")
                    first = False
            elif isinstance(child, list):
                lines.append(prefix + "-")
                lines.extend(_yaml_lines(child, indent + 2))
            else:
                lines.append(f"{prefix}- {_yaml_scalar(child)}")
        return lines
    return [prefix + _yaml_scalar(value)]


def write_inventory(
    inventory: dict[str, Any],
    normalized: dict[str, Any],
    *,
    inventory_path: Path,
    normalized_path: Path,
) -> None:
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text("---\n" + "\n".join(_yaml_lines(inventory)) + "\n", encoding="utf-8")
    normalized_path.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
