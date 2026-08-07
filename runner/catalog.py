"""Check-catalog helpers shared by the command-line runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    pass


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"Catalog not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Catalog is not valid JSON: {exc}") from exc
    if not isinstance(data.get("checks"), list) or not isinstance(data.get("profiles"), dict):
        raise CatalogError(f"Catalog {path} must contain checks[] and profiles{{}}")
    return data


def resolve_tags(catalog: dict[str, Any], profile: str | None, checks: str | None):
    if profile:
        selected = catalog["profiles"].get(profile)
        if selected is None:
            raise CatalogError(
                f"Unknown profile {profile!r}. Known: {', '.join(catalog['profiles'])}"
            )
        return selected.get("tags", []), selected.get("extra_vars", {})
    if checks:
        by_id = {item["id"]: item for item in catalog["checks"]}
        tags: list[str] = []
        for check_id in [item.strip() for item in checks.split(",") if item.strip()]:
            check = by_id.get(check_id)
            if check is None:
                raise CatalogError(f"Unknown check id: {check_id} (see --list)")
            tag = check.get("ansible_tag")
            if not tag:
                raise CatalogError(f"Check {check_id!r} has no Ansible automation yet")
            tags.append(tag)
        return tags, {}
    return None, {}


def print_checks(catalog: dict[str, Any], category: str | None, automated_only: bool) -> None:
    checks = []
    for check in catalog["checks"]:
        if category and check["category"].lower() != category.lower():
            continue
        if automated_only and not check.get("ansible_tag"):
            continue
        checks.append(check)
    if not checks:
        print("(no checks match that filter)")
        return
    rows = [
        (
            "ID",
            "SCOPE",
            "COMPONENT",
            "CATEGORY",
            "GROUP",
            "CONTRIBUTOR",
            "TAG",
            "TASK FILE",
        )
    ]

    for check in checks:
        rows.append(
            (
                check["id"],
                check.get("scope", "-"),
                check.get("component", "-"),
                check.get("category", "-"),
                check.get("group", "-"),
                check.get("contributor", "-"),
                check.get("ansible_tag") or "-",
                check.get("task_file") or "-",
            )
        )
    widths = [max(len(str(row[i])) for row in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))
