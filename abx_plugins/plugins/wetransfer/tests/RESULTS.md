# Live verification, 2026-10-07

Automatic Terms acceptance is implemented. A real expired SAMSA share first failed the regression at the Terms gate; after the fix, the hook clicked I agree and correctly reported the provider's expired-transfer result without publishing a manifest. **1 passed in 14.87 seconds.**

Run from this repository: `uv run pytest -xq abx_plugins/plugins/wetransfer/tests/test_wetransfer.py`.

Evidence: `wetransfer-autoaccept-red2.log` and `wetransfer-autoaccept-green.log` in `/Users/squash/Local/Code/archiveboxes/workspace-artifacts/provider-plugins-20261007/`.

The positive fixture is the [Dr Katherine Hunt media kit](https://we.tl/t-vRtsYmlUVM), published by her [official media-kit page](https://www.drkatherinehunt.com/media-kit). The real hook downloaded `katherinehunt_headshots-dr-katherine-hunt_2025-10-06_0144.zip` unchanged: 12,086,255 bytes, SHA256 `ebda49edc542b1a65625cc8789ea0486d1c0c3c8b96fcf069dea4462f1b657a6`. All six original JPEG filenames, ZIP CRCs, image signatures, manifest hashes, and dimensions passed. Chrome decoded every JPEG with `createImageBitmap`; the test did not publish extracted copies.

Both real tests passed in **34.55 seconds**. Evidence: `wetransfer-positive-tests.log` and `wetransfer-positive-tests/` under the evidence directory above. The standalone CLI also succeeded in 11.1 seconds (`wetransfer-media-kit.log`). The screenshot recipe waits for the actual ZIP filename.

```console
uv run pytest abx_plugins/plugins/wetransfer/tests/test_wetransfer.py -q
```

Real ArchiveBox CLI and replay UI acceptance passed on 2026-10-07. The test followed the public index, provider stack, file browser, original download and rendered saved content; downloaded bytes matched the capture. Unmodified browser screenshots at desktop/tablet/mobile sizes are stored beside this record. Source run: `replay-wetransfer`, case `0` in the external evidence directory.

## Final presentation acceptance

The final 2026-10-07 run passed all 16 verified provider CLI/replay cases in
494.45 seconds. This provider was captured again from its real public recipe URL,
its originals were downloaded through the replayer and byte-compared, and the
three screenshots were regenerated directly from the browser. Document providers
also verify every format button, returning from the file explorer, and native PDF
reloads where applicable. No screenshots or provider responses were fabricated.
