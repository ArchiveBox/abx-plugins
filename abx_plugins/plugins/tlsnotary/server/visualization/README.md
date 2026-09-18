# Network exchange walkthrough

`../web/diagram.mjs` defines 27 ordered message groups and deterministic playback.
The network diagram is inline SVG in `../web/index.html`. Only the play/pause button
and native range input are interactive. Scrubbing recomputes the current message,
hop, envelope contents and disclosure explanation directly from the playhead.
The illustration never reads selected capture files or makes network requests.

After editing markup, CSS or JavaScript, run `node build.mjs` here to refresh asset
cache keys and synchronize the plugin's full-output template when present. There
are no build dependencies, generated vendor bundles or runtime libraries.

The walkthrough follows the capture hook, gateway and tunnel configuration in this
plugin, plus the extension's ProveManager and TLSNotary's MPC-TLS documentation.
Sources are linked under the diagram. It includes the initial ordinary browser
navigation and the extension's separate authenticated request. Message payloads
use explicit example values, not invented raw proof bytes. The fixed playback
speed is illustrative, not a latency measurement or recorded packet trace.
Repeated MPC rounds and transport details are condensed; the actual channels can
interleave. Key shares are private protocol inputs, not plaintext messages.

Playback pauses offscreen and in background tabs. Reduced-motion mode starts
paused. Compact snapshot thumbnails skip initialization. Without JavaScript, the
static network diagram and initial explanation remain available. The native range
input supports arrow keys, Home and End. The diagram can scroll horizontally on
small screens, while the current hop and message contents stay readable below it.
