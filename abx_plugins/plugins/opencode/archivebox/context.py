"""Read a bounded server overview and the collection agent's browser state."""

import json

import rich_click as click


def server_context(host, *, crawl_id=None, limit=20):
    from .browser import browser_inventory, selection_path

    config = host.request_config()
    try:
        selected_browser = json.loads(selection_path(config).read_text())
    except (OSError, ValueError):
        selected_browser = None
    return {
        **host.collection_context(crawl_id=crawl_id, limit=limit),
        "browsers": browser_inventory(
            config,
            sources=host.browser_sources(),
            snapshot_targets=True,
        ),
        "selected_browser": selected_browser,
    }


def get_cli_command(host):
    @click.command("context")
    @click.option("--crawl", help="Inspect one crawl, including finished crawls")
    @click.option(
        "--limit",
        type=click.IntRange(1, 100),
        default=20,
        help="Maximum records per status/category",
    )
    def context_command(crawl, limit):
        """Read current collection and browser state."""
        click.echo(
            json.dumps(
                server_context(host, crawl_id=crawl, limit=limit),
                default=str,
                indent=2,
            ),
        )

    return context_command
