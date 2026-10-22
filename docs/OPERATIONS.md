# Command builder, snapshots, storage, and purge

Version 0.3 implements these workflows. It does not run a hosted OAuth service or an Asana browser scraper. No source data is deleted by `export`, `watch`, `upload`, or `purge-plan`.

## Browser command builder

```sh
python -m asana_export_kit gui
```

This opens a bundled offline HTML page. It builds commands for PowerShell or Bash with individually quoted arguments. It does not execute commands, accept secrets, connect to Asana, or save your settings. `gui --print-path` prints the file path without opening a browser; the HTML and JavaScript can also be served as ordinary static files.

Controls select workspace scope, request budget, attachment metadata/bytes, comments/activity, time entries, portfolios, goals, status updates, briefs, and templates/membership detail. The core project/task/people/relationship collector always runs. Omitted categories may still appear as embedded references in other records; selection is a collection policy, not a redaction guarantee.

The read scopes for OAuth must match the selected collectors. A shared OAuth app can serve users of the Kit, but this release's CLI still expects a self-owned app registration. One registration for the public Kit requires a hosted confidential token-exchange service; that has not been deployed.

## Remote storage through rclone

Configure a named remote using rclone's own setup, then:

```sh
python -m asana_export_kit upload ../asana-archive drive:Asana-backups --receipt ../asana-archive.upload.json
```

Or append `--remote drive:Asana-backups` to `export`. The CLI then creates a sibling upload receipt after collection. `--rclone PATH` selects the executable when it is not on PATH.

The handoff is:

1. Verify the sealed local archive.
2. Derive a destination directory from the complete SHA-256 of its manifest.
3. Run `rclone copy ... --immutable` to that destination, excluding only the transient exporter lock.
4. Run `rclone check ... --download`, then reverify the local archive and original manifest hash.
5. Write a receipt marked `verified` only after every step succeeds.

The remote is a separately configured rclone service such as Drive, S3, OneDrive, or an encrypted crypt remote. No remote credentials are stored in this repository. Subprocess calls use argument arrays, never a shell. A successful check is evidence at that time, not a promise of indefinite retention or restoration fidelity. Download verification can incur bandwidth/egress costs. Receipts remain outside the sealed archive to avoid invalidating its manifest.

`copy` preserves earlier remote snapshots. `sync` can delete destination files, so it is not used for this backup handoff. There is no source/local cleanup after upload. A stdout pipe would bypass useful local checkpoints and the sealed multi-file manifest; the handoff therefore uses the completed directory.

References: [rclone copy](https://rclone.org/commands/rclone_copy/), [rclone check](https://rclone.org/commands/rclone_check/).

## Interval snapshots

```sh
python -m asana_export_kit watch --root ../asana-snapshots --interval 3600 --prompt-token --remote drive:Asana-backups -- --download-attachments --extended
```

The prompt occurs once. Alternatively provide `ASANA_ACCESS_TOKEN` through your environment/secret manager. The watcher passes the token only through the child process environment. Browser OAuth scheduling is not supported yet; it needs persistent refresh-token handling. For unattended runs, use the environment route and supervise the process with your OS service manager or scheduler.

Each cycle gets a new directory and performs a fresh full collection, local verification, and optional remote handoff. It does not reuse stale operations from earlier snapshots. `--runs N` stops after N cycles; 0 continues until stopped. The process must remain running.

For requested start interval I and collection + verification + transfer duration D, the next cycle starts after max(I, D), plus scheduling overhead. Only one cycle runs at a time. Missed ticks are not queued. Failures stop the loop, retain evidence, and record a failed receipt. Successful partial exports (exit 2) are retained as partial; the schedule does not relabel them complete coverage. Review their coverage reports.

The root lock prevents two watchers using the same output root. After a crash, inspect the running processes before clearing a stale lock. Do not run independent writers against a snapshot being transferred or considered for purge. Run receipts include actual duration, effective interval and wait time.

This is periodic snapshotting, not incremental synchronization. It preserves versions observed in separate snapshots; changes entirely between snapshots can still be missed.

## Two-phase purge (experimental)

Purge is a separate opt-in mutation path with its own credential. The current executor supports only explicitly selected **isolated leaf tasks**: no parent, subtasks, dependencies or dependents. It does not delete projects, task trees, messages, external-provider files, or every exported category. Broader deletion needs explicit cascade/dependency planning.

### Phase 1: prepare and review

```sh
python -m asana_export_kit purge-plan ../asana-archive --task 123456 --task 789012 --out ../purge-plan.json
```

No Asana DELETE calls occur. The plan binds the exact task selection, principal, archive path, manifest hash, captured-operation fingerprints, attachment byte hashes, limitations and a 24-hour expiry. Its `plan_id` hashes the plan content. The plan must live outside the archive. A frontend shortcut must still produce this durable plan before applying it.

The planner verifies the archive and requires successful saved REST responses for each candidate's base/expanded task detail, subtasks, dependencies, dependents, stories, time entries and attachments. Every attachment must have archived metadata and bytes. Missing or denied operations block that task. A connector-only archive is insufficient. Even an account where an unsupported time-entry endpoint fails is blocked; there is no silent assumption that denied means empty.

Inspect `targets`, `blocked`, `coverage` and `limitations`. If any requested task is blocked, apply refuses the entire plan. To proceed with a smaller selection, create and review a new plan for those exact IDs. Task names and IDs make plans private artifacts.

### Phase 2: apply that exact plan

```sh
python -m asana_export_kit purge-apply ../purge-plan.json --confirm PLAN_ID --writers-paused --accept-coverage-limits --prompt-token
```

Use a separate credential with task-deletion permission belonging to the same principal. Without `--prompt-token`, this command reads `ASANA_DELETE_TOKEN`, not the export token variable. Do not place the token in command arguments. Read-only OAuth export credentials do not acquire deletion permission automatically.

Apply revalidates the plan/expiry, manifest and captured evidence, checks the authenticated principal, and rereads every required endpoint with pagination. It also downloads and rehashes attachments against the archived bytes. It checks every candidate before the first mutation, then rechecks each candidate immediately before its DELETE. A changed or inaccessible record stops execution. Every write intent is durably recorded before the request, with a success record after an acknowledged response.

There is no automatic DELETE retry. An interrupted or ambiguous request leaves an unresolved intent and blocks resume; inspect/reconcile it before continuing. Completed deletions are journaled and skipped on a subsequent apply of the same valid plan. Failure partway through is a partial purge, not a rollback. Retain the archive, plan and journal.

`--writers-paused` is an operator assertion, not a lock on Asana. Users, integrations and automations must actually remain quiescent. The API provides no documented atomic compare-and-delete on this path, so a race remains if that assertion is false. `--accept-coverage-limits` explicitly acknowledges that preserved public-API content does not include every unavailable revision/rule/app feature. Verified bytes do not establish complete restorability. If the archive was uploaded, inspect its successful remote receipt before applying; apply verifies the local archive and does not re-certify a previously checked remote.

Asana's [task DELETE endpoint](https://developers.asana.com/reference/deletetask) moves tasks to the deleting user's trash, with documented recovery for 30 days. This feature is not secure permanent erasure. It has automated tests with fake transports, including refusal and ambiguous-result cases, but has not been validated by deleting tasks in a live sandbox. A sandbox drill is the remaining gate before operational use.
