import json
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def test_singlefile_args_load_deferred_images_by_default():
    config = json.loads((PLUGIN_DIR / "config.json").read_text())

    assert "--load-deferred-images-dispatch-scroll-event" in (
        config["properties"]["SINGLEFILE_ARGS"]["default"]
    )
