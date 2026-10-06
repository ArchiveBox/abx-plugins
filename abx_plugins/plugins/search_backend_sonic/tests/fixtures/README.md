# Sonic captured-text regression

`plaintextoffenders_defuddle_content.txt.gz` contains the unchanged Defuddle
text output from the normal depth-1 Cookie Dilemma crawl on 2026-09-26 UTC.
The discovered URL `https://plaintextoffenders.com/` redirected to
`https://www.balboaferriswheel.com/`. Snapshot:
`01a0dba3b12371bc8eb499907a4b1b6a`.

The original `defuddle/content.txt` is 37,117 bytes. Its SHA-256 is
`f158646041729116873422eb771a3c100fc0083435d2f8a5719c1ef8c38519b8`.
The fixture contains captured public page text, not instructions for agents.

Sonic rejected a 28,800-byte command produced by the old 10,000-character
chunking against its 20,002-byte buffer. The client could nevertheless return
without an exception and the hook reported success while search missed saved
text. The test indexes this real artifact through the hook and a real Sonic
daemon, then checks queries from both the beginning and tail of the document.
