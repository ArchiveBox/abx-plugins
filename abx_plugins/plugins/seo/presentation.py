"""Small, optional saved-artifact summaries for server-rendered link previews."""

import json
from html.parser import HTMLParser
from pathlib import Path


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def template_context(slot, context):
    if slot != "snapshot_head":
        return {}
    root = Path(context["output_dir"]).resolve()
    files = context["output_files"]

    def read(path, limit=262144):
        if path not in files:
            return ""
        try:
            source = (root / path).resolve()
            if not source.is_relative_to(root):
                return ""
            with source.open("rb") as handle:
                data = handle.read(limit + 1)
            # Never parse truncated JSON or load an unbounded capture into memory.
            if path.endswith(".json") and len(data) > limit:
                return ""
            return data[:limit].decode("utf-8", errors="replace")
        except (OSError, ValueError):
            return ""

    def metadata(path):
        try:
            value = json.loads(read(path))
            return value if isinstance(value, dict) else {}
        except (ValueError, RecursionError):
            return {}

    def plain(value, limit):
        if not isinstance(value, str):
            return ""
        parser = _Text()
        parser.feed(value)
        text = " ".join("".join(parser.parts).split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    seo = metadata("seo/seo.json")
    article = metadata("readability/article.json")
    title = next(
        (
            text
            for value in (
                seo.get("og:title"),
                seo.get("twitter:title"),
                seo.get("title"),
                article.get("title"),
                context["title"],
                context["url"],
            )
            if (text := plain(value, 300))
        ),
        "",
    )
    description = next(
        (
            text
            for value in (
                seo.get("og:description"),
                seo.get("twitter:description"),
                seo.get("description"),
                article.get("excerpt"),
                article.get("description"),
            )
            if (text := plain(value, 300))
        ),
        "",
    )
    if not description:
        for path in ("readability/content.txt", "htmltotext/htmltotext.txt"):
            if description := plain(read(path, 16384), 300):
                break
    if not description:
        description = plain(f"Archived snapshot of {context['url']}", 300)
    image = ""
    for plugin in context["primary_preview_plugins"]:
        for path, file_metadata in files.items():
            if file_metadata["plugin"] != plugin or Path(path).suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
            }:
                continue
            try:
                source = (root / path).resolve()
                if (
                    source.is_relative_to(root)
                    and source.is_file()
                    and source.stat().st_size
                ):
                    image = path
                    break
            except (OSError, ValueError):
                continue
        if image:
            break
    return {"social": {"title": title, "description": description, "image_path": image}}
