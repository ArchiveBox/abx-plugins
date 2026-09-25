from __future__ import annotations

from pathlib import Path
import subprocess


def test_tlsnotary_gallery_card_includes_snapshot_screenshots(tmp_path: Path) -> None:
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
    card = html.split('id="tlsnotary"', maxsplit=1)[1].split("</details>", maxsplit=1)[
        0
    ]

    for breakpoint in ("desktop", "tablet", "mobile"):
        image = f"snapshot-view-tlsnotary-{breakpoint}.png"
        assert (
            f'data-screenshot-src="https://archivebox.io/screenshots/{image}"' in card
        )
        assert f"TLSNotary — Snapshot view ({breakpoint})" in card
