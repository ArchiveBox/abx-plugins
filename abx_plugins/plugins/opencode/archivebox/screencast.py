"""Superuser-only cached inventory and a demand-driven, latest-frame viewer."""

import asyncio
import json
import os
from pathlib import Path
import time
import uuid

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse

from .browser import (
    browser_inventory,
    published_browsers,
    save_selection,
    selection_path,
)


def browser_view(request, settings, action):
    config = settings["config"]
    headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    if action == "list" and request.method == "GET":
        try:
            selected = json.loads(selection_path(config).read_text())
        except (OSError, ValueError):
            selected = None
        return JsonResponse(
            {"browsers": browser_inventory(config), "selected": selected},
            headers=headers,
        )
    if action == "select" and request.method == "POST":
        selected = json.loads(request.body)
        browser = next(
            (
                item
                for item in browser_inventory(config)
                if item["id"] == selected.get("id") and item["available"]
            ),
            None,
        )
        if not browser or not any(
            tab["target_id"] == selected.get("target_id") and tab["available"]
            for tab in browser["tabs"]
        ):
            return JsonResponse({"error": "Browser or tab unavailable"}, status=409)
        save_selection(
            config,
            {"id": browser["id"], "target_id": selected["target_id"]},
        )
        return JsonResponse({"ok": True}, headers=headers)
    if action == "stream" and request.method == "GET":
        browser = next(
            (
                item
                for item in published_browsers()
                if item["id"] == request.GET.get("id")
            ),
            None,
        )
        target = request.GET.get("target") or ""
        if (
            not browser
            or not target.isalnum()
            or not any(
                tab["target_id"] == target and tab["available"]
                for tab in browser["tabs"]
            )
        ):
            return HttpResponse("Browser or tab unavailable", status=404)
        directory = Path(browser["directory"])

        async def frames():
            # The launch owner shares one native subscription per selected tab.
            # No CDP connections, subprocesses, or database queries in this loop.
            demand = directory / "viewers" / f"{uuid.uuid4().hex}.json"
            frame_path = directory / "frames" / f"{target}.json"
            previous = None
            document_id = None
            checked_at = 0.0
            try:
                demand.write_text(
                    json.dumps({"cdp_url": browser["cdp_url"], "target_id": target}),
                )
                yield b'{"type":"connecting"}\n'
                while True:
                    now = time.monotonic()
                    state = json.loads((directory / "tabs.json").read_text())
                    tab = next(
                        (
                            tab
                            for tab in state["tabs"]
                            if tab["target_id"] == target and tab["available"]
                        ),
                        None,
                    )
                    if (
                        not state["connected"]
                        or state["cdp_url"] != browser["cdp_url"]
                        or not tab
                    ):
                        raise ValueError("Browser or tab disconnected")
                    if tab.get("preview_error"):
                        raise ValueError("Browser preview unavailable")
                    if now - checked_at >= 1:
                        os.kill(int(state["owner_pid"]), 0)
                        demand.touch()
                        checked_at = now
                    if document_id != tab["document_id"]:
                        document_id = tab["document_id"]
                        yield (
                            json.dumps({"type": "navigation", "url": tab["url"]}) + "\n"
                        ).encode()
                    try:
                        modified = frame_path.stat().st_mtime_ns
                        if modified != previous:
                            data = frame_path.read_bytes()
                            if json.loads(data)["document_id"] == document_id:
                                previous = modified
                                yield data
                    except FileNotFoundError:
                        pass
                    # Backpressure discards superseded frames: only the current
                    # mailbox is read after the HTTP send completes.
                    await asyncio.sleep(0.025)
            except (OSError, ValueError, KeyError, TypeError):
                yield b'{"type":"unavailable","message":"Browser preview disconnected"}\n'
            finally:
                demand.unlink(missing_ok=True)

        return StreamingHttpResponse(
            frames(),
            content_type="application/x-ndjson",
            headers=headers,
        )
    return HttpResponse(status=405)
