# Metadata and article previews

These plugins now provide their own `templates/card.html` and `templates/full.html`.
Each card occupies one 42px snapshot grid row (normal cards occupy three), with a
20px summary below the existing plugin label and file controls.

| Plugin | Main output | Full view |
| --- | --- | --- |
| title | title.txt | Centered page title |
| seo | seo.json | Open Graph and Twitter cards, metadata badges |
| sslcerts | sslcerts.jsonl | TLS sessions, certificate chains, PEM and CT links |
| dns | dns.jsonl | Requests, hosts, address records, configured resolvers |
| hashes | hashes.json | Merkle file tree, copyable hashes and sizes |
| headers | headers.json | Request and response header panes |
| redirects | redirects.jsonl | URL transitions and captured redirect causes |
| chrome | navigation.json / browser.json | Browser window, page target, navigation and extensions |
| accessibility | accessibility.json | Accessibility tree, roles, landmarks and outline |
| consolelog | console.jsonl | Console levels, filters, objects and stack traces |
| parse_dom_outlinks | urls.jsonl | Discovered links and row metadata |
| parse_html_urls | urls.jsonl | Discovered links and row metadata |
| parse_jsonl_urls | urls.jsonl | Discovered links and row metadata |
| parse_txt_urls | urls.jsonl | Discovered links and row metadata |
| parse_rss_urls | urls.jsonl | Discovered links and row metadata |
| parse_netscape_urls | urls.jsonl | Discovered links and row metadata |
| htmltotext | htmltotext.txt | Article reader |
| defuddle | content.html | Article reader |
| trafilatura | configured primary content file | Article reader |

Full views use inline CSS and JavaScript, with no new runtime dependencies or
cross-plugin template imports. On narrow screens diagrams stack, long values
wrap, and toolbar labels collapse to accessible icons.

Every viewer has three output actions:

- **View all files** opens the plugin directory with `?files=1`.
- **Download** targets the main output file directly.
- **View raw** opens that file with `?preview=1&raw=1`, bypassing its plugin
  template and using ArchiveBox's text/JSON preview.

The compact iframe uses `?preview=1&card=1`. ArchiveBox must prefer an explicit
card template over the fallback text thumbnail. Static text exports retain their
inline snippets, because a plain file server cannot render live preview routes.

## Existing views retained

Existing media/document viewers remain in place for archivewebpage, archivedotorg,
chrome_mhtml, claudecode, claudecodecleanup, claudecodeextract, claudechrome, dom,
favicon, forumdl, gallerydl, git, mercury, opentimestamps, papersdl, pdf,
readability, screenshot, singlefile, staticfile, tlsnotary, wget and ytdlp.
Responses remains a file-browser output. Runtime/setup helpers (base, chrome_screencast, media, ssl,
opencode, search backends, infiniscroll, istilldontcareaboutcookies, modalcloser,
twocaptcha and ublock) do not need metadata viewers.

DNS displays only captured relationships: configured nameservers are not proof
that a specific server answered a browser request. SEO images use locally
captured response files when available. Certificate downloads use captured PEM
files; certificate-chain labels reflect the captured certificate metadata.

The newer document extractors liteparse and opendataloader are pending an explicit
viewer choice; their existing file views remain available.
