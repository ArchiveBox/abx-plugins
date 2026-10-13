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
Each importer uses a named Browser Harness daemon, which owns and cleans up its
dedicated tab. The tab is activated so headless Chrome accepts browser input.
An already-running browser is reused. A browser started by the importer is closed
by its existing owner on completion/cancellation.

## Learn, replay, repair

- Every source has its own private `state/current/importer.py` and harness workspace.
- A saved script replays without an agent. Failures trigger up to two live repairs.
- Every repair starts a fresh OpenCode session with the current task and replay
  error. Only scripts and Browser Harness domain knowledge persist.
- Importer sessions hide host-wide OpenCode skills, which may assume a collection
  working directory or a different browser lifecycle. They use the supplied
  connection, installed Harness API, and Harness domain knowledge instead.
- The agent edits the script in place; the host independently replays it and uses
  the shared JSONL validator before returning output, including checking that
  continuation advances the checkpoint. Failed runs never commit progress.
- Each nonempty import also probes one continuation using its returned checkpoint
  and a bounded `delivered_ids` list. Repeated IDs fail shared validation and reach
  the existing fresh-session repair loop. The probe is read-only and its items are
  not committed; the next normal batch reads them with the committed checkpoint.
  Checks/previews and empty imports do not add a probe. This detects cursor defects;
  the learner must still verify complete pagination against the real source.
- The agent reports verified login, CAPTCHA, MFA, and account mismatches as
  `needs_login`; ordinary script failures remain repairable errors.
- Discovery is read-only. Check/preview never commit discovery progress.

The natural-language contract requires complete pagination, including new items
at the head and unfinished older history. `limit` is only a batch size. The host
drains batches until explicit terminal pagination is verified. Site-imposed
history limits or unverified ends must be reported as failures, not empty success.
The agent verifies the end against the real site. The host does not maintain
site-specific recovery rules, completion heuristics, or prompt-version state.

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
