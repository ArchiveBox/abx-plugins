"""Native per-socket byte counters for processes owned by the runner."""

import csv
from itertools import islice
import os
import socket
import struct
import subprocess
import sys
import time


def sample_sockets(pids):
    if not pids:
        return []
    try:
        if sys.platform == "darwin":
            return macos_sockets(pids)
        if sys.platform == "linux":
            return linux_sockets(pids)
    except (OSError, subprocess.SubprocessError):
        return []
    return []


def macos_sockets(pids):
    args = [
        "/usr/bin/nettop",
        "-n",
        "-x",
        "-L",
        "1",
        "-J",
        "interface,bytes_in,bytes_out,rtt_min",
    ]
    for pid in pids:
        args.extend(("-p", str(pid)))
    output = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=1.5,
        check=True,
    ).stdout
    rows = csv.reader(output.splitlines())
    columns = {name: index for index, name in enumerate(next(rows, []))}
    if not {"interface", "bytes_in", "bytes_out"} <= columns.keys():
        return []
    pid = None
    flows = []
    for row in rows:
        if not row or not row[0]:
            continue
        name = row[0]
        if "<->" not in name:
            suffix = name.rsplit(".", 1)[-1]
            pid = int(suffix) if suffix.isdecimal() else None
            continue
        if pid not in pids:
            continue
        protocol, _, endpoints = name.partition(" ")
        local, remote = endpoints.split("<->", 1)
        # nettop uses a dot before the port for IPv6 addresses.
        address_separator = "." if protocol.endswith("6") else ":"
        local_ip = local.rpartition(address_separator)[0].split("%")[0]
        remote_ip, _, remote_port = remote.rpartition(address_separator)
        if not remote_port.isdecimal():
            continue
        try:
            received = int(row[columns["bytes_in"]] or 0)
            sent = int(row[columns["bytes_out"]] or 0)
        except (IndexError, ValueError):
            continue
        rtt = (
            row[columns["rtt_min"]]
            if "rtt_min" in columns and len(row) > columns["rtt_min"]
            else ""
        )
        try:
            latency = float(rtt.removesuffix(" ms")) if rtt.endswith(" ms") else None
        except ValueError:
            latency = None
        flows.append(
            (
                pid,
                name,
                local_ip,
                remote_ip.split("%")[0],
                row[columns["interface"]],
                received,
                sent,
                int(remote_port),
                latency,
            ),
        )
        if len(flows) >= 2048:
            break
    return flows


def linux_sockets(pids):
    netlink = getattr(socket, "AF_NETLINK", None)
    if netlink is None:
        return []
    # Map socket inodes to the sampled process tree before asking the kernel for TCP_INFO.
    owners = {}
    deadline = time.monotonic() + 0.75
    for pid in pids:
        try:
            with os.scandir(f"/proc/{pid}/fd") as descriptors:
                for fd in islice(descriptors, 4096):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        target = os.readlink(fd.path)
                    except OSError:
                        continue
                    if target.startswith("socket:["):
                        owners.setdefault(int(target[8:-1]), pid)
        except OSError:
            continue
    if not owners:
        return []
    flows = []
    with socket.socket(
        netlink,
        socket.SOCK_RAW,
        4,
    ) as sock:  # NETLINK_SOCK_DIAG
        sock.settimeout(0.15)
        for sequence, family in enumerate((socket.AF_INET, socket.AF_INET6), 1):
            # inet_diag_req_v2: request INET_DIAG_INFO for non-listening TCP sockets.
            query = struct.pack(
                "=BBBBI48x",
                family,
                socket.IPPROTO_TCP,
                2,
                0,
                0xFFF & ~(1 << 10),
            )
            sock.sendto(
                struct.pack("=IHHII", 16 + len(query), 20, 0x301, sequence, 0) + query,
                (0, 0),
            )
            done = False
            while not done and time.monotonic() < deadline and len(flows) < 2048:
                try:
                    packet = sock.recv(65536)
                except TimeoutError:
                    break
                offset = 0
                while offset + 16 <= len(packet):
                    length, kind, _, response_seq, _ = struct.unpack_from(
                        "=IHHII",
                        packet,
                        offset,
                    )
                    if length < 16 or offset + length > len(packet):
                        break
                    body = packet[offset + 16 : offset + length]
                    offset += (length + 3) & ~3
                    if response_seq != sequence:
                        continue
                    if kind in (2, 3):  # NLMSG_ERROR / NLMSG_DONE
                        done = True
                        break
                    if kind != 20 or len(body) < 72:
                        continue
                    inode = struct.unpack_from("=I", body, 68)[0]
                    if inode not in owners:
                        continue
                    flow = linux_tcp_info(family, body, inode, owners[inode])
                    if flow:
                        flows.append(flow)
                        if len(flows) >= 2048:
                            break
    return flows


def linux_tcp_info(family, body, inode, pid):
    address_bytes = 4 if family == socket.AF_INET else 16
    local = socket.inet_ntop(family, body[8 : 8 + address_bytes])
    remote = socket.inet_ntop(family, body[24 : 24 + address_bytes])
    _, port = struct.unpack_from("!HH", body, 4)
    if not port:
        return None
    offset = 72  # inet_diag_msg ends here, followed by aligned netlink attributes.
    while offset + 4 <= len(body):
        length, kind = struct.unpack_from("=HH", body, offset)
        if length < 4 or offset + length > len(body):
            break
        if kind == 2 and length >= 140:  # INET_DIAG_INFO: struct tcp_info
            info = body[offset + 4 : offset + length]
            acked, received = struct.unpack_from("=QQ", info, 120)
            sent = struct.unpack_from("=Q", info, 200)[0] if len(info) >= 208 else acked
            cookie = body[44:52].hex()
            rtt = (
                struct.unpack_from("=I", info, 148)[0]
                if len(info) >= 152
                else struct.unpack_from("=I", info, 68)[0]
            )
            return (
                pid,
                f"{inode}:{cookie}",
                local,
                remote,
                "",
                received,
                sent,
                port,
                rtt / 1000 if 0 < rtt < 0xFFFFFFFF else None,
            )
        offset += (length + 3) & ~3
    return None
