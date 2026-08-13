# Refactor Summary

## Added

- `sap_validate.py`: bulk inventory compilation, batching, filtering, logging,
  aggregation, discovery orchestration, and retry support.
- `inventory_compiler/`: standard-library CSV/JSON inventory compiler.
- `runner/`: catalog and run-summary helpers.
- `inputs/servers.csv`, `inputs/instances.csv`, and `inputs/overrides.json`.
- `config/defaults.json` and reusable `config/credentials.json` profiles.
- `playbooks/discover.yml` for optional read-only discovery evidence.
- Structured per-server `metadata.json`, `results.json`, and `status.json`.
- Run-level `_summary.json`, `_summary.csv`, `_summary.md`, and `_results.csv`.
- Regression tests, including compilation of a 1,000-server sample fleet.

## Changed

- Default inventory is now generated from source files.
- The main play targets `sap_hosts` and uses configurable `serial` batches.
- Each timestamp folder now contains one direct subfolder per server.
- Raw evidence moved to `<server_id>/raw/`.
- The checks catalog now includes `scope` and `component` metadata.
- The legacy inventory defines the sandbox server once and uses child groups.
- Usage and raw-output documentation now describe fleet operation.

## Compatibility

- Validation task files under `tasks/` were not functionally changed.
- Generated inventory still provides `hana_db`, `nw_app`, `hana_sid`,
  `app_sid`, and the other variables consumed by the existing task code.
- `run_hana_checks.py` remains as a compatibility entry point and delegates to
  `sap_validate.py`.