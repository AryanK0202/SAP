import json
from pathlib import Path

catalog_path = Path("checks_catalog.json")

data = json.loads(catalog_path.read_text(encoding="utf-8"))

new_checks = []

for check in data["checks"]:
    old_category = check.get("category", "Uncategorized")

    updated = {}

    for key, value in check.items():
        if key == "category":
            updated["category"] = "Build Validation"
            updated["group"] = old_category
            updated["contributor"] = "Sophia"
        elif key == "task_file" and isinstance(value, str) and value.startswith("tasks/"):
            updated[key] = "tasks/build_validation/" + value[len("tasks/"):]
        else:
            updated[key] = value

    new_checks.append(updated)

data["checks"] = new_checks

catalog_path.write_text(
    json.dumps(data, indent=2) + "\n",
    encoding="utf-8"
)