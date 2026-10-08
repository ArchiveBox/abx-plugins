# Live verification, 2026-10-07

The real Chrome native export test passed for the public document linked by
e-flux criticism. It is third-party fixture content, not an endorsement. The
test is included in the combined provider run: **5 passed in 90.60 seconds**.

The document menu exported Markdown, HTML, and DOCX. Manifest sizes and SHA-256
hashes match the saved files. English section text and Japanese text are present
in both text exports and the DOCX `word/document.xml`.

Run from this repository:

```console
uv run pytest -xq abx_plugins/plugins/protondocs/tests
```

The ArchiveBox replay test passed real CLI capture, Embedded media card, stock
file explorer, and a byte-identical download through the local replayer. This
pass is among the first five results in `replay-ten.log`; that run later failed
on another provider. This does not verify native DOCX visual rendering.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/parent-plugins-final.log`
and `replay-ten.log`. Final gallery screenshots and the expanded PDF replay
rerun are not established by these logs. The public document may change or be
withdrawn.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered document; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-final11b`, case `2` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
