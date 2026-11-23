"""Lanzador del servidor HTTP.

El healthcheck y el ruteo de Railway pueden llegar por IPv4 o por IPv6
(la red privada es solo IPv6), y asyncio marca los sockets `::` como
solo-IPv6. Acá el socket se crea a mano en modo dual-stack y se le pasa
a uvicorn ya abierto; con `--host` no hay forma de lograr lo mismo.
"""

import os
import socket

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    uvicorn.run("app.main:app", fd=sock.fileno())


if __name__ == "__main__":
    main()
