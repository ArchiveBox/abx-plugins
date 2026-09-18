# Protocol illustration

`scene.mjs` is the readable Three.js source for the educational diagram. The
committed `../web/scene.mjs` bundle includes the required parts of Three.js and its
license; no CDN, network API or build tool is needed to run the server or open an
archived viewer. It is separate from the real receipt verification code.

To rebuild with Node.js and npm, run `npm ci` then `npm run build` in this directory.
The lockfile pins the build dependencies. The build updates asset hashes in the
HTML/module references and synchronizes the plugin's full-output template when
present. Commit source, lockfile, generated bundle, and updated references together.
`../web/diagram.mjs` handles the HTML controls and the real, local SHA-256 example.

The 3D diagram is a teaching model, not a live TLS session or zero-knowledge proof.
Its sample response has no relationship to files selected in the receipt verifier.
The module loads on entering the viewport, skips compact thumbnails, limits pixel
ratio to 1.5, pauses offscreen/in background tabs, respects reduced motion, and has
an explicit pause control. A static diagram and explanatory text remain available
if WebGL is unavailable. The generated bundle is about 544 KiB uncompressed and is
copied once into each full plugin output alongside the other viewer assets.
