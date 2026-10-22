# Coverage beyond the documented public API

Most current, visible product state is a reasonable browser-collector target. A missing public API endpoint does not establish that the data is inaccessible to its owner. The stronger route is to inspect the authorized web application's own responses while navigating the feature, preserve useful structured payloads, and use DOM extraction or screenshots for state available only through presentation.

These are feasibility assessments based on product documentation, not successful extraction measurements. No site-specific browser scraper is shipped in 0.3. We have not yet inspected a live signed-in session for these categories, so neither completeness percentages nor internal endpoint contracts are claimed.

| Category | Defensible target | Important limit |
| --- | --- | --- |
| Messages | Visible bodies, authors, timestamps, thread relationships, comments, attachments and parent containers | Must discover/paginate each authorized container; bulk resource export is another route where entitled |
| Inbox | Visible notifications, references, archived/read state, available history | Inbox is personal notification state, not the same dataset as messages; capture initial state before navigation that may mark entries read |
| Deleted records | Visible retained trash entries and any detail still returned for them | Retention window and permanent deletion bound retrieval; no hidden promise of recovering erased records |
| Rules | Visible triggers, conditions, branches, actions, enabled state, ownership and configuration | Inspect each editor; referenced integrations may hide credentials or configuration; preserve unsupported action types as raw observed data |
| Forms | Questions, types, choices, required flags, branches, mappings, settings, sharing and presentation | Walk every branch/editor state; a rendered form alone misses authoring configuration |
| Dashboards | Chart definitions, filters/groupings, layout, text widgets, rendered results and underlying permitted rows | Capture permissions and observation time; chart snapshots and structured definitions are different outputs |
| App configuration | Connected-app inventory, visible scope/authorization details, exposed settings, integration references | Depends on user/admin access; hidden credentials and server-only state are not reconstructable from a settings screen |
| Audit logs | Logs exposed to an entitled administrator/service account, including available retention | Browser automation does not create an entitlement or data the account is not shown |
| Revision history | Any history actually exposed by the UI or its responses; task activity through REST already exists | Activity is not a complete sequence of prior descriptions/comments; prospective snapshots only preserve observed versions |

## Adapter requirements

Use the user's authorized session and ordinary navigation. Observe actual request/response contracts instead of guessing hidden URLs. Save returned record JSON with source URL, resource/category, timestamp, pagination boundaries, capture method and explicit failures. Do not archive cookies, Authorization headers or session tokens. Authentication state belongs in the credential/session store, not exported business data.

Preserve raw source evidence alongside normalized fields. Track UI version/selector assumptions. Reconcile counts where the UI supplies them and record when it does not. Exercise all branches and lazy-loaded pages; opening a feature once is not exhaustive discovery. A screenshot can establish visual evidence but cannot substitute for structured record coverage when structured payloads are available.

Treat source changes and notification-read side effects explicitly. Reuse the normal archive manifest, comparison and storage pipeline. A browser supplement must not automatically make an archive eligible for purge: each deletion adapter needs its own demonstrated coverage and cascade model.

Suggested implementation order: messages/inbox and retained trash; rules/forms editor configuration; dashboard definitions and results; app settings; entitled audit adapters. Start by inspecting a real session and building fixtures for the observed responses. This order prioritizes additional user-owned content over cosmetic screenshots.

## Official references checked 2026-10-22

- [Inbox](https://help.asana.com/s/article/inbox)
- [Retained deleted items](https://help.asana.com/s/article/recover-deleted-tasks-projects-and-more)
- [Rules](https://help.asana.com/s/article/rules) and [conditions/branching](https://help.asana.com/s/article/conditions-and-branching-in-rules)
- [Forms](https://help.asana.com/s/article/forms) and [form branching](https://help.asana.com/s/article/how-to-use-forms-branching)
- [Reporting dashboards](https://help.asana.com/s/article/reporting-with-dashboards) and [chart styles](https://help.asana.com/s/article/chart-styles)
- [Connected apps](https://help.asana.com/s/article/apps-settings) and [admin app settings](https://help.asana.com/s/article/app-management-and-integrations)
- [Audit-log entitlement](https://developers.asana.com/reference/audit-log-api)
- [Native resource export](https://developers.asana.com/reference/createresourceexport)
