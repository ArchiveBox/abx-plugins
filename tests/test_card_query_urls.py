"""Card templates preserve host-provided URL queries in standalone rendering."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader


class FrameSources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.urls.append(dict(attrs)["src"])


@pytest.mark.parametrize(
    "plugin",
    ["readability", "hashes", "chrome_mhtml", "consolelog", "forumdl"],
)
@pytest.mark.parametrize("query", ["", "?_abx_rev=1792091956.733234&reader=1"])
def test_card_preview_keeps_output_query(plugin, query):
    templates = Environment(
        loader=FileSystemLoader(
            Path(__file__).parents[1] / "abx_plugins/plugins" / plugin / "templates",
        ),
        autoescape=True,
    )
    output = f"https://snap-test.archivebox.localhost/{plugin}/output.html{query}"
    document = templates.get_template("card.html").render(output_path=output)
    frames = FrameSources()
    frames.feed(document)
    assert len(frames.urls) == 1
    url = urlsplit(frames.urls[0])
    assert url.scheme == "https"
    assert url.netloc == "snap-test.archivebox.localhost"
    assert url.path == f"/{plugin}/output.html"
    expected = {"preview": ["1"]}
    if plugin != "chrome_mhtml":
        expected["titlebar"] = ["0"]
    if query:
        expected.update({"_abx_rev": ["1792091956.733234"], "reader": ["1"]})
    assert parse_qs(url.query) == expected
