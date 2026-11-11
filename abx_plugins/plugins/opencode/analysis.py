"""Tool-free structured analysis using the existing collection OpenCode server."""

import json
from pathlib import Path

import requests

from . import runtime


def create_session(config, *, data_dir: Path, title: str):
    settings = runtime._settings(config, data_dir)
    if not config.get("OPENCODE_ENABLED"):
        raise RuntimeError(
            "Enable the OpenCode agent and configure a provider before requesting AI enrichment.",
        )
    ok, error = runtime._ensure_opencode(settings)
    if not ok:
        raise RuntimeError(error)
    response = requests.post(
        f"{settings['origin']}/session",
        params={"directory": str(settings["workdir"].resolve())},
        auth=runtime._server_auth(settings),
        json={
            "title": title,
            "permission": [{"permission": "*", "pattern": "*", "action": "deny"}],
        },
        timeout=settings["timeout"],
    )
    response.raise_for_status()
    session = response.json()
    if (
        not session.get("id")
        or Path(session.get("directory", "")).resolve() != settings["workdir"].resolve()
    ):
        raise RuntimeError(
            "OpenCode returned a session outside the requested collection.",
        )
    return session["id"]


def submit(config, *, data_dir, session_id, prompt, model=""):
    settings = runtime._settings(config, data_dir)
    payload = {"parts": [{"type": "text", "text": prompt}], "tools": {"*": False}}
    if model:
        provider, separator, model_id = model.partition("/")
        if not separator or not provider or not model_id:
            raise ValueError("Model must use provider/model format.")
        payload["model"] = {"providerID": provider, "modelID": model_id}
    response = requests.post(
        f"{settings['origin']}/session/{session_id}/prompt_async",
        params={"directory": str(settings["workdir"].resolve())},
        auth=runtime._server_auth(settings),
        json=payload,
        timeout=settings["timeout"],
    )
    response.raise_for_status()


def result(config, *, data_dir, session_id):
    """Return validated server structured output, None while running, or an error."""
    settings = runtime._settings(config, data_dir)
    ok, error = runtime._ensure_opencode(settings)
    if not ok:
        raise RuntimeError(error)
    response = requests.get(
        f"{settings['origin']}/session/{session_id}/message",
        params={"directory": str(settings["workdir"].resolve()), "limit": 20},
        auth=runtime._server_auth(settings),
        timeout=min(settings["timeout"], 15),
    )
    response.raise_for_status()
    messages = response.json()
    unfinished_assistant = False
    for message in reversed(messages):
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        unfinished_assistant = True
        if info.get("error"):
            raise RuntimeError(
                f"OpenCode analysis failed: {info['error'].get('name', 'provider error')}",
            )
        if info.get("time", {}).get("completed") and info.get("finish") not in {
            "tool-calls",
            None,
        }:
            text = "".join(
                part.get("text", "")
                for part in message.get("parts", [])
                if part.get("type") == "text"
            )
            try:
                return json.loads(text)
            except (TypeError, ValueError) as error:
                raise RuntimeError(
                    "OpenCode returned invalid JSON. Inspect the linked session before retrying.",
                ) from error
    if unfinished_assistant:
        state = requests.get(
            f"{settings['origin']}/session/status",
            params={"directory": str(settings["workdir"].resolve())},
            auth=runtime._server_auth(settings),
            timeout=min(settings["timeout"], 15),
        )
        state.raise_for_status()
        if state.json().get(session_id, {}).get("type", "idle") == "idle":
            raise RuntimeError(
                "OpenCode stopped without a completed summary. Check the linked session and provider credentials or quota before retrying.",
            )
    return None
