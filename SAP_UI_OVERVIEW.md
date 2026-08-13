# SAP Validation UI Documentation

## 1. Purpose

The SAP Validation UI is a lightweight localhost web interface for running the bulk SAP validation controller, `sap_validate.py`.

The UI is responsible for:

- Loading SAP system inventory data.
- Loading the validation-check catalog and profiles.
- Letting the user select systems and checks.
- Converting UI selections into a validated `sap_validate.py` command.
- Starting and stopping one validation process at a time.
- Displaying live controller output.
- Reading aggregate run results.
- Providing links to generated artifacts.
- Browsing and previewing artifact files through a built-in file explorer.

The UI uses only the Python standard library. It does not require Flask, FastAPI, Django, or other third-party Python web frameworks.

---

## 2. High-Level Architecture

The UI runs a local threaded HTTP server using Python's `BaseHTTPRequestHandler` and `ThreadingHTTPServer`.

The browser communicates with the Python server through JSON API endpoints.

The UI reads:

| Input | Default path | Purpose |
|---|---|---|
| Server inventory | `inputs/servers.csv` | Defines target SAP servers |
| Instance inventory | `inputs/instances.csv` | Maps components to servers |
| Check catalog | `checks_catalog.json` | Defines validation checks and profiles |
| Defaults | `config/defaults.json` | Provides execution defaults such as batch size and forks |
| Artifact root | `artifacts/` | Stores and serves generated run output |

The validation runner is expected at:

```text
<repository-root>/sap_validate.py
```

The UI refuses to start if this file does not exist.

---

# 3. Starting the UI

## 3.1 Basic startup

```bash
python3 UI.py
```

Default URL:

```text
http://127.0.0.1:8765/
```

The browser opens automatically unless `--no-browser` is supplied.

## 3.2 Startup parameters

### `--root`

Repository root.

Default: the directory containing the UI Python file.

Relative paths supplied by the other file arguments are resolved relative to this root.

Example:

```bash
python3 UI.py --root /opt/sap-validation
```

### `--servers`

Path to the server inventory CSV.

Default:

```text
inputs/servers.csv
```

### `--instances`

Path to the instance/component inventory CSV.

Default:

```text
inputs/instances.csv
```

### `--catalog`

Path to the validation check catalog.

Default:

```text
checks_catalog.json
```

The catalog must be valid JSON and contain `checks[]` and `profiles{}`.

### `--defaults`

Path to the defaults JSON.

Default:

```text
config/defaults.json
```

The UI reads:

```json
{
  "execution": {
    "batch_size": 50,
    "forks": 50
  }
}
```

If absent, both values fall back to `50`.

### `--artifact-root`

Directory containing generated validation artifacts.

Default:

```text
artifacts
```

The directory is created automatically if it does not exist.

### `--host`

HTTP bind address.

Default:

```text
127.0.0.1
```

Localhost is recommended by the program.

### `--port`

HTTP port.

Default:

```text
8765
```

Valid range:

```text
0 through 65535
```

Example:

```bash
python3 UI.py --port 8779
```

Port `0` lets the operating system choose an available port:

```bash
python3 UI.py --port 0
```

### `--no-browser`

Prevents the UI from automatically opening a browser.

```bash
python3 UI.py --no-browser
```

### `--access-log`

Prints every HTTP request to the terminal.

```bash
python3 UI.py --access-log
```

Without this option, the UI generally uses a single replaceable terminal status line instead of continuously printing successful GET requests.

---

# 4. Input Data

## 4.1 Server inventory

The UI reads these server fields when present:

| Field | Usage |
|---|---|
| `server_id` | Unique UI/runner identifier |
| `address` | Displayed in Systems table |
| `physical_ip` | Loaded into UI data |
| `physical_hostname` | Displayed in Systems table |
| `environment` | Filterable inventory field |
| `landscape` | Filterable inventory field |
| `credential_profile` | Loaded into server metadata |
| `enabled` | Controls whether the system can be selected |

A valid `server_id` may contain letters, numbers, `.`, `_`, and `-`.

The UI considers the following enabled values true, case-insensitively:

```text
1
true
yes
y
on
```

Only enabled systems may be used as run targets.

## 4.2 Instance inventory

Relevant fields:

```text
server_id
component
```

The UI builds a component set for each server and displays it in Section 1.

## 4.3 Validation check catalog

Each check can contain:

| Field | Purpose |
|---|---|
| `id` | Check identifier |
| `category` | Top-level validation tab |
| `group` | Expandable group within a category |
| `contributor` | Contributor displayed in the table |
| `task` | Human-readable description |
| `scope` | Scope displayed in the table |
| `component` | Component associated with the check |
| `ansible_tag` | Ansible tag used to execute the check |
| `implementation_status` | Determines whether the check is selectable |

A check is selectable only when:

```text
ansible_tag is present
AND
implementation_status == "runs_today"
```

## 4.4 Profiles

Profiles are defined by tags in the catalog.

The UI maps each profile's tags to the selectable check IDs carrying those tags.

Selecting a profile automatically selects those checks. Manually changing a check resets the profile field to Custom selection.

---

# 5. Section 1 — Systems

Section 1 determines the system IDs passed to the runner.

## Environment

Filters by exact environment value. Options are generated from the server inventory.

## Landscape

Filters by exact landscape value. Options are generated from the server inventory.

## Component

Filters to systems containing the selected component from `instances.csv`.

## Search systems

Performs a case-insensitive search across the loaded server values, including values such as server ID, environment, landscape, components, hostname, and address.

## Select visible

Selects every currently visible enabled system.

Filtered-out rows are not changed. Disabled systems cannot be selected.

## Clear visible

Clears all currently visible system selections without changing filtered-out rows.

## Systems table

| Column | Description |
|---|---|
| Select | System checkbox |
| System | `server_id` |
| Environment | Inventory environment |
| Landscape | Inventory landscape |
| Components | Components mapped from `instances.csv` |
| Hostname | Physical hostname |
| Address | Inventory address |
| Enabled | Whether the system may be selected |

The sortable headers are System, Environment, Landscape, Components, Hostname, Address, and Enabled. Repeated clicks reverse the sort direction.

## Selection count

The section displays:

```text
X selected / Y visible
```

---

# 6. Section 2 — Validation Checks

Section 2 determines which automated checks are sent to the runner.

## 6.1 Category tabs

The UI defines four categories:

1. `Build Validation`
2. `Prepatch`
3. `Health`
4. `Availability`

The count in each tab is the number of selectable checks in that category.

Only one category is displayed at a time, but selections in other tabs are retained.

## 6.2 Groups

Checks are grouped by the catalog's `group` field.

Each group is expandable/collapsible and includes a group checkbox.

The group checkbox selects or clears visible selectable checks in that group.

## 6.3 Profile

Profiles come from `checks_catalog.json`.

Selecting a profile selects the check IDs associated with the profile's tags.

If the user manually changes a check selection afterward, the Profile field is reset to Custom selection.

## 6.4 Search checks

Search is case-insensitive and includes:

- Category
- Group
- Contributor
- Check ID
- Ansible tag
- Component
- Scope
- Task/description
- Implementation status

## 6.5 Show unavailable checks

Unavailable checks are hidden by default.

When `Show unavailable checks` is enabled, unavailable catalog entries become visible but remain disabled and cannot be selected.

## 6.6 Select visible

Selects visible, selectable checks in the currently active category only.

## 6.7 Clear visible

Clears visible selectable checks in the currently active category only.

## 6.8 Check table columns

| Column | Meaning |
|---|---|
| Select | Check checkbox |
| Check ID | Catalog `id` |
| Tag | Catalog `ansible_tag` |
| Component | Catalog `component` |
| Scope | Catalog `scope` |
| Contributor | Catalog `contributor` |
| Status | Available or implementation status |
| Description | Catalog `task` |

Check-table horizontal scrolling is synchronized across groups so corresponding columns stay aligned.

## 6.9 Availability logic

A check displays as Available when:

```text
ansible_tag exists
AND
implementation_status == runs_today
```

Otherwise, its implementation status is displayed when unavailable checks are shown.

## 6.10 Selection count

Section 2 displays:

```text
X selected / Y visible
```

`selected` counts selected checks across all categories.

`visible` counts visible rows in the active category.

---

# 7. Section 3 — Run Parameters

Section 3 controls command construction.

## 7.1 Mode

### Validate

UI value:

```text
validate
```

Behavior:

- Uses selected systems.
- Uses selected checks or selected profile.
- Adds no discovery-mode flag.
- Requires at least one selected validation check.

### Discover, then validate

UI value:

```text
discover_validate
```

Adds:

```bash
--discover
```

Requires at least one selected validation check.

### Discovery only

UI value:

```text
discover_only
```

Adds:

```bash
--discover-only
```

The UI does not require validation checks for this mode.

### Prepare inventory only

UI value:

```text
prepare_only
```

Adds:

```bash
--prepare-only
```

The UI does not require validation checks for this mode.

## 7.2 Batch size

Runner argument:

```bash
--batch-size <number>
```

Default comes from:

```text
defaults.json -> execution.batch_size
```

Fallback:

```text
50
```

Allowed by the UI:

```text
integer from 1 through 10000
```

## 7.3 Forks

Runner argument:

```bash
--forks <number>
```

Default comes from:

```text
defaults.json -> execution.forks
```

Fallback:

```text
50
```

Allowed by the UI:

```text
integer from 1 through 10000
```

## 7.4 Verbosity

| UI value | Runner argument |
|---|---|
| `0` | No verbosity flag |
| `1` | `-v` |
| `2` | `-vv` |
| `3` | `-vvv` |
| `4` | `-vvvv` |

Other values are rejected.

## 7.5 Save raw outputs

Adds:

```bash
--save-raw-outputs
```

The UI passes this flag to `sap_validate.py`; the runner defines the exact storage behavior.

## 7.6 Strict validation

Adds:

```bash
--strict
```

## 7.7 Enable incrond validation

Adds:

```bash
--enable-incrond
```

## 7.8 Enable Backint validation

Adds:

```bash
--enable-backint
```

## 7.9 Dry run

Adds:

```bash
--dry-run
```

## 7.10 Syntax check

Adds:

```bash
--syntax-check
```

## 7.11 Ansible check mode

Adds:

```bash
--check-mode
```

---

# 8. Exact Command Construction

Every run starts with a command equivalent to:

```bash
<current-python> <root>/sap_validate.py   --servers <servers.csv>   --instances <instances.csv>   --catalog <checks_catalog.json>   --defaults <defaults.json>   --artifact-root <artifact-root>   --limit <comma-separated-system-ids>   --batch-size <batch-size>   --forks <forks>
```

The UI then adds either:

```bash
--profile <profile>
```

or:

```bash
--checks <comma-separated-check-ids>
```

Mode flags, optional boolean flags, and verbosity are appended afterward.

Selected systems and checks are sorted and deduplicated before command construction.

---

# 9. Run Validation Rules

Before launching the subprocess, the UI verifies the request.

## Systems

- At least one enabled system is required in every mode.
- Unknown server IDs are rejected.
- Disabled server IDs are rejected.

## Checks

- Selected checks must exist in the current selectable-check set.
- Unknown or unavailable check IDs are rejected.
- Checks are required for `validate` and `discover_validate`.
- Checks are not required by the UI for `discover_only` and `prepare_only`.

## Profile consistency

When a profile is supplied, the current selected check IDs must exactly equal that profile's resolved check IDs.

If not, the UI rejects the request and requires Custom selection.

## Numeric validation

`batch_size` and `forks` must be integers between `1` and `10000`.

Verbosity must be between `0` and `4`.

---

# 10. Run Lifecycle

## Run validation

The Run validation button sends:

```text
POST /api/run
```

The server then:

1. Validates the request.
2. Constructs the command.
3. Prevents a second run from starting while one is active.
4. Clears the previous live-output buffer.
5. Starts `sap_validate.py`.
6. Captures stdout and stderr together.
7. Updates in-memory run state.
8. Records the process return code when it exits.

Only one validation subprocess may be active per UI server.

## Python executable

The runner uses `sys.executable`, meaning the same Python interpreter that started the UI is used for `sap_validate.py`.

## Working directory

The subprocess runs with the repository root as its current working directory.

## Unbuffered output

The UI sets:

```text
PYTHONUNBUFFERED=1
```

## Stop

The Stop button sends:

```text
POST /api/stop
```

On non-Windows systems, the UI attempts to terminate the process group with `SIGTERM`.

On Windows, or when process-group termination is unavailable, it terminates the process directly.

---

# 11. Live Controller Output

The bottom output panel displays combined runner stdout/stderr.

The UI keeps at most:

```text
5000 lines
```

in memory.

When the user is already at the bottom of the panel, new output automatically scrolls into view.

---

# 12. Run Directory Detection

The UI scans controller output for:

```text
Run directory: <path>
```

The path must resolve inside the configured artifact root.

Once detected, the run directory is used for:

- Browse current run
- Open folder
- Aggregate summary loading
- Known artifact links
- Per-system report links

---

# 13. Section 4 — Results

## Run status

The interface can show states such as:

```text
Idle
Run #N running
Run #N completed
Run #N failed (exit X)
```

Controller errors may be appended to the status.

## Command display

The exact generated command is shown in the run bar.

## Quick summary

The UI looks for:

```text
<run-directory>/_summary.json
```

When present and valid, the UI displays totals for:

- Pass
- Fail
- Error
- Warn
- Skipped
- Number of systems

## Per-system summary

When the summary contains server entries, the table displays:

| Column | Meaning |
|---|---|
| System | Server ID |
| Environment | Server environment |
| Overall | Overall server status |
| Quick summary | Pass/fail/error/warn/skipped counts |
| Report | Open report link when available |

A report link is only created if the referenced file resolves inside the artifact root and exists.

---

# 14. Result Actions

## Browse all outputs

Opens:

```text
/artifacts/
```

## Browse current run

Appears when a valid current run directory has been detected.

## Open folder

Opens the run directory in the operating system:

| Platform | Method |
|---|---|
| macOS | `open` |
| Windows | `os.startfile()` |
| Linux/Unix | `xdg-open` |

## Known artifact links

When present, the UI exposes:

| UI label | File |
|---|---|
| Summary (Markdown) | `_summary.md` |
| Summary (JSON) | `_summary.json` |
| Summary (CSV) | `_summary.csv` |
| All results (CSV) | `_results.csv` |
| Controller log | `_controller.log` |
| Run metadata | `_run.json` |

---

# 15. Artifact Browser

The artifact browser uses a split-pane layout:

- Left: expandable file tree
- Right: file preview

## Hidden files

Hidden artifacts are excluded.

An artifact is considered hidden when:

- Any path component begins with `.`, or
- Windows marks the entry with the hidden file attribute.

Examples:

```text
.git/
.hidden
.folder/file.txt
```

Direct UI access to hidden artifacts is also rejected.

Files beginning with `_`, such as `_summary.json`, are not considered hidden by this rule.

## Path containment

Every artifact path is resolved and verified to remain within the configured artifact root.

This applies to browsing, preview, raw access, downloads, and report links.

---

# 16. Artifact Tree Functions

## Expand/collapse

Directories are loaded through:

```text
GET /api/artifacts?path=<relative-path>
```

Loaded directory data is cached in the browser for the current page session.

## Breadcrumbs

Breadcrumbs track the active artifact directory and can be clicked to navigate.

## Search

The artifact search compares text against:

- Name
- Type
- Path

It filters the currently loaded tree. It does not automatically crawl every unopened directory solely because a search term was entered.

## All files

The All files button:

- Clears expanded-folder state.
- Returns to the artifact root.
- Clears the current preview.
- Renders the root tree.
- Updates browser history.

---

# 17. Resizable Artifact Split Pane

On desktop layouts, the separator between the file tree and preview is resizable.

Mouse:

- Drag left/right to resize.
- Double-click to reset.

Keyboard:

| Key | Behavior |
|---|---|
| Left Arrow | Reduce sidebar by 10 px |
| Right Arrow | Increase sidebar by 10 px |
| Shift + Arrow | Resize by 50 px |
| Home | Minimum sidebar width |
| End | Maximum allowed sidebar width |

Limits:

```text
Minimum sidebar width: 220 px
Minimum preview width: 320 px
```

Below the mobile breakpoint, the splitter is hidden and the panes stack vertically.

---

# 18. Artifact Preview

Preview endpoint:

```text
GET /api/artifact-preview?path=<relative-file-path>
```

Maximum inline preview content:

```text
1,000,000 bytes
```

Larger previews are marked truncated. Download still returns the original file.

---

# 19. Preview Types

## Markdown

Extensions:

```text
.md
.markdown
```

Provides Preview and Code modes.

The built-in Markdown renderer supports:

- Headings
- Paragraphs
- Inline code
- Fenced code blocks
- Compact one-line fenced code blocks
- Links
- Images
- Bold
- Italic
- Strikethrough
- Horizontal rules
- Blockquotes
- Markdown tables
- Ordered lists
- Unordered lists
- Task-list checkboxes

It also avoids treating underscores inside identifiers such as `component_abap` and `hana_db` as emphasis markers.

Code mode shows raw source with line numbers.

## CSV

`.csv` files are parsed in the browser.

The first row becomes the table header.

Quoted CSV fields and escaped double quotes are handled by the client parser.

## JSON

`.json` files are pretty-printed by the server when valid.

## Text/source files

Recognized text-like extensions include:

```text
.txt
.log
.out
.tsv
.yaml
.yml
.ini
.cfg
.conf
.xml
.html
.htm
.py
.sh
.j2
.sql
.properties
```

These are shown as source with line numbers.

## Images

Supported inline image extensions:

```text
.png
.jpg
.jpeg
.gif
.webp
.svg
```

## PDF

PDFs render in an iframe and also provide an Open raw action.

## Unknown/binary files

Unknown files begin as binary.

If the preview block contains no NUL bytes and decodes as UTF-8, the UI reclassifies it as text and previews it as source.

Otherwise, the UI displays `No inline preview available` and keeps Download available.

---

# 20. Artifact Preview Actions

## Download

Endpoint:

```text
/artifact-download/<path>
```

Uses attachment content disposition.

## Open raw

Endpoint:

```text
/artifact-raw/<path>
```

Uses inline content disposition.

The UI explicitly shows Open raw for PDFs.

---

# 21. Browser History

Artifact navigation uses the browser History API:

```text
history.pushState()
history.replaceState()
popstate
```

Browser Back and Forward can therefore move among visited artifact files/directories.

---

# 22. HTTP API Reference

## GET `/`

Returns the main SAP Validation UI.

## GET `/api/config`

Returns:

- Servers
- Checks
- Profiles
- Default batch size
- Default forks
- UI token

## GET `/api/status`

Returns current run state, including:

- Running flag
- Command
- Buffered output
- Start time
- Finish time
- Return code
- Error
- Run number
- Run directory
- Aggregate summary
- Artifact metadata

The main UI polls this endpoint once per second.

## GET `/api/artifacts?path=...`

Returns artifact-directory entries with fields such as:

- Name
- Path
- Directory flag
- Type
- Human-readable size
- Byte size
- Modified time

## GET `/api/artifact-preview?path=...`

Returns file preview metadata/content.

## GET `/artifact-raw/<path>`

Returns the original artifact inline.

## GET `/artifact-download/<path>`

Returns the original artifact as an attachment.

## GET `/artifacts/`

Loads the artifact browser.

## POST `/api/run`

Starts a validation run.

## POST `/api/stop`

Stops the active run.

## POST `/api/open-output`

Opens the current run directory in the OS file browser.

---

# 23. POST Request Protection

State-changing POST requests require:

```text
X-SAP-UI-Token
```

The token is generated at server startup using:

```python
os.urandom(24).hex()
```

The browser receives it from `/api/config`.

POST requests must use:

```text
Content-Type: application/json
```

Maximum accepted POST body size:

```text
1,000,000 bytes
```

---

# 24. Artifact Security Behavior

Artifact responses use:

```text
X-Content-Type-Options: nosniff
```

Raw inline HTML and SVG also receive a restrictive Content Security Policy:

```text
sandbox;
default-src 'none';
img-src data:;
style-src 'unsafe-inline'
```

Markdown links allow relative/anchor URLs and the protocols:

```text
http:
https:
mailto:
```

---

# 25. Cache Behavior

JSON API responses use no-cache/no-store headers such as:

```text
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
Expires: 0
```

This reduces the chance that the browser displays stale validation state.

---

# 26. Terminal Status Behavior

Without `--access-log`, a TTY normally shows a replaceable line such as:

```text
UI active | requests 25 | last GET /api/status 200 | run #2 running
```

With `--access-log`, every request is printed.

In non-TTY mode without full access logging, successful GETs are generally suppressed while non-GET requests and errors are printed.

---

# 27. Error Handling

The UI explicitly handles errors including:

- Missing files
- Invalid JSON
- Invalid server IDs
- Unknown or disabled systems
- Unknown or unavailable checks
- Unknown profile
- Profile/check mismatch
- No selected systems
- Missing checks for validation modes
- Invalid batch size
- Invalid forks
- Invalid verbosity
- Invalid POST token
- Invalid JSON request body
- Oversized POST body
- Artifact paths outside the artifact root
- Hidden artifact access
- Missing artifacts
- Starting a second run while one is active
- Stopping when no run is active
- Opening an unavailable output directory

---

# 28. Troubleshooting

## Address already in use

Example:

```text
Could not start UI on 127.0.0.1:8765: [Errno 98] Address already in use
```

Use another port:

```bash
python3 UI.py --port 8779
```

or:

```bash
python3 UI.py --port 0
```

## `sap_validate.py` not found

The UI expects:

```text
<root>/sap_validate.py
```

Use the correct repository or specify `--root`.

## Inventory/catalog file missing

Check:

```text
--servers
--instances
--catalog
--defaults
```

Relative paths are resolved below `--root`.

## Cannot select a check

A check is selectable only when it has an Ansible tag and `implementation_status` equals `runs_today`.

Use Show unavailable checks to inspect unavailable entries.

## Browse current run is missing

The UI must detect a valid line:

```text
Run directory: <path>
```

The path must be inside the artifact root.

## No aggregate summary

The UI expects:

```text
<run-directory>/_summary.json
```

Some modes may create output without creating an aggregate summary.

---

# 29. Typical Workflow

1. Start the UI:

```bash
python3 UI.py --port 8779
```

2. In Systems:
   - Filter inventory.
   - Select one or more enabled systems.

3. In Validation checks:
   - Choose a category.
   - Choose a profile or individual checks.
   - Search/filter as necessary.

4. In Run parameters:
   - Choose Mode.
   - Confirm Batch size.
   - Confirm Forks.
   - Set Verbosity.
   - Enable optional flags when needed.

5. Click Run validation.

6. Monitor:
   - Run status
   - Generated command
   - Live controller output

7. After completion:
   - Review the quick summary.
   - Open per-system reports.
   - Browse the current run.
   - Browse all outputs.
   - Open the output folder if desired.

---

# 30. Quick Parameter Reference

## UI startup parameters

| Parameter | Default | Purpose |
|---|---|---|
| `--root` | UI script directory | Repository root |
| `--servers` | `inputs/servers.csv` | Server inventory |
| `--instances` | `inputs/instances.csv` | Instance/component inventory |
| `--catalog` | `checks_catalog.json` | Check catalog/profiles |
| `--defaults` | `config/defaults.json` | Execution defaults |
| `--artifact-root` | `artifacts` | Output root |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8765` | HTTP port |
| `--no-browser` | Off | Suppress automatic browser launch |
| `--access-log` | Off | Print every HTTP request |

## Section 3 runner parameters

| UI control | Runner argument | UI behavior |
|---|---|---|
| Validate | none | Checks required |
| Discover, then validate | `--discover` | Checks required |
| Discovery only | `--discover-only` | Checks not required by UI |
| Prepare inventory only | `--prepare-only` | Checks not required by UI |
| Batch size | `--batch-size N` | Integer 1–10000 |
| Forks | `--forks N` | Integer 1–10000 |
| Verbosity 0 | none | No verbosity flag |
| Verbosity 1 | `-v` | One level |
| Verbosity 2 | `-vv` | Two levels |
| Verbosity 3 | `-vvv` | Three levels |
| Verbosity 4 | `-vvvv` | Four levels |
| Save raw outputs | `--save-raw-outputs` | Optional |
| Strict validation | `--strict` | Optional |
| Enable incrond validation | `--enable-incrond` | Optional |
| Enable Backint validation | `--enable-backint` | Optional |
| Dry run | `--dry-run` | Optional |
| Syntax check | `--syntax-check` | Optional |
| Ansible check mode | `--check-mode` | Optional |

---

# 31. Operational Notes

- The server uses `ThreadingHTTPServer`.
- Only one validation subprocess may run at a time.
- UI run state is stored in memory and resets when the UI server restarts.
- Artifact files remain on disk independently of the UI process.
- The UI is designed primarily for localhost use.
- The build identifier and absolute UI source path are printed to the terminal at startup.

Example:

```text
SAP Validation UI: http://127.0.0.1:8779/
UI source: /path/to/UI.py
UI build: 2026.08.06-artifact-tree-v10-vscode-code-blocks
Press Ctrl+C to stop the UI server.
```
