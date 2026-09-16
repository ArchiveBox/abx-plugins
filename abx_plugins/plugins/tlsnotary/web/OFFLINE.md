# Offline browser verification

Extract this archive and serve this directory with a local static HTTP server.
For example, with Python already installed through uv:

```bash
uv run --offline python -m http.server 8080 --bind 127.0.0.1 --directory .
```

Open http://127.0.0.1:8080/ and select a capture.tlsn file. Do not open index.html
with a file:// URL: browser module and WASM fetching requires an HTTP origin.
There are no external scripts, fonts, telemetry, proof uploads or verification APIs.
You may disconnect from the Internet after extracting; use an already installed
local server. The source archive includes the native offline CLI alternative.

The bundled trust.json pins the ArchiveBox notary public key. Authenticate that key
independently when operator identity matters. A proof's own public key is not a trust
anchor. The proof file contains its full URL and response, including any secrets.
