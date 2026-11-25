"""Collection graph routes; capture and permission operations belong to the host."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import errno
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urldefrag, urlsplit
from uuid import UUID

ROOT = Path(__file__).parent
METADATA_DOMAINS = frozenset(
    {
        "cloudflareinsights.com",
        "google-analytics.com",
        "googletagmanager.com",
        "ogp.me",
        "schema.org",
        "w.org",
        "w3.org",
    },
)
ASSET_HOSTS = frozenset(
    {
        "ajax.googleapis.com",
        "cdn.jsdelivr.net",
        "cdnjs.cloudflare.com",
        "code.jquery.com",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "maxcdn.bootstrapcdn.com",
        "oss.maxcdn.com",
        "stackpath.bootstrapcdn.com",
        "unpkg.com",
    },
)
ASSET_SUFFIXES = (".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".eot")
BOILERPLATE_PATHS = {
    "gmpg.org": {"/xfn/1", "/xfn/11"},
    "getbootstrap.com": {""},
    "reddit.com": {"/submit"},
    "facebook.com": {"/sharer", "/sharer/sharer.php"},
    "linkedin.com": {"/sharing/share-offsite", "/shareArticle"},
    "x.com": {"/intent/tweet", "/intent/post", "/share"},
    "twitter.com": {"/intent/tweet", "/intent/post", "/share"},
    "disqus.com": {
        "",
        "/privacy-policy",
        "/data-sharing-settings",
        "/home",
        "/home/notifications",
    },
    "docs.disqus.com": {"/help/83", "/kb/terms-and-policies"},
    "help.disqus.com": {"/customer/portal/articles/466260-terms-of-service"},
}
CC_REFERENCE = re.compile(
    r"^/(?:licenses/[^/]+|publicdomain/(?:zero|mark))/\d+(?:\.\d+)+(?:/|$)",
)


def is_boilerplate_url(url):
    if not url:
        return False
    try:
        parts = urlsplit(url)
        hostname = (parts.hostname or "").rstrip(".").removeprefix("www.")
    except ValueError:
        return False
    path = parts.path.rstrip("/")
    if hostname in ASSET_HOSTS or path.lower().endswith(ASSET_SUFFIXES):
        return True
    if path in BOILERPLATE_PATHS.get(hostname, ()):
        return True
    if hostname == "creativecommons.org":
        return path == "/ns" or bool(CC_REFERENCE.match(path))
    if hostname == "i.creativecommons.org":
        return path.startswith("/l/")
    if hostname == "disqus.com" or hostname.endswith(".disqus.com"):
        return path == "/embed" or path.startswith("/embed/")
    return hostname == "disquscdn.com" or hostname.endswith(".disquscdn.com")


def target_domains(host, request, urls):
    group = getattr(host, "group_snapshot_urls", None) or host.resolve_snapshot_urls
    return group(request, urls)


def snapshot_detail_card(host, request, *, snapshot_id):
    from django.urls import reverse
    from django.utils.html import format_html
    from django.utils.translation import gettext as _

    if not host.request_config(request).get("NETWORKGRAPH_ENABLED", True):
        return None
    neighbors = host.snapshot_relationships(request, snapshot_id=snapshot_id, limit=2)
    if not neighbors or not neighbors["root"]:
        return None
    parent = neighbors["parent"]
    children = neighbors["children"]
    # The summary is entirely DB-backed. It never reads captured files or loads WebGL.
    left = (
        format_html(
            '<path d="M28 28h42"/><circle cx="25" cy="28" r="4"><title>{}</title></circle>',
            parent["title"] or parent["url"],
        )
        if parent
        else ""
    )
    right = (
        format_html(
            '<path d="M80 28 124 {}"/><circle cx="127" cy="{}" r="4"><title>{}</title></circle>',
            17 if len(children) > 1 else 28,
            17 if len(children) > 1 else 28,
            children[0]["title"] or children[0]["url"],
        )
        if children
        else ""
    )
    last = (
        format_html(
            '<path d="M80 28 124 39"/><circle cx="127" cy="39" r="4"><title>{}</title></circle>',
            children[1]["title"] or children[1]["url"],
        )
        if len(children) > 1
        else ""
    )
    return {
        "path": reverse("networkgraph-index") + f"?snapshot={snapshot_id}",
        "title": _("Graph"),
        "output_group": "metadata",
        "thumbnail": format_html(
            '<svg viewBox="0 0 152 56" width="100%" height="56" aria-hidden="true"'
            ' fill="none" stroke="currentColor" stroke-width="1.5">'
            '{}<circle cx="75" cy="28" r="5" fill="currentColor"/>{}{}</svg>',
            left,
            right,
            last,
        ),
    }


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
        StreamingHttpResponse,
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
        return serve(request, asset, document_root=str(ROOT / "static"))
    batch = max(1, min(32, int(config.get("NETWORKGRAPH_BATCH_SIZE", 8))))
    if len(request.META.get("QUERY_STRING", "")) > 4096:
        return JsonResponse({}, status=400)
    snapshot = request.GET.get("snapshot")
    if snapshot is not None:
        try:
            snapshot = str(UUID(snapshot))
        except ValueError:
            return JsonResponse({}, status=400)
        if not hasattr(host, "snapshot_neighborhood_records"):
            raise Http404
        if data:
            from asgiref.sync import sync_to_async
            from django.core.handlers.asgi import ASGIRequest

            records = host.snapshot_neighborhood_records(request, ids=(snapshot,))
            if not records or not graph_url(records[0]["url"]):
                raise Http404
            frames = (
                json.dumps(frame, separators=(",", ":")) + "\n"
                for frame in snapshot_neighborhood(host, request, records[0])
            )

            async def async_frames():
                read = sync_to_async(next)
                while (frame := await read(frames, None)) is not None:
                    yield frame

            return StreamingHttpResponse(
                async_frames() if isinstance(request, ASGIRequest) else frames,
                content_type="application/x-ndjson",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Accel-Buffering": "no",
                },
            )
    if not data:
        context = {
            **host.admin_context(request),
            "admin_url": host.route_urls(request)["admin"] + "/",
            "graph_snapshot": snapshot,
        }
        return HttpResponse(
            engines["django"]
            .from_string((ROOT / "templates/index.html").read_text())
            .render(context, request),
        )
    key = (
        "collection-graph:v6:"
        + sha256(
            f"{config['DATA_DIR']}:{batch}:{request.GET.urlencode()}".encode(),
        ).hexdigest()
    )
    result = cache.get(key)
    if result is None:
        try:
            if request.GET.get("scan") == "links":
                result = outlink_page(
                    host,
                    request,
                    request.GET.get("after") or None,
                    batch,
                )
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
        # These domains predominantly appear as document namespace references.
        if "nodes" in result:
            result["nodes"] = [
                node for node in result["nodes"] if node["id"] not in METADATA_DOMAINS
            ]
        if "links" in result:
            result["links"] = [
                edge
                for edge in result["links"]
                if not METADATA_DOMAINS.intersection((edge["source"], edge["target"]))
            ]
        cache.set(key, result, timeout=30)
    return JsonResponse(result, headers={"Cache-Control": "private, no-store"})


def iter_saved_targets(record, *, default_paths, max_line=None):
    if saved_files := record.get("saved_url_outputs"):
        directory, paths = saved_files()
    else:
        directory, paths = record["output_dir"], record.get("paths", default_paths)
    root = Path(directory)
    for relative in paths:
        try:
            with open(
                root / relative,
                "rb",
                opener=lambda path, flags: os.open(
                    path,
                    flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                ),
            ) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                while line := stream.readline(max_line or -1):
                    if max_line and len(line) == max_line and not line.endswith(b"\n"):
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(max_line)
                        continue
                    try:
                        item = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    url = item.get("url") if isinstance(item, dict) else None
                    if (
                        isinstance(url, str)
                        and len(url) <= 8192
                        and not is_boilerplate_url(url)
                    ):
                        yield url
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            continue
        except OSError as error:
            if error.errno != errno.ELOOP:
                raise


def saved_targets(record, *, default_paths):
    return set(iter_saved_targets(record, default_paths=default_paths))


def graph_url(url):
    if not isinstance(url, str) or len(url) > 8192:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname:
            return urldefrag(url).url
    except ValueError:
        pass
    return None


def snapshot_neighborhood(host, request, root_capture):
    root = graph_url(root_capture["url"])
    captures = {root: root_capture}
    positions = {root: 0}
    edges = set()
    chunk_size = 16
    incoming = [root]
    outgoing = [root]
    seen = {-1: {root}, 1: {root}}

    def frame(urls, links):
        return {
            "nodes": [
                {"id": url, "column": positions[url], "count": int(url in captures)}
                for url in urls
            ],
            "links": [
                {"source": source, "target": target, "value": 1}
                for source, target in links
            ],
            "captures": [
                {
                    **{
                        key: captures[url][key]
                        for key in (
                            "id",
                            "url",
                            "title",
                            "status",
                            "size",
                            "created",
                            "downloaded",
                        )
                    },
                    "domain": url,
                }
                for url in urls
                if url in captures
            ],
        }

    def discover(frontier, direction, depth, next_frontier):
        for source in frontier:
            if not (record := captures.get(source)):
                continue
            if direction == -1:
                # Crawl parents are indexed. Saved outlinks have no reverse index.
                parents = record.get("parents")
                if parents is None:
                    parents = [
                        {
                            "id": record.get("parent_id"),
                            "url": record.get("parent_url"),
                        },
                    ]
                links = ((item["url"], item["id"]) for item in parents)
            else:
                links = (
                    (url, None)
                    for url in iter_saved_targets(
                        record,
                        default_paths=(),
                        max_line=64 * 1024,
                    )
                )
            for url, capture_id in links:
                target = graph_url(url)
                if not target or target == source:
                    continue
                if target not in seen[direction]:
                    seen[direction].add(target)
                    next_frontier.append(target)
                    positions.setdefault(target, direction * depth)
                edge = (target, source) if direction == -1 else (source, target)
                if edge not in edges:
                    edges.add(edge)
                    yield target, capture_id, edge

    def resolve(chunk):
        ids = tuple(
            dict.fromkeys(
                capture_id
                for target, capture_id, _ in chunk
                if capture_id and target not in captures
            ),
        )
        urls = tuple(
            dict.fromkeys(
                target
                for target, capture_id, _ in chunk
                if not capture_id and target not in captures
            ),
        )
        if ids or urls:
            for record in host.snapshot_neighborhood_records(
                request,
                ids=ids,
                urls=urls,
            ):
                if url := graph_url(record["url"]):
                    captures.setdefault(url, record)
        return frame(
            dict.fromkeys(target for target, _, _ in chunk),
            [edge for _, _, edge in chunk],
        )

    yield frame((root,), ())
    for hop in (1, 2):
        next_incoming, next_outgoing = [], []
        for frontier, direction, next_frontier in (
            (incoming, -1, next_incoming),
            (outgoing, 1, next_outgoing),
        ):
            chunk = []
            for edge in discover(frontier, direction, hop, next_frontier):
                chunk.append(edge)
                if len(chunk) == chunk_size:
                    yield resolve(chunk)
                    chunk.clear()
            if chunk:
                yield resolve(chunk)
        incoming, outgoing = next_incoming, next_outgoing


def saved_domain_targets(host, request, records, default_paths):
    with ThreadPoolExecutor(max_workers=4) as executor:
        url_sets = list(
            executor.map(partial(saved_targets, default_paths=default_paths), records),
        )
    candidates = sorted(set().union(*url_sets))
    domains = {}
    for start in range(0, len(candidates), 256):
        domains.update(
            target_domains(host, request, candidates[start : start + 256]),
        )
    return [{domains[url] for url in urls if url in domains} for urls in url_sets]


def neighbors(host, request, domain, after, batch):
    page = host.snapshot_records(request, domain=domain, after=after, limit=batch)
    edges = Counter()
    nodes = set()
    captures = [
        record
        for record in page["records"]
        if record["domain"] and not is_boilerplate_url(record["url"])
    ]
    saved = saved_domain_targets(
        host,
        request,
        captures,
        ("parse_dom_outlinks/urls.jsonl",),
    )
    for record, targets in zip(captures, saved, strict=True):
        source = record["domain"]
        nodes.add(source)
        targets.update(
            {
                target
                for target, url in record["child_links"]
                if not is_boilerplate_url(url)
            }
            if "child_links" in record
            else set(record["children"]),
        )
        for target in targets:
            if target and target != source:
                edges[source, target] += 1
                nodes.add(target)
        parent = record["parent"]
        if (
            parent
            and parent != source
            and not is_boilerplate_url(record.get("parent_url"))
        ):
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
            for record in captures
        ],
    }


def collection_page(host, request, after):
    page = host.collection_records(request, after=after, limit=512)
    nodes = {}
    edges = Counter()
    for record in page["records"]:
        domain = record["domain"]
        if not domain or is_boilerplate_url(record.get("url")):
            continue
        node = nodes.setdefault(
            domain,
            {
                "id": domain,
                "scope": record.get("scope", f"*.{domain}"),
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
        if (
            parent
            and parent != domain
            and not is_boilerplate_url(record.get("parent_url"))
        ):
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


def outlink_page(host, request, after, batch):
    page = host.outlink_records(request, after=after, limit=batch)
    records = [
        record
        for record in page["records"]
        if record["domain"] and not is_boilerplate_url(record.get("url"))
    ]
    saved = saved_domain_targets(host, request, records, page["paths"])
    edges = Counter()
    for record, targets in zip(records, saved, strict=True):
        source = record["domain"]
        for domain in targets:
            if domain and source != domain:
                edges[source, domain] += 1
    return {
        "links": [
            {"source": source, "target": target, "value": count}
            for (source, target), count in edges.items()
        ],
        "sampled": len(page["records"]),
        "cursor": page["cursor"],
    }
