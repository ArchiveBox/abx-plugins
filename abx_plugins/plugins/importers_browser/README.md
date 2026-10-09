# Browser importers

Browser discovery learned and repaired by OpenCode using Browser Harness and
Stagehand. There are no maintained social-site selectors in ArchiveBox core or
handwritten social scripts in this plugin. Feed declarations contain short task
prompts; the agent learns against the selected account's real browser.

## Setup

- Install the declared `chrome` and `opencode` dependencies, including Browser
  Harness and Stagehand: `abx-dl install chrome opencode`.
- Configure a working model provider in ArchiveBox's existing Agent UI.
- Sync a dedicated browser profile to a server Persona using the extension.
- Select that Persona under **Importers**. The signed-in account is discovered
  automatically; an optional account restriction lives under Advanced options.
- Check access, preview, then **Import all**. Scheduling and overrides are advanced.

ArchiveBox attaches through the same published browser and environment as
OpenCode. A cold Persona opens through `archivebox persona open NAME --headless`.
Each importer owns one tab and one harness daemon, cleaning those up afterwards.
An already-running browser is reused. A browser started by the importer is closed
by its existing owner on completion/cancellation.

## Learn, replay, repair

- Every source has its own private `state/current/importer.py` and harness workspace.
- A saved script replays without an agent. Failures trigger up to two live repairs.
- Task/contract changes force revalidation instead of trusting an older script.
- The host independently replays candidates and validates output before promotion.
- Previous revisions remain in `state/history`. A failed repair preserves the last
  working script and committed checkpoint; logs explain the failure.
- Login, CAPTCHA, MFA, and account mismatches return `needs_login` for human action.
- Discovery is read-only. Check/preview never commit discovery progress.

The natural-language contract requires complete pagination, including new items
at the head and unfinished older history. `limit` is only a batch size. The host
drains batches until explicit terminal pagination is verified. Site-imposed
history limits or unverified ends must be reported as failures, not empty success.
Terminal browser results also include `end.kind` (`cursor`, `marker`, or `count`)
and `end.evidence` describing the observed proof. A repeatedly stagnant viewport
without a loader does not establish complete history.

## Standalone protocol

The `import` command requires an existing `BU_CDP_WS` / `BU_CDP_URL` environment outside
ArchiveBox. Provide installed Browser Harness/OpenCode dependencies and set
`IMPORTERS_STATE_DIR` to a private source-specific directory. Its optional host
adapter is the only module that imports ArchiveBox; no ArchiveBox dependency is
needed with a supplied browser endpoint. ArchiveBox selects the optional declared
`import_archivebox` command, which prepares the persona connection and invokes the
same standalone importer. Plugins without that command continue to use `import`.

Stdin:

```json
{"version":1,"action":"import","feed":"x_bookmarks","settings":{},"checkpoint":{},"limit":100}
```

Stdout (one JSON object per line):

```json
{"type":"ImporterItem","id":"stable-id","url":"https://example.com/item","title":"Item","metadata":{"relationship":"bookmark"}}
{"type":"ImporterResult","status":"succeeded","account":{"id":"stable-account-id","label":"Detected account"},"checkpoint":{"cursor":"opaque"},"has_more":true,"message":"Batch verified; more history remains"}
```

`ImporterProgress` optionally carries a `message`. Final statuses are `succeeded`,
`failed`, and `needs_login`. Every successful import explicitly returns boolean
`has_more`; the checkpoint must advance when it is true. Stable IDs, HTTP(S) URLs,
record types, item counts, and bounded output are checked by the shared
`base.importers.read_records` parser. Diagnostics go to stderr/private run logs.

New feeds need an entry in `config.json`, task bullets, and only genuinely required
fields. `x-advanced` fields remain collapsed. Shared `importers_setup` steps or
per-feed `setup` steps may include plugin-relative `image` and `image_alt` values.
Only clean demo screenshots belong in the plugin; live personal test data does not.
