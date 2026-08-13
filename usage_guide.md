# SAP S/4HANA and HANA Bulk Validation — Usage Guide

## Purpose

The project is designed to run the existing validation checks against one SAP
server or a fleet of 1,000 or more servers with minimal per-server setup.

The operator maintains source data files. The runner generates Ansible
inventory, selects applicable component groups, controls concurrency, and
creates a timestamped result tree with one direct subfolder per server.

```text
inputs/servers.csv + inputs/instances.csv
                    |
                    v
          inventory compiler
                    |
                    v
          generated/inventory.yml
                    |
                    v
       batched ansible-playbook run
                    |
                    v
artifacts/<timestamp>/<server_id>/...
```

## Important compatibility guarantee

The check implementations in `tasks/` were not functionally rewritten. They
still use the tested variables and conditions such as:

- `hana_sid`, `hana_instance_number`, `hana_admin_user`
- `app_sid`, `app_instance_number`, `app_admin_user`
- inventory groups `hana_db` and `nw_app`
- result accumulator `hana_check_results`

The inventory compiler derives those values from the scalable input model. It
also creates modern groups such as `component_hana` and `component_abap`.

## Repository layout

```text
.
├── sap_validate.py                  # primary bulk runner
├── run_hana_checks.py               # backward-compatible runner name
├── site.yml                         # existing validation orchestration
├── tasks/                           # existing validation task implementations
├── checks_catalog.json              # checks, tags, profiles, scope metadata
├── config/
│   ├── defaults.json                # connection/execution defaults
│   └── credentials.json             # reusable SSH credential profiles
├── inputs/
│   ├── servers.csv                  # one row per physical/virtual server
│   ├── instances.csv                # zero or more SAP components per server
│   └── overrides.json               # host/customer exceptions
├── inventory_compiler/              # CSV normalization and inventory generation
├── runner/                          # catalog and result aggregation helpers
├── generated/                       # generated inventory and normalized model
├── group_vars/                      # global and generic component defaults
├── inventory/                       # one-host legacy/manual example
├── playbooks/discover.yml           # optional read-only discovery evidence
└── artifacts/                       # timestamped run output
```

## Prerequisites

Install on the controller:

- Python 3.10 or later
- Ansible Core
- SSH access to the target servers
- privilege escalation to the required SAP administrator accounts

The Python runner and inventory compiler use only the Python standard library.

Verify the tools:

```bash
python3 --version
ansible-playbook --version
```

## Configure reusable connection profiles

Edit `config/credentials.json`:

```json
{
  "profiles": {
    "sap-lab": {
      "ssh_user": "root",
      "private_key_file": "/Users/example/.ssh/sap_lab_key"
    },
    "sap-production": {
      "ssh_user": "ansible",
      "private_key_file": "/secure/path/sap_prod_key"
    }
  }
}
```

Do not store private-key contents or passwords in this repository. The file
contains references to credentials, not the credentials themselves.

Global defaults are in `config/defaults.json`:

```json
{
  "connection": {
    "ssh_port": 22,
    "python_interpreter": "/usr/bin/python3.11",
    "credential_profile": "sap-lab"
  },
  "execution": {
    "batch_size": 50,
    "forks": 50
  }
}
```

## Add servers

`inputs/servers.csv` contains one row per Ansible connection target.

```csv
server_id,address,physical_ip,physical_hostname,environment,customer,credential_profile,ssh_user,ssh_port,private_key_file,python_interpreter,host_agent_expected,enabled
sap-sandbox-01,54.84.120.187,10.0.22.162,sid-hdb-s4h.dummy.nodomain,sandbox,RTS,sap-lab,,,,,true,true
hdb-prod-001,10.20.10.11,10.20.10.11,hdb-prod-001.example.com,production,RTS,sap-production,,,,,true,true
app-prod-001,10.20.20.11,10.20.20.11,app-prod-001.example.com,production,RTS,sap-production,,,,,true,true
```

Required fields:

- `server_id`: stable unique folder and inventory name
- `address`: SSH endpoint used by Ansible

Recommended fields:

- `environment`
- `customer`
- `credential_profile`
- `physical_hostname`

Blank connection fields inherit from the selected credential profile and global
defaults. Set `enabled=false` to exclude a row without deleting it.

Use a stable `server_id`; do not use a temporary IP address as the identifier.
Allowed characters are letters, digits, `.`, `_`, and `-`.

## Add SAP components and instances

`inputs/instances.csv` contains one row per SAP component installed on a server.
A server can therefore have multiple rows while remaining one Ansible host.

```csv
server_id,component,sid,instance_number,instance_name,virtual_hostname,admin_user,role,tenant,userstore_key,hdbsql_bin,profile_dir,default_pfl_path,db_schema
sap-sandbox-01,hana,HDB,02,HDB02,vhcalhdbdb,hdbadm,primary,S4H,HDB_KEY_CAL,,,,
sap-sandbox-01,abap,S4H,00,D00,vhcals4hci,s4hadm,dialog,,DEFAULT,,,,
sap-sandbox-01,ascs,S4H,01,ASCS01,vhcals4hcs,s4hadm,central,,,,,,
sap-sandbox-01,webdispatcher,S4H,01,,vhcals4hcs,s4hadm,,,,,,,
```

Supported component values:

- `hana`
- `abap`
- `ascs`
- `webdispatcher`
- `host_agent`

For the current checks, the first `hana` row on a server becomes the legacy
HANA variable set and the first `abap` row becomes the legacy S4H variable set.
This supports large fleets where each server has one primary HANA or ABAP
instance while retaining the complete `sap_instances` list for future
multi-instance role loops.

Paths and administrator names are derived when omitted. Use the optional path
columns or `inputs/overrides.json` for exceptions.

## Configure host and customer exceptions

Use `inputs/overrides.json` instead of adding many one-off columns:

```json
{
  "servers": {
    "sap-sandbox-01": {
      "tls_cert_paths": [
        "/usr/sap/S4H/D00/sec/SAPSSLS.pse"
      ],
      "tls_cert_become_user": "s4hadm",
      "sap_landscape": {
        "application_sid": "S4H",
        "database_sid": "HDB",
        "database_tenant": "S4H"
      }
    }
  }
}
```

Most servers should inherit defaults. Add only genuine exceptions here.

## Compile inventory without running checks

```bash
python3 -m inventory_compiler.cli
```

This creates:

```text
generated/inventory.yml
generated/normalized_inventory.json
```

Or use the main runner:

```bash
./sap_validate.py --prepare-only
```

Inspect the generated inventory before a large first run:

```bash
ansible-inventory -i generated/inventory.yml --graph
ansible-inventory -i generated/inventory.yml --host sap-sandbox-01
```

Generated groups include:

```text
sap_hosts
component_hana
component_abap
component_ascs
component_webdispatcher
component_host_agent
environment_<name>
customer_<name>
hana_db                 # compatibility alias
nw_app                  # compatibility alias
```

## Validate runner and playbook selection

List available checks:

```bash
./sap_validate.py --list
./sap_validate.py --list --automated-only
```

Preview a command without running Ansible:

```bash
./sap_validate.py --profile production-readiness --dry-run
```

Run Ansible syntax validation after inventory generation:

```bash
./sap_validate.py --profile production-readiness --syntax-check
```

## Run validations

Run the production-readiness profile against every enabled server:

```bash
./sap_validate.py \
  --profile production-readiness \
  --save-raw-outputs
```

Run selected checks:

```bash
./sap_validate.py \
  --checks login,sudo_root,hana_version,services_green
```

The compatibility command remains valid:

```bash
./run_hana_checks.py --profile production-readiness
```

## Filter large runs

By environment:

```bash
./sap_validate.py --environment production --profile production-readiness
```

By customer:

```bash
./sap_validate.py --customer RTS --profile production-readiness
```

By component:

```bash
./sap_validate.py --component hana --profile production-readiness
./sap_validate.py --component abap --profile production-readiness
```

For named profiles, component filtering affects both host selection and check
selection. Host-scoped checks remain included, while component-scoped tags for
other components are removed. This prevents a combined HANA/S4H server from
running ABAP profile checks during a HANA-only profile run. Explicit `--checks`
are always honored exactly as requested.

By specific server IDs:

```bash
./sap_validate.py \
  --limit "hdb-prod-001,app-prod-001" \
  --profile production-readiness
```

Filters are intersected. For example, this selects production servers in the
customer `RTS` that contain HANA:

```bash
./sap_validate.py \
  --environment production \
  --customer RTS \
  --component hana \
  --profile production-readiness
```

## Control batch size and concurrency

Two controls are independent:

- `--batch-size`: hosts in each Ansible `serial` batch
- `--forks`: maximum concurrent Ansible workers

Example:

```bash
./sap_validate.py \
  --profile production-readiness \
  --batch-size 50 \
  --forks 40
```

Start conservatively for the first fleet run. Increase only after observing
controller CPU, memory, SSH latency, remote load, and artifact storage speed.

The play uses `max_fail_percentage: 100`, so one failed or unreachable server
does not stop the remaining fleet. Existing check assertions are unchanged and
may stop later checks on the affected host, as they did before this refactor.

## Optional discovery evidence

Run a read-only discovery pass before validation:

```bash
./sap_validate.py --discover --profile production-readiness
```

Discovery saves `/usr/sap/sapservices`, SAP Host Agent `ListInstances`, and SAP
directory evidence under each server folder. It does not currently rewrite the
source CSV or automatically change validation routing. `instances.csv` remains
the authoritative component declaration.

Discovery only:

```bash
./sap_validate.py --discover-only
```

## Artifact layout

Every run receives one timestamp directory. Every selected server is a direct
subfolder:

```text
artifacts/20260721_150000/
├── _run.json
├── _invocation.txt
├── _controller.log
├── _manifest.md
├── _summary.json
├── _summary.csv
├── _summary.md
├── _results.csv
├── _input/
│   ├── servers.csv
│   ├── instances.csv
│   ├── overrides.json
│   ├── defaults.json
│   ├── checks_catalog.json
│   ├── generated_inventory.yml
│   └── normalized_inventory.json
├── sap-sandbox-01/
│   ├── metadata.json
│   ├── status.json
│   ├── results.json
│   ├── report.md
│   ├── discovery/
│   │   └── discovery.json
│   └── raw/
│       ├── hdb_version.txt
│       ├── sapcontrol_process_list.txt
│       └── ...
└── hdb-prod-001/
    └── ...
```

`results.json` is the structured per-server check list. `_results.csv` flattens
all server results for spreadsheets or downstream ingestion. `_summary.csv`
contains one row per server.

Servers that never reach the Ansible post-tasks still receive `metadata.json`
and `status.json` from the Python aggregator when the runner can identify them.

## Resume failed or unreachable servers

Retry only failed, unreachable, not-completed, or check-failing servers from a
previous run:

```bash
./sap_validate.py --resume artifacts/20260721_150000
```

Choose retry states explicitly:

```bash
./sap_validate.py \
  --resume artifacts/20260721_150000 \
  --retry UNREACHABLE,NOT_COMPLETED
```

The retry reuses the input and generated inventory snapshot stored inside the
original run directory.

## Direct Ansible usage

The recommended interface is `sap_validate.py`, because it compiles inventory,
creates a single run directory, captures logs, and aggregates results.

For troubleshooting, direct execution is still possible:

```bash
python3 -m inventory_compiler.cli
ansible-playbook -i generated/inventory.yml site.yml \
  -e run_timestamp=$(date +%Y%m%d_%H%M%S) \
  -e validation_batch_size=25 \
  --forks 25 \
  --tags login,sudo_root
```

The legacy one-host `inventory/hosts.ini` contains one server definition and
child groups. It is retained for manual compatibility, not fleet management.

## Adding hundreds or thousands of servers

1. Export or generate rows into `inputs/servers.csv`.
2. Export or generate component rows into `inputs/instances.csv`.
3. Reuse credential profiles rather than repeating connection settings.
4. Put common policy in `group_vars/all.yml` or `config/defaults.json`.
5. Put only exceptions in `inputs/overrides.json`.
6. Run `--prepare-only` and inspect the generated inventory graph.
7. Test a small `--limit` set.
8. Increase batch size and forks gradually.
9. Preserve the timestamp directory as the reproducible run record.

No source-code edits are required when adding ordinary servers or landscapes.
