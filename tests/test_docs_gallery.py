from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess


def test_gallery_cards_include_screenshots_without_a_plugin_allowlist(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "site"
    subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "docs/generate.py",
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )
    index_path = output_dir / "index.html"
    html = index_path.read_text(encoding="utf-8")
    for config_path in Path("abx_plugins/plugins").glob("*/config.json"):
        plugin_dir = config_path.parent
        config = json.loads(config_path.read_text())
        if config.get("hidden"):
            continue
        recipe = (
            json.loads((plugin_dir / config["screenshot"]).read_text())
            if config.get("screenshot")
            else {}
        )
        if not recipe and not any(
            (plugin_dir / "templates" / filename).is_file()
            for filename in ("card.html", "full.html")
        ):
            continue
        card = html.split(f'id="{plugin_dir.name}"', maxsplit=1)[1].split(
            "</details>",
            maxsplit=1,
        )[0]
        view = recipe.get("view", f"Snapshot View ({plugin_dir.name})")
        slug = re.sub(r"[^a-z0-9]+", "-", view.lower()).strip("-")
        for breakpoint in ("desktop", "tablet", "mobile"):
            image = f"{slug}-{breakpoint}.png"
            assert (
                f'data-screenshot-src="https://archivebox.io/screenshots/{image}"'
                in card
            ), plugin_dir.name
