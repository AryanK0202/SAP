# SAP Validation Runner Parameters and Raw-Output Guide

## Recommended command

Use the bulk runner rather than calling `ansible-playbook` directly:

```bash
./sap_validate.py \
  --profile production-readiness \
  --save-raw-outputs \
  --batch-size 50 \
  --forks 40
```

The runner performs four jobs that direct Ansible execution does not:

1. Compiles CSV server and instance data into inventory.
2. Creates one shared timestamp directory.
3. captures the full controller transcript.
4. Aggregates per-server JSON, CSV, and Markdown results.

## Output types

### Controller transcript

Every runner invocation writes:

```text
artifacts/<timestamp>/_controller.log
```

This contains the combined Ansible console output and play recap. It is useful
for troubleshooting execution failures and unreachable hosts.

### Structured per-server results

Every server that completes post-processing writes:

```text
artifacts/<timestamp>/<server_id>/results.json
artifacts/<timestamp>/<server_id>/status.json
artifacts/<timestamp>/<server_id>/metadata.json
artifacts/<timestamp>/<server_id>/report.md
```

The Python aggregator also creates missing metadata/status records for selected
servers when possible.

### Raw evidence

Enable raw output files with:

```bash
./sap_validate.py --profile production-readiness --save-raw-outputs
```

Files are written below:

```text
artifacts/<timestamp>/<server_id>/raw/
```

The existing validation tasks determine which evidence files are created. The
refactor changes the containing directory, not the command or validation logic.

### Run-level summaries

```text
_summary.json   machine-readable run summary
_summary.csv    one row per server
_summary.md     human-readable fleet summary
_results.csv    one row per check result per server
_manifest.md    Ansible-generated table of contents for completed hosts
```

## Inventory parameters

### `--servers PATH`

Server source CSV. Default:

```text
inputs/servers.csv
```

### `--instances PATH`

SAP component/instance CSV. Default:

```text
inputs/instances.csv
```

### `--overrides PATH`

Host and customer exception JSON. Default:

```text
inputs/overrides.json
```

### `--defaults PATH`

Global compiler/execution defaults. Default:

```text
config/defaults.json
```

### `--credentials PATH`

Reusable SSH credential profile references. Default:

```text
config/credentials.json
```

### `-i`, `--inventory PATH`

Use a pre-existing Ansible inventory instead of compiling the CSV files.
Fleet summaries are most complete when a corresponding normalized inventory is
available in `generated/normalized_inventory.json`.

### `--prepare-only`

Compile and snapshot the inventory without running Ansible:

```bash
./sap_validate.py --prepare-only
```

The standalone equivalent is:

```bash
python3 -m inventory_compiler.cli
```

## Check selection

### `--profile NAME`

Select a named profile from `checks_catalog.json`:

```bash
./sap_validate.py --profile core-connectivity
./sap_validate.py --profile lab-quick
./sap_validate.py --profile strict-full
./sap_validate.py --profile production-readiness
```

### `--checks ID1,ID2`

Select exact catalog check IDs:

```bash
./sap_validate.py --checks login,sudo_root,hana_version
```

A check without `ansible_tag` is documented but not runnable.

### `--list`

List catalog checks:

```bash
./sap_validate.py --list
./sap_validate.py --list --automated-only
./sap_validate.py --list --category "Configuration & Settings"
```

The listing includes the check scope and component metadata.

## Fleet selection

### `--environment NAME`

Restrict to the generated `environment_<name>` group:

```bash
./sap_validate.py --environment production --profile production-readiness
```

### `--customer NAME`

Restrict to one customer:

```bash
./sap_validate.py --customer RTS --profile production-readiness
```

### `--component NAME`

Restrict to servers that contain a component:

```bash
./sap_validate.py --component hana --profile production-readiness
./sap_validate.py --component abap --profile production-readiness
```

For profile runs, the runner also removes component-scoped tags that do not
match the selected component. Host-scoped checks remain enabled. Explicit
`--checks` selections are not modified.

Repeat the option to require multiple components on the same server:

```bash
./sap_validate.py --component hana --component abap --profile lab-quick
```

### `--limit PATTERN`

Pass an Ansible limit pattern or an exact comma-separated server list:

```bash
./sap_validate.py --limit sap-sandbox-01 --profile lab-quick
./sap_validate.py --limit "hdb-prod-001,hdb-prod-002" --profile strict-full
```

Environment, customer, component, and limit filters are intersected.

### `--target-group GROUP`

Legacy play-host override. Normal runs should use generated filters instead.
The default play target is `sap_hosts`.

## Execution controls

### `--batch-size NUMBER`

Sets the Ansible play `serial` value. Default: `50`.

```bash
./sap_validate.py --profile production-readiness --batch-size 25
```

### `--forks NUMBER`

Sets the Ansible worker count. Default: `50`.

```bash
./sap_validate.py --profile production-readiness --forks 40
```

Batch size controls how many hosts form a logical wave. Forks controls how many
workers can execute concurrently inside that wave.

### `--strict`

Sets the existing `hana_strict_validation=true` behavior. The validation task
assertions are unchanged.

### `--enable-incrond`

Sets `hana_validate_incrond=true`.

### `--enable-backint`

Sets `hana_validate_backint=true`.

### `--save-raw-outputs`

Sets `save_raw_outputs=true` and creates each server's `raw/` directory.

### `--syntax-check`

Runs Ansible syntax validation using the generated inventory and selected
playbook.

### `--check-mode`

Adds Ansible check mode. Many validation commands are already read-only, but
check mode can still affect modules and should not replace a normal test run.

### `-v` through `-vvvv`

Increase Ansible verbosity:

```bash
./sap_validate.py -vv --profile core-connectivity
```

### `--dry-run`

Compile inventory, create the run snapshot, and print commands without invoking
Ansible.

## Discovery

### `--discover`

Run the read-only discovery play before validation. Evidence is stored in:

```text
<server_id>/discovery/discovery.json
```

The evidence includes:

- `/usr/sap/sapservices`
- SAP Host Agent `ListInstances`
- local SAP directory paths

Discovery does not currently rewrite `instances.csv`; declared component rows
remain authoritative.

### `--discover-only`

Run only the discovery play.

## Resume and retry

### `--resume RUN_DIRECTORY`

Reuse the generated inventory snapshot and timestamp folder from a prior run:

```bash
./sap_validate.py --resume artifacts/20260721_150000
```

### `--retry STATUS1,STATUS2`

Choose the server states to rerun. Default:

```text
FAILED,UNREACHABLE,NOT_COMPLETED,FAIL
```

Example:

```bash
./sap_validate.py \
  --resume artifacts/20260721_150000 \
  --retry UNREACHABLE,NOT_COMPLETED
```

## Direct `ansible-playbook` execution

Generate inventory first:

```bash
python3 -m inventory_compiler.cli
```

Run the full play:

```bash
RUN_TS=$(date +%Y%m%d_%H%M%S)
ansible-playbook -i generated/inventory.yml site.yml \
  -e run_timestamp="$RUN_TS" \
  -e validation_batch_size=50 \
  -e save_raw_outputs=true \
  --forks 40
```

Select tags:

```bash
ansible-playbook -i generated/inventory.yml site.yml \
  --tags login,sudo_root,hana_version \
  -e run_timestamp=$(date +%Y%m%d_%H%M%S)
```

Limit to a generated component group:

```bash
ansible-playbook -i generated/inventory.yml site.yml \
  --limit component_hana \
  --tags hana_version,services_green \
  -e run_timestamp=$(date +%Y%m%d_%H%M%S)
```

Direct execution still creates per-server reports and `_manifest.md`, but it
does not create the runner's `_summary.json`, `_summary.csv`, `_results.csv`,
input snapshots, or retry metadata.

## Exit codes

The runner returns the Ansible process return code:

- `0`: Ansible completed successfully
- nonzero: Ansible reported an execution, assertion, syntax, or connectivity
  failure
- `2`: input or catalog configuration error
- `127`: `ansible-playbook` was not found

Check results recorded as `WARN` or `FAIL` do not necessarily produce a nonzero
Ansible exit code unless the existing task implementation asserts or fails.
Inspect `_summary.json` or `_summary.csv` when an automation system must evaluate
reported findings independently of process execution.

## Operational recipes

### Validate ten servers first

```bash
./sap_validate.py \
  --limit "server-001,server-002,server-003,server-004,server-005,server-006,server-007,server-008,server-009,server-010" \
  --profile production-readiness \
  --save-raw-outputs \
  --batch-size 5 \
  --forks 5
```

### Validate every production HANA server

```bash
./sap_validate.py \
  --environment production \
  --component hana \
  --profile production-readiness \
  --batch-size 50 \
  --forks 40
```

### Validate one customer and keep verbose evidence

```bash
./sap_validate.py \
  --customer RTS \
  --profile production-readiness \
  --save-raw-outputs \
  -vv
```

### Find the latest run

```bash
latest=$(find artifacts -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
echo "$latest"
cat "$latest/_summary.md"
```
