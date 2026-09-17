# Real extension acceptance — 2026-09-17

These are measured runs, not synthetic fixtures. Images were built from this PR,
abx-dl 1.12.274, abxpkg 1.12.117 and ArchiveBox 0.9.35rc449. The official extension
is 0.1.0.1501 with the version-checked local-export/window-position patch described
in the README. The upstream verifier image is pinned in `server/docker-compose.yml`.

## Successful captures

| CLI / verifier | URL | TLSNotary elapsed | Response | Receipt |
| --- | --- | ---: | ---: | ---: |
| abx-dl Docker / local | news.ycombinator.com | 5.392 s | 6,209 B | 434 B |
| abx-dl Docker / public Cabbage | news.ycombinator.com | 7.945 s | 6,203 B | 434 B |
| abx-dl Docker / local | httpbin cookie round trip | 4.545 s | 302 B | 418 B |
| ArchiveBox Docker / local | news.ycombinator.com | 4.794 s | complete response | compact receipt |
| ArchiveBox Docker / local | sweeting.me | 9.942 s, including shared-browser lock wait | 9,961 B | 422 B |
| ArchiveBox Docker / public Cabbage | sweeting.me | approximately 8.3 s | 9,969 B | 422 B |

Elapsed values are the TLSNotary hook's duration, not a controlled estimate of
incremental whole-crawl latency. Title, screenshot, DOM and headers succeeded in
the ArchiveBox captures. The final Sweeting capture also produced a 183.3 KB
ArchiveWebPage WACZ. SingleFile failed with an unresolved module specifier; a
separate abx-dl run with only SingleFile and title reproduced that error without
TLSNotary. It is visible in the screenshot and is not counted as a TLSNotary pass.

The cookie test navigated Chrome to
`https://httpbin.org/cookies/set/tlsnotary_probe/private-capture-test-20260917`.
After the redirect to `/cookies`, the authenticated response JSON contained
`cookies.tlsnotary_probe == "private-capture-test-20260917"`. The plugin used the
extension's `useHeaders` API and Chrome profile. This proves that cookie case;
it does not establish compatibility with every logged-in site or bot challenge.

## Reproduce with the source-built image

The image must contain this PR's plugin, installed through the normal plugin
installer. A released image predating the PR will not contain it.

```bash
mkdir capture
# TLSNOTARY_ENABLED is required: installation does not enable the plugin.
docker run --rm -v "$PWD/capture:/out" -e TLSNOTARY_ENABLED=true \
  archivebox/abx-dl:tlsnotary-test dl \
  --plugins=title,screenshot,tlsnotary https://news.ycombinator.com/
node abx_plugins/plugins/tlsnotary/tests/check_capture.mjs capture/tlsnotary/current INDEPENDENTLY_TRUSTED_BASE64_SPKI_KEY
```

Run these commands from the repository root.

For ArchiveBox, run its ordinary collection commands with the source-built image:

```bash
mkdir collection
docker run --rm -v "$PWD/collection:/data" archivebox/archivebox:tlsnotary-test init
docker run --rm -v "$PWD/collection:/data" archivebox/archivebox:tlsnotary-test \
  config --set TLSNOTARY_ENABLED=True
docker run --rm -v "$PWD/collection:/data" \
  archivebox/archivebox:tlsnotary-test add \
  --plugins=title,screenshot,dom,headers,archivewebpage,tlsnotary https://sweeting.me/
docker run --rm -p 127.0.0.1:8000:8000 -v "$PWD/collection:/data" \
  archivebox/archivebox:tlsnotary-test server 0.0.0.0:8000
```

Persist the opt-in in collection config so workers launched by an already-running
supervisor receive it too. A one-command environment override does not change the
environment of an existing supervisor.

Open the snapshot detail page and select TLSNotary. Its trusted plugin preview
renders the verifier; the compact card shows the authenticated hostname/status.
Normal single-domain safe mode remains enabled. The separate raw archived HTML
page is not given permission to execute scripts by ArchiveBox.

## Verification and failure behavior

`check_capture.mjs` verifies a real receipt and rejects five corruptions: changed
response, truncated response, altered signed payload, wrong opening and wrong key.
It also requires a compact receipt without a response/transcript copy. These checks
passed for Hacker News, Sweeting and the cookie response.

The live public UI at <https://tlsnotary.zervice.io/> accepted the ArchiveBox
receipt plus `response.http`, displayed the verified hostname and decoded response,
and made **zero network requests during file verification**, observed through the
browser Network event stream. A changed response displayed a commitment mismatch.
The verifier's signing key/code must be independently trusted; the archive cannot
supply its own trust anchor. The receipt authenticates this plugin's separate
extension response, not automatically another plugin's WACZ or rendered DOM.

With an unreachable verifier and a 20-second configured timeout, TLSNotary failed
in 0.897 seconds, saved no receipt, and title/screenshot still succeeded. Chrome
cleanup completed. Multiple sequential snapshots in the same ArchiveBox crawl
also passed, exercising extension unload/reload and shared-browser isolation.

## Public admission

Two independent abx-dl Docker captures of Hacker News and Sweeting overlapped on
Cabbage through Cloudflare. Both exited 0 and produced verifiable receipts; an
actual third WebSocket upgrade received HTTP 503. The same test passed locally.

```bash
# Use a dedicated idle server. This local command requires Docker Desktop.
# On Linux, use a dedicated reachable HTTPS service URL instead.
TLSNOTARY_TEST_CONTROL_URL=http://127.0.0.1:7047 \
node abx_plugins/plugins/tlsnotary/tests/check_parallel.mjs http://host.docker.internal:7047 \
  archivebox/abx-dl:tlsnotary-test ./new-parallel-evidence
# Run on the same idle service; holds real upstream sessions for admission.
node abx_plugins/plugins/tlsnotary/tests/check_admission.mjs http://127.0.0.1:7047
```

The closed-session test first failed with HTTP 101 against the old gateway, then
passed with HTTP 403 locally and on Cabbage after the lifecycle fix. Closed session
IDs remain briefly available only to accept an asynchronous verifier webhook;
they cannot open another verifier/proxy connection.

After the public parallel captures the verifier used 244.7 MiB and gateway
19.63 MiB at idle; both reported no OOM and zero restarts. **Peak memory was not
measured.** Compose caps verifier memory at 6 GiB, gateway at 256 MiB, active
sessions at two and lifetime at 180 seconds. This is a small concurrency acceptance
test, not an Internet abuse/load benchmark.

## Limits and image size

The YouTube watch URL `https://www.youtube.com/watch?v=jNQXAC9IVRw` timed out at
180 seconds without a receipt. Title and screenshot succeeded. Large response
hashing is constrained by the pinned extension; uncompressed Hacker News also
hit the upstream multiplexer's stream limit, while requesting gzip succeeded.
No claim is made about certifying video prefixes or all page assets.

The ArchiveBox image preserves abx-dl's installed plugin package. A Docker-save
layer audit found **zero repeated inherited regular-file content bytes** in its
added layers (only two empty dpkg lock files repeated). Measured image sizes were
866,545,402 B for abx-dl and 912,039,801 B for ArchiveBox, a 45,494,399 B increase.
The audit compares inherited paths and content; it is not a claim that unrelated
files at different paths never share any bytes. No Rust toolchain/native prover
is shipped. The images are local acceptance builds, not published releases.

The public service is deployed on Cabbage. The `archivebox.io` aliases still need
Cloudflare DNS changes; the working zervice.io endpoint is the configured default.

## Late review regression checks

The malformed-upgrade test reproduced two leaked admission slots with invalid
WebSocket keys before the fix. With admission reserved only after a successful
upgrade, both requests returned 400 and health remained at zero active sessions.
The closed-session 403 check and two real concurrent Docker captures passed again
against the updated gateway (third admission 503, both captures exit 0).

The browser viewer cleared all prior authenticated details/content when only a
receipt was selected after a successful verification. A generation counter also
prevents older asynchronous reads, verification, and auto-loading from publishing
a result for a newer selection. HTTP parsing checks use the real Hacker News
fixture with coding-case and trailer variations; forbidden framing trailers and
truncated trailers fail. Windows locking has a native msvcrt path but has not been
runtime-tested on Windows.

Admission checks hold two real registered control sessions until the third request
returns 503; the separate parallel-capture check observes overlap and verifies both
receipts. Separating these avoids racing a live capture finishing during the
admission probe.
