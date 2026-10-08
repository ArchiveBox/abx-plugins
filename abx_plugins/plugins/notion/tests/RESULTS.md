# Live verification, 2026-10-07

`uv run pytest abx_plugins/plugins/notion/tests/test_notion.py -q` passed both
tests (19.35 seconds). The original missing-hook test failed first. An actual
content test then caught Defuddle omitting the document title; the hook now
preserves the title from the rendered DOM in Markdown.

The real public Notion Cookie Tables page produced HTML and Markdown with all
four table sections, first and last cookie entries, heading levels, and Markdown
links. Both artifacts were verified against manifest sizes and SHA256 hashes.

The real CLI also succeeded:

```console
env ABX_PLUGINS_DIR=/Users/squash/Local/Code/archiveboxes/new/abx-plugins/abx_plugins/plugins \
uv run --project /Users/squash/Local/Code/archiveboxes/new/abx-dl abx-dl \
  --plugins=notion --timeout=120 \
  --dir=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/notion-cli-local \
  'https://notion.notion.site/Notion-Cookie-Tables-c38abeb47f8e420a94ade9ac053d90fd'
```

`index.jsonl` records Notion succeeded in 3.7 seconds: `public-page.html` 368677
bytes and `public-page.md` 23443 bytes. The inherited generic Defuddle hook had
noresults because its earlier generic DOM snapshot contained only the initial
Notion shell; the Notion hook explicitly waits for rendered page content.

`uv run ruff check abx_plugins/plugins/notion/tests/test_notion.py` and
`node --check abx_plugins/plugins/notion/on_Snapshot__53_notion.js` passed.

Evidence is outside source checkouts at
`/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`.

After review, classification was moved after Chrome attachment and uses only
the settled current page URL. The public capture and unrelated-page tests both
passed again in 22.50 seconds. The unrelated case now uses a real Chrome session.

The shared `tests/test_provider_noresults.py` suite includes all 17 providers and
uses one real example.com Chrome session. Its initial 13-provider subset passed
in 20.98 seconds; Notion returned noresults in 0.623 seconds and created no output
directory. The four pending providers were selected out explicitly for this
development run; the test file contains no skips or xfails.

The next shared run passed all 16 available provider hooks in 23.30 seconds.
Each hook returned noresults in 0.325–0.706 seconds after using the same real
example.com Chrome session, without creating its output directory. Only iWork
was explicitly selected out because its hook was still being implemented.

The 17-provider scoped Ruff, Ty, and Pyright audits pass. The shared configuration,
presentation, screenshot-recipe, and dependency metadata checks also pass.
Notion's screenshot recipe waits for `public-page.md`, matching its stock file
browser template.

After iWork became available, the full unfiltered suite passed all 17 provider
hooks in 21.88 seconds:

```console
uv run pytest tests/test_provider_noresults.py -q -s \
  --basetemp=/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/provider-noresults-17
```

Individual hook times were 0.306–0.673 seconds; Notion took 0.673 seconds. All
returned noresults and created no provider output directory. The final scoped
Ruff, Ty, and Pyright runs and four metadata tests also passed with all 17
provider directories present.

## Document URL regression

A real `https://www.notion.com/product` Chrome regression failed first because
the domain-only matcher exceeded the hook subprocess's 10-second timeout.
Classification now happens after attachment and recognizes standard document
URLs ending in a 32-hex page ID. A published `notion.site` subdomain root is
accepted only when its actual `.notion-page-content` DOM marker is present.
Ordinary product, help, pricing, and home pages return noresults immediately.

The complete Notion suite then passed **3 tests in 29.57 seconds**, including
the original Cookie Tables HTML/Markdown content/hash test, example.com, and the
real Notion product page. Both negatives create no Notion output directory.
Prettier, node syntax validation, and Ruff passed.

Red/green logs: `notion-domain-red.log` and `notion-domain-green.log` in the
external evidence directory above.

## Table content and replay limitation

Review of the actual saved replay Markdown found four valid contiguous tables,
with 42, 11, 8, and 34 pipe lines and no blank lines between rows. The live
content test now checks all four table blocks against the row counts of the
saved HTML, including each header and separator. The complete real Notion
suite passed **3 tests in 26.74 seconds** (`notion-table-tests.log`).

ArchiveBox's existing fallback Markdown renderer does not support these tables
and displays their rows as paragraphs. This is a replay rendering limitation,
not a conversion failure. The saved `public-page.html` retains the real tables;
the valid Markdown remains downloadable. No new converter or Markdown parser
was added. The replay below opens the saved HTML and verifies all four rendered tables.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-notion-html`, case `0` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
