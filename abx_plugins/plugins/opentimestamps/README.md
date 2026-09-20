# OpenTimestamps

Opt-in Bitcoin timestamping of the completed `hashes/hashes.json` manifest. Uses
only CLI arguments, environment configuration, sibling files, shared base helpers,
and the upstream `ots` CLI. No ArchiveBox database or abx-dl imports are required.

The stamped bytes include the Merkle root **and** the file paths, hashes, sizes,
and metadata. The client sends a blinded commitment to the configured calendars;
it does not upload the manifest, URL, archive files, or TLSNotary responses.
Calendar acceptance is a **pending proof**, not a verified Bitcoin timestamp.
See the [upstream client](https://github.com/opentimestamps/opentimestamps-client)
for protocol details and verification requirements.

## Run with abx-dl

From an environment with abx-dl installed:

```console
abx-dl install opentimestamps
OPENTIMESTAMPS_ENABLED=true abx-dl dl \
  --plugins=wget,hashes,opentimestamps --dir=/path/to/snapshot 'https://example.com/'
```

To include a new TLSNotary capture, install `tlsnotary` too and select it:

```console
TLSNOTARY_ENABLED=true OPENTIMESTAMPS_ENABLED=true abx-dl dl \
  --plugins=title,tlsnotary,hashes,opentimestamps \
  --dir=/path/to/snapshot 'https://news.ycombinator.com/'
```

TLSNotary remains optional; OpenTimestamps does not require Chrome or TLSNotary.
Its `required_plugins` declares only `hashes`.

## Run the hooks directly

Install this plugin package and `uv tool install opentimestamps-client==0.7.2`.
After your capture hooks finish, run these from any working directory, with
`PLUGIN_ROOT` pointing to the installed `abx_plugins/plugins` directory:

```console
export SNAP_DIR=/path/to/snapshot
export HASHES_ENABLED=true OPENTIMESTAMPS_ENABLED=true
"$PLUGIN_ROOT/hashes/on_Snapshot__93_hashes.py" --url='https://example.com/'
"$PLUGIN_ROOT/opentimestamps/on_Snapshot__99_opentimestamps.py" --url='https://example.com/'
```

Hooks accept the usual `--snapshot-id` / `--depth` arguments without requiring
those values. `--url` identifies the hook invocation; it is not sent to calendars.

## Ordering and filesystem contract

`on_Snapshot__92_tlsnotary.js` → `on_Snapshot__93_hashes.py` →
`on_Snapshot__99_opentimestamps.py`. All three are foreground hooks: the next one
starts after the previous process exits. Hashes also declares `wait_for_plugins`
for optional download producers, so runners supporting that metadata wait for
already-started downloads without enabling them.

TLSNotary publishes real files under `tlsnotary/capture-*/` before atomically
switching its `current` symlink. Hashes walks those real directories, including
`receipt.json`, `response.http`, and viewer assets; it does not need to follow
`current`. An old retained capture may also appear in the manifest. Check the
TLSNotary hook result to distinguish a new successful capture from retained
outputs after a failure.

Hashes removes `hashes.sha256` when it starts (including disabled/failed runs),
then atomically replaces `hashes.json` and finally publishes its SHA-256 in
`hashes.sha256`. OpenTimestamps requires that checksum to match before submission
and checks the pair again afterward. Missing, partial, or changed manifests fail
explicitly; the plugin neither runs hashes itself nor waits for arbitrary stale
files. Direct callers must finish producers, run hashes successfully, and then
run OpenTimestamps. Concurrent writers/runs in the same snapshot are unsupported.

The manifest describes files at the hashing boundary. Browser monitors and runner
logs may still change during cleanup; timestamping does not freeze those files or
certify later changes. This ordering guarantees completed TLSNotary captures and
the completed manifest, not quiescence of every background browser recorder.
`hashes/` and `opentimestamps/` are excluded from hashing to avoid circular
commitments and recursive inclusion of prior timestamp proofs.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `OPENTIMESTAMPS_ENABLED` | `false` | Opt in to remote calendar submission. |
| `OPENTIMESTAMPS_BINARY` | `ots` | Upstream Python client executable. |
| `OPENTIMESTAMPS_TIMEOUT` | `60` | Hook budget in seconds, including submission. |
| `OPENTIMESTAMPS_CALENDARS` | Alice and Bob HTTPS calendars | JSON array of calendar URLs. |
| `OPENTIMESTAMPS_REQUIRED_CALENDARS` | `2` | Required successful calendar replies. |

For one self-hosted calendar, configure both its URL and a required count of one.
No API key or Bitcoin wallet is needed for submission. `HASHES_ENABLED` must be
true; it is inherited from the hashes config, not duplicated here. Disabled
OpenTimestamps emits `skipped`; missing prerequisites and network errors emit
`failed` with a nonzero exit status. The hook does not retry failed submissions.

## Outputs and verification

`opentimestamps/current/` points to an atomically published generation containing:

- `hashes.json`: exact bytes of the submitted manifest, retained for verification.
- `hashes.json.ots`: detached OpenTimestamps proof.
- `index.html`: offline summary, downloads, and verification instructions.

Successful reruns publish a new generation and retain previous evidence. A failed
rerun leaves the previous generation intact; that retained proof is not a claim
that the failed run succeeded. `card.html`, `full.html`, and `icon.html` provide
the card, full preview wrapper, and plugin icon without host imports. The hook
renders `viewer.html` into each generation's standalone `index.html`; the host
templates embed that completed output instead of trying to reconstruct its data.

After Bitcoin confirmation, from `opentimestamps/current/`:

```console
ots info hashes.json.ots
ots upgrade hashes.json.ots
ots verify hashes.json.ots
```

Custom calendars may require `ots -l https://your-calendar.example upgrade ...`.
Verification with the upstream client normally uses your Bitcoin node. Keep the
manifest alongside its proof; a hash mismatch must fail verification. Independently
compare archived files against the retained manifest, and separately verify
TLSNotary receipts with an independently trusted verifier key. Timestamping
establishes prior existence after successful verification, not page authenticity,
truth, or an exact capture time. The viewer intentionally does not claim it has
verified Bitcoin confirmation, even after you upgrade the proof externally.

## Tests

From the abx-plugins repository, using its development environment:

```console
uv run pytest abx_plugins/plugins/opentimestamps/tests -q
```

Tests use real hook subprocesses, the real signed TLSNotary fixture, a real
OpenTimestamps client installation, and public calendar submissions. Network or
calendar failures fail the live test; no calendar responses are mocked.
