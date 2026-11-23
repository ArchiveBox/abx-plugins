"""Live process and network telemetry sampled only by authenticated requests."""

from functools import lru_cache, partial
from hashlib import sha256
from pathlib import Path
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = Path(__file__).parent


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
    if not data:
        context = {
            **host.admin_context(request),
            "poll_ms": interval,
            "admin_url": host.route_urls(request)["admin"] + "/",
        }
        return HttpResponse(
            engines["django"]
            .from_string((ROOT / "templates/index.html").read_text())
            .render(context, request),
        )
    after = request.GET.get("after")
    ids = tuple(request.GET.get("ids", "").split(",")) if request.GET.get("ids") else ()
    if len(ids) > 128 or any(len(value) > 36 for value in ids):
        return JsonResponse({}, status=400)
    if after and len(after) > 36:
        return JsonResponse({}, status=400)
    limit = max(16, min(128, int(config.get("BANDWIDTH_MAX_PROCESSES", 128))))
    key = (
        "live-network:"
        + sha256(f"{config['DATA_DIR']}:{limit}:{after}:{ids}".encode()).hexdigest()
    )
    result = cache.get(key)
    if result is None:
        try:
            page = host.process_records(request, after=after, ids=ids, limit=limit)
        except ValueError:
            return JsonResponse({}, status=400)
        result = sample(page)
        groups = host.group_domains(
            request,
            list(dict.fromkeys(event["host"] for event in result["events"])),
        )
        for event in result["events"]:
            event["domain"] = groups.get(event["host"], event["host"])
        cache.set(key, result, timeout=interval / 1000)
    return JsonResponse(result, headers={"Cache-Control": "private, no-store"})


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
    for row in page["records"]:
        try:
            domain = urlsplit(row["archiveresult__snapshot__url"] or "").hostname or ""
        except ValueError:
            domain = ""
        record = {
            "id": str(row["id"]),
            "pid": row["pid"],
            "parent": str(row["parent_id"] or ""),
            "status": row["status"],
            "name": row["archiveresult__plugin"]
            or row["worker_type"]
            or row["process_type"],
            "domain": domain,
            "snapshot": str(row["archiveresult__snapshot_id"] or ""),
            "started": row["started_at"].timestamp() if row["started_at"] else None,
            "ended": row["ended_at"].timestamp() if row["ended_at"] else None,
            "exit_code": row["exit_code"],
            "crawl": str(row["archiveresult__snapshot__crawl_id"] or ""),
            "crawl_label": row["archiveresult__snapshot__crawl__label"] or "",
            "title": row["archiveresult__snapshot__title"] or "",
            "url": row["archiveresult__snapshot__url"] or "",
            "hook": row["archiveresult__hook_name"] or "",
            "disk": row["archiveresult__output_size"] or 0,
            "interface": row["iface__iface"] or "",
            "local_ip": row["iface__ip_local"] or "",
            "wan_ip": row["iface__ip_public"] or "",
            "network_updated": row["iface__modified_at"].timestamp()
            if row["iface__modified_at"]
            else None,
            "sockets": [],
        }
        if row["pid"] and row["status"] == "running":
            try:
                process = psutil.Process(row["pid"])
                with process.oneshot():
                    # Never attribute a recycled PID's telemetry to an old DB row.
                    if (
                        record["started"]
                        and abs(process.create_time() - record["started"]) < 10
                    ):
                        cpu = process.cpu_times()
                        record.update(
                            cpu=cpu.user + cpu.system,
                            rss=process.memory_info().rss,
                        )
                        if len(processes) < 32:
                            for connection in process.net_connections(kind="inet")[:32]:
                                if connection.raddr and connection.laddr:
                                    local = connection.laddr.ip.split("%")[0]
                                    interface = addresses.get(local, "")
                                    if interface:
                                        related_interfaces.add(interface)
                                    record["sockets"].append(
                                        {
                                            "local": local,
                                            "remote": connection.raddr.ip,
                                            "port": connection.raddr.port,
                                            "interface": interface,
                                        },
                                    )
                    else:
                        record["status"] = "exited"
            except psutil.NoSuchProcess:
                record["status"] = "exited"
                record["ended"] = now
            except (psutil.Error, OSError):
                pass
        if record["interface"] and record["status"] == "running" and record["snapshot"]:
            related_interfaces.add(record["interface"])
        processes.append(record)
    events = []
    for snapshot_id, directory in page["output_dirs"].items():
        file = Path(directory) / "responses" / "index.jsonl"
        try:
            with file.open("rb") as stream:
                stream.seek(0, 2)
                offset = max(0, stream.tell() - 65536)
                stream.seek(offset)
                if offset:
                    stream.readline(65536)
                content = stream.read(65536)
            for line in content.splitlines()[-256:]:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    domain = urlsplit(event.get("url", "")).hostname
                    if not domain:
                        continue
                    try:
                        timestamp = (
                            datetime.strptime(event.get("ts", ""), "%Y%m%dT%H%M%S")
                            .replace(tzinfo=timezone.utc)
                            .timestamp()
                        )
                    except (ValueError, TypeError):
                        continue
                    size = response_size(file.parent, event.get("path", ""))
                    events.append(
                        {
                            "id": sha256(snapshot_id.encode() + line).hexdigest()[:24],
                            "host": domain,
                            "bytes": size,
                            "time": timestamp,
                            "latency_ms": event.get("latency_ms"),
                            "snapshot": snapshot_id,
                            "ts": event.get("ts"),
                            "status": event.get("status"),
                        },
                    )
                except (ValueError, TypeError, UnicodeError):
                    continue
        except OSError:
            continue
    return {
        "time": now,
        "interfaces": [
            interface
            for interface in interfaces
            if interface["id"] in related_interfaces
        ],
        "processes": processes,
        "events": events,
        "cursor": page["cursor"],
    }


@lru_cache(maxsize=8192)
def response_size(root, relative):
    """Response bodies are immutable; resolve only paths inside this capture."""
    if not isinstance(relative, str):
        return None
    try:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None
        return path.stat().st_size
    except (OSError, ValueError):
        return None
