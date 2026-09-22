"""Image dimensions survive real article extraction, without viewer lookups."""

import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = {}

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            attrs = dict(attrs)
            self.images[attrs.get("alt")] = attrs


@pytest.mark.parametrize(
    "plugin,binary",
    [("defuddle", "defuddle"), ("readability", "readability-extractor")],
)
def test_real_article_hook_preserves_image_sizes(tmp_path, plugin, binary):
    plugin_dir = (
        Path(__file__).resolve().parents[1] / "abx_plugins" / "plugins" / plugin
    )
    installed = install_required_binary_from_config(plugin_dir, binary)
    assert installed and installed.abspath
    source = tmp_path / "dom" / "output.html"
    source.parent.mkdir()
    source.write_text(
        "<html><head><title>Image sizing article</title></head><body><article>"
        "<h1>Image sizing article</h1>"
        + "<p>This article explains why original image dimensions matter when reading an archived page. "
        "Decorative icons should remain inline while photographs should retain their natural proportions.</p>"
        * 6
        + '<p><img src="https://example.com/icon.svg" alt="Inline icon" style="width:4%"> Navigation links</p>'
        '<img src="https://example.com/logo.svg" alt="Logo" height="80">'
        '<a href="https://example.com/photo.svg"><img src="https://example.com/photo.svg" alt="Featured photo"></a>'
        "</article></body></html>",
    )
    original = source.read_bytes()
    images_dir = tmp_path / "responses" / "image" / "example.com"
    images_dir.mkdir(parents=True)
    for name in ["icon", "logo", "photo"]:
        (images_dir / f"{name}.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400"><rect width="800" height="400" fill="blue"/></svg>',
        )
    hook = next(plugin_dir.glob(f"on_Snapshot__*_{plugin}.py"))
    result = subprocess.run(
        [str(hook), "--url", "https://example.com/article"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            f"{plugin.upper()}_BINARY": str(installed.abspath),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    parsed = Images()
    parsed.feed((tmp_path / plugin / "content.html").read_text())
    assert "width:4%" in parsed.images["Inline icon"].get("style", "")
    assert "height:80px" in parsed.images["Logo"].get("style", "")
    assert "width" not in parsed.images["Featured photo"].get("style", "")
    assert source.read_bytes() == original
