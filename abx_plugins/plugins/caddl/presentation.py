"""Present saved CAD files from any extractor using the local Three.js player."""

from functools import lru_cache
from html import escape
from pathlib import Path
from string import Template
import json


@lru_cache(maxsize=1)
def _manifest():
    return json.loads(Path(__file__).with_name("config.json").read_text())


def serve_replay_asset_response(path, config, response_factory):
    if path not in _manifest()["replay_assets"]["file-preview"]:
        return None
    asset = Path(__file__).parent / "static" / path.removeprefix("replay/")
    mime = "text/javascript" if asset.suffix == ".js" else "text/plain"
    return response_factory(asset.read_bytes(), content_type=mime)


def render_file_preview(filename, output_path, *, replay_base):
    template = Path(__file__).parent / "templates" / "caddl" / "model_viewer.html"
    values = {
        "title": filename,
        "model_format": Path(filename).suffix.lower()[1:],
        "output_path": output_path,
        "viewer_script_url": f"{replay_base.rstrip('/')}/model-viewer/viewer.js",
    }
    return Template(template.read_text(encoding="utf-8")).substitute(
        {name: escape(value, quote=True) for name, value in values.items()},
    )
