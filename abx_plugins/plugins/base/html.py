"""HTML resource spans shared by saved-output transforms and host navigation."""

import html
import re
from html.parser import HTMLParser


class Resources(HTMLParser):
    # A request-local class closes over every offset and URL span; its class/MRO
    # cycle keeps that large state alive until cyclic GC. Instances instead own
    # only this document and release it when the request finishes.
    def __init__(
        self,
        html_text: str,
        *,
        passive: bool = False,
        navigation: bool = False,
    ):
        super().__init__()
        self.spans: list[tuple[int, int, str]] = []
        self.inserted_sources: set[int] = set()
        self.background_styles: dict[int, bool] = {}
        self.stylesheet_integrities: dict[int, tuple[int, int]] = {}
        # HTMLParser advances lines only on LF, not CR or Unicode separators.
        self.offsets = [0, *(match.end() for match in re.finditer("\n", html_text))]
        self.base_href = None
        self.passive = passive
        self.navigation = navigation
        self.base_tags: list[tuple[int, int]] = []
        self.head_end = 0
        self.doctype_end = 0
        self.script_tags: list[tuple[int, int, str]] = []

    attribute = re.compile(
        r"\s([^\s=/>]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        re.IGNORECASE,
    )

    def handle_decl(self, decl):
        if self.navigation and decl.lower().startswith("doctype"):
            line, column = self.getpos()
            self.doctype_end = self.offsets[line - 1] + column + len(decl) + 3

    def handle_starttag(self, tag, attrs):
        tag_text = self.get_starttag_text()
        assert tag_text is not None, (
            "HTMLParser start-tag callback requires source text"
        )
        values = dict(attrs)
        if self.navigation and tag in {"base", "head"}:
            line, column = self.getpos()
            start = self.offsets[line - 1] + column
            end = start + len(tag_text)
            if tag == "base":
                self.base_tags.append((start, end))
            elif not self.head_end:
                self.head_end = end
        if self.passive:
            # Browsers keep the first duplicate attribute. Leave the ordinary
            # resource mapper unchanged; passive selection follows that rule too.
            passive_values = dict(reversed(attrs))
            rel = (passive_values.get("rel") or "").lower().split()
            script_fetch = tag == "script" or (
                tag == "link"
                and (
                    "modulepreload" in rel
                    or (
                        "preload" in rel
                        and (passive_values.get("as") or "").lower() == "script"
                    )
                )
            )
            if script_fetch:
                line, column = self.getpos()
                offset = self.offsets[line - 1] + column
                omitted = "src" if tag == "script" else "href"
                if omitted in passive_values:
                    # Rebuild only these start tags from parsed attributes: HTML
                    # permits missing spaces after quoted attributes, so regex
                    # spans requiring whitespace can leave a script URL behind.
                    attributes = "".join(
                        f' {name}="{html.escape(value, quote=True)}"'
                        if value is not None
                        else f" {name}"
                        for name, value in attrs
                        if name != omitted
                    )
                    original_tag = tag_text
                    closing = "/>" if original_tag.endswith("/>") else ">"
                    self.script_tags.append(
                        (
                            offset,
                            offset + len(original_tag),
                            f"<{tag}{attributes}{closing}",
                        ),
                    )
                return
        if tag == "base" and self.base_href is None:
            self.base_href = values.get("href")
        lazy_names = ("data-src", "data-lazy-src", "data-echo")
        lazy_src = (
            next((values[name] for name in lazy_names if values.get(name)), None)
            if tag == "img"
            else None
        )
        names = (
            {"src", *lazy_names}
            if tag == "img"
            else {"src"}
            if tag == "script"
            else set()
        )
        if self.navigation:
            names = {"href"} if tag in {"a", "area"} else set()
        if not self.navigation and values.get("data-echo-background"):
            names.add("data-echo-background")
        if (
            not self.navigation
            and tag == "link"
            and "stylesheet" in (values.get("rel") or "").lower().split()
        ):
            names = {"href"}
        if not names:
            return
        line, column = self.getpos()
        offset = self.offsets[line - 1] + column
        if lazy_src and "src" not in values:
            start = offset + len(tag) + 1
            self.spans.append((start, start, lazy_src))
            self.inserted_sources.add(start)
        attributes = list(self.attribute.finditer(tag_text))
        background = values.get("data-echo-background")
        if not self.navigation and background:
            style = next(
                (match for match in attributes if match.group(1).lower() == "style"),
                None,
            )
            if style:
                group = next(i for i in (2, 3, 4) if style.group(i) is not None)
                start = offset + style.end(group)
            else:
                start = offset + len(tag_text) - (2 if tag_text.endswith("/>") else 1)
            self.spans.append((start, start, background))
            self.background_styles[start] = style is None
        integrity = next(
            (match for match in attributes if match.group(1).lower() == "integrity"),
            None,
        )
        for match in attributes:
            if match.group(1).lower() in names:
                group = next(i for i in (2, 3, 4) if match.group(i) is not None)
                start = offset + match.start(group)
                resource_url = html.unescape(match.group(group))
                if (
                    lazy_src
                    and match.group(1).lower() == "src"
                    and (
                        not resource_url or resource_url.startswith(("data:", "about:"))
                    )
                ):
                    # Cards intentionally disable scripts. Resolve an explicit
                    # lazy image from saved bytes instead of its placeholder.
                    resource_url = lazy_src
                self.spans.append((start, offset + match.end(group), resource_url))
                if tag == "link" and integrity:
                    self.stylesheet_integrities[start] = (
                        offset + integrity.start(),
                        offset + integrity.end(),
                    )

    handle_startendtag = handle_starttag
