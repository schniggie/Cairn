#!/usr/bin/env python3
"""Loopback forwarder for a research sandbox that has no external network.

The process listens on 127.0.0.1 inside the sandbox network namespace and
splices each accepted TCP connection to the host egress proxy over a Unix
socket. It then replaces itself with the real command. The forwarder child
stays behind; bubblewrap tears the namespace down when that command exits.

This file is copied into the sandbox and run with the system Python. It must
not import the cairn package.
"""
from __future__ import annotations

import os
import socket
import sys
import threading


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _splice(client: socket.socket, upstream: socket.socket) -> None:
    left = threading.Thread(target=_pump, args=(client, upstream), daemon=True)
    right = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    left.start()
    right.start()
    left.join()
    right.join()


def main(argv: list[str]) -> None:
    if "--" not in argv:
        raise SystemExit(2)
    command = argv[argv.index("--") + 1 :]
    if not command:
        raise SystemExit(2)
    sock_path = os.environ["CAIRN_EGRESS_SOCK"]
    port = int(os.environ["CAIRN_EGRESS_PORT"])
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            # Bind before telling the parent it may exec the real command.
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(64)
            os.write(write_fd, b"1")
        except OSError:
            os.close(write_fd)
            os._exit(1)
        os.close(write_fd)
        while True:
            try:
                client, _addr = server.accept()
            except OSError:
                os._exit(0)
            try:
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(sock_path)
            except OSError:
                client.close()
                continue
            try:
                _splice(client, upstream)
            finally:
                client.close()
                try:
                    upstream.close()
                except OSError:
                    pass
    os.close(write_fd)
    if os.read(read_fd, 1) != b"1":
        raise SystemExit(1)
    os.close(read_fd)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv)
