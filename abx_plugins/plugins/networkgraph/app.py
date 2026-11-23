"""Collection graph routes; capture and permission operations belong to the host."""

from collections import Counter
from functools import partial
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).parent


def get_urlpatterns(host):
    from django.urls import path

    return [
        path("admin/graph/", partial(view, host=host), name="networkgraph-index"),
        path(
            "admin/graph/data/",
            partial(view, host=host, data=True),
            name="networkgraph-data",
        ),
        path(
            "admin/graph/assets/<path:asset>",
            partial(view, host=host),
            name="networkgraph-asset",
        ),
    ]


def view(request, *, host, data=False, asset=None):
    from django.contrib.auth.views import redirect_to_login
    from django.core import signing
    from django.core.cache import cache
    from django.http import (
        Http404,
        HttpResponse,
        HttpResponseForbidden,
        HttpResponseNotAllowed,
        JsonResponse,
    )
    from django.template import engines
    from django.views.static import serve

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    user = host.authenticate(request)
    if not user or not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not user.is_active or not user.is_superuser:
        return HttpResponseForbidden()
    config = host.request_config(request)
    if not config.get("NETWORKGRAPH_ENABLED", True):
        raise Http404
    if asset is not None:
        if asset not in {"graph.mjs", "graph.css", "vendor/3d-force-graph.min.js"}:
            raise Http404
        return serve(request, asset, document_root=ROOT / "static")
    batch = max(1, min(32, int(config.get("NETWORKGRAPH_BATCH_SIZE", 16))))
    if not data:
        context = {
            **host.admin_context(request),
            "max_nodes": max(
                100,
                min(3000, int(config.get("NETWORKGRAPH_MAX_NODES", 1200))),
            ),
            "admin_url": host.route_urls(request)["admin"] + "/",
        }
        return HttpResponse(
            engines["django"]
            .from_string((ROOT / "templates/index.html").read_text())
            .render(context, request),
        )
    if len(request.META.get("QUERY_STRING", "")) > 4096:
        return JsonResponse({}, status=400)
    key = (
        "collection-graph:"
        + sha256(
            f"{config['DATA_DIR']}:{batch}:{request.GET.urlencode()}".encode(),
        ).hexdigest()
    )
    result = cache.get(key)
    if result is None:
        try:
            if domain := request.GET.get("domain"):
                if len(domain) > 253:
                    raise ValueError
                after = request.GET.get("after") or None
                result = neighbors(host, request, domain, after, batch)
            else:
                cursor = (
                    signing.loads(
                        request.GET["cursor"],
                        salt="domain-page",
                        max_age=3600,
                    )
                    if request.GET.get("cursor")
                    else None
                )
                result = host.domain_records(request, cursor=cursor, limit=batch)
                if result["cursor"] is not None:
                    result["cursor"] = signing.dumps(
                        result["cursor"],
                        salt="domain-page",
                        compress=True,
                    )
        except (ValueError, signing.BadSignature):
            return JsonResponse({}, status=400)
        cache.set(key, result, timeout=30)
    return JsonResponse(result, headers={"Cache-Control": "private, no-store"})


def neighbors(host, request, domain, after, batch):
    page = host.snapshot_records(request, domain=domain, after=after, limit=batch)
    edges = Counter()
    nodes = set()
    # Each request reads at most 32 x 64 KiB; no archive-wide file walk.
    # Each saved target is resolved through the host's existing URL index.
    for record in page["records"]:
        source = record["domain"]
        if not source:
            continue
        nodes.add(source)
        targets = set(record["children"])
        urls = []
        file = Path(record["output_dir"]) / "parse_dom_outlinks" / "urls.jsonl"
        try:
            with file.open("rb") as stream:
                content = stream.read(65536)
            for line in content.splitlines()[:256]:
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    urls.append(item["url"][:8192])
        except OSError:
            pass
        targets.update(host.resolve_snapshot_urls(request, urls).values())
        for target in targets:
            if target and target != source:
                edges[source, target] += 1
                nodes.add(target)
        if parent := record["parent"]:
            if parent != source:
                edges[parent, source] += 1
                nodes.add(parent)
    return {
        "nodes": [
            {"id": name, "scope": f"*.{name}" if ":" not in name else name}
            for name in sorted(nodes)
        ],
        "links": [
            {"source": source, "target": target, "value": count}
            for (source, target), count in edges.items()
        ],
        "cursor": page["cursor"],
        "sampled": len(page["records"]),
        "captures": [
            {
                key: record[key]
                for key in (
                    "id",
                    "url",
                    "domain",
                    "title",
                    "status",
                    "size",
                    "created",
                    "downloaded",
                )
            }
            for record in page["records"]
        ],
    }
