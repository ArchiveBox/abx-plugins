# Reddit comment-toggle regression fixture

`reddit-init.js.gz` is the byte-exact public Reddit browser bundle retrieved on
2026-10-12 from https://www.redditstatic.com/reddit-init.en.Jre8Hvy5gDE.js.
Uncompressed: 280793 bytes, SHA256
`11bc831318292392808e355a415bae45b64c8b94a8844aa77b74a06117cedb61`.
It contains the actual jQuery, Reddit `.thing()` helper and `togglecomment`
implementation; the test executes this client code without replacing handlers.
No persona, cookies, server configuration or captured user content is included.

The markup models the two native toggle links in the saved MacOS comment:
`.expand` and `.numchildren` both call `togglecomment(this)`. The native handler
switches the nearest `.thing` between `collapsed` and `noncollapsed`; see
https://github.com/reddit-archive/reddit/blob/master/r2/r2/public/static/js/reddit.js.
The fixture is excluded from distributable plugin packages with other tests.
