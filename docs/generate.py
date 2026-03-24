#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup


SITE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SITE_DIR.parent
PLUGINS_DIR = REPO_ROOT / "abx_plugins" / "plugins"
TEMPLATE_DIR = SITE_DIR
DEFAULT_OUTPUT_DIR = SITE_DIR
EXCLUDED_PLUGIN_DIRS = {"__pycache__"}
GITHUB_REPO = "https://github.com/ArchiveBox/abx-plugins"
DEFAULT_GITHUB_REF = os.environ.get("ABX_MARKETPLACE_GITHUB_REF", "main")
LANGUAGE_NAMES = {
    "js": "JavaScript",
    "py": "Python",
    "sh": "Shell",
}
HOOK_PHASES = ("Crawl", "Snapshot", "Binary")


def github_tree_url(relative_path: str) -> str:
    return f"{GITHUB_REPO}/tree/{DEFAULT_GITHUB_REF}/{relative_path}"


def github_blob_url(relative_path: str) -> str:
    return f"{GITHUB_REPO}/blob/{DEFAULT_GITHUB_REF}/{relative_path}"


def fallback_icon(plugin_name: str) -> Markup:
    letter = plugin_name[:1].upper() or "?"
    return Markup(
        f"""
        <span class="abx-output-icon abx-output-icon--fallback" title="{plugin_name}">
          <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <rect x="3" y="3" width="18" height="18" rx="6" fill="currentColor" opacity="0.18"></rect>
            <text x="12" y="16" text-anchor="middle" font-size="11" font-family="IBM Plex Mono, monospace" fill="currentColor">{letter}</text>
          </svg>
        </span>
        """,
    )


def load_icon(plugin_dir: Path) -> Markup:
    icon_path = plugin_dir / "templates" / "icon.html"
    if not icon_path.exists():
        return fallback_icon(plugin_dir.name)
    return Markup(icon_path.read_text(encoding="utf-8").strip())


def load_config(plugin_dir: Path) -> dict[str, Any]:
    config_path = plugin_dir / "config.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


def as_required_binary_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        binproviders = item.get("binproviders")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(binproviders, str) or not binproviders:
            continue
        normalized = dict(item)
        overrides = normalized.get("overrides")
        overrides_summary = ""
        if isinstance(overrides, dict) and overrides:
            overrides_summary = json.dumps(
                overrides, ensure_ascii=False, sort_keys=True
            )
        min_version = normalized.get("min_version")
        summary_parts = [f"providers={binproviders}"]
        if min_version:
            summary_parts.append(f"min={min_version}")
        if overrides_summary:
            summary_parts.append(f"overrides={overrides_summary}")
        normalized["overrides_summary"] = overrides_summary
        normalized["summary"] = " | ".join(summary_parts)
        normalized["title"] = "\n".join(
            part
            for part in (
                str(name),
                f"providers: {binproviders}",
                f"min_version: {min_version}" if min_version else None,
                f"overrides: {overrides_summary}" if overrides_summary else None,
            )
            if part
        )
        items.append(normalized)
    return items


def format_json_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return json.dumps(value, ensure_ascii=False)


def normalize_type(schema_type: Any) -> str:
    if isinstance(schema_type, list):
        return " | ".join(str(item) for item in schema_type)
    if schema_type:
        return str(schema_type)
    return "unknown"


def parse_hook_filename(filename: str) -> dict[str, Any]:
    hook_prefix, _, payload = filename.partition("__")
    phase = hook_prefix.removeprefix("on_") or "Hook"
    pieces = payload.split(".")
    extension = pieces[-1] if len(pieces) > 1 else ""
    stem_pieces = pieces[:-1] if extension else pieces
    is_background = "bg" in stem_pieces
    base_parts = [
        part for part in stem_pieces if part not in {"daemon", "finite", "bg"}
    ]
    base_name = ".".join(base_parts)
    order_text, _, slug = base_name.partition("_")
    order = int(order_text) if order_text.isdigit() else None
    slug = slug if order is not None else base_name
    label = slug.replace("_", " ") if slug else phase.lower()
    return {
        "filename": filename,
        "phase": phase,
        "label": label,
        "order": order,
        "is_background": is_background,
        "language_code": extension or None,
        "language": LANGUAGE_NAMES.get(extension, extension or "unknown"),
    }


def collect_hooks(plugin_dir: Path) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    for path in sorted(plugin_dir.iterdir()):
        if not path.is_file():
            continue
        if not path.name.startswith(
            (
                "on_CrawlSetup__",
                "on_Snapshot__",
                "on_BinaryRequest__",
            ),
        ):
            continue
        hook = parse_hook_filename(path.name)
        hook["source_url"] = github_blob_url(path.relative_to(REPO_ROOT).as_posix())
        hooks.append(hook)
    return hooks


def collect_config_fields(
    plugin_dir: Path,
    config_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    properties = config_schema.get("properties", {})
    fields: list[dict[str, Any]] = []
    for key, details in properties.items():
        fields.append(
            {
                "key": key,
                "type": normalize_type(details.get("type")),
                "description": details.get("description", ""),
                "default": format_json_value(details.get("default"))
                if "default" in details
                else None,
                "aliases": list(details.get("x-aliases", [])),
                "fallback": details.get("x-fallback"),
                "minimum": details.get("minimum"),
                "pattern": details.get("pattern"),
                "enum": list(details.get("enum", [])),
            },
        )
    return fields


def build_commands(
    plugin_name: str,
    hooks: list[dict[str, Any]],
    config_fields: list[dict[str, Any]],
) -> dict[str, str]:
    has_snapshot = any(hook["phase"] == "Snapshot" for hook in hooks)
    has_setup = any(hook["phase"] in {"CrawlSetup", "BinaryRequest"} for hook in hooks)
    enable_key = next(
        (field["key"] for field in config_fields if field["key"].endswith("_ENABLED")),
        None,
    )
    env_prefix = f"{enable_key}=true " if enable_key else ""

    if has_snapshot:
        archivebox = f"{env_prefix}archivebox add 'https://example.com'"
        abx_dl = f"abx-dl dl --plugins={plugin_name} 'https://example.com'"
        note = "Runtime plugins execute while archiving a URL."
    elif has_setup:
        archivebox = f"{env_prefix}archivebox init --setup"
        abx_dl = f"abx-dl plugins --install {plugin_name}"
        note = "Setup plugins install dependencies or prepare shared runtime state."
    else:
        archivebox = "archivebox add 'https://example.com'"
        abx_dl = f"abx-dl plugins {plugin_name}"
        note = "Utility plugins are typically consumed indirectly, so the example shows the closest inspection workflow."

    return {
        "archivebox": archivebox,
        "abx_dl": abx_dl,
        "note": note,
    }


def plugin_phases(hooks: list[dict[str, Any]]) -> list[str]:
    phases = {hook["phase"] for hook in hooks}
    return [phase for phase in HOOK_PHASES if phase in phases]


def dominant_language(hooks: list[dict[str, Any]]) -> str | None:
    counts = {"py": 0, "js": 0}
    for hook in hooks:
        language_code = hook.get("language_code")
        if language_code in counts:
            counts[language_code] += 1

    if counts["py"] > counts["js"]:
        return "python"
    if counts["js"] > counts["py"]:
        return "javascript"
    return None


def highest_hook_order_for_phase(hooks: list[dict[str, Any]], phase: str) -> int | None:
    orders = [
        hook["order"]
        for hook in hooks
        if hook["phase"] == phase and hook["order"] is not None
    ]
    if not orders:
        return None
    return max(orders)


def plugin_sort_metadata(
    hooks: list[dict[str, Any]],
) -> tuple[int, int | float, str, int | None]:
    snapshot_order = highest_hook_order_for_phase(hooks, "Snapshot")
    crawl_order = highest_hook_order_for_phase(hooks, "Crawl")
    binary_order = highest_hook_order_for_phase(hooks, "Binary")

    if snapshot_order is not None:
        return (0, snapshot_order, "Snapshot", snapshot_order)
    if crawl_order is not None:
        return (1, crawl_order, "Crawl", crawl_order)
    if binary_order is not None:
        return (2, binary_order, "Binary", binary_order)
    return (3, float("inf"), "", None)


def template_badges(plugin_dir: Path) -> list[str]:
    templates_dir = plugin_dir / "templates"
    badges: list[str] = []
    if (templates_dir / "card.html").exists():
        badges.append("Embed")
    if (templates_dir / "full.html").exists():
        badges.append("Fullscreen")
    return badges


def build_plugin(plugin_dir: Path) -> dict[str, Any]:
    relative_dir = plugin_dir.relative_to(REPO_ROOT).as_posix()
    config_schema = load_config(plugin_dir)
    hooks = collect_hooks(plugin_dir)
    config_fields = collect_config_fields(plugin_dir, config_schema)
    commands = build_commands(plugin_dir.name, hooks, config_fields)
    phases = plugin_phases(hooks)
    primary_language = dominant_language(hooks)
    sort_group, sort_order, sort_phase, display_order = plugin_sort_metadata(hooks)
    template_labels = template_badges(plugin_dir)
    display_title = str(config_schema.get("title") or plugin_dir.name)
    description = str(config_schema.get("description") or "").strip()
    required_plugins = as_string_list(config_schema.get("required_plugins"))
    required_binaries = as_required_binary_list(config_schema.get("required_binaries"))
    required_binary_names = [item["name"] for item in required_binaries]
    required_binary_summaries = [
        f"{item['name']} ({item['summary']})" for item in required_binaries
    ]
    output_mimetypes = as_string_list(config_schema.get("output_mimetypes"))
    search_parts = [plugin_dir.name, *phases]
    search_parts.append(display_title)
    if description:
        search_parts.append(description)
    if primary_language:
        search_parts.append(primary_language)
    if sort_phase:
        search_parts.append(sort_phase)
    search_parts.extend(template_labels)
    search_parts.extend(required_plugins)
    search_parts.extend(required_binary_names)
    search_parts.extend(item["binproviders"] for item in required_binaries)
    search_parts.extend(
        str(item.get("min_version") or "") for item in required_binaries
    )
    search_parts.extend(
        item["overrides_summary"]
        for item in required_binaries
        if item.get("overrides_summary")
    )
    search_parts.extend(output_mimetypes)
    search_parts.extend(hook["filename"] for hook in hooks)
    for field in config_fields:
        search_parts.append(field["key"])
        if field["description"]:
            search_parts.append(field["description"])
    return {
        "name": plugin_dir.name,
        "display_title": display_title,
        "description": description,
        "phases": phases,
        "primary_language": primary_language,
        "icon_html": load_icon(plugin_dir),
        "source_url": github_tree_url(relative_dir),
        "hooks": hooks,
        "hook_count": len(hooks),
        "config_fields": config_fields,
        "config_count": len(config_fields),
        "has_config": bool(config_fields),
        "commands": commands,
        "template_labels": template_labels,
        "required_plugins": required_plugins,
        "required_binaries": required_binaries,
        "required_binary_names": required_binary_names,
        "required_binary_summaries": required_binary_summaries,
        "output_mimetypes": output_mimetypes,
        "display_order": display_order,
        "sort_group": sort_group,
        "sort_order": sort_order,
        "search_text": " ".join(search_parts).lower(),
    }


def collect_plugins() -> list[dict[str, Any]]:
    plugins = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not plugin_dir.is_dir() or plugin_dir.name in EXCLUDED_PLUGIN_DIRS:
            continue
        plugins.append(build_plugin(plugin_dir))
    plugins.sort(
        key=lambda plugin: (
            plugin["sort_group"],
            plugin["sort_order"],
            plugin["name"].lower(),
        ),
    )
    return plugins


def render_marketplace(output_dir: Path, template_name: str) -> Path:
    plugins = collect_plugins()
    environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template(template_name)
    html = template.render(
        site={
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "github_repo": GITHUB_REPO,
            "github_ref": DEFAULT_GITHUB_REF,
            "plugin_count": len(plugins),
            "hook_count": sum(plugin["hook_count"] for plugin in plugins),
            "config_count": sum(plugin["config_count"] for plugin in plugins),
            "plugins": plugins,
        },
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return index_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the abx-plugins marketplace site.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to write the generated GitHub Pages site into.",
    )
    parser.add_argument(
        "--template",
        default="index.html.j2",
        help="Template file to render from the docs/ directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = render_marketplace(Path(args.output_dir), args.template)
    print(f"Generated marketplace site at {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
