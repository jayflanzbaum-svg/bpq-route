#!/usr/bin/env python3
"""Drive bpq_route.py the way BPQ does, from a terminal - no node needed.

    python tools/route_probe.py [host:port] CALLSIGN 'ROUTE 33445 TO Boca Raton' MORE ...

Connects, sends the callsign as the first line (what BPQ's S flag does), then each
command in turn, printing whatever comes back with bare CR shown as newlines.
Waits for the '> ' prompt between commands. Interactive when no commands are given.
"""
import socket
import sys
import time


def recv_until_prompt(sock, timeout=25.0):
    sock.settimeout(0.5)
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"> "):
                break
        except socket.timeout:
            if buf.endswith(b"> "):
                break
    return buf.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def main():
    args = sys.argv[1:]
    hostport = "127.0.0.1:63053"
    if args and ":" in args[0] and args[0].split(":")[-1].isdigit():
        hostport = args.pop(0)
    if not args:
        sys.exit(__doc__)
    call = args.pop(0)
    host, port = hostport.rsplit(":", 1)
    s = socket.create_connection((host, int(port)), timeout=10)
    s.sendall((call + "\r\n").encode())
    sys.stdout.write(recv_until_prompt(s))
    cmds = args if args else iter(lambda: input(), "Q")
    for c in cmds:
        sys.stdout.write(c + "\n")
        s.sendall((c + "\r\n").encode())
        sys.stdout.write(recv_until_prompt(s))
        if c.strip().upper() in ("Q", "QUIT", "EXIT", "BYE", "NODE"):
            break
    s.close()


if __name__ == "__main__":
    main()
