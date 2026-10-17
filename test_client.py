#!/usr/bin/env python3
"""Drive the calculator the way it will be marked: one socket, every request.

    ADMIN_PORT=8081 python3 server.py &
    python3 test_client.py 8080 8081
"""

import socket
import sys
import time

HOST = "localhost"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
ADMIN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 0

CASES = [
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=9&b=3", 200, "3"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/add?a=x&b=3", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]


class Reader:
    """Reads one response and stops on its last byte, never a byte further."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise AssertionError("server closed the connection unexpectedly")
        self.buf += chunk

    def read_response(self):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        while len(self.buf) < length:
            self._fill()
        body, self.buf = self.buf[:length], self.buf[length:]
        return status, headers, body.decode().strip()

    def leftover(self):
        return self.buf


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    return ok


def request_bytes(method, target, host=HOST, extra=()):
    lines = [f"{method} {target} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}:{PORT}")
    lines.extend(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def main():
    failures = 0

    # --- the marking run: one handshake, every request -----------------------
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    print("one socket, sequential requests")
    for method, target, want_status, want_body in CASES:
        sock.sendall(request_bytes(method, target))
        status, _, body = reader.read_response()
        ok = status == want_status and (want_body is None or body == want_body)
        failures += not check(f"{method} {target}", ok, f"-> {status}  {body}")

    sock.sendall(b"x")  # a dead socket would raise here
    failures += not check("socket still open", True)
    failures += not check("nothing over-read", reader.leftover() == b"", 
                          repr(reader.leftover()))
    sock.close()

    # --- pipelining: all eight at once, answered in order --------------------
    print("\npipelined, one write")
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(b"".join(request_bytes(m, t) for m, t, _, _ in CASES))
    for method, target, want_status, want_body in CASES:
        status, _, body = reader.read_response()
        ok = status == want_status and (want_body is None or body == want_body)
        failures += not check(f"{method} {target}", ok, f"-> {status}")
    sock.close()

    # --- the edges -----------------------------------------------------------
    print("\nedges")
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(request_bytes("GET", "/add?a=2&b=3", host=None))
    status, _, _ = reader.read_response()
    failures += not check("no Host -> 400", status == 400, f"-> {status}")
    sock.close()

    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(request_bytes("GET", "/add?a=2&b=3", extra=["Connection: close"]))
    status, headers, _ = reader.read_response()
    ok = status == 200 and headers.get("connection") == "close"
    failures += not check("Connection: close honoured", ok)
    failures += not check("server hung up after it", sock.recv(1) == b"")
    sock.close()

    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    body = b"ignored"
    sock.sendall(
        request_bytes("POST", "/add", extra=[f"Content-Length: {len(body)}"])[:-2]
        + b"\r\n"
        + body
    )
    sock.sendall(request_bytes("GET", "/add?a=1&b=1"))
    status, _, _ = reader.read_response()
    failures += not check("POST body consumed, not leaked", status == 405)
    status, _, text = reader.read_response()
    failures += not check("next request framed correctly",
                          status == 200 and text == "2", f"-> {status} {text}")
    sock.close()

    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    chunked = (
        request_bytes("POST", "/add", extra=["Transfer-Encoding: chunked"])[:-2]
        + b"\r\n4\r\nnoop\r\n0\r\n\r\n"
    )
    sock.sendall(chunked + request_bytes("GET", "/mul?a=3&b=3"))
    status, _, _ = reader.read_response()
    failures += not check("chunked body consumed", status == 405)
    status, _, text = reader.read_response()
    failures += not check("stream still aligned after chunked",
                          status == 200 and text == "9", f"-> {status} {text}")
    sock.close()

    failures += operations()
    failures += hardening()

    print(f"\n{'all green' if not failures else str(failures) + ' failing'}")
    return 1 if failures else 0


def operations():
    """Probes, metrics and trace ids: what Kubernetes and Prometheus rely on."""
    failures = 0
    print("\noperations")
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    for target, want in (("/healthz", "ok"), ("/readyz", "ready")):
        sock.sendall(request_bytes("GET", target))
        status, _, body = reader.read_response()
        failures += not check(f"GET {target}", status == 200 and body == want, f"-> {status} {body}")

    sock.sendall(request_bytes("GET", "/version"))
    status, _, body = reader.read_response()
    failures += not check("GET /version", status == 200 and body != "", f"-> {body}")

    sock.sendall(request_bytes("GET", "/metrics"))
    status, _, body = reader.read_response()
    ok = status == 200 and 'calc_requests_total{op="/add",status="200"}' in body
    failures += not check("GET /metrics counts requests", ok)

    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    sock.sendall(request_bytes("GET", "/add?a=1&b=2",
                               extra=[f"traceparent: 00-{trace}-00f067aa0ba902b7-01"]))
    status, headers, _ = reader.read_response()
    ok = headers.get("traceresponse", "").startswith(f"00-{trace}-")
    failures += not check("trace id carried through", ok, headers.get("traceresponse", ""))

    sock.sendall(request_bytes("POST", "/healthz"))
    status, _, _ = reader.read_response()
    failures += not check("POST /healthz -> 405", status == 405, f"-> {status}")
    sock.close()
    return failures


def hardening():
    """The cases that are not about arithmetic, but about not being lied to."""
    failures = 0
    print("\nhardening")

    # Content-Length and Transfer-Encoding together: two framings, one stream.
    # Answering it at all lets a smuggled request ride in behind the body.
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(
        b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
        b"GET /mul?a=9&b=9 HTTP/1.1\r\nHost: x\r\n\r\n"
    )
    status, _, _ = reader.read_response()
    failures += not check("CL + TE rejected", status == 400, f"-> {status}")
    failures += not check("no smuggled request answered", sock.recv(400) == b"")
    sock.close()

    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n")
    status, _, _ = reader.read_response()
    failures += not check("duplicate Host rejected", status == 400, f"-> {status}")
    sock.close()

    body = b"9999"
    sock = socket.create_connection((HOST, PORT))
    reader = Reader(sock)
    sock.sendall(
        b"POST /add HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\n"
        b"Expect: 100-continue\r\n\r\n"
    )
    sock.settimeout(5)
    status, _, _ = reader.read_response()
    failures += not check("Expect gets 100 Continue", status == 100, f"-> {status}")
    sock.sendall(body)
    status, _, _ = reader.read_response()
    failures += not check("then the real response", status == 405, f"-> {status}")
    sock.close()

    # A socket timeout only bounds the gap between packets, which a slow client
    # can reset forever. The request as a whole needs its own deadline.
    sock = socket.create_connection((HOST, PORT))
    sock.sendall(b"GET /add?a=2&b=3 HTTP/1.1\r\nHost: x\r\n")
    start = time.time()
    sock.settimeout(40)
    try:
        while time.time() - start < 35:
            time.sleep(2)
            sock.sendall(b"X-Pad: y\r\n")
        held = None
    except OSError:
        held = time.time() - start
    failures += not check("slow drip cut off", held is not None,
                          f"held {held:.0f}s" if held else "still open after 35s")
    sock.close()

    socks = [socket.create_connection((HOST, PORT)) for _ in range(65)]
    refused = 0
    for s in socks:
        s.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: x\r\n\r\n")
    for s in socks:
        s.settimeout(5)
        if b" 503 " in s.recv(200):
            refused += 1
    failures += not check("connection ceiling enforced", refused >= 1,
                          f"{refused} refused with 503")

    # The bug this guards: probes shared the ceiling, so a flood made kubelet
    # restart healthy pods. The admin port must answer while 8080 is full.
    if ADMIN_PORT:
        probe = socket.create_connection((HOST, ADMIN_PORT), timeout=5)
        probe.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        status, _, _ = Reader(probe).read_response()
        failures += not check("admin port answers while 8080 is full",
                              status == 200, f"-> {status}")
        probe.close()
    for s in socks:
        s.close()
    return failures


if __name__ == "__main__":
    sys.exit(main())
