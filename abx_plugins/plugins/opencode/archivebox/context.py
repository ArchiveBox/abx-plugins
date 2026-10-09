"""Read a bounded, current server overview for the collection's OpenCode agent."""

import argparse
import json
from datetime import datetime, timezone


def server_context(*, crawl_id=None, limit=20):
    from django.db.models import Count

    from archivebox.config import CONSTANTS
    from archivebox.config.common import get_config
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl
    from archivebox.machine.models import Machine, Process
    from archivebox.personas.models import Persona
    from archivebox.progressmonitor.metrics import system_metrics_path
    from .browser import browser_inventory, selection_path

    config = get_config(resolve_plugins=False)
    crawls = Crawl.objects.select_related("created_by", "persona").defer(
        "config",
        "notes",
    )
    if crawl_id:
        selected_crawls = [crawls.get(pk=crawl_id)]
    else:
        # Separate limits ensure a large queue cannot hide running or paused work.
        selected_crawls = [
            crawl
            for status in ("started", "queued", "paused", "backoff")
            for crawl in crawls.filter(status=status).order_by("-modified_at")[:limit]
        ]
    queue = {}
    for row in (
        Snapshot.objects.filter(crawl_id__in=[c.pk for c in selected_crawls])
        .values("crawl_id", "status")
        .annotate(count=Count("id"))
    ):
        queue.setdefault(str(row["crawl_id"]), {})[row["status"]] = row["count"]
    try:
        performance = json.loads(system_metrics_path().read_text())
    except (OSError, ValueError):
        performance = None
    try:
        selected_browser = json.loads(
            selection_path(config.model_dump(mode="json")).read_text(),
        )
    except (OSError, ValueError):
        selected_browser = None
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "limit_per_status": limit,
        "paths": {
            "collection": str(CONSTANTS.DATA_DIR),
            "archive": str(CONSTANTS.ARCHIVE_DIR),
            "config": str(CONSTANTS.CONFIG_FILE),
            "logs": str(CONSTANTS.LOGS_DIR),
            "personas": str(CONSTANTS.PERSONAS_DIR),
            "system_metrics": str(system_metrics_path()),
        },
        "crawls": [
            {
                "id": str(c.pk),
                "status": c.status,
                "label": c.label,
                "persona": c.persona.name if c.persona else None,
                "output_dir": str(c.output_dir),
                "snapshots": queue.get(str(c.pk), {}),
            }
            for c in selected_crawls
        ],
        "running_processes": list(
            Process.objects.filter(
                machine=Machine.current_readonly(),
                status=Process.StatusChoices.RUNNING,
            )
            .order_by("-started_at")
            .values("id", "pid", "process_type", "worker_type", "pwd", "started_at")[
                :limit
            ],
        ),
        "personas": [
            {
                "name": p.name,
                "path": str(p.path),
                "profile": str(p.CHROME_USER_DATA_DIR),
            }
            for p in Persona.objects.only("name").order_by("name")[:limit]
        ],
        "browsers": browser_inventory(config.model_dump(mode="json")),
        "selected_browser": selected_browser,
        "performance": performance,
        "only_new": config.ONLY_NEW,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crawl", help="Inspect one crawl, including finished crawls")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum records per status/category (1–100)",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    from archivebox.config.django import setup_django

    setup_django(check_db=True)
    print(
        json.dumps(
            server_context(crawl_id=args.crawl, limit=args.limit),
            default=str,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
