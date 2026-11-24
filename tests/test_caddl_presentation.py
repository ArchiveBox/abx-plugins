"""The saved model viewer is reusable without a Django installation."""

from html import escape

import pytest

from abx_plugins.plugins.caddl.presentation import render_file_preview


@pytest.mark.parametrize("extension", ["STL", "3mf", "glb"])
def test_saved_model_viewer_renders_safe_html_without_host_templates(extension):
    filename = f'Example $output_path "<body>" & model.{extension}'
    raw_path = '/caddl/files/example?raw=1&token="quotation"'
    rendered = render_file_preview(filename, raw_path, replay_base="/caddl/replay/")
    assert f"<title>{escape(filename)}</title>" in rendered
    assert f'data-model-format="{extension.lower()}"' in rendered
    assert f'data-model-url="{escape(raw_path)}"' in rendered
    assert f'href="{escape(raw_path)}" download="{escape(filename)}"' in rendered
    assert '<canvas id="model"' in rendered
    assert (
        '<script type="module" src="/caddl/replay/model-viewer/viewer.js"></script>'
        in rendered
    )
