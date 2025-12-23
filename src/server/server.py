import socket
import threading
from typing import Optional

from .utils import env_int, clamp_team_name
from .network import (
    OfferBroadcaster, MAGIC_COOKIE, TYPE_REQUEST,
    pack_payload_server, unpack_payload_decision, unpack_request,
    RESULT_NOT_OVER, RESULT_WIN, RESULT_LOSS, RESULT_TIE
)
from .game_logic import Deck, Card, card_str, total, dealer_should_hit

SERVER_NAME = "Blackijecky"
TCP_BACKLOG = 50

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

def try_read_request(sock: socket.socket) -> Optional[tuple[int, str]]:
    """
    Supports:
    1) Binary request format (cookie + type + rounds + name)
    2) Fallback: ASCII number ended by '\n' (some teams might implement the example literally)
    """
    sock.settimeout(5.0)

    # Peek first 5 bytes to see if it's our binary header
    try:
        head = sock.recv(5, socket.MSG_PEEK)
    except Exception:
        return None

    if len(head) >= 5:
        cookie = int.from_bytes(head[:4], "big", signed=False)
        mtype = head[4]
        if cookie == MAGIC_COOKIE and mtype == TYPE_REQUEST:
            data = recv_exact(sock, 4 + 1 + 1 + 32)
            if not data:
                return None
            parsed = unpack_request(data)
            return parsed

    # ASCII fallback
    line = b""
    while b"\n" not in line and len(line) < 64:
        try:
            ch = sock.recv(1)
        except socket.timeout:
            return None
        except Exception:
            return None
        if not ch:
            return None
        line += ch
    try:
        rounds = int(line.strip().decode("utf-8", errors="ignore"))
        return rounds, "UNKNOWN"
    except Exception:
        return None

def send_card_update(conn: socket.socket, result: int, c: Card):
    conn.sendall(pack_payload_server(result, c.rank, c.suit))

def handle_client(conn: socket.socket, addr):
    try:
        req = try_read_request(conn)
        if not req:
            return
        rounds, client_name = req
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

            # Initial deal: send two player cards + dealer upcard
            send_card_update(conn, RESULT_NOT_OVER, player[0])
            send_card_update(conn, RESULT_NOT_OVER, player[1])
            send_card_update(conn, RESULT_NOT_OVER, dealer[0])

            # Player turn
            while True:
                psum = total(player)
                if psum > 21:
                    print(f"Player busts with {psum}")
                    losses += 1
                    # End round: send final result (card can be any; send last player card)
                    send_card_update(conn, RESULT_LOSS, player[-1])
                    break

                # Wait for decision payload
                conn.settimeout(20.0)
                data = recv_exact(conn, 4 + 1 + 5)
                if not data:
                    print("Client disconnected / timeout during decision")
                    return

                decision = unpack_payload_decision(data)
                if decision is None:
                    # Treat invalid decision as Stand (robustness)
                    decision = "Stand"

                print(f"Player decision: {decision} (sum={psum})")

                if decision == "Stand":
                    # Dealer turn begins
                    break
                else:
                    # Hit: draw a card, send it
                    c = deck.draw()
                    player.append(c)
                    print(f"Player draws: {card_str(c)} (new sum={total(player)})")
                    send_card_update(conn, RESULT_NOT_OVER, c)

            # If player already busted, continue to next round
            if total(player) > 21:
                continue

            # Dealer reveals hidden card
            print(f"Dealer reveals: {card_str(dealer[1])}")
            send_card_update(conn, RESULT_NOT_OVER, dealer[1])

            while dealer_should_hit(total(dealer)):
                c = deck.draw()
                dealer.append(c)
                print(f"Dealer draws: {card_str(c)} (dealer sum={total(dealer)})")
                send_card_update(conn, RESULT_NOT_OVER, c)

            dsum = total(dealer)
            psum = total(player)

            # Decide winner
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
        print(f"[ERROR] Client handler exception: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def main():
    team_name = clamp_team_name(SERVER_NAME, 32)

    tcp_port = env_int("TCP_PORT", 0)  # 0 means OS chooses
    host = "0.0.0.0"

    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind((host, tcp_port))
    tcp_sock.listen(TCP_BACKLOG)

    actual_port = tcp_sock.getsockname()[1]
    print(f"Server started, listening on TCP port {actual_port}")

    broadcaster = OfferBroadcaster(actual_port, team_name, interval_sec=1.0)
    broadcaster.start()
    print("Broadcasting offers over UDP every 1s...")

    try:
        while True:
            conn, addr = tcp_sock.accept()
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try:
            broadcaster.stop()
        except Exception:
            pass
        try:
            tcp_sock.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
