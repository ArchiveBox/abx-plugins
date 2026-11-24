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
from urllib.parse import urlsplit

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
    batch = max(1, min(32, int(config.get("NETWORKGRAPH_BATCH_SIZE", 8))))
    if not data:
        context = {
            **host.admin_context(request),
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


def saved_targets(record, *, default_paths):
    root = Path(record["output_dir"])
    targets = set()
    for relative in record.get("paths", default_paths):
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
                for line in stream:
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
                        targets.add(url)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            continue
        except OSError as error:
            if error.errno != errno.ELOOP:
                raise
    return targets


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
