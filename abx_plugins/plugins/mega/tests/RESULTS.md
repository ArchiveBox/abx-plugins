# MEGA evidence — 2026-10-07

**Native capture UNVERIFIED.**

Read the official `meganz/webclient` source at commit
`a3900cd4c19be52ef6e0daad423acf89edaa2e02` to identify the Download and ZIP
controls. The folder hook incorrectly selected `.zipdownload-item` after opening
the toolbar menu. Official `js/ui/components/meganz/fm-secondary-nav.js` lines
353–383 and 939–974 show that this menu instead contains a button with
`.icon-download-zip`; its native handler calls `M.addDownload([dlId], true)`.
The hook now selects that actual toolbar button. This fixes a concrete source
mismatch but does not establish runtime download behavior.

After two third-party fixtures were blocked, researched a materially safer
alternative published by MEGA itself in its [MEGAcmd guide](https://github.com/meganz/MEGAcmd/blob/master/UserGuide.md#exporting-and-importing):
the 1.31 MB Pictures demo folder containing MEGAcmd promotional GIFs. The same
browser tool rejected that official folder before navigation with:

> Browser use is not permitted on https://mega.nz/folder/iaZlEBIL.

The tool explicitly prohibited alternate browser surfaces, raw CDP, indirect
execution and other workarounds. No permission prompt or automatic approval
review occurred. Navigation work stopped; no MEGA HTTP/API/CDP request, CLI
capture or download followed the rejection. The provider's own availability and
export behavior remain unknown.

No live successful MEGA file/folder capture was performed. No simulated provider
response, invented fixture, skip, xfail or fake success assertion was added.
There is no screenshot recipe that would route gallery automation to the blocked
resource. The workspace's common unrelated-page checks cover early applicability.
Native content, decryption, nested folder paths, CRC/hash integrity and offline
replay still require a permitted real share fixture.

After the selector correction, the real shared Chrome lifecycle test passed:

```console
TMPDIR=/tmp ABXPKG_LIB_DIR=/path/to/isolated/runtime uv run --no-sync pytest -xq tests/test_provider_noresults.py -k mega
```

Result: `1 passed, 16 deselected` in 6.91 seconds. The hook attached to the
real settled `https://example.com` target, emitted `noresults`, and created no
MEGA output. `node --check` and scoped `prek` checks passed, including Prettier,
Ty and Pyright. These checks establish safe unrelated-page behavior and static
validity; they do not establish a successful MEGA export.
