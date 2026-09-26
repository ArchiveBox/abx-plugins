# Run your own TLSNotary verifier

This directory is a complete Docker Compose deployment. Copy it to your server;
it needs no ArchiveBox checkout, Node installation, Rust toolchain, or files from
its parent directory. The gateway serves the public verification UI and signs
compact receipts after the pinned official TLSNotary verifier completes MPC.

Install Docker with Compose v2, OpenSSL, and curl. The official verifier image is
currently amd64, so an ARM host needs Docker's amd64 emulation. Allow up to 6 GiB
for the verifier, 256 MiB for the gateway, and another 128 MiB for cloudflared
when using the `public` profile, plus memory for the host OS. Two sessions can run concurrently;
a third receives HTTP 503. This is experimental public-service software.

## Start locally

From this directory, copy `.env.example` to `.env`. Its defaults use `./state` for
the signing key and bind the gateway to loopback port 7047. Set `TLSNOTARY_UID`
and `TLSNOTARY_GID` in `.env` to the output of `id -u` and `id -g` for the account
creating the key. The gateway runs with that identity, so it can read a private
mode-600 key with all Linux capabilities dropped. Generate the key once:
`mkdir -p state && openssl genpkey -algorithm ED25519 -out state/signing.pem && chmod 600 state/signing.pem`.
Back up this key securely; replacing it changes which receipts your server can
sign. Do not regenerate it during upgrades.

Run `docker compose up -d --build`, then check
`curl -fsS http://127.0.0.1:7047/health` and
`curl -fsS http://127.0.0.1:7047/key` once the gateway is ready.
Open <http://127.0.0.1:7047/> for the verification UI. Select `receipt.json` and
`response.http` together; signature and response validation run in the browser,
without uploading those files. `docker compose logs gateway` shows lifecycle
errors. The upstream verifier's transcript logging is disabled in Compose.
Gateway lifecycle lines include a random per-session log ID, a short hash of the
client's random receipt ID matching the hook's correlation line, the control,
verifier, or proxy channel, socket close direction/code, and byte counts. They
contain no request or response bytes. Match lines by log ID to distinguish a
gateway limit, upstream close, and browser close after a failed proof; a close
code of 1005 alone does not identify the initiator.

`docker compose config --quiet` checks the configuration before startup.
`docker compose down` stops the service without deleting `state/`.
For upgrades, replace these source files and run `docker compose up -d --build`.
The signing state is mounted read-only and excluded from the Docker build context.

## Publish through Cloudflare Tunnel

The optional `public` profile includes cloudflared. Create a tunnel in your own
Cloudflare account. Copy `cloudflared.yaml.example` to `cloudflared.yaml`, replace
`YOUR_TUNNEL_UUID` and `tlsnotary.example.com`, and place the tunnel's credentials
JSON at `cloudflared/credentials.json`. The cloudflared container runs as UID/GID
65532; ensure that identity can read the credentials file (for example, owner
65532:65532 with mode 600). Keep the containing directory traversable.

Route the public hostname's DNS to that tunnel before advertising it. Start with
`docker compose --profile public up -d --build`. Check
`https://YOUR_HOSTNAME/health`, `/key`, and `/` from outside the server.
Alternatively, use any HTTPS reverse proxy in front of `127.0.0.1:7047` that
supports WebSockets and connections lasting at least 180 seconds.

Our deployment uses `tlsnotary.zervice.io` routed through Cloudflare Tunnel to the
gateway container. To add `tlsnotary.archivebox.io` or `verify.archivebox.io`, first
create the proxied DNS aliases to that working hostname, then add matching ingress
entries. They are not required to run your own instance. The shipped
`cloudflared.yaml` records our currently routed hostname; use the example for a
new deployment. Credentials, `.env`, and signing state are ignored by Git.

## Point the plugin at your instance

The plugin's `config.json` defines `TLSNOTARY_VERIFIER_URL`; its default is
`https://tlsnotary.zervice.io`. Set it to your HTTPS endpoint and set
`TLSNOTARY_TRUSTED_KEY` to the `publicKey` from your server's `/key` response,
obtained over an independently trusted connection. Enable `TLSNOTARY_ENABLED=true`.
These can be normal ArchiveBox config values or environment variables for abx-dl.
No change to ArchiveBox or abx-dl code is needed.

The bundled public key belongs to our service. A self-hosted signing key must have
its own explicitly configured trusted public key. Distribute that public key to
people verifying your receipts; a key found only inside an untrusted archive is
not an authentication anchor. The UI served by your gateway automatically pins
your server's public key. The plugin uses the same verifier code from `web/`.

## Privacy and scope

The plugin reveals the destination hostname, traffic sizes/timing, and fixed HTTP
protocol markers. URL paths/queries, cookies, response headers, and page content
remain private during the MPC protocol. The signed receipt contains a blinded
SHA-256 commitment; response bytes live once in the local `response.http` file.
The viewer trusts your server and its clock. It does not prove that the server
operator cannot collude, or authenticate unrelated WACZ/DOM outputs automatically.

The public gateway accepts no authentication. It limits active sessions, allocation
sizes, bandwidth buffering and session lifetime, and connects only to public IPv4
addresses on port 443. The verifier and webhook listener stay on a private Docker
network; do not expose them separately. Receipts are held in memory for up to one
hour for collection by clients, not stored as a public archive. There is no durable
capture database in the server. Keep the gateway's signing key and tunnel
credentials private and monitor resource use for your deployment.

## Educational diagram

The verification page includes a self-hosted, scrub-able network exchange below
the file verifier. Its HTML, SVG and JavaScript need no dependencies or build step. The diagram never reads selected capture files and is not a live
proof. See [visualization/README.md](visualization/README.md) to rebuild it.
