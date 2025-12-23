import socket
import struct
from typing import Optional, Tuple

from server.network import (
    UDP_LISTEN_PORT,
    pack_request, unpack_offer,
    pack_payload_decision, unpack_payload_server,
    RESULT_NOT_OVER, RESULT_WIN, RESULT_LOSS, RESULT_TIE
)

def decision_to_5bytes(decision: str) -> bytes:
    if decision.lower().startswith("h"):
        return b"Hittt"   # exactly 5 bytes
    return b"Stand"      # exactly 5 bytes

def open_udp_listener() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except Exception:
        pass
    s.bind(("", UDP_LISTEN_PORT))
    s.settimeout(5.0)
    return s

def wait_for_offer(udp_sock: socket.socket) -> Optional[Tuple[str, int, str]]:
    # returns (server_ip, tcp_port, server_name)
    while True:
        try:
            data, (ip, _) = udp_sock.recvfrom(1024)
        except socket.timeout:
            return None
        except Exception:
            return None
        parsed = unpack_offer(data)
        if parsed:
            tcp_port, server_name = parsed
            return ip, tcp_port, server_name

def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            return None
        except Exception:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf

def send_request_tcp(sock: socket.socket, rounds: int, client_name_32: bytes):
    sock.sendall(pack_request(rounds, client_name_32))

def send_decision(sock: socket.socket, decision: str):
    sock.sendall(pack_payload_decision(decision_to_5bytes(decision)))

def recv_server_payload(sock: socket.socket) -> Optional[Tuple[int, int, int]]:
    data = recv_exact(sock, 4 + 1 + 1 + 3)
    if not data:
        return None
    return unpack_payload_server(data)
