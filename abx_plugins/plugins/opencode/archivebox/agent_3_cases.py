import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest

from archivebox.tests.conftest import ADMIN_TEST_HOST, API_TEST_HOST


from archivebox.tests.conftest import (
    get_free_port as get_free_port,
    _reset_runtime_config as _reset_runtime_config,
    _set_archivebox_config as _set_archivebox_config,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_opencode_proxy_blocks_cross_origin_mutation(admin_client, db, live_opencode):
    response = admin_client.post(
        "/admin/agent/opencode/session",
        data=b"{}",
        content_type="application/json",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_ORIGIN="https://evil.example",
    )

    assert response.status_code == 403


def test_opencode_cold_agent_wrapper_returns_before_server_starts(
    admin_client,
    installed_opencode,
):
    import time

    from abx_plugins.plugins.opencode import runtime

    assert not runtime._owned_process_running()
    started = time.monotonic()
    response = admin_client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200
    assert time.monotonic() - started < 3
    assert not runtime._owned_process_running()
    assert b'id="opencode-agent-welcome"' in response.content
    assert (
        f'<iframe data-src="{response.context["proxy_url"]}"'.encode()
        in response.content
    )


@pytest.mark.parametrize(
    "task_route,task_host",
    [
        ("/admin/agent/tasks/", ADMIN_TEST_HOST),
        ("/api/v1/agent/tasks/", API_TEST_HOST),
    ],
)
def test_opencode_proxy_serves_real_project_and_session(
    admin_client,
    admin_user,
    snapshot,
    live_opencode,
    task_route,
    task_host,
):
    from django.test import Client
    from archivebox.api.models import APIToken

    snapshot.title = "Capture task fixture"
    snapshot.save(update_fields=["title"])
    token = APIToken.objects.create(created_by=admin_user)
    client = Client(enforce_csrf_checks=True)
    payload = {
        "snapshot_id": str(snapshot.id),
        "task": "Reply with CAPTURE_TASK_ACCEPTED. Do not modify any files.",
    }
    for route in ("/admin/agent/tasks/", "/api/v1/agent/tasks/"):
        denied = client.post(
            route,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_HOST=ADMIN_TEST_HOST,
        )
        assert denied.status_code == 401

    def submit(data, auth_token=token.token):
        return client.post(
            task_route,
            data=json.dumps(data),
            content_type="application/json",
            HTTP_HOST=task_host,
            HTTP_AUTHORIZATION=f"Bearer {auth_token}",
        )

    assert submit(payload, "invalid").status_code == 401
    assert (
        admin_client.post(
            task_route,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_HOST=task_host,
        ).status_code
        == 401
    )
    assert (
        client.post(
            task_route + "?api_key=" + token.token,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_HOST=task_host,
        ).status_code
        == 401
    )
    for invalid in (
        [],
        {},
        {**payload, "task": " "},
        {**payload, "task": 42},
        {**payload, "task": "x" * 8001},
        {**payload, "snapshot_id": "invalid"},
    ):
        assert submit(invalid).status_code == 400
    assert (
        submit(
            {**payload, "snapshot_id": "00000000-0000-4000-8000-000000000000"},
        ).status_code
        == 404
    )
    admin_user.is_active = False
    admin_user.save(update_fields=["is_active"])
    assert submit(payload).status_code == 403
    admin_user.is_active = True
    admin_user.is_superuser = False
    admin_user.save(update_fields=["is_active", "is_superuser"])
    assert submit(payload).status_code == 403
    admin_user.is_superuser = True
    admin_user.save(update_fields=["is_superuser"])
    response = client.post(
        task_route,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_HOST=task_host,
        HTTP_AUTHORIZATION=f"Bearer {token.token}",
        HTTP_ORIGIN="chrome-extension://test-extension",
    )
    assert response.status_code == 201, response.content
    task = response.json()
    assert task["session_id"].startswith("ses_")
    assert task["snapshot_id"] == str(snapshot.id)
    assert task["session_url"].endswith("/admin/agent/?session=" + task["session_id"])
    deadline = time.monotonic() + 10
    while True:
        messages = admin_client.get(
            f"/admin/agent/opencode/session/{task['session_id']}/message",
            HTTP_HOST=ADMIN_TEST_HOST,
        ).json()
        if messages or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    prompt = next(
        part["text"]
        for message in messages
        if message["info"]["role"] == "user"
        for part in message["parts"]
        if part["type"] == "text"
    )
    assert str(snapshot.id) in prompt
    assert snapshot.url in prompt
    assert snapshot.title in prompt
    assert payload["task"] in prompt
    task_page = admin_client.get(
        "/admin/agent/",
        {"session": task["session_id"]},
        HTTP_HOST=ADMIN_TEST_HOST,
    )
    assert task_page.context["proxy_url"].endswith("/session/" + task["session_id"])

    workdir = str(live_opencode.config.data_dir.resolve())
    encoded_workdir = quote(workdir)

    agent = admin_client.get("/admin/agent", HTTP_HOST=ADMIN_TEST_HOST)
    assert agent.status_code == 200
    frame_path = agent.context["proxy_url"]
    frame = admin_client.get(frame_path, HTTP_HOST=ADMIN_TEST_HOST)
    assert frame.status_code == 302
    session_id = frame.headers["Location"].rsplit("/", 1)[-1]

    project = admin_client.get(
        f"/admin/agent/opencode/project/current?directory={encoded_workdir}",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )
    assert project.status_code == 200
    assert project.json()["id"] == "global"
    assert not project.json().get("vcs")

    path = admin_client.get(
        f"/admin/agent/opencode/path?directory={encoded_workdir}",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )
    assert path.status_code == 200
    assert path.json()["directory"] == workdir

    sessions = admin_client.get(
        f"/admin/agent/opencode/session?directory={encoded_workdir}&roots=true&limit=55",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )
    assert sessions.status_code == 200
    assert any(
        session["id"] == session_id and session["directory"] == workdir
        for session in sessions.json()
    )
    assert not (Path(workdir) / ".git").exists()


def test_opencode_proxy_restarts_server_for_an_existing_agent_page(
    admin_client,
    live_opencode,
):
    from abx_plugins.plugins.opencode import runtime

    old_process = runtime._PROCESS
    runtime._stop_owned_process()

    response = admin_client.get(
        "/admin/agent/opencode/global/health",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )

    assert response.status_code == 200, {
        "installed_lib": live_opencode.settings["config"].get("ABXPKG_LIB_DIR"),
        "request_lib": response.wsgi_request.archivebox_config.ABXPKG_LIB_DIR,
        "installed_binary": live_opencode.settings["binary"],
        "request_binary": getattr(
            response.wsgi_request.archivebox_config,
            "OPENCODE_BINARY",
            None,
        ),
    }
    assert runtime._PROCESS is not None
    assert runtime._PROCESS is not old_process
    assert runtime._PROCESS.poll() is None


def test_opencode_proxy_does_not_wait_for_recovery_lock(admin_client, live_opencode):
    from abx_plugins.plugins.opencode import runtime

    workdir = quote(str(live_opencode.config.data_dir.resolve()))
    assert runtime._owned_process_ready()
    executor = ThreadPoolExecutor(max_workers=1)
    runtime._PROCESS_LOCK.acquire()
    try:
        request = executor.submit(
            admin_client.get,
            f"/admin/agent/opencode/path?directory={workdir}",
            HTTP_HOST=ADMIN_TEST_HOST,
            HTTP_SEC_FETCH_SITE="same-origin",
        )
        response = request.result(timeout=5)
    finally:
        runtime._PROCESS_LOCK.release()
        executor.shutdown(wait=True)

    assert response.status_code == 200
    assert str(live_opencode.config.data_dir.resolve()).encode() in response.content


@pytest.mark.parametrize("path", ["event", "global/event"])
def test_opencode_proxy_sse_delivers_first_event_immediately(
    admin_client,
    live_opencode,
    path,
):
    from abx_plugins.plugins.opencode import runtime

    # The real /event endpoint emits heartbeats indefinitely. A bounded
    # upstream read exposes accidental buffering without hanging the test.
    status, headers, body = runtime.proxy(
        {**live_opencode.settings, "timeout": 1},
        "GET",
        path,
        (),
        {},
        b"",
    )
    assert status == 200
    assert not isinstance(body, bytes)
    assert headers["Content-Type"] == "text/event-stream"
    asyncio.run(body.aclose())
    response = admin_client.get(
        f"/admin/agent/opencode/{path}",
        HTTP_HOST=ADMIN_TEST_HOST,
        HTTP_SEC_FETCH_SITE="same-origin",
    )
    assert response.status_code == 200

    async def first_event():
        stream = response.streaming_content
        try:
            async with asyncio.timeout(2):
                return await anext(stream)
        finally:
            await stream.aclose()

    assert b"server.connected" in asyncio.run(first_event())
