"""Tool-denied native OpenCode CLI analysis of existing saved capture outputs."""

import json
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path

from . import runtime


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.text.append(data.strip())


def saved_text(snapshot_dir: Path, inputs: list[str]) -> tuple[str, str]:
    """Read the first nonempty saved text source, never logs or outside files."""
    from abx_plugins.plugins.base.search_text import read_indexable_text

    root = snapshot_dir.resolve()
    for pattern in inputs:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError("Analysis inputs must be relative snapshot paths.")
        for path in root.glob(pattern):
            if (
                not path.resolve().is_relative_to(root)
                or not path.is_file()
                or path.suffix.lower() not in {".txt", ".md", ".html", ".htm"}
                or path.name.endswith((".stdout.txt", ".stderr.txt"))
            ):
                continue
            text = read_indexable_text(path, 256_000)
            if path.suffix.lower() in {".html", ".htm"}:
                parser = _PageText()
                parser.feed(text)
                text = "\n".join(parser.text)
            text = text.strip()[:60_000]
            if text:
                return text, str(path.relative_to(root))
    return "", ""


def _configured_model(binary, settings, env, timeout):
    """Use OpenCode's resolved config and previous user selection, never auto-pick a provider."""
    completed = subprocess.run(
        [str(binary.loaded_abspath), "debug", "config", "--pure"],
        cwd=settings["workdir"],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        raise RuntimeError("OpenCode could not resolve its model configuration.")
    model = json.loads(completed.stdout).get("model")
    if model:
        return model
    # This is OpenCode's own persisted model selection, not ArchiveBox state.
    path = settings["state_home"] / "opencode" / "model.json"
    try:
        selected = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(selected, dict) or not isinstance(selected.get("recent"), list):
        return ""
    recent = selected["recent"]
    for entry in recent:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("providerID"), str)
            and isinstance(entry.get("modelID"), str)
            and entry["providerID"]
            and entry["modelID"]
        ):
            return f"{entry['providerID']}/{entry['modelID']}"
    return ""


def analyze(config, *, data_dir, title, prompt, model, timeout):
    """One foreground CLI process owns its request, using the existing auth store."""
    settings = runtime._settings(config, data_dir)
    binary, binary_env = runtime._resolve_binary(settings["binary"], config)
    env = runtime.process_environment(settings, binary_env)
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    content.update(
        instructions=[],
        permission={"*": "deny"},
        agent={
            "archivebox-analysis": {
                "mode": "primary",
                "description": "Analyze supplied captured text without tools.",
                "permission": {"*": "deny"},
                "tools": {"*": False},
            },
        },
    )
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(content)
    deadline = time.monotonic() + timeout
    model = model or _configured_model(binary, settings, env, timeout)
    if not model:
        return None
    command = [
        str(binary.loaded_abspath),
        "run",
        "--pure",
        "--format=json",
        "--agent=archivebox-analysis",
        f"--title={title}",
    ]
    provider, separator, model_id = model.partition("/")
    if not separator or not provider or not model_id:
        raise ValueError("Model must use provider/model format.")
    command.append(f"--model={model}")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            "OpenCode model configuration exceeded the analysis deadline.",
        )
    completed = subprocess.run(
        command,
        input=prompt,
        cwd=settings["workdir"],
        env=env,
        capture_output=True,
        text=True,
        timeout=remaining,
    )
    if completed.returncode:
        raise RuntimeError(
            f"OpenCode exited with status {completed.returncode}: {completed.stderr.strip()[:2000]}",
        )
    text = []
    session_id = ""
    finished = False
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError as error:
            raise RuntimeError(
                "OpenCode returned an invalid JSON event stream.",
            ) from error
        kind = event.get("type")
        if kind == "error":
            error = event.get("error", {})
            raise RuntimeError(
                f"OpenCode analysis failed: {error.get('name', 'provider error')}: {error.get('data', {}).get('message', '')[:1000]}",
            )
        if kind == "tool_use" or event.get("part", {}).get("type") == "tool":
            raise RuntimeError(
                "OpenCode attempted a tool call in a tool-denied analysis session.",
            )
        if kind == "text":
            text.append(event.get("part", {}).get("text", ""))
        if kind == "step_finish":
            finished = event.get("part", {}).get("reason") == "stop"
        if event.get("sessionID"):
            if session_id and session_id != event["sessionID"]:
                raise RuntimeError(
                    "OpenCode mixed multiple sessions in one analysis result.",
                )
            session_id = event["sessionID"]
    if not session_id or not finished or not text:
        raise RuntimeError("OpenCode stopped without a completed analysis result.")
    try:
        return json.loads("".join(text)), session_id, model
    except ValueError as error:
        raise RuntimeError("OpenCode returned invalid analysis JSON.") from error
