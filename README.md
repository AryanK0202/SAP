# Bulk SAP Validation

This repository runs the existing read-only SAP HANA and S/4HANA validation
checks across one server or thousands of servers.

The scalable workflow is:

1. Maintain server connection records in `inputs/servers.csv`.
2. Maintain SAP component/instance records in `inputs/instances.csv`.
3. Run `./sap_validate.py`.
4. The runner compiles `generated/inventory.yml`, executes Ansible in controlled
   batches, and writes one directory per server below a shared timestamp.

The validation task implementations under `tasks/` remain unchanged. The
inventory compiler generates their existing `hana_sid`, `app_sid`, `hana_db`,
and `nw_app` interfaces for compatibility.


### Where to go next?
1. See [usage_guide.md](usage_guide.md) for setup and operating instructions.

2. See [codebase_summary.md](codebase_summary.md) for a high level yet somewhat techical overview of the entire codebase. (I recommend you start here to get the gist.)

3. See [OVERVIEW.md](OVERVIEW.md) for a brief layman summary of the workflow along with a surface level demo that covers how to perform basic validation tasks using tags.

4. See [Ansible_raw_output_and_parameter_guide.md](Ansible_raw_output_and_parameter_guide.md) for thorough documentation of every single tag and a guide on how to understand the output including understanding exit codes, the outputs located in the `artifacts` folder that are generated with each validation run, and basic operational recipes to get you started.

---

### Validation Status

#### Fully Tested and Validated

The following validation tasks have been fully tested, and their output has been confirmed:

* **SSH/Ansible login**
* **Become root and all database administrator accounts**
* **HANA version**
* **All HANA services report GREEN**
* **SSL minimum protocol version**
* **Certificate expiry**
* **Time zone is UTC**
* **Disk usage**
* **Memory usage**
* **OS facts**

  * Operating system version
  * Kernel version
  * System architecture
* **ICM ports**
* **CVERS add-on and version report**

#### Implemented but Not Yet Fully Validated

The following tasks are listed in the validation catalog and have accompanying automation code. However, they are either still being tested or their output cannot yet be validated with complete confidence.

##### SAP HANA Parameters

The following parameter checks are currently being tested:

* **SAP HANA license status**
* **TCP backlog**
* **Log mode**
* **Backup interval mode**

Valid output has been obtained by manually running the relevant SQL commands after connecting to the server through SSH. The current work is focused on confirming that the Ansible playbook returns the same valid and reliable output.

##### Services and Installed Packages

The following service and package checks are implemented:

* **incrond**
* **ClamAV**
* **ClamSAP**
* **Watchman**
* **SAP Diagnostics Agent**

These packages and services are not currently installed on the sandbox system. The playbook correctly reports that they are not present, but the results cannot yet be validated with complete certainty.

Testing against systems where these packages are installed is required to confirm that the checks correctly identify both installed and missing states and do not produce false positives.
