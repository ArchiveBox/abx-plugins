# Real public capture

`hacker-news/response.http` and `receipt.json` were produced by the official
extension through the public Cabbage verifier on 2026-09-17, using the normal
abx-dl Docker CLI. The HTTP response contains public Hacker News content.
The fixture has not been synthesized or resigned for testing. The JSON file has
a final newline for repository formatting; its signed payload is unchanged.

Use the independently pinned public key in `server/web/verify.mjs` to verify it.
`tests/check_capture.mjs` also tests rejection of five corruptions. Test fixtures
are excluded from wheel/sdist builds and therefore do not inflate runtime images.
