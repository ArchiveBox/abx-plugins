# Three-party exchange and evidence assembly

`../web/diagram.mjs` defines 22 message groups and deterministic playback. The SVG
has exactly three participants (Client, Server, Verifier) and two connections.
Packets carry abbreviated contents and use different colors for TLS handshakes,
requests, responses, private computation and receipts. Hosting and relay details
are deliberately omitted from this protocol explanation.

The side worksheet types entries at the same playhead position as the packet
animation, identifies their source, and removes future entries when rewound.
Ciphertext arrival precedes readable response assembly; the client-only plaintext
appears after authenticated decryption. The animation uses illustrative values.
The worksheet is an explanation, not a third output file or an uploaded transcript.

A real SHA-256 calculation uses the displayed example response and a fixed all-zero
16-byte example blinder. Actual captures require their random private blinder.
The signature is symbolic, not a real signature or notarization of the example.
The diagram never contacts a service, performs MPC or reads selected capture files.
The real receipt verifier remains independent.

After editing markup, CSS or JavaScript, run `node build.mjs` here to refresh cache
keys and synchronize the plugin's full-output template. No dependencies or build
libraries are needed. Protocol sources are linked under the diagram. Repeated
rounds and timing are illustrative; real TLS and MPC work can interleave.

Playback starts automatically when visible and pauses offscreen or in background
tabs. Local steps last 1.5 seconds; network steps last 4 seconds. Play/Pause always
controls playback explicitly. Compact thumbnails skip initialization. The range input supports arrows,
Home and End. The worksheet sits beside the diagram on desktop and below it on
small screens. With JavaScript disabled, the static diagram remains visible.
