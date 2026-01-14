import socket
import struct
import threading
import time
import random
from dataclasses import dataclass
from typing import Optional, Tuple, List

MAGIC_COOKIE = 0xabcddcba

TYPE_OFFER   = 0x2
TYPE_REQUEST = 0x3
TYPE_PAYLOAD = 0x4

RESULT_NOT_OVER = 0x0
RESULT_TIE      = 0x1
RESULT_LOSS     = 0x2
RESULT_WIN      = 0x3

SUITS = ["Heart", "Diamond", "Club", "Spade"]

SERVER_NAME = "Blackijecky"
UDP_PORT = 13122
TCP_BACKLOG = 50


# ---------- Game ----------
@dataclass(frozen=True)
class Card:
    rank: int  # 1-13
    suit: int  # 0-3

def card_value(rank: int) -> int:
    if rank == 1:
        return 11
    if rank >= 11:
        return 10
    return rank

def card_str(c: Card) -> str:
    rr = {1:"A", 11:"J", 12:"Q", 13:"K"}.get(c.rank, str(c.rank))
    return f"{rr} of {SUITS[c.suit]}"

class Deck:
    def __init__(self):
        self.cards: List[Card] = [Card(rank, suit) for suit in range(4) for rank in range(1, 14)]
        random.shuffle(self.cards)

    def draw(self) -> Card:
        if not self.cards:
            self.__init__()
        return self.cards.pop()

def total(hand: List[Card]) -> int:
    return sum(card_value(c.rank) for c in hand)

def dealer_should_hit(dealer_sum: int) -> bool:
    return dealer_sum < 17


# ---------- Network helpers ----------
def clamp_name(name: str, n: int = 32) -> bytes:
    b = name.encode("utf-8", errors="ignore")[:n]
    return b + b"\x00" * (n - len(b))

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

def pack_offer(tcp_port: int, server_name: str) -> bytes:
    return struct.pack("!IBH32s", MAGIC_COOKIE, TYPE_OFFER, tcp_port, clamp_name(server_name))

def unpack_request(data: bytes) -> Optional[Tuple[int, str]]:
    if len(data) != 4 + 1 + 1 + 32:
        return None
    cookie, mtype, rounds, name = struct.unpack("!IBB32s", data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_REQUEST:
        return None
    team = name.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    return rounds, team

def pack_payload_server(result: int, rank: int, suit: int) -> bytes:
    return struct.pack("!IBBHB", MAGIC_COOKIE, TYPE_PAYLOAD, result, rank, suit)

def unpack_payload_decision(data: bytes) -> Optional[str]:
    if len(data) != 10:
        return None
    cookie, mtype = struct.unpack("!IB", data[:5])
    if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
        return None
    decision = data[5:10].decode("ascii", errors="ignore")
    if decision not in ("Hittt", "Stand"):
        return None
    return decision

def send_card_update(conn: socket.socket, result: int, c: Card):
    conn.sendall(pack_payload_server(result, c.rank, c.suit))

def drain_extra_decisions(conn: socket.socket, max_packets: int = 8):
    try:
        conn.setblocking(False)
        for _ in range(max_packets):
            chunk = conn.recv(10, socket.MSG_PEEK)
            if len(chunk) < 10:
                break
            cookie = int.from_bytes(chunk[:4], "big", signed=False)
            mtype = chunk[4]
            if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
                break
            _ = conn.recv(10)  # consume
    except Exception:
        pass
    finally:
        conn.setblocking(True)


# ---------- UDP broadcaster ----------
class OfferBroadcaster(threading.Thread):
    def __init__(self, tcp_port: int, name: str, interval_sec: float = 1.0):
        super().__init__(daemon=True)
        self.tcp_port = tcp_port
        self.name = name
        self.interval = interval_sec
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def run(self):
        pkt = pack_offer(self.tcp_port, self.name)
        while not self._stop.is_set():
            try:
                self.sock.sendto(pkt, ("<broadcast>", UDP_PORT))
            except Exception:
                pass
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


# ---------- Server game session ----------
def handle_client(conn: socket.socket, addr):
    try:
        conn.settimeout(120.0)

        req = recv_exact(conn, 4 + 1 + 1 + 32)
        if not req:
            return
        parsed = unpack_request(req)
        if not parsed:
            return

        rounds, client_name = parsed
        if rounds <= 0:
            return

        print(f"[TCP] Client {addr} connected. Name={client_name}, rounds={rounds}")

        wins = losses = ties = 0

        for r in range(1, rounds + 1):
            deck = Deck()

            player = [deck.draw(), deck.draw()]
            dealer = [deck.draw(), deck.draw()]

            print(f"\n--- Round {r}/{rounds} ---")
            print(f"Player gets: {card_str(player[0])}, {card_str(player[1])}")
            print(f"Dealer shows: {card_str(dealer[0])} (second card hidden)")

            # Initial deal (3 updates)
            send_card_update(conn, RESULT_NOT_OVER, player[0])
            send_card_update(conn, RESULT_NOT_OVER, player[1])
            send_card_update(conn, RESULT_NOT_OVER, dealer[0])

            # ✅ 21 on initial deal => immediate win
            psum = total(player)
            if psum == 21:
                print("Player hits 21 on initial deal -> Player wins")
                wins += 1
                send_card_update(conn, RESULT_WIN, player[-1])
                continue

            # Player turn
            while True:
                psum = total(player)

                if psum > 21:
                    print(f"Player busts with {psum}")
                    losses += 1
                    send_card_update(conn, RESULT_LOSS, player[-1])
                    break

                data = recv_exact(conn, 10)
                if not data:
                    print("Client disconnected / timeout during decision")
                    return

                decision = unpack_payload_decision(data)
                if decision is None:
                    decision = "Stand"

                drain_extra_decisions(conn)

                print(f"Player decision: {decision} (sum={psum})")

                if decision == "Stand":
                    break

                # Hit -> send exactly one card
                c = deck.draw()
                player.append(c)
                psum = total(player)
                print(f"Player draws: {card_str(c)} (new sum={psum})")
                send_card_update(conn, RESULT_NOT_OVER, c)

                # ✅ 21 after hit => immediate win
                if psum == 21:
                    print("Player hits 21 -> Player wins")
                    wins += 1
                    send_card_update(conn, RESULT_WIN, player[-1])
                    break

            # If round ended by bust or 21-win, go next round
            psum = total(player)
            if psum > 21 or psum == 21:
                continue

            # Dealer reveal
            print(f"Dealer reveals: {card_str(dealer[1])}")
            send_card_update(conn, RESULT_NOT_OVER, dealer[1])

            while dealer_should_hit(total(dealer)):
                c = deck.draw()
                dealer.append(c)
                print(f"Dealer draws: {card_str(c)} (dealer sum={total(dealer)})")
                send_card_update(conn, RESULT_NOT_OVER, c)

            dsum = total(dealer)

            # Decide winner (ONE final result)
            if dsum > 21:
                print(f"Dealer busts with {dsum} -> Player wins")
                wins += 1
                send_card_update(conn, RESULT_WIN, dealer[-1])
            else:
                if psum > dsum:
                    print(f"Player {psum} > Dealer {dsum} -> Player wins")
                    wins += 1
                    send_card_update(conn, RESULT_WIN, dealer[-1])
                elif dsum > psum:
                    print(f"Dealer {dsum} > Player {psum} -> Dealer wins")
                    losses += 1
                    send_card_update(conn, RESULT_LOSS, dealer[-1])
                else:
                    print(f"Tie {psum} = {dsum}")
                    ties += 1
                    send_card_update(conn, RESULT_TIE, dealer[-1])

        print(f"\nSession done for {addr}. W/L/T = {wins}/{losses}/{ties}")

    except Exception as e:
        print(f"[ERROR] {addr}: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind(("0.0.0.0", 0))
    tcp_sock.listen(TCP_BACKLOG)

    tcp_port = tcp_sock.getsockname()[1]
    print(f"Server started, listening on TCP port {tcp_port}")

    bc = OfferBroadcaster(tcp_port, SERVER_NAME, interval_sec=1.0)
    bc.start()
    print("Broadcasting offers over UDP every 1s...")

    try:
        while True:
            conn, addr = tcp_sock.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try:
            bc.stop()
        except Exception:
            pass
        try:
            tcp_sock.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
