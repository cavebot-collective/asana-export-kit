# Authentication, consistency, recovery, and coverage

For features added after 0.2, see the [0.3 operations guide](OPERATIONS.md), including the experimental two-phase purge and offline command builder.

Implemented in 0.2: OAuth login/refresh, configurable attachment limits, streaming integrity checks, template and membership reads, archive comparison, local benchmarks and profiling. Event-driven reconciliation, a restore writer, persistent credential storage, and browser collection adapters are designs below, not implemented features.

## Browser sign-in instead of a manually copied token

Asana supports authorization-code OAuth with PKCE. This CLI opens Asana's sign-in/consent page, receives a loopback callback, exchanges the code, and refreshes the access token during a long export. Your password, SSO, and MFA stay in Asana's browser flow. The exporter only receives authorization credentials.

For this self-owned CLI setup:

1. Register an application in the [developer console](https://app.asana.com/0/my-apps).
2. Register the exact redirect `http://127.0.0.1:8765/callback`.
3. Enable the read scopes needed for your chosen resources in the app. Set `ASANA_OAUTH_SCOPES` to that same space-separated list. For example, `tasks:read` is a task scope, not sufficient for the entire exporter. Consult the current [OAuth scope table](https://developers.asana.com/docs/oauth); scopes and account capabilities both constrain coverage.
4. Supply `ASANA_CLIENT_ID` and optionally `ASANA_CLIENT_SECRET` through your local environment/secret manager. Without the secret environment variable, the CLI prompts without echoing it.

```sh
python -m asana_export_kit export --out ../oauth-archive --oauth --extended --download-attachments
```

`--oauth-no-browser` prints the authorization URL for manual opening. The callback must reach the CLI's loopback port; on a remote host that requires an explicitly configured local tunnel. It does not turn a localhost callback into a publicly reachable service.

This implementation checks state, uses S256 PKCE, binds only loopback, prevents token-endpoint redirects, and keeps access/refresh tokens in process memory. It does not persist a refresh token across separate runs. Unattended recurring exports need an additional OS-keychain/secret-manager integration with refresh-token rotation and revocation handling. Neither usernames/passwords nor a password-grant flow are supported.

A public application offering one-click login to many users should keep its shared client secret in a confidential backend. Do not distribute a shared app secret in this open-source CLI. The current mode is for users who own the registered app and its secret. Live OAuth validation remains outstanding.

PAT creation is documented as a developer-console action; no public PAT-creation API was identified. An authorized browser adapter could automate that UI and hand off for MFA or consent, but OAuth already provides native token issuance. A headless PAT creator is therefore not included in this release.

## Large attachments

The old 1 GiB limit was an exporter budget, not an Asana export limit. The default is now unlimited; `--max-attachment-bytes 0` is equivalent. Positive values impose a per-file limit, checked both against Content-Length when available and the bytes actually received. Failed downloads remove the temporary file.

Downloads, manifest creation, and verification process bounded chunks. ZIP packaging uses Python's ZIP64 support. This does not remove disk space, filesystem, provider, or download-link restrictions. Linked Drive/Dropbox/etc. files may require provider-specific authorization. Metadata/JSON/CSV rendering still holds objects in memory; removing the attachment limit does not make dataset memory usage constant. Byte-level resume and split archives are future work.

## What latency can buy

There are three different promises:

| Mode | Meaning | Status |
| --- | --- | --- |
| Time-spanning archive | Each captured response is retained with collection provenance | Implemented |
| Equal observed state | Two verified archives contain the same observed objects, relationships, and downloaded attachment bytes | Implemented comparison; no source transaction guarantee |
| Quiesced export | Writers are externally paused, pending source updates have settled, and independent scans reconcile | Proposed operational mode; depends on enforceable source assumptions |

```sh
python -m asana_export_kit compare ../fresh-pass-1 ../fresh-pass-2
```

Use the same principal, scope, configuration, and collector. Run each pass into a new directory: `--resume` reuses cached operations and is not a fresh pass. The comparison verifies both manifests, ignores rotating attachment download URLs, and reports changed/new/no-longer-observed records. Missing records can mean deletion, revoked access, changed scope, or failed collection. Read the included coverage reports even when observations match. Exit 0 means observed equality; 2 means differences; 1 means comparison failed. The output always says `transactional_snapshot_proven: false`.

Waiting alone cannot establish an arbitrary historical point-in-time snapshot while edits continue. Asana's [event stream](https://developers.asana.com/reference/events) can exceptionally omit events, retains events for 24 hours, and has expiring sync tokens. Its [bulk resource export](https://developers.asana.com/reference/createresourceexport) also explicitly allows individual resources to reflect different times within the export interval. Polling can recover the latest visible state after changes settle; it cannot reconstruct every overwritten intermediate version.

A stronger future collector would establish event cursors before the baseline, continuously drain events, rescan affected records, reconcile full inventories/relationships/bytes, and run independent full passes. It would restart reconciliation after cursor expiry or a permission change. Events are a latency optimization; full polling is still required. Report source assumptions, scan start/end times, error counts and observation agreement. If users and integrations cannot be reliably paused, call this convergence evidence, not transactional proof. Exact source snapshots require a source-side snapshot/version or equivalent complete ordered history that the available API does not promise.

## Restore and disaster recovery design

An Asana reconstruction tool is feasible for resources with supported write APIs. It needs a separate, explicit write credential and isolated target workspace. The export path should retain read-only data access.

1. Validate checksums/coverage, inventory unsupported fields, and produce a dry-run plan plus identity/role mappings.
2. Create supported containers, custom fields/options, sections, tasks and subtasks in dependency order. Journal each source-to-target GID mapping durably.
3. Apply cross-links, multi-homing, dependencies, assignees and supported memberships. Restore accessible attachment bytes and comment text with source provenance.
4. Resolve ambiguous create timeouts before retrying; blind POST retries can duplicate records. Use durable write intents and verified correlation where supported.
5. Read back the target and compare mapped semantic content. Report losses and unresolved mappings. Run interruption/resume drills and measure recovery time and data-loss window before making DR claims.

New records get new IDs and server-owned timestamps. Historical authorship/activity cannot simply be impersonated; preserve originals as archive metadata and clearly attributed restored text where appropriate. Roles, paid features and external links depend on the destination. These fidelity limits are independent of source consistency. No restore writer or transactional rollback is shipped yet; partial restoration must be journaled and reviewable.

## Extending excluded categories

| Category | Path | Status |
| --- | --- | --- |
| Project/task templates | Paginated discovery plus detail endpoints | Implemented with `--extended`; returned metadata only |
| Unified project/portfolio/goal memberships | Paginated discovery plus individual membership detail | Implemented with `--extended` |
| Comments, activity, status updates, briefs, goals, portfolios, time entries | Ordinary REST reads where permitted | Already in REST collector; connector coverage is narrower |
| Linked external file bytes | Provider adapter with separate authorization | Proposed |
| Messages | Native resource export where entitled; authorized visible-UI adapter otherwise | Proposed; no blanket inbox-coverage claim |
| Rules, native project forms, dashboards | Authorized UI collection where native read APIs are insufficient | Proposed; screenshots/visible configuration may be the available representation |
| Audit logs / SCIM | Enterprise/service-account APIs where entitled | Proposed optional adapters; plan restrictions remain |
| Deleted records / historical revisions | Available retained trash/history, plus prospective versioned archives | Only what the source still exposes; no recovery of never-captured unavailable history |

Browser fallback should use an explicitly authorized session, paginate visible records, save collection provenance, and mark unsupported/incomplete representations. It needs fixture-based UI drift tests and session renewal handling before unattended use. Exporting visible configuration and being able to recreate it are separate capabilities.

Endpoint references: [project templates](https://developers.asana.com/reference/getprojecttemplates), [task templates](https://developers.asana.com/reference/gettasktemplates), [memberships](https://developers.asana.com/reference/getmemberships), [audit log access](https://developers.asana.com/docs/audit-log-events).
