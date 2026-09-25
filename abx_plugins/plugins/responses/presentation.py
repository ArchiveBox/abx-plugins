from pathlib import PurePosixPath
from typing import Any


def extra_snapshot_output_card(outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Build the Responses HTML card alongside the plugin's media gallery."""
    for output in outputs:
        if output["name"] != "responses" or not (result := output.get("result")):
            continue
        path = str(output.get("path") or "")
        if not path.startswith("responses/") or ".." in PurePosixPath(path).parts:
            continue
        files = result.output_file_map()
        info = files.get(path) or files.get(path.removeprefix("responses/")) or {}
        mime = str(info.get("mimetype") or "").split(";")[0].lower()
        if not info or result._coerce_output_file_size(info.get("size")) <= 0:
            continue
        if mime not in {"text/html", "application/xhtml+xml"} and PurePosixPath(
            path,
        ).suffix.lower() not in {".html", ".htm"}:
            continue
        return {
            "name": "responses_html",
            "path": path,
            "folder_path": "responses",
            "ts": output.get("ts"),
            "size": 0,
            "result": None,
            "direct_preview_path": f"{path}?card=responses_html",
            "output_group": "html",
            "output_order": 1000,
        }
    return None
