import socket
import struct
import threading
import time
from typing import Optional, Tuple

MAGIC_COOKIE = 0xabcddcba

TYPE_OFFER   = 0x2
TYPE_REQUEST = 0x3
TYPE_PAYLOAD = 0x4

RESULT_NOT_OVER = 0x0
RESULT_TIE   = 0x1
RESULT_LOSS  = 0x2
RESULT_WIN   = 0x3

UDP_LISTEN_PORT = 13122

# ---- Card encoding: rank (1-13) in 2 bytes (big endian), suit (0-3) in 1 byte ----
def pack_card(rank: int, suit: int) -> bytes:
    return struct.pack("!HB", rank & 0xFFFF, suit & 0xFF)

def unpack_card(b: bytes) -> Tuple[int, int]:
    rank, suit = struct.unpack("!HB", b)
    return int(rank), int(suit)

# ---- Offer: cookie(4) type(1) tcp_port(2) server_name(32) ----
def pack_offer(tcp_port: int, server_name_32: bytes) -> bytes:
    return struct.pack("!IBH", MAGIC_COOKIE, TYPE_OFFER, tcp_port & 0xFFFF) + server_name_32

def unpack_offer(data: bytes) -> Optional[Tuple[int, str]]:
    if len(data) < 4 + 1 + 2 + 32:
        return None
    cookie, mtype, tcp_port = struct.unpack("!IBH", data[:7])
    if cookie != MAGIC_COOKIE or mtype != TYPE_OFFER:
        return None
    name_raw = data[7:7+32]
    name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    return tcp_port, name

# ---- Request: cookie(4) type(1) rounds(1) client_name(32) ----
def pack_request(rounds: int, client_name_32: bytes) -> bytes:
    return struct.pack("!IBB", MAGIC_COOKIE, TYPE_REQUEST, rounds & 0xFF) + client_name_32

def unpack_request(data: bytes) -> Optional[Tuple[int, str]]:
    if len(data) < 4 + 1 + 1 + 32:
        return None
    cookie, mtype, rounds = struct.unpack("!IBB", data[:6])
    if cookie != MAGIC_COOKIE or mtype != TYPE_REQUEST:
        return None
    name_raw = data[6:6+32]
    name = name_raw.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    return int(rounds), name

# ---- Payload (client->server): cookie(4) type(1) decision(5) ----
def pack_payload_decision(decision5: bytes) -> bytes:
    return struct.pack("!IB", MAGIC_COOKIE, TYPE_PAYLOAD) + decision5

def unpack_payload_decision(data: bytes) -> Optional[str]:
    if len(data) < 4 + 1 + 5:
        return None
    cookie, mtype = struct.unpack("!IB", data[:5])
    if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
        return None
    d = data[5:10].decode("utf-8", errors="ignore")
    if d == "Hittt":
        return "Hit"
    if d == "Stand":
        return "Stand"
    return None

# ---- Payload (server->client): cookie(4) type(1) result(1) card(3) ----
def pack_payload_server(result: int, rank: int, suit: int) -> bytes:
    return struct.pack("!IBB", MAGIC_COOKIE, TYPE_PAYLOAD, result & 0xFF) + pack_card(rank, suit)

def unpack_payload_server(data: bytes) -> Optional[Tuple[int, int, int]]:
    if len(data) < 4 + 1 + 1 + 3:
        return None
    cookie, mtype, result = struct.unpack("!IBB", data[:6])
    if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
        return None
    rank, suit = unpack_card(data[6:9])
    return int(result), int(rank), int(suit)

# ---- UDP broadcaster thread ----
class OfferBroadcaster(threading.Thread):
    def __init__(self, tcp_port: int, server_name_32: bytes, interval_sec: float = 1.0):
        super().__init__(daemon=True)
        self.tcp_port = tcp_port
        self.server_name_32 = server_name_32
        self.interval_sec = interval_sec
        self._stop = threading.Event()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass

    def run(self):
        msg = pack_offer(self.tcp_port, self.server_name_32)
        while not self._stop.is_set():
            try:
                self.sock.sendto(msg, ("<broadcast>", UDP_LISTEN_PORT))
            except Exception:
                # ignore transient network errors; keep broadcasting
                pass
            self._stop.wait(self.interval_sec)
