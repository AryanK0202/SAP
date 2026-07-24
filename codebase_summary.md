# Core Codebase Explanation

## What this system does

This repository runs read-only SAP validation checks against one server or a large fleet of servers.

It has two main layers:

1. **Python fleet-management layer**

   * Reads server and SAP-instance data.
   * Generates Ansible inventory.
   * Selects servers and checks.
   * Starts Ansible.
   * Aggregates results.

2. **Ansible validation layer**

   * Connects to the servers.
   * Runs the actual SAP, HANA, operating-system, security, backup, and monitoring checks.
   * Writes reports and structured result files.

The main execution flow is:

```text
servers.csv + instances.csv
          ↓
inventory compiler
          ↓
generated Ansible inventory
          ↓
sap_validate.py
          ↓
site.yml
          ↓
tasks/*.yml
          ↓
per-server artifacts
          ↓
fleet summary
```

## The most important concept: server versus component

A **server** is an SSH connection target.

A **component** describes SAP software installed on that server.

Components are not separate Ansible hosts.

For example:

```text
sap-sandbox-01
├── HANA database instance
├── ABAP application instance
├── ASCS instance
├── Web Dispatcher
└── SAP Host Agent
```

This is still one server and one SSH connection. It simply belongs to several component groups.

The server has one row in:

```text
inputs/servers.csv
```

Its components have multiple rows in:

```text
inputs/instances.csv
```

## What each component means

### `hana`

Represents an SAP HANA database instance.

It causes the server to join:

```text
component_hana
hana_db
```

It also creates variables such as:

```text
hana_sid
hana_instance_number
hana_admin_user
hana_hdbsql_bin
global_ini_remote_path
backint_integration_path
hana_mounts_to_validate
```

These variables are used by HANA health, configuration, backup, and license checks.

### `abap`

Represents an ABAP application instance, such as an S/4HANA application server.

It causes the server to join:

```text
component_abap
nw_app
```

It creates variables such as:

```text
app_sid
app_instance_number
app_admin_user
app_profile_dir
app_default_pfl_remote_path
app_hdbsql_bin
```

These are used for checks such as DEFAULT.PFL parsing and CVERS reporting.

### `ascs`

Represents ABAP Central Services.

It creates:

```text
component_ascs
app_ascs_instance_number
```

There is no dedicated active ASCS validation task yet.

### `webdispatcher`

Represents SAP Web Dispatcher.

It creates:

```text
component_webdispatcher
```

Its information is retained in inventory and reports, but its catalog check is not currently automated.

### `host_agent`

Represents SAP Host Agent.

A server can be placed in `component_host_agent` automatically when:

```text
host_agent_expected = true
```

An explicit Host Agent instance row is not required.

SAP Host Agent should not be confused with SAP Diagnostics Agent. The active monitoring task checks for Diagnostics Agent or `smdagent`.

## What groups are and how they work

### What is a group?

A **group** is an Ansible concept used to organize servers into logical categories.

Groups allow the system to:

* Select which servers to run checks on.
* Apply variables or logic to subsets of servers.
* Route tasks only to relevant systems.

A server can belong to **multiple groups at the same time**.

### What does it mean to "join a group"?

When a server "joins a group", it means:

* It is listed under that group in the generated Ansible inventory.
* Any tasks targeting that group will run on that server.
* Any filters using that group will include that server.

Joining a group does **not** create a new server or connection. It is only a label.

### Types of groups in this system

There are three main dynamic group types:

```text
environment_<environment>
landscape_<landscape>
component_<component>
```

There are also fixed groups:

```text
sap_hosts
hana_db
nw_app
```

Each group type represents a different dimension of classification.

---

## Environments

### What is an environment?

An **environment** describes the lifecycle stage or purpose of a system.

Typical meanings:

* Whether the system is production or non-production.
* Whether it is used for testing, development, or sandboxing.

### What does joining an environment group mean?

If a server joins:

```text
environment_production
```

it means:

* It is considered a production system.
* Filters like `--environment production` will include it.
* Production-specific validation profiles may apply.

### Possible environment values

These values come from `servers.csv` and are free-form, but commonly include:

| Value        | Meaning                                       |
| ------------ | --------------------------------------------- |
| `production` | Live business system with real users and data |
| `nonprod`    | General non-production system                 |
| `dev`        | Development system used by developers         |
| `qa`         | Quality assurance / testing system            |
| `test`       | Functional or integration testing system      |
| `sandbox`    | Experimental or disposable system             |
| `training`   | Used for training users                       |
| `dr`         | Disaster recovery environment                 |

Each value produces a group:

```text
environment_<value>
```

---

## Landscapes

### What is a landscape?

A **landscape** represents a logical SAP system grouping.

It usually corresponds to:

* A specific SAP system (e.g., S/4HANA system)
* A business domain
* A deployment grouping across multiple servers

For example:

```text
s4p
ecc6
bw4
crm
```

A landscape typically includes multiple servers:

* Database server
* Application servers
* ASCS server
* Web Dispatcher

### What does joining a landscape group mean?

If a server joins:

```text
landscape_s4p
```

it means:

* It belongs to the S/4HANA production system.
* Filters like `--landscape s4p` will include it.
* All servers in that SAP system can be targeted together.

### Possible landscape values

Landscape values are user-defined and come from `servers.csv`.

Common examples:

| Value  | Meaning                    |
| ------ | -------------------------- |
| `s4p`  | S/4HANA production system  |
| `s4d`  | S/4HANA development system |
| `ecc6` | SAP ECC system             |
| `bw4`  | SAP BW/4HANA system        |
| `crm`  | SAP CRM system             |
| `pi`   | SAP Process Integration    |
| `epo`  | SAP Enterprise Portal      |

Each value produces a group:

```text
landscape_<value>
```

---

## Components (group dimension)

### What is a component group?

A **component group** represents the type of SAP software installed on a server.

Unlike environments and landscapes, components are derived from `instances.csv`.

### What does joining a component group mean?

If a server joins:

```text
component_hana
```

it means:

* The server has at least one HANA instance.
* HANA-specific checks will run on it.
* Filters like `--component hana` will include it.

### Possible component values

These are defined by the system:

| Value           | Meaning                    |
| --------------- | -------------------------- |
| `hana`          | SAP HANA database instance |
| `abap`          | ABAP application server    |
| `ascs`          | ABAP Central Services      |
| `webdispatcher` | SAP Web Dispatcher         |
| `host_agent`    | SAP Host Agent             |

Each value produces:

```text
component_<value>
```

---

## Special and compatibility groups

### `sap_hosts`

All enabled servers join:

```text
sap_hosts
```

Meaning:

* This is the master group used by the playbook.
* Every validation run targets this group.

---

### `hana_db`

Alias group for HANA servers:

```text
hana_db
```

Meaning:

* Equivalent to `component_hana`.
* Used by existing task files for compatibility.

---

### `nw_app`

Alias group for ABAP application servers:

```text
nw_app
```

Meaning:

* Equivalent to `component_abap`.
* Used by legacy task logic.

---

## Summary of all group types

| Group type    | Example             | Meaning                  |
| ------------- | ------------------- | ------------------------ |
| Global        | `sap_hosts`         | All servers              |
| Environment   | `environment_prod`  | Lifecycle classification |
| Landscape     | `landscape_s4p`     | SAP system grouping      |
| Component     | `component_hana`    | Installed SAP component  |
| Compatibility | `hana_db`, `nw_app` | Legacy task routing      |

A single server might belong to:

```text
sap_hosts
environment_production
landscape_s4p
component_hana
component_abap
hana_db
nw_app
```

---

## How inventory compilation works

The compiler is in:

```text
inventory_compiler/compiler.py
```

It performs these steps:

1. Reads global defaults.
2. Reads reusable SSH credential profiles.
3. Reads servers.
4. Reads component instances.
5. Associates component rows with servers.
6. Derives missing values.
7. Generates Ansible groups.
8. Generates legacy compatibility variables.
9. Applies landscape overrides.
10. Applies server-specific overrides.
11. Writes generated inventory and normalized JSON.

Connection settings follow this precedence:

```text
server CSV value
    overrides credential profile
        overrides global default
```

Overrides follow this precedence:

```text
derived host values
    overridden by landscape values
        overridden by server-specific values
```

## Generated inventory groups

Every enabled server joins:

```text
sap_hosts
```

It also joins groups based on its data:

```text
environment_<environment>
landscape_<landscape>
component_<component>
```

A HANA server additionally joins:

```text
hana_db
```

An ABAP server additionally joins:

```text
nw_app
```

`hana_db` and `nw_app` are compatibility aliases. The existing task files were written against those names, so the compiler keeps producing them.

A host may appear in many group membership lists, but its variables are defined only once under `all.hosts`.

## Modern inventory versus legacy compatibility

The modern representation is:

```yaml
sap_instances:
  - component: hana
    sid: HDB
    instance_number: "02"
  - component: abap
    sid: S4H
    instance_number: "00"
```

The older validation tasks expect scalar variables:

```yaml
hana_sid: HDB
hana_instance_number: "02"
app_sid: S4H
app_instance_number: "00"
```

The compiler creates both representations.

This lets newer code inspect the complete `sap_instances` list while older tasks continue working unchanged.

## Important multi-instance limitation

All component records are retained in `sap_instances`.

However, the current tasks generally do not loop over that list.

Instead:

* the first HANA row supplies the HANA scalar variables;
* the first ABAP row supplies the application scalar variables;
* the first ASCS row supplies the ASCS instance number.

Therefore, CSV row order matters when one server contains multiple HANA or ABAP instances.

Additional same-type instances are preserved as metadata but are not fully validated by the current tasks.

## What `sap_validate.py` does

`sap_validate.py` is the primary command.

It:

1. Loads the check catalog.
2. Resolves a profile or explicit check IDs.
3. Compiles inventory unless an existing inventory was supplied.
4. Creates a timestamped run directory.
5. Copies input snapshots into the run.
6. Selects hosts.
7. Filters profile tags by component.
8. Builds the `ansible-playbook` command.
9. Optionally runs discovery.
10. Runs validation.
11. Aggregates results.

Filters are intersections.

For example:

```bash
--environment production \
--landscape s4p \
--component hana
```

means:

```text
production AND s4p AND contains HANA
```

Repeating a component filter also means intersection:

```bash
--component hana --component abap
```

selects servers containing both components.

## Profiles and checks

`checks_catalog.json` connects three things:

```text
user-facing check ID
        ↓
Ansible tag
        ↓
task implementation
```

Checks without an `ansible_tag` are documented future or manual checks and cannot currently run through `--checks`.

Profiles include:

* `core-connectivity`
* `lab-quick`
* `strict-full`
* `production-readiness`

With a component-filtered profile, the runner keeps:

* all host-scoped checks;
* checks belonging to the selected component.

For example, a HANA-only production-readiness run removes ABAP-specific profile tags but keeps host-level login, OS, and security checks.

Explicit `--checks` selections are not automatically filtered.

## How the Ansible playbook routes tasks

`site.yml` targets:

```yaml
hosts: sap_hosts
```

Host-level task files are imported for all selected servers.

HANA task files are imported only when the host is in:

```text
hana_db
```

ABAP sections inside `tasks/sap_app.yml` check for:

```text
nw_app
```

The task routing currently looks like this:

| Task family                | Applicability                                   |
| -------------------------- | ----------------------------------------------- |
| Connectivity               | Every selected server                           |
| HANA system health         | HANA servers                                    |
| HANA configuration         | HANA servers                                    |
| HANA backup                | HANA servers                                    |
| Security                   | Every selected server, subject to configuration |
| OS baseline                | Every selected server                           |
| ABAP DEFAULT.PFL and CVERS | ABAP servers                                    |
| HANA license               | HANA servers                                    |
| Diagnostics Agent          | Every selected server                           |
| Cloud                      | Not currently imported                          |
| Replication/DR             | Not currently imported                          |
| HA/cluster                 | Not currently imported                          |

## How check results work

Before checks run, every host receives:

```yaml
hana_check_results: []
```

Each check appends an object:

```json
{
  "check": "HANA services",
  "status": "PASS",
  "details": "All reported services are GREEN"
}
```

Possible result statuses include:

```text
PASS
FAIL
ERROR
WARN
SKIPPED
```

These reported statuses are different from Ansible task success.

A task can finish successfully while recording a warning or validation error. Consequently, an Ansible process can return exit code `0` while the final server status is `FAIL`.

## Execution status versus validation status

Each server receives two related statuses.

### Execution status

Describes whether Ansible ran successfully:

```text
COMPLETED
FAILED
UNREACHABLE
NOT_COMPLETED
DISCOVERED
```

### Overall status

Describes the final validation outcome:

```text
PASS
WARN
FAIL
```

The decision order is:

1. Execution problem wins.
2. Any check `FAIL` or `ERROR` produces overall `FAIL`.
3. Otherwise any `WARN` or `SKIPPED` produces overall `WARN`.
4. Otherwise the result is `PASS`.

The included sample run completed with Ansible return code `0`, but its HANA license result was `ERROR`, so the final server status was:

```text
execution_status: COMPLETED
overall_status: FAIL
```

## Output organization

Each run gets one timestamp folder:

```text
artifacts/<timestamp>/
```

Each server gets one direct child folder:

```text
artifacts/<timestamp>/<server_id>/
```

Per-server output includes:

```text
metadata.json
status.json
results.json
report.md
raw/
discovery/
```

Run-level output includes:

```text
_run.json
_attempts.json
_invocation.txt
_controller.log
_manifest.md
_summary.json
_summary.csv
_summary.md
_results.csv
_input/
```

`_summary.csv` contains one row per server.

`_results.csv` contains one row per check result per server.

The `_input` directory preserves the inventory inputs and generated model used for the run. The credentials file itself is not copied.

## Batch size and forks

These options control different things.

`--batch-size` controls Ansible `serial`: the number of servers in one logical wave.

`--forks` controls the number of concurrent Ansible workers.

For example:

```text
batch size: 50
forks:      20
```

means up to 50 servers are in the current wave, with at most 20 workers operating concurrently.

The play uses:

```yaml
max_fail_percentage: 100
```

so one failed or unreachable server does not stop the remaining fleet.

## Discovery

The discovery play collects evidence from:

```text
/usr/sap/sapservices
SAP Host Agent ListInstances
/usr/sap directory paths
```

Discovery does not update `instances.csv` and does not automatically decide which components are installed.

The declared component rows remain authoritative.

## Important Notes

* Components are labels and instance records, not separate SSH hosts.
* One server may belong to many groups.
* Modern component groups and legacy aliases intentionally coexist.
* Only the first HANA and ABAP instance currently drive scalar task variables.
* Discovery does not update inventory.
* ASCS, Web Dispatcher, and Host Agent representation does not imply dedicated active checks.
* Some catalog checks are documentation or future work only.
* A successful Ansible exit code does not guarantee a passing validation result.
* Existing-inventory mode requires a matching normalized inventory for complete reporting.
* `component_host_agent` can be inferred without a Host Agent instance row.
* An ASCS-only host is not currently included in the `nw_app`-based SAP administrator privilege test.
* Generated inventory and artifacts contain usernames and private-key paths, even though they do not contain the private keys themselves.
