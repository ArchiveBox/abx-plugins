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
        if not recipe and not (
            (plugin_dir / "templates/full.html").is_file()
            or (
                (plugin_dir / "templates/card.html").is_file()
                and not config.get("card_hidden")
            )
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


def test_marketplace_uses_first_manifest_commit_and_follows_renames(
    tmp_path: Path,
) -> None:
    import os
    import runpy

    plugin_added_times = runpy.run_path("docs/generate.py")["plugin_added_times"]
    repo = tmp_path / "repo"
    repo.mkdir()
    plugins = repo / "plugins"

    def git(*args: str, day: int = 1) -> None:
        date = f"2025-01-{day:02}T00:00:00+00:00"
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
                "GIT_AUTHOR_NAME": "Fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "Fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            },
        )

    def add(name: str, day: int) -> Path:
        manifest = plugins / name / "config.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"title": name}))
        git("add", ".")
        git("commit", "-m", f"Add {name}", day=day)
        return manifest

    git("init")
    legacy = add("legacy", 1)
    fresh = add("fresh", 2)
    git("mv", str(legacy.parent), str(plugins / "renamed"))
    git("commit", "-m", "Rename legacy", day=3)
    fresh.write_text(json.dumps({"title": "Updated"}))
    git("commit", "-am", "Update fresh", day=4)
    assert plugin_added_times(repo, plugins) == {
        "legacy": 1735689600,
        "renamed": 1735689600,
        "fresh": 1735776000,
    }

    git("clone", "--depth=1", repo.as_uri(), str(tmp_path / "shallow"))
    shallow = tmp_path / "shallow"
    assert plugin_added_times(shallow, shallow / "plugins") == {}
    assert plugin_added_times(tmp_path, tmp_path / "plugins") == {}
