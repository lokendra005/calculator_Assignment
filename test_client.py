#!/usr/bin/env python3
"""Drive the calculator the way it will be marked: one socket, every request.

    python3 server.py &
    python3 test_client.py
"""

import socket
import sys

HOST = "localhost"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

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

    print(f"\n{'all green' if not failures else str(failures) + ' failing'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
