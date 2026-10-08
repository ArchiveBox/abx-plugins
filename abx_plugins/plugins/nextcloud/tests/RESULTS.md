# Live verification, 2026-10-07

The real Chrome tests cover Nextcloud's public ISV press folder and the
University of Goettingen's public ownCloud teaching file. Both passed in the
combined five-test provider run: **5 passed in 90.60 seconds**.

The folder download produced ten files with verified manifest sizes and SHA-256
hashes, including the genuine 603266-byte PNG. The delivery ZIP was unpacked.
The ownCloud original was `join_exp.txt`, with the expected header and ten lines.

Run from this repository:

```console
uv run pytest -xq abx_plugins/plugins/nextcloud/tests
```

The ArchiveBox replay test also passed: real CLI capture, Embedded media card,
stock file explorer, folder browsing, and a byte-identical download through the
local replayer. Its selected PNG rendered. This pass is among the first five
results in `replay-ten.log`; that run later failed on another provider.

Evidence: `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/parent-plugins-final.log`
and `replay-ten.log`. Final gallery screenshots and the expanded PDF replay
rerun are not established by these logs. Public shares may change or be removed.

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-images-green2`, case `0` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
