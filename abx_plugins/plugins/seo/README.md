# SEO

The capture hook saves page metadata in `seo.json`.

## Snapshot sharing previews

`templates/snapshot_head.html` contributes server-rendered social tags to the
ArchiveBox snapshot page through the existing `plugin_ui` slot renderer.
`config.json` registers `presentation_module`; its optional
`template_context(slot, context)` prepares this fragment's `social` context.
The presentation module imports neither ArchiveBox nor Django.

The plugin prefers saved SEO titles/descriptions, then optionally reads
`readability/article.json`, `readability/content.txt`, and
`htmltotext/htmltotext.txt`. It chooses a saved screenshot from `screenshot`,
`chrome_extension_screenshot`, or `chrome_extension_viewport`. These sibling
artifacts are optional: absent, malformed, oversized JSON, failed outputs, and
paths outside the snapshot directory are ignored. Reads are bounded and only
successful output manifests supply candidate files. No original site is fetched.

The fragment uses host-provided snapshot URL template tags so file permissions
and replay origins remain the host's responsibility. Static exports omit social
URLs/images because their future public origin is unknown. The original URL's
canonical link belongs to the host snapshot template and does not depend on SEO.

## UI fragment registration

ArchiveBox's `{% plugin_ui "slot_name" %}` renders each installed plugin's
`templates/slot_name.html` in plugin-name order. The existing explicit form,
`{% plugin_ui "plugin_name" "slot_name" %}`, renders only that plugin. Missing
fragments contribute nothing; a failed optional fragment does not break the page.
A plugin's existing `presentation_module` may implement
`template_context(slot, context)` to return additional values for its fragment.
Each fragment gets its own context dictionary. The host supplies page context and
URL helpers; plugin-specific artifact parsing and markup stay in the plugin.

The snapshot detail page exposes `snapshot_head` inside its `<head>` for live
pages and static exports. The SEO fragment above uses this slot.
