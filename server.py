#!/usr/bin/env python3
"""A tiny HTTP/1.1 calculator served straight off a socket.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3

The connection is persistent: one TCP handshake answers as many requests as the
client cares to send, in order, and stays open until the client says otherwise.
"""

import socket
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

HOST = "::"  # dual-stack, so "localhost" answers on both ::1 and 127.0.0.1
PORT = 8080

IDLE_TIMEOUT = 30       # seconds a quiet connection is allowed to linger
HEADER_TIMEOUT = 10     # seconds to finish a request once it has started
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 1 << 20

OPS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a / b,
}

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}


class BadRequest(Exception):
    """Fatal for the connection: the byte stream can no longer be trusted."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Stream:
    """A byte reader that hands back exactly what it is asked for.

    Whatever arrives past the current request's last byte belongs to the next
    request, so it is kept in the buffer rather than consumed.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            self.closed = True
            return False
        self.buf.extend(chunk)
        return True

    def buffered(self):
        return bytes(self.buf)

    def read_line(self, limit):
        while True:
            end = self.buf.find(b"\r\n")
            if end != -1:
                if end > limit:
                    raise BadRequest("header line too long", 431)
                line = bytes(self.buf[:end])
                del self.buf[: end + 2]
                return line
            if len(self.buf) > limit:
                raise BadRequest("header line too long", 431)
            if not self._fill():
                return None

    def read_exactly(self, n):
        while len(self.buf) < n:
            if not self._fill():
                raise BadRequest("connection closed mid-body")
        body = bytes(self.buf[:n])
        del self.buf[:n]
        return body


class Request:
    def __init__(self, method, target, version, headers, body):
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def wants_close(self):
        token = (self.header("connection") or "").lower()
        if self.version == "HTTP/1.0":
            return "keep-alive" not in token
        return "close" in token


def http_date():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def read_request(stream):
    """Return the next request on the stream, or None if the peer went away."""
    request_line = stream.read_line(MAX_HEADER_BYTES)
    if request_line is None:
        return None
    while request_line == b"":  # leniency for stray CRLF between requests
        request_line = stream.read_line(MAX_HEADER_BYTES)
        if request_line is None:
            return None

    parts = request_line.decode("latin-1").split(" ")
    if len(parts) != 3:
        raise BadRequest("malformed request line")
    method, target, version = parts
    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise BadRequest("unsupported version", 505)

    headers = {}
    budget = MAX_HEADER_BYTES
    while True:
        line = stream.read_line(budget)
        if line is None:
            raise BadRequest("headers truncated")
        if line == b"":
            break
        budget -= len(line) + 2
        if budget <= 0:
            raise BadRequest("headers too large", 431)
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep or name != name.strip():
            raise BadRequest("malformed header")
        name = name.strip().lower()
        value = value.strip()
        headers[name] = f"{headers[name]}, {value}" if name in headers else value

    body = read_body(stream, headers, version)
    return Request(method, target, version, headers, body)


def read_body(stream, headers, version):
    encoding = headers.get("transfer-encoding", "").lower()
    length = headers.get("content-length")

    if encoding:
        if encoding != "chunked":
            raise BadRequest("unsupported transfer-encoding", 501)
        return read_chunked(stream)

    if length is None:
        return b""
    if not length.isdigit():
        raise BadRequest("bad content-length")
    size = int(length)
    if size > MAX_BODY_BYTES:
        raise BadRequest("body too large", 413)
    return stream.read_exactly(size)


def read_chunked(stream):
    body = bytearray()
    while True:
        line = stream.read_line(MAX_HEADER_BYTES)
        if line is None:
            raise BadRequest("chunked body truncated")
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            raise BadRequest("bad chunk size")
        if size == 0:
            break
        if len(body) + size > MAX_BODY_BYTES:
            raise BadRequest("body too large", 413)
        body.extend(stream.read_exactly(size))
        if stream.read_exactly(2) != b"\r\n":
            raise BadRequest("chunk not terminated")
    while True:  # trailers, discarded
        trailer = stream.read_line(MAX_HEADER_BYTES)
        if trailer is None or trailer == b"":
            break
    return bytes(body)


def format_number(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def handle(request):
    """Map a parsed request onto (status, body-text)."""
    if request.version == "HTTP/1.1" and request.header("host") is None:
        return 400, "missing Host header"

    url = urlsplit(request.target)
    path = url.path.rstrip("/") or "/"

    if path not in OPS:
        return 404, f"no such operation: {path}"
    if request.method != "GET":
        return 405, f"{request.method} not allowed"

    params = parse_qs(url.query, keep_blank_values=True)
    values = []
    for name in ("a", "b"):
        if name not in params:
            return 400, f"missing parameter: {name}"
        if len(params[name]) > 1:
            return 400, f"repeated parameter: {name}"
        try:
            values.append(int(params[name][0]))
        except ValueError:
            return 400, f"parameter {name} is not an integer"

    a, b = values
    if path == "/div" and b == 0:
        return 400, "division by zero"
    return 200, format_number(OPS[path](a, b))


def build_response(status, text, close, allow_header=False):
    body = (text + "\n").encode()
    lines = [
        f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}",
        f"Date: {http_date()}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        f"Connection: {'close' if close else 'keep-alive'}",
    ]
    if not close:
        lines.append(f"Keep-Alive: timeout={IDLE_TIMEOUT}")
    if allow_header:
        lines.append("Allow: GET")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def serve_connection(sock, peer):
    stream = Stream(sock)
    served = 0
    try:
        while True:
            sock.settimeout(IDLE_TIMEOUT)
            try:
                request = read_request(stream)
            except socket.timeout:
                if stream.buffered():
                    sock.sendall(build_response(408, "request timed out", True))
                break
            except BadRequest as err:
                sock.sendall(build_response(err.status, str(err), True))
                break

            if request is None:
                break

            sock.settimeout(HEADER_TIMEOUT)
            status, text = handle(request)
            close = request.wants_close()
            sock.sendall(build_response(status, text, close, allow_header=status == 405))
            served += 1
            log(f"{peer} {request.method} {request.target} -> {status}")
            if close:
                break
    except (socket.timeout, OSError):
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        log(f"{peer} closed after {served} request(s)")


def log(message):
    print(f"[{http_date()}] {message}", flush=True)


def listen_socket(port):
    """Prefer a dual-stack IPv6 listener, fall back to IPv4 where it is refused."""
    for family, host in ((socket.AF_INET6, HOST), (socket.AF_INET, "0.0.0.0")):
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            try:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                listener.close()
                continue
        try:
            listener.bind((host, port))
        except OSError:
            listener.close()
            if family == socket.AF_INET:
                raise
            continue
        listener.listen(64)
        return listener, host


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    listener, host = listen_socket(port)
    log(f"listening on {host}:{port}")

    try:
        while True:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = f"{addr[0]}:{addr[1]}"
            log(f"{peer} connected")
            threading.Thread(
                target=serve_connection, args=(sock, peer), daemon=True
            ).start()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        listener.close()


if __name__ == "__main__":
    main()
