# Mobile Size

Archives should remain useful when replayed at a different screen size from
the recording. Responsive layouts often make additional requests at smaller
breakpoints: `<picture>`/`srcset` selects another image, CSS media queries load
different backgrounds, and JavaScript `resize`/`matchMedia` handlers fetch
different data or code. If recording only visits the desktop layout, those
responses may never enter the archive. Mobile replay can then show broken
images or missing content even though the desktop capture looked complete.

The supplementary phone-width pass gives the active recorders a chance to save
those responses too, so replay can use archived resources instead of needing
the live site. The goal is useful replay at any screen size; one additional
viewport improves coverage without guaranteeing every breakpoint, DPR, or
interaction, or triggering scripts that check width only during initial load.

`mobilesize` briefly resizes the existing Chrome tab to **390 × 844 CSS pixels**
after the desktop screenshot, PDF, DOM, and text outputs have finished. Active
`responses` and `archivewebpage` recorders can then save assets requested by
responsive image and CSS media queries. It restores the original viewport and
scroll position without navigating or opening another tab.

The foreground hook is `on_Snapshot__60_mobilesize.js`, before ArchiveWeb.page's
stop hook at 65. It requires only `chrome`, inherits its binary dependencies,
and creates no archive files or preview card. It does not enable either recorder
if the user has disabled it.

Configuration:

| Setting | Default | Meaning |
| --- | --- | --- |
| `MOBILESIZE_ENABLED` | `true` | Enable the supplementary pass |
| `MOBILESIZE_WIDTH` | `390` | Phone viewport width in CSS pixels |
| `MOBILESIZE_HEIGHT` | `844` | Phone viewport height in CSS pixels |
| `MOBILESIZE_WAIT` | `3` | Maximum seconds to wait for one second of network quiet |
| `MOBILESIZE_TIMEOUT` | `30` | Overall hook timeout in seconds |

The timeout must exceed the network wait by more than five seconds, leaving
time to connect and restore the viewport before the runner's deadline.

The pass preserves DPR and does not enable mobile/touch emulation or change the
user agent. It skips non-HTML documents and viewports already at or below the
requested width. Continuous traffic ends the network wait at the configured
limit; restoration still runs. This is one supplementary viewport, not a crawl
of every breakpoint or lazy-loaded image. JavaScript resize handlers may alter
page state; desktop files have already been saved before the hook runs.
