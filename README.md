# Asana Export Kit

Export the Asana data your account can read, using ordinary read-only API requests. No organization-export endpoint, Enterprise service account, paid export service, or runtime dependencies required.

**Version 0.3:** an offline browser command builder (`gui`), category selection, serial interval snapshots (`watch`), verified rclone storage handoff (`upload` / `export --remote`), and a separate experimental two-phase task purge (`purge-plan`, `purge-apply`). Export never deletes source data. See the [operations guide](docs/OPERATIONS.md) for exact behavior and remaining validation limits.

**An honest archive, not a claim of omniscience.** Asana's API does not expose every product feature or provide a universal free-plan task-discovery endpoint. This tool records failures and blind spots in `coverage.json`. It never equates permission denied with empty, or successful execution with a complete organization backup.

Asana also has built-in **per-project CSV/JSON exports**. Its organization-wide export API is a different, Enterprise+ service-account feature. This project automates ordinary API reads; it does not bypass permissions or subscription restrictions.

## Quick start

Requires Python 3.10 or newer. Works without installation from a checkout:

```sh
git clone https://github.com/cavebot-collective/asana-export-kit.git
cd asana-export-kit
python -m asana_export_kit export --out ../asana-archive --prompt-token --download-attachments
```

Create a personal access token in the [Asana developer console](https://app.asana.com/0/my-apps). The prompt hides your token and keeps it out of shell history. Alternatively supply `ASANA_ACCESS_TOKEN` through your environment or secret manager. Do not paste tokens into issues, chat, command-line arguments, or committed files. OAuth access tokens work through the same environment variable when their granted scopes permit the reads.

Version 0.2 adds browser OAuth login with PKCE and automatic refresh during the run, optional template/membership collection, configurable attachment limits, archive comparison, and reproducible local benchmarks. See [authentication and extension details](docs/EXTENSIONS.md) and [benchmark results](benchmarks/README.md). OAuth requires a registered application; no username/password is sent to this exporter.

On Windows, `py -3` may be used in place of `python`. No Node.js installation is needed for the normal exporter.

Optional installation:

```sh
python -m pip install .
asana-export --help
```

## Output

| File | Purpose |
| --- | --- |
| `export.json` | Normalized objects keyed by string GID, relationships, principal, and coverage |
| `raw/*.json` | Returned API JSON with request path, parameters, and collection time |
| `operations/*.json` | Completed operation checkpoints with content hashes |
| `failures/*.json` | Failed operations, including denied and plan-gated reads |
| `coverage.json` | Counts, omissions, and errors |
| `tables/*.csv` | Spreadsheet-safe derivatives; nested values encoded as JSON |
| `tasks.md` | Readable task descriptions; REST activity is in the stories table/JSON |
| `attachments/*.bin` | Optional attachment bytes, named by GID; original names remain in metadata |
| `manifest.json` | SHA-256 and byte length for every archive file |

JSON is the source of truth. CSV prefixes formula-like values and long numeric GIDs with an apostrophe to reduce spreadsheet injection and ID rounding. Exact original values remain in JSON. Raw responses are **exactly what was returned**, not a guarantee of every possible optional field.

Archive files are private data. Signed attachment URLs, email addresses, internal links, and descriptions can be sensitive. Keep exports outside this repository, out of public Actions artifacts, and out of issue attachments.

## Collection coverage

The REST collector discovers all accessible workspaces (or repeated `--workspace GID` selections), then traverses:

- Active and archived projects; project details, sections, memberships, custom-field settings, brief pointers and brief content, status updates, attachment metadata.
- Project tasks, tasks assigned to visible users, user task lists, tagged tasks, explicit task seeds, recursive subtasks, parents, dependencies and dependents. Completed tasks are included.
- Task descriptions, dates, completion, memberships, selected custom-field values, followers, paginated stories/comments/activity, optional time tracking entries, attachment metadata and optional bytes.
- Workspace users and teams, team memberships, tags, custom-field definitions, time periods, portfolios owned by visible users (including nested portfolios), goals, goal relationships, and their status updates.
- With `--extended`: project templates, task templates, and individual unified memberships for projects, portfolios, and goals. This collects the fields returned by their detail endpoints, not a promise of complete template recreation.

Every list operation follows `next_page.offset`. It does not follow server-supplied pagination URLs. Graph traversal deduplicates by object kind and GID, so cycles and multi-homed tasks do not loop indefinitely.

Unsupported or inaccessible endpoints are recorded and collection continues. Invalid authentication stops the run. A `402`, `403`, or `404` is not proof that no data exists. Some list resources return compact metadata; the collector does not hydrate every membership or auxiliary record individually. All responses remain available in `raw/`.

### Boundaries that matter

- Your token sees only its account's accessible data. An admin title does not grant this tool access to everyone else's private tasks.
- Unassigned, untagged, projectless tasks with no discovered graph connection are a discovery gap. Add known IDs with repeated `--task GID`; add project seeds with repeated `--project GID`.
- Messages/inbox, deleted records, rules, forms, dashboards, audit logs, app configuration and revision histories are outside this version's coverage.

  This is an implementation boundary. Much of their visible current state is a plausible authorized-browser capture target. See the [browser coverage assessment](docs/BROWSER-COVERAGE.md); site-specific collectors for those categories are not implemented yet.
- Portfolios/goals/time tracking and other features remain subject to account capabilities. The exporter records denials rather than requesting a different identity or circumventing a gate.
- Data may change during collection. This is a time-spanning archive, not a transactional snapshot or a ready-to-import disaster-recovery backup.
- Linked external files may need a different service's login. Metadata is retained when file bytes cannot be downloaded. There is no fixed attachment-size ceiling by default; set `--max-attachment-bytes N` for an optional per-file budget. Downloads and checksum verification stream file bytes; metadata rendering still uses memory proportional to the dataset.

## Resume, verify, package

Repeat the **same options** and output directory, adding `--resume`:

```sh
python -m asana_export_kit export --out ../asana-archive --prompt-token --download-attachments --resume
python -m asana_export_kit verify ../asana-archive
python -m asana_export_kit zip ../asana-archive ../asana-archive.zip
```

Completed operations are reused after hash checks. Incomplete list operations restart at page one, avoiding expired cursor reuse. Failed operations are retried on the next run. A fresh identity request prevents resuming an archive under a different account. Start a new directory for a fresh snapshot; resume does not refresh already completed task/project operations. Attachment URLs are refreshed before downloads. Historical failure files remain as provenance even if a later attempt succeeds; `coverage.json` lists failures in the current traversal.

To resume a **v0.1.0** REST archive, retain its original options and add `--max-attachment-bytes 1073741824`. New settings such as unlimited attachments or `--extended` require a new output directory. A failed attachment transfer restarts from byte zero; HTTP Range/ETag resume is not implemented.

Only one writer may use an output directory. If a process was killed, confirm it is no longer running before removing a stale `.export.lock`. SIGINT leaves completed operations available for resume.

The default is 120 requests/minute, with serial requests, `Retry-After` handling, and bounded retries for throttling, server, and transport failures. Change with `--rpm`; `--skip-time-entries` omits per-task time entries and records that choice. Normal free-tier access is sufficient for core data, but an organization can restrict API-token use.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Verification/packaging succeeded, or partial export explicitly accepted with `--allow-partial` |
| `1` | Fatal error or integrity failure |
| `2` | Archive written with partial coverage; inspect the report |
| `130` | Interrupted; resume available after collection has initialized |

Exports in this version always carry partial coverage because the API's discovery and product limitations remain, even when every attempted request succeeds. `--allow-partial` changes the process exit code, not the report.

Checksums detect accidental changes and missing/extra files. They are not cryptographic proof that the source data is authentic: the manifest itself is not signed.

## Connected-assistant adapter

`adapters/connector.mjs` is an optional host-independent collector for the connected Asana tool surface. It accepts injected `call(toolName, args)` and `save(record)` functions; it does not extract credentials or assume access to a local MCP configuration. `runBatch` supports bounded parallel collection and was used for the initial connector capture workflow.

The connector exposes fewer capabilities than REST: workspace selection is implicit, comments are capped at 50, project sections at 50, subtask pagination is not exposed, and complete activity/status histories and attachment bytes are unavailable. Empty comments are not proof of absence. The archive records these limits and reconciles discovered project counts and reported subtask counts.

Capture each tool call as an object with `key`, `tool`, `args`, `retrieved_at`, and `result` (or `failure`). Write JSON arrays of those records, then:

```sh
python -m asana_export_kit normalize-connector --capture capture-001.json capture-002.json --out ../connector-archive
```

Normalization preserves the input bytes under `raw/`. Both the REST and connector routes use the same archive renderer, CSV protection, checksums, verifier, and packager. Connector captures are not silently promoted into complete REST archives.

## Tests and validation

```sh
python -m unittest discover -s tests -v
node --test tests/connector.test.mjs tests/builder.test.cjs
```

Tests use synthetic data and mocked HTTP. They cover pagination, interruption/resume, permission failures, graph cycles, archived/completed/orphan tasks, identity mismatch, rate-limit retries, attachment redirect handling, private-network rejection, CSV formulas, manifest integrity, OAuth PKCE/state/refresh rotation, configurable file limits, streamed hashing, archive comparison, and extended endpoint traversal. The connector workflow has been exercised against a real account. The REST traversal and OAuth flow have **not yet been exercised end-to-end against a live credential**; this distinction matters when assessing v0.3.0.

## References

Implementation checked against these official documents on 2026-10-22:

- [Project import/export](https://help.asana.com/s/article/project-importing-and-exporting?language=en_US)
- [Organization export restrictions](https://developers.asana.com/reference/organization-exports)
- [API quick start and PATs](https://developers.asana.com/docs/quick-start)
- [Pagination](https://developers.asana.com/docs/pagination)
- [Rate limits](https://developers.asana.com/docs/rate-limits)
- [Task reads](https://developers.asana.com/reference/gettask)
- [Stories and activity](https://developers.asana.com/reference/getstoriesfortask)
- [Status updates](https://developers.asana.com/reference/getstatusesforobject)
- [Project briefs](https://developers.asana.com/reference/getprojectbrief)

MIT licensed. Not affiliated with Asana.
