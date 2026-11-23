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
        if asset not in {
            "graph.mjs",
            "graph.css",
            "rank.js",
            "vendor/3d-force-graph.min.js",
            "vendor/three.core.js",
            "vendor/graphology.umd.min.js",
            "vendor/graphology-library.min.js",
            "vendor/d3-binarytree.min.js",
            "vendor/d3-quadtree.min.js",
            "vendor/d3-octree.min.js",
            "vendor/d3-dispatch.min.js",
            "vendor/d3-timer.min.js",
            "vendor/d3-force-3d.min.js",
        }:
            raise Http404
        return serve(request, asset, document_root=ROOT / "static")
    batch = max(1, min(32, int(config.get("NETWORKGRAPH_BATCH_SIZE", 16))))
    if not data:
        context = {
            **host.admin_context(request),
            "max_nodes": max(
                100,
                min(20000, int(config.get("NETWORKGRAPH_MAX_NODES", 12000))),
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
            if request.GET.get("scan") == "links":
                result = outlink_page(host, request, request.GET.get("after") or None)
            elif request.GET.get("scan") == "1":
                result = collection_page(
                    host,
                    request,
                    request.GET.get("after") or None,
                )
            elif domain := request.GET.get("domain"):
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


def collection_page(host, request, after):
    page = host.collection_records(request, after=after, limit=512)
    nodes = {}
    edges = Counter()
    for record in page["records"]:
        domain = record["domain"]
        if not domain:
            continue
        node = nodes.setdefault(
            domain,
            {
                "id": domain,
                "scope": f"*.{domain}",
                "count": 0,
                "size": 0,
                "favicon": "",
            },
        )
        node["count"] += 1
        node["size"] += record["size"]
        if record["favicon"]:
            node["favicon"] = record["favicon"]
        parent = record["parent"]
        if parent and parent != domain:
            edges[parent, domain] += 1
    return {
        "nodes": list(nodes.values()),
        "links": [
            {"source": source, "target": target, "value": count}
            for (source, target), count in edges.items()
        ],
        "sampled": len(page["records"]),
        "cursor": page["cursor"],
    }


def outlink_page(host, request, after):
    page = host.outlink_records(request, after=after, limit=32)
    sources = []
    urls = set()
    for record in page["records"]:
        targets = set()
        root = Path(record["output_dir"])
        budget = 65536
        for relative in page["paths"]:
            if not budget:
                break
            try:
                with (root / relative).open("rb") as stream:
                    content = stream.read(budget)
                budget -= len(content)
                for line in content.splitlines()[:256]:
                    try:
                        item = json.loads(line)
                        url = item.get("url") if isinstance(item, dict) else None
                        if isinstance(url, str) and len(url) <= 8192:
                            targets.add(url)
                    except (ValueError, UnicodeError):
                        continue
            except OSError:
                continue
        sources.append((record["domain"], targets))
        urls.update(targets)
    resolved = {}
    candidates = sorted(urls)
    for start in range(0, len(candidates), 256):
        resolved.update(
            host.resolve_snapshot_urls(request, candidates[start : start + 256]),
        )
    edges = Counter()
    for source, targets in sources:
        for target in targets:
            domain = resolved.get(target)
            if source and domain and source != domain:
                edges[source, domain] += 1
    return {
        "links": [
            {"source": source, "target": target, "value": count}
            for (source, target), count in edges.items()
        ],
        "sampled": len(page["records"]),
        "cursor": page["cursor"],
    }
