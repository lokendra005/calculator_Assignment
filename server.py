#!/usr/bin/env python3
"""A tiny HTTP/1.1 calculator served straight off a socket.

    GET /add?a=2&b=3   -> 200  5
    GET /sub?a=10&b=4  -> 200  6
    GET /mul?a=6&b=7   -> 200  42
    GET /div?a=9&b=3   -> 200  3

The connection is persistent: one TCP handshake answers as many requests as the
client cares to send, in order, and stays open until the client says otherwise.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

HOST = "::"  # dual-stack, so "localhost" answers on both ::1 and 127.0.0.1
PORT = int(os.environ.get("PORT", "8080"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "0"))  # 0: serve probes on PORT only
VERSION = os.environ.get("APP_VERSION", "dev")  # baked in at image build: the git sha

IDLE_TIMEOUT = 30       # seconds a quiet connection may wait for its next request
REQUEST_TIMEOUT = 10    # seconds one request has to arrive in full, start to finish
WRITE_TIMEOUT = 10      # seconds to get a response out
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 1 << 20
MAX_CONNECTIONS = 64
ADMIN_CONNECTIONS = 8   # kubelet probes and Prometheus need only a few
DRAIN_SECONDS = float(os.environ.get("DRAIN_SECONDS", "5"))  # keep serving after SIGTERM

# Set on SIGTERM. Readiness fails so Kubernetes stops routing here, but the
# listener stays open for DRAIN_SECONDS to answer whatever is already on its way.
DRAINING = threading.Event()

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


class Metrics:
    """Prometheus text exposition, by hand: a few series do not need a library."""

    BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0)

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = {}    # (op, status) -> count
        self.latency = {}     # op -> [count per bucket..., sum, count]
        self.connections = 0

    def observe(self, op, status, seconds):
        with self.lock:
            key = (op, str(status))
            self.requests[key] = self.requests.get(key, 0) + 1
            h = self.latency.setdefault(op, [0] * len(self.BUCKETS) + [0.0, 0])
            for i, le in enumerate(self.BUCKETS):
                if seconds <= le:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1

    def connection(self, delta):
        with self.lock:
            self.connections += delta

    def render(self):
        with self.lock:
            out = [
                "# HELP calc_build_info Running version.",
                "# TYPE calc_build_info gauge",
                f'calc_build_info{{version="{VERSION}"}} 1',
                "# HELP calc_open_connections Connections currently held open.",
                "# TYPE calc_open_connections gauge",
                f"calc_open_connections {self.connections}",
                "# HELP calc_requests_total Responses sent, by operation and status.",
                "# TYPE calc_requests_total counter",
            ]
            for (op, status), n in sorted(self.requests.items()):
                out.append(f'calc_requests_total{{op="{op}",status="{status}"}} {n}')
            out += [
                "# HELP calc_request_duration_seconds Time from parsed request to response sent.",
                "# TYPE calc_request_duration_seconds histogram",
            ]
            for op, h in sorted(self.latency.items()):
                for le, n in zip(self.BUCKETS, h):
                    out.append(f'calc_request_duration_seconds_bucket{{op="{op}",le="{le}"}} {n}')
                out.append(f'calc_request_duration_seconds_bucket{{op="{op}",le="+Inf"}} {h[-1]}')
                out.append(f'calc_request_duration_seconds_sum{{op="{op}"}} {h[-2]}')
                out.append(f'calc_request_duration_seconds_count{{op="{op}"}} {h[-1]}')
        return "\n".join(out)


METRICS = Metrics()

# Operational endpoints, answered like any other GET. Liveness only says the
# process can still serve; readiness also says it wants new traffic.
ADMIN = {
    "/healthz": lambda: (200, "ok"),
    "/readyz": lambda: (503, "draining") if DRAINING.is_set() else (200, "ready"),
    "/version": lambda: (200, VERSION),
    "/metrics": lambda: (200, METRICS.render()),
}


def op_label(target):
    """Metric label for a target, bounded to known paths so cardinality stays fixed."""
    path = urlsplit(target).path.rstrip("/") or "/"
    return path if path in OPS or path in ADMIN else "other"


def trace_context(request):
    """W3C traceparent: join the caller's trace if it sent one, else start one.

    ponytail: ids are logged and echoed, not exported. One service has no spans
    to stitch together; add an OpenTelemetry exporter when a second hop exists.
    """
    parts = (request.header("traceparent") or "").split("-")
    trace_id = parts[1] if len(parts) == 4 else ""
    if len(trace_id) != 32 or trace_id.strip("0123456789abcdef") or trace_id == "0" * 32:
        trace_id = os.urandom(16).hex()
    return trace_id, os.urandom(8).hex()


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

    if path not in OPS and path not in ADMIN:
        return 404, f"no such operation: {path}"
    if request.method != "GET":
        return 405, f"{request.method} not allowed"
    if path in ADMIN:
        return ADMIN[path]()

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


def build_response(status, text, close, allow_header=False, extra=()):
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
    lines.extend(extra)
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
                METRICS.observe("invalid", err.status, 0)
                log("rejected", level="warn", peer=peer, status=err.status, error=str(err))
                break

            if request is None:
                break

            started = time.monotonic()
            trace_id, span_id = trace_context(request)
            status, text = handle(request)
            # While draining, ask keep-alive clients to reconnect, which lands
            # them on a pod that is still ready.
            close = request.wants_close() or DRAINING.is_set()
            sock.settimeout(WRITE_TIMEOUT)
            sock.sendall(build_response(
                status, text, close, allow_header=status == 405,
                extra=[f"traceresponse: 00-{trace_id}-{span_id}-01"],
            ))
            served += 1
            elapsed = time.monotonic() - started
            op = op_label(request.target)
            METRICS.observe(op, status, elapsed)
            if op not in ADMIN:  # probes and scrapes every few seconds are noise
                log("request", peer=peer, method=request.method, op=op, status=status,
                    ms=round(elapsed * 1000, 3), trace_id=trace_id, span_id=span_id)
            if close:
                break
    except OSError:  # includes socket.timeout on the write side
        pass
    finally:
        polite_close(sock)
        log("closed", level="debug", peer=peer, served=served)


def log(message, level="info", **fields):
    """One JSON object per line on stdout; the container runtime keeps the file."""
    if level == "debug" and not DEBUG:
        return
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "level": level, "msg": message, "version": VERSION, **fields}
    print(json.dumps(record), flush=True)


DEBUG = os.environ.get("LOG_LEVEL", "info") == "debug"


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


def serve_admin(port):
    """Probes and scrapes on their own port and their own connection budget.

    Sharing the public port meant a flood of user connections got the probes
    refused too: liveness failed, and kubelet restarted pods that were busy,
    not broken, dropping every connection they held.
    """
    listener, _ = listen_socket(port)
    slots = threading.BoundedSemaphore(ADMIN_CONNECTIONS)
    while True:
        sock, addr = listener.accept()
        if not slots.acquire(blocking=False):
            sock.close()
            continue
        threading.Thread(
            target=run_connection, args=(sock, f"{addr[0]}:{addr[1]}", slots, False),
            daemon=True,
        ).start()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    listener, host = listen_socket(port)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
    if ADMIN_PORT:
        threading.Thread(target=serve_admin, args=(ADMIN_PORT,), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: DRAINING.set())
    listener.settimeout(1)  # wake up regularly to notice SIGTERM
    log("listening", host=host, port=port, admin_port=ADMIN_PORT)

    stop_at = None
    try:
        while True:
            if DRAINING.is_set():
                if stop_at is None:
                    stop_at = time.monotonic() + DRAIN_SECONDS
                    log("draining", seconds=DRAIN_SECONDS)
                elif time.monotonic() >= stop_at:
                    log("drained, exiting")
                    break
            try:
                sock, addr = listener.accept()
            except socket.timeout:
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = f"{addr[0]}:{addr[1]}"

            # Persistent connections are held open by definition, so without a
            # ceiling a handful of idle clients could hold every thread.
            if not slots.acquire(blocking=False):
                log("refused", level="warn", peer=peer, limit=MAX_CONNECTIONS)
                METRICS.observe("refused", 503, 0)
                try:
                    sock.sendall(build_response(503, "too many connections", True))
                finally:
                    sock.close()
                continue

            log("connected", level="debug", peer=peer)
            threading.Thread(
                target=run_connection, args=(sock, peer, slots), daemon=True
            ).start()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        listener.close()


def run_connection(sock, peer, slots, counted=True):
    if counted:  # the gauge tracks user connections against MAX_CONNECTIONS
        METRICS.connection(1)
    try:
        serve_connection(sock, peer)
    finally:
        if counted:
            METRICS.connection(-1)
        slots.release()


if __name__ == "__main__":
    main()
