# Live verification, 2026-10-07

The real Chrome download test passed for the public folder published in pdown's
upstream README. It is included in the combined provider run: **5 passed in
90.60 seconds**.

The native download produced `example.txt`, `subfolder/example.jpeg`, and
`subfolder/subfolder-2/example.mp4`. Tests verify every manifest size and SHA-256,
the exact text `:)\n`, JPEG and MP4 signatures, and preserved nested directories
after unpacking the delivery ZIP. Proton's application performed decryption.

Run from this repository:

```console
uv run pytest -xq abx_plugins/plugins/protondrive/tests
```

The ArchiveBox replay test passed real CLI capture, Embedded media card, stock
file explorer, folder browsing, a byte-identical download, and rendering the
selected JPEG. This pass is among the first five results in `replay-ten.log`;
that run later failed on another provider. MP4 playback was not asserted.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/parent-plugins-final.log`
and `replay-ten.log`. Final gallery screenshots and the expanded PDF replay
rerun are not established by these logs. Public fixture contents may change.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are retained in the external historical acceptance evidence directory. Source run: `replay-images-green2`, case `2` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.

Historical browser screenshots and captures remain in `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`. Current screenshots are regenerated and published by scheduled CI in the [live screenshot gallery](https://archivebox.io/screenshots/#snapshot-view-protondrive); screenshot binaries are not checked in beside this record.
