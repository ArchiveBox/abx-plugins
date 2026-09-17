"""Exercise real WebSocket admission and disconnect cleanup against a running notary."""

import base64
import hashlib
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit


def connect(endpoint: str):
    url = urlsplit(endpoint)
    connection = socket.create_connection(
        (url.hostname, url.port or (443 if url.scheme == "wss" else 80)),
        timeout=5,
    )
    if url.scheme == "wss":
        connection = ssl.create_default_context().wrap_socket(
            connection,
            server_hostname=url.hostname,
        )
    nonce = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: abx-tlsnotary-batch-v1\r\n\r\n"
    )
    connection.sendall(request.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        data = connection.recv(4096)
        assert data, "connection closed before admission response"
        response += data
        assert len(response) < 16384, "oversized admission response"
    status = int(response.split(b" ")[1])
    if status == 101:
        expected = base64.b64encode(
            hashlib.sha1(
                (nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
            ).digest(),
        )
        assert expected in response
    return connection, status


def main(endpoint: str):
    connections = []
    try:
        for expected in (101, 101, 503):
            connection, status = connect(endpoint)
            connections.append(connection)
            assert status == expected, (status, expected)
        for connection in connections:
            connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        connections.clear()
        # Give the peer one event-loop interval to observe real TCP disconnects.
        time.sleep(0.5)
        connection, status = connect(endpoint)
        connections.append(connection)
        assert status == 101, "disconnects did not promptly release admission slots"
        print(
            "PASS: two admitted, third rejected with 503, disconnected slots reusable",
        )
    finally:
        for connection in connections:
            connection.close()


if __name__ == "__main__":
    main(sys.argv[1])
