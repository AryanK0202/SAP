# Workflow

### *Input:* One CSV file containing your server information.

### *Output:* Allows you to run flexible, tag-driven validations against each server and receive detailed reports.

```
1. Import servers.csv. The ONLY mandatory values are:
   - server_id
   - server address
   - SSH credentials or credential profile to login
        ↓
2. Run discovery script and populate instances.csv with results 
(gets components, SIDs, instance numbers, etc. for each server in the csv)
   $ ./sap_validate.py --discover-only
   - one row per discovered SAP component or instance
   (Each server corresponds to 1 row in servers, can have many rows in instances.)
        ↓
1. Compile & verify inventory  ──┐
   $ ./sap_validate.py --prepare-only
        ↑                        │
   Correct issues ───────────────┘ (repeat until clean)
        ↓
2. Run automated validations
   $ ./sap_validate.py --put whatever tags you want here
   You can run against specific servers, validation tasks, arbitrary groups you define, etc.
   Can also specify batch size and forking, and create reusable profiles with a list of tags, etc.
        ↓
3. Review results
   - validation summaries (if you ran against multiple servers)
   - supporting evidence (raw files queried from/raw SQL output for every server)
   - per-server reports
```


### Surface Level Demo

```
// How to generate inventory from csvs:
./sap_validate.py --prepare-only
ansible-inventory -i generated/inventory.yml --graph

// Inspect the one server we have atm
ansible-inventory -i generated/inventory.yml --host sap-sandbox-01

// View catalog of validation tasks that have been automated (not all are 100% tested)
./sap_validate.py --list --automated-only

// All the 100% certain tags have been assigned to an arbitrarily created profile 'fully-tested' (No need to remember lists of tags for repeated use)
// See tags that belong to the created profile 'fully-tested'
jq -r '.profiles["fully-tested"].tags[]' checks_catalog.json

// preview command without running the validation (shows entire ansible command)
./sap_validate.py --profile fully-tested --dry-run

// Using the arbitrary groups you define, I defined the environment 'sandbox' which has no actual meaning and is only used for user organization (Allows you to make customer groups, etc. and specify runs against these groups)

./sap_validate.py --checks {whatever checks you want} --environment sandbox
// OR
./sap_validate.py --checks {whatever checks you want} --limit environment_sandbox
// the second format gets created for all group variables so --limit group_{specific group} works for all of these

// This works for:
--limit component_{component name}
--limit landscape_{landscape name} // etc.
// and in the future
--limit customer_{customer name} //

// Basic join logic works, for example:
// Intersections using :& and exclusions using :!
--limit "environment_sandbox:&component_hana"

// limit to ONE specific server
./sap_validate.py --checks {etc} --limit {server ID which is the 1st column in the servers.csv}

// Append these to specify:
--forks 40 --batch-size 50
```

