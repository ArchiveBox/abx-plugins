"""Live process and network telemetry sampled only by authenticated requests."""

from functools import lru_cache, partial
from hashlib import sha256
from ipaddress import IPv6Address, ip_address
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import stat
import time
from urllib.parse import urlencode, urlsplit

from .network import sample_sockets

ROOT = Path(__file__).parent
RANGES = {
    "1h": ("Last 1 hour", 3600),
    "24h": ("Last 24 hours", 86400),
    "1w": ("Last 1 week", 7 * 86400),
    "1mo": ("Last 1 month", 30 * 86400),
    "3mo": ("Last 3 months", 90 * 86400),
    "1y": ("Last 1 year", 365 * 86400),
    "all": ("All time", 0),
}


def get_urlpatterns(host):
    from django.urls import path

    return [
        path("admin/bandwidth/", partial(view, host=host), name="bandwidth-index"),
        path(
            "admin/bandwidth/data/",
            partial(view, host=host, data=True),
            name="bandwidth-data",
        ),
        path(
            "admin/bandwidth/assets/<str:asset>",
            partial(view, host=host),
            name="bandwidth-asset",
        ),
    ]


def view(request, *, host, data=False, asset=None):
    from django.contrib.auth.views import redirect_to_login
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
    from django.urls import reverse

    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    user = host.authenticate(request)
    if not user or not user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    if not user.is_active or not user.is_superuser:
        return HttpResponseForbidden()
    config = host.request_config(request)
    if not config.get("BANDWIDTH_ENABLED", True):
        raise Http404
    if asset is not None:
        if asset not in {"bandwidth.mjs", "bandwidth.css"}:
            raise Http404
        return serve(request, asset, document_root=ROOT / "static")
    interval = max(1000, min(10000, int(config.get("BANDWIDTH_POLL_MS", 2000))))
    history_records = getattr(host, "process_history_records", None)
    artifact_history = getattr(host, "process_artifact_history_records", None)
    group_history = getattr(host, "bandwidth_groups", None)
    period = request.GET.get("range", "24h")
    if period not in RANGES:
        return JsonResponse({}, status=400)
    range_label, range_seconds = RANGES[period]
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=range_seconds)
        if range_seconds
        else None
    )
    if not data:
        context = {
            **host.admin_context(request),
            "poll_ms": interval,
            "history_enabled": history_records is not None,
            "hit_history_enabled": artifact_history is not None,
            "group_history_enabled": group_history is not None,
            "ranges": RANGES,
            "selected_range": period,
            "range_label": range_label,
            "range_seconds": range_seconds,
            "monitor_key": sha256(str(config["DATA_DIR"]).encode()).hexdigest()[:16],
            "admin_url": host.route_urls(request)["admin"] + "/",
        }
        return HttpResponse(
            engines["django"]
            .from_string((ROOT / "templates/index.html").read_text())
            .render(context, request),
        )
    after = request.GET.get("after")
    before = request.GET.get("history")
    source = request.GET.get("source", "downloads")
    if source not in {"downloads", "hits", "groups"} or (
        source != "downloads" and before is None
    ):
        return JsonResponse({}, status=400)
    if source == "groups" and period != "all":
        return JsonResponse({}, status=400)
    ids = tuple(request.GET.get("ids", "").split(",")) if request.GET.get("ids") else ()
    if len(ids) > 128 or any(len(value) > 36 for value in ids):
        return JsonResponse({}, status=400)
    if (after and len(after) > 36) or (before and len(before) > 36):
        return JsonResponse({}, status=400)
    limit = max(16, min(128, int(config.get("BANDWIDTH_MAX_PROCESSES", 128))))
    cache_scope = (
        f"{config['DATA_DIR']}:{limit}:{after}:{before}:{period}:{source}:{ids}"
    )
    key = "live-network:" + sha256(cache_scope.encode()).hexdigest()
    result = cache.get(key)
    if result is None:
        try:
            if source == "groups":
                if group_history is None:
                    raise Http404
                page = group_history(request, before=before)
            elif before is None:
                page = host.process_records(request, after=after, ids=ids, limit=limit)
            elif source == "hits" and artifact_history is not None:
                page = artifact_history(
                    request,
                    plugin="dns",
                    before=before,
                    since=since,
                )
            elif source == "hits":
                raise Http404
            elif history_records is not None:
                page = history_records(request, before=before, since=since, limit=limit)
            else:
                raise Http404
        except ValueError:
            return JsonResponse({}, status=400)
        domains_url = reverse("domains:index")
        if source == "groups":
            result = page
        elif source == "hits":
            result = {
                "cursor": page["cursor"],
                "observations": browser_hits(host, request, page, domains_url, since),
            }
        elif before is None:
            result = sample(page)
        else:
            result = {
                "time": time.time(),
                "flows": [],
                "processes": [process_record(row) for row in page["records"]],
                "cursor": page["cursor"],
            }
        if source == "downloads":
            result["downloads"] = completed_downloads(
                host,
                request,
                result["processes"],
                since=since.timestamp() if since else None,
            )
            identify_sites(host, request, page, result, domains_url)
        cache.set(key, result, timeout=interval / 1000)
    return JsonResponse(result, headers={"Cache-Control": "private, no-store"})


def process_record(row):
    hook = Path(row.get("cmd__0") or "")
    plugin = row["archiveresult__plugin"]
    if not plugin and row["process_type"] == "hook" and hook.name.startswith("on_"):
        plugin = hook.parent.name
    return {
        "id": str(row["id"]),
        "status": row["status"],
        "name": plugin,
        "snapshot": str(row["archiveresult__snapshot_id"] or ""),
        "started": row["started_at"].timestamp() if row["started_at"] else None,
        "ended": row["ended_at"].timestamp() if row["ended_at"] else None,
        "crawl": str(row["archiveresult__snapshot__crawl_id"] or ""),
        "crawl_label": row["archiveresult__snapshot__crawl__label"] or "",
        "hook": row["archiveresult__hook_name"] or hook.name,
        "disk": row["archiveresult__output_size"] or 0,
        "interface": row["iface__iface"] or "",
        "local_ip": row["iface__ip_local"] or "",
        "wan_ip": row["iface__ip_public"] or "",
    }


def sample(page):
    import psutil

    now = time.time()
    interfaces = []
    related_interfaces = set()
    addresses = {
        address.address.split("%")[0]: name
        for name, values in psutil.net_if_addrs().items()
        for address in values
    }
    try:
        interfaces = [
            {
                "id": name,
                "rx": value.bytes_recv,
                "tx": value.bytes_sent,
                "packets": value.packets_recv + value.packets_sent,
                "errors": value.errin + value.errout,
                "dropped": value.dropin + value.dropout,
            }
            for name, value in psutil.net_io_counters(pernic=True).items()
        ][:64]
    except (OSError, psutil.Error):
        pass
    processes = []
    roots = {}
    for row in page["records"]:
        record = process_record(row)
        if not record["name"]:
            continue
        if row["pid"] and row["status"] == "running":
            try:
                process = psutil.Process(row["pid"])
                with process.oneshot():
                    # Match process identity before sampling its counters or descendants.
                    started = record["started"]
                    if started is not None and abs(process.create_time() - started) < 2:
                        roots[process.pid] = record
                        cpu = process.cpu_times()
                        record.update(
                            cpu=cpu.user + cpu.system,
                            rss=process.memory_info().rss,
                        )
                    else:
                        record["status"] = "exited"
            except psutil.NoSuchProcess:
                record["status"] = "exited"
                record["ended"] = now
            except (psutil.Error, OSError):
                pass
        processes.append(record)

    flows = []
    owners = process_tree(roots)
    for pid, key, local, remote, interface, received, sent, port, rtt in sample_sockets(
        owners.keys(),
    ):
        try:
            peer = ip_address(remote)
        except ValueError:
            continue
        if isinstance(peer, IPv6Address) and (mapped := peer.ipv4_mapped) is not None:
            peer = mapped
        if peer.is_loopback or peer.is_unspecified:
            continue
        # A tracked hook owns its descendants until another tracked hook takes over.
        owner = owners[pid]
        interface = interface or addresses.get(local, "")
        if interface:
            related_interfaces.add(interface)
        flows.append(
            {
                "id": f"{owner['id']}:{pid}:{key}",
                "process": owner["id"],
                "local": local,
                "peer": str(peer),
                "interface": interface,
                "rx": received,
                "tx": sent,
                "port": port,
                "rtt": rtt,
            },
        )
    return {
        "time": now,
        "interfaces": [
            interface
            for interface in interfaces
            if interface["id"] in related_interfaces
        ],
        "processes": processes,
        "flows": flows,
        "cursor": page["cursor"],
    }


def process_tree(roots):
    if not roots:
        return {}
    import psutil

    children = {}
    for process in psutil.process_iter(["ppid"]):
        if (parent := process.info["ppid"]) is not None:
            children.setdefault(parent, []).append(process.pid)
    owners = dict(roots)
    queue = list(roots)
    for pid in queue:
        for child in children.get(pid, ()):
            if child in owners:
                continue
            owners[child] = owners[pid]
            queue.append(child)
            if len(owners) >= 512:
                return owners
    return owners


def completed_downloads(host, request, processes, *, since=None):
    from django.core.cache import cache

    get_reporters = getattr(host, "download_reporters", None)
    log_tails = getattr(host, "process_log_tails", None)
    if get_reporters is None or log_tails is None:
        return []
    reporters = get_reporters(request, [process["name"] for process in processes])
    pending = {}
    reports = []
    for process in processes:
        if process["name"] not in reporters:
            continue
        process["reports_downloads"] = True
        ended = process["ended"]
        if (
            process["status"] != "exited"
            or ended is None
            or (since is not None and ended <= since)
        ):
            continue
        cache_key = f"bandwidth-downloads:{process['id']}"
        entries = cache.get(cache_key)
        if entries is None:
            pending[process["id"]] = process
        elif entries:
            reports.append(
                {"process": process["id"], "time": ended, "entries": entries},
            )

    if pending:
        for pid, log in log_tails(request, list(pending)).items():
            process = pending[pid]
            counts = defaultdict(lambda: [0, 0])
            for hostname, size in reporters[process["name"]](log):
                total = counts[hostname]
                total[0] += size
                total[1] += 1
            entries = [
                {"hostname": name, "bytes": size, "hits": hits}
                for name, (size, hits) in counts.items()
            ]
            cache.set(f"bandwidth-downloads:{pid}", entries, 86400)
            if entries:
                reports.append(
                    {"process": pid, "time": process["ended"], "entries": entries},
                )
    return reports


@lru_cache(maxsize=128)
def read_dns_observations(path, mtime, size):
    # Only the most recent portion of the capture's existing DNS artifact.
    with path.open("rb") as stream:
        if size > 65536:
            stream.seek(size - 65536)
            stream.readline(65536)
        data = stream.read(65536)
    records = []
    for line in data.splitlines()[-256:]:
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("source") not in {
                "cdp",
                "node-dns",
            }:
                continue
            host = record["hostname"].lower().rstrip(".")
            if not host or len(host) > 253:
                continue
            ip = ip_address(record["ip"].split("%")[0])
            if isinstance(ip, IPv6Address) and (mapped := ip.ipv4_mapped) is not None:
                ip = mapped
            ts = datetime.fromisoformat(record["ts"]).timestamp()
            port = record["port"]
            if port is None or isinstance(port, int):
                records.append((str(ip), port or 0, host, ts))
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
    return records


def capture_hosts(directory):
    path = Path(directory) / "dns" / "dns.jsonl"
    hosts = {}
    try:
        with open(
            path,
            "rb",
            opener=lambda path, flags: os.open(
                path,
                flags | os.O_NOFOLLOW | os.O_NONBLOCK,
            ),
        ) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return hosts
            for line in stream:
                try:
                    row = json.loads(line)
                    if row["source"] != "cdp" or not row.get("ip"):
                        continue
                    hostname = row["hostname"].lower().rstrip(".")
                    timestamp = datetime.fromisoformat(row["ts"]).timestamp()
                    if hostname and len(hostname) <= 253:
                        hosts[hostname] = max(timestamp, hosts.get(hostname, timestamp))
                except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
                    continue
    except OSError:
        pass
    return hosts


def browser_hits(host, request, page, domains_url, since):
    directories = page["output_dirs"]
    if not directories:
        return []
    with ThreadPoolExecutor(max_workers=4) as pool:
        captures = list(pool.map(capture_hosts, directories.values()))
    earliest = since.timestamp() if since else float("-inf")
    names = sorted(
        {name for hosts in captures for name, ts in hosts.items() if ts > earliest},
    )
    grouped = {}
    for start in range(0, len(names), 512):
        grouped.update(host.group_domains(request, names[start : start + 512]))
    observations = []
    for snapshot, hosts in zip(directories, captures, strict=True):
        sites = {}
        for name, timestamp in hosts.items():
            if timestamp <= earliest:
                continue
            site = grouped.get(name, name)
            if site not in sites or timestamp > sites[site]["time"]:
                sites[site] = {
                    "snapshot": snapshot,
                    "hostname": name,
                    "site": site,
                    "site_url": domains_url + "?" + urlencode({"q": site}),
                    "time": timestamp,
                }
        observations.extend(sites.values())
    return observations


def identify_sites(host, request, page, result, domains_url):
    if not result["flows"] and not result["downloads"]:
        return
    recent = {
        row["snapshot"] for row in result["processes"] if row["status"] == "running"
    }
    addresses = defaultdict(set)
    peers = {flow["peer"] for flow in result["flows"]}
    for snapshot, directory in page["output_dirs"].items() if peers else ():
        if snapshot not in recent:
            continue
        path = Path(directory) / "dns" / "dns.jsonl"
        try:
            stat = path.stat()
            records = read_dns_observations(path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        for ip, port, hostname, timestamp in records:
            if ip in peers and result["time"] - timestamp < 3600:
                addresses[ip, port].add(hostname)

    # A literal-IP URL needs no DNS evidence. Never assign a socket to an
    # arbitrary snapshot URL merely because its process belongs to that capture.
    urls = {}
    captured = {}
    for row in page["records"]:
        if url := row["archiveresult__snapshot__url"]:
            try:
                parsed = urlsplit(url)
                pid = str(row["id"])
                captured[pid] = (parsed.hostname, row["archiveresult__snapshot_id"])
                name = ip_address(parsed.hostname or "")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                urls[str(row["id"])] = (str(name), port, parsed.netloc)
            except ValueError:
                pass
    downloads = [entry for report in result["downloads"] for entry in report["entries"]]
    names = sorted(
        {name for values in addresses.values() for name in values}
        | {entry["hostname"] for entry in downloads},
    )
    grouped = {}
    for start in range(0, len(names), 512):
        grouped.update(host.group_domains(request, names[start : start + 512]))
    for flow in result["flows"]:
        key = (flow["peer"], flow["port"])
        matches = addresses[key] | addresses[flow["peer"], 0]
        domains = {grouped.get(name, name) for name in matches}
        literal = urls.get(flow["process"])
        if literal and literal[:2] == key:
            domains.add(literal[2])
        flow["site"] = next(iter(domains)) if len(domains) == 1 else ""
        if flow["site"]:
            flow["site_url"] = domains_url + "?" + urlencode({"q": flow["site"]})
    for entry in downloads:
        entry["site"] = grouped.get(entry["hostname"], entry["hostname"])
        entry["site_url"] = domains_url + "?" + urlencode({"q": entry["site"]})
    site_names = list(
        dict.fromkeys(
            entry["site"] for entry in [*downloads, *result["flows"]] if entry["site"]
        ),
    )[:30]
    get_icons = getattr(host, "domain_favicons", None)
    if site_names and get_icons is not None:
        preferred = {}
        for report in result["downloads"]:
            hostname, snapshot = captured.get(report["process"], (None, None))
            if snapshot:
                for entry in report["entries"]:
                    if entry["hostname"] == hostname:
                        preferred[entry["site"]] = str(snapshot)
        result["favicons"] = (
            get_icons(request, site_names, preferred=preferred)
            if preferred
            else get_icons(request, site_names)
        )
