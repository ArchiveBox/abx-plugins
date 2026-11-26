from pathlib import Path

from abx_plugins.plugins.responses.presentation import (
    _encoded_responses_image_url,
    _index_responses_paths_for_html_images,
)


def test_indexed_replay_resolves_common_parent_once(tmp_path, monkeypatch):
    responses = tmp_path / "responses"
    saved = responses / "all"
    saved.mkdir(parents=True)
    (responses / "index.jsonl").touch()
    paths = [f"{i}.png" for i in range(40)]
    for path in paths:
        (saved / path).touch()
    (saved / "internal.png").symlink_to(saved / paths[0])
    outside = tmp_path / "outside.png"
    outside.touch()
    (saved / "external.png").symlink_to(outside)
    (responses / "external-dir").symlink_to(tmp_path, target_is_directory=True)
    relative = [f"all/{path}" for path in paths]
    relative += [
        "all/internal.png",
        "all/external.png",
        "external-dir/outside.png",
        "all/missing.png",
    ]
    records: list[dict] = [
        {
            "method": "GET",
            "status": 200,
            "url": f"https://example.com/{path}",
            "path": path,
        }
        for path in relative
    ]
    encoded: list[str] = []
    for path in relative:
        key = _encoded_responses_image_url(f"https://example.com/{path}", None)
        assert key is not None
        encoded.append(key)

    original_resolve = Path.resolve
    resolved = []

    def track_resolve(self, *args, **kwargs):
        resolved.append(self)
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", track_resolve)
    actual = _index_responses_paths_for_html_images(
        tmp_path,
        "dom/output.html",
        set(encoded),
        records=records,
    )

    assert actual == {
        **dict(zip(encoded[:40], (f"../responses/all/{path}" for path in paths))),
        encoded[40]: "../responses/all/0.png",
    }
    assert resolved.count(saved) == 1
    assert not any(path in resolved for path in (saved / name for name in paths))
