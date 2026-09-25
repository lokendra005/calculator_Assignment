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
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

HOST = "::"  # dual-stack, so "localhost" answers on both ::1 and 127.0.0.1
PORT = 8080

IDLE_TIMEOUT = 30       # seconds a quiet connection may wait for its next request
REQUEST_TIMEOUT = 10    # seconds one request has to arrive in full, start to finish
WRITE_TIMEOUT = 10      # seconds to get a response out
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 1 << 20
MAX_CONNECTIONS = 64

# Headers that change how the message is framed or routed. A second copy is not
# a list to join, it is an ambiguity, and ambiguity is how requests get smuggled.
SINGLETON_HEADERS = {"host", "content-length", "transfer-encoding"}

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
    413: "Payload Too Large",
    417: "Expectation Failed",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}

CONTINUE = b"HTTP/1.1 100 Continue\r\n\r\n"


class BadRequest(Exception):
    """Fatal for the connection: the byte stream can no longer be trusted."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Stream:
    """A byte reader that hands back exactly what it is asked for.

    Whatever arrives past the current request's last byte belongs to the next
    request, so it is kept in the buffer rather than consumed.

    It also owns the clock. A socket timeout on its own only bounds the gap
    between two packets, which a slow client can reset forever; the deadline
    here bounds the whole request.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.closed = False
        self.deadline = None   # absolute monotonic time the current read must end by
        self.budget = None     # seconds to grant once the request's first byte lands

    def expect_request(self, idle, budget):
        """Idle for `idle` seconds, then give the request `budget` to complete."""
        if self.buf:  # a pipelined request is already here; its clock starts now
            self.deadline, self.budget = time.monotonic() + budget, None
        else:
            self.deadline, self.budget = time.monotonic() + idle, budget

    def request_started(self):
        return self.budget is None

    def _fill(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("deadline exceeded")
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(65536)
        if not chunk:
            self.closed = True
            return False
        self.buf.extend(chunk)
        if self.budget is not None:
            self.deadline, self.budget = time.monotonic() + self.budget, None
        return True

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


def read_request(stream, interim):
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

    headers = read_headers(stream)

    expect = headers.get("expect", "").lower()
    if expect:
        if expect != "100-continue":
            raise BadRequest(f"unsupported expectation: {expect}", 417)
        if version == "HTTP/1.1" and has_body(headers):
            interim(CONTINUE)

    body = read_body(stream, headers)
    return Request(method, target, version, headers, body)


def read_headers(stream):
    headers = {}
    budget = MAX_HEADER_BYTES
    while True:
        line = stream.read_line(budget)
        if line is None:
            raise BadRequest("headers truncated")
        if line == b"":
            return headers
        budget -= len(line) + 2
        if budget <= 0:
            raise BadRequest("headers too large", 431)
        name, sep, value = line.decode("latin-1").partition(":")
        if not sep or name != name.strip():
            raise BadRequest("malformed header")
        name = name.strip().lower()
        value = value.strip()
        if name in headers:
            if name in SINGLETON_HEADERS:
                raise BadRequest(f"duplicate {name} header")
            headers[name] = f"{headers[name]}, {value}"
        else:
            headers[name] = value


def has_body(headers):
    return "transfer-encoding" in headers or headers.get("content-length", "0") != "0"


def read_body(stream, headers):
    encoding = headers.get("transfer-encoding", "").lower()
    length = headers.get("content-length")

    if encoding:
        # Two framings at once means the sender and I may disagree about where
        # this request ends, and a proxy in between may disagree with us both.
        if length is not None:
            raise BadRequest("Content-Length with Transfer-Encoding")
        if encoding != "chunked":
            raise BadRequest(f"unsupported transfer-encoding: {encoding}", 501)
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


def polite_close(sock):
    """Half-close and drain, so the last response is not lost to a reset.

    Closing outright while the peer still has bytes in flight makes the kernel
    answer them with RST, which can discard a response the client never read.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(1)
        while sock.recv(65536):
            pass
    except OSError:
        pass
    finally:
        sock.close()


def serve_connection(sock, peer):
    stream = Stream(sock)
    served = 0
    try:
        while True:
            stream.expect_request(IDLE_TIMEOUT, REQUEST_TIMEOUT)
            try:
                request = read_request(stream, sock.sendall)
            except socket.timeout:
                if stream.request_started():
                    sock.settimeout(WRITE_TIMEOUT)
                    sock.sendall(build_response(408, "request timed out", True))
                break
            except BadRequest as err:
                sock.settimeout(WRITE_TIMEOUT)
                sock.sendall(build_response(err.status, str(err), True))
                break

            if request is None:
                break

            status, text = handle(request)
            close = request.wants_close()
            sock.settimeout(WRITE_TIMEOUT)
            sock.sendall(build_response(status, text, close, allow_header=status == 405))
            served += 1
            log(f"{peer} {request.method} {request.target} -> {status}")
            if close:
                break
    except OSError:  # includes socket.timeout on the write side
        pass
    finally:
        polite_close(sock)
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
        listener.listen(MAX_CONNECTIONS)
        return listener, host


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    listener, host = listen_socket(port)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    log(f"listening on {host}:{port}")

    try:
        while True:
            sock, addr = listener.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = f"{addr[0]}:{addr[1]}"

            # Persistent connections are held open by definition, so without a
            # ceiling a handful of idle clients could hold every thread.
            if not slots.acquire(blocking=False):
                log(f"{peer} refused: {MAX_CONNECTIONS} connections already open")
                try:
                    sock.sendall(build_response(503, "too many connections", True))
                finally:
                    sock.close()
                continue

            log(f"{peer} connected")
            threading.Thread(
                target=run_connection, args=(sock, peer, slots), daemon=True
            ).start()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        listener.close()


def run_connection(sock, peer, slots):
    try:
        serve_connection(sock, peer)
    finally:
        slots.release()


if __name__ == "__main__":
    main()
