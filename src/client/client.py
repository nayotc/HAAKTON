import socket
import struct

MAGIC_COOKIE = 0xabcddcba

TYPE_OFFER   = 0x2
TYPE_REQUEST = 0x3
TYPE_PAYLOAD = 0x4

RESULT_NOT_OVER = 0x0
RESULT_TIE      = 0x1
RESULT_LOSS     = 0x2
RESULT_WIN      = 0x3

UDP_PORT = 13122
SUITS = ["Heart", "Diamond", "Club", "Spade"]

CLIENT_NAME = "TeamJoker"


def clamp_name(name: str, n: int = 32) -> bytes:
    b = name.encode("utf-8", errors="ignore")[:n]
    return b + b"\x00" * (n - len(b))


def card_to_str(rank: int, suit: int) -> str:
    rr = {1: "A", 11: "J", 12: "Q", 13: "K"}.get(rank, str(rank))
    suit_name = SUITS[suit] if 0 <= suit < 4 else f"Suit{suit}"
    return f"{rr} of {suit_name}"


def card_value(rank: int) -> int:
    if rank == 1:
        return 11
    if rank >= 11:
        return 10
    return rank


def recv_exact(sock: socket.socket, n: int):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def pack_request(rounds: int, client_name: str) -> bytes:
    return struct.pack("!IBB32s", MAGIC_COOKIE, TYPE_REQUEST, rounds, clamp_name(client_name))


def pack_decision(decision5: str) -> bytes:
    b = decision5.encode("ascii", errors="ignore")[:5]
    if len(b) != 5:
        b = (b + b" " * 5)[:5]
    return struct.pack("!IB", MAGIC_COOKIE, TYPE_PAYLOAD) + b


def unpack_offer(data: bytes):
    if len(data) != 4 + 1 + 2 + 32:
        return None
    cookie, mtype, port, name = struct.unpack("!IBH32s", data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_OFFER:
        return None
    server_name = name.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    return port, server_name


def recv_server_payload(tcp: socket.socket):
    data = recv_exact(tcp, 9)  # cookie(4) type(1) result(1) rank(2) suit(1)
    if not data:
        return None
    cookie, mtype, result, rank, suit = struct.unpack("!IBBHB", data)
    if cookie != MAGIC_COOKIE or mtype != TYPE_PAYLOAD:
        return None
    return result, rank, suit


def ask_rounds() -> int:
    while True:
        s = input("How many rounds do you want to play? (1-255) ").strip()
        try:
            r = int(s)
            if 1 <= r <= 255:
                return r
        except:
            pass
        print("Please enter a number between 1 and 255.")


def ask_decision_once() -> str:
    while True:
        d = input("Hit or stand? ").strip().lower()
        if d in ("hit", "h", "hittt"):
            return "Hittt"
        if d in ("stand", "s"):
            return "Stand"
        print("Please type 'hit' or 'stand'.")


def wait_for_offer(timeout_sec: float = 10.0):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("", UDP_PORT))
    udp.settimeout(timeout_sec)

    try:
        while True:
            data, (ip, _) = udp.recvfrom(1024)
            offer = unpack_offer(data)
            if offer:
                port, name = offer
                return ip, port, name
    except socket.timeout:
        return None
    finally:
        udp.close()


def main():
    while True:
        rounds = ask_rounds()

        print("Client started, listening for offer requests...")
        offer = wait_for_offer(timeout_sec=10.0)
        if not offer:
            print("No offers received (timeout). Trying again...\n")
            continue

        server_ip, tcp_port, server_name = offer
        print(f"Received offer from {server_ip} ({server_name}), TCP port {tcp_port}")

        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(10.0)
            tcp.connect((server_ip, tcp_port))
            tcp.settimeout(120.0)

            tcp.sendall(pack_request(rounds, CLIENT_NAME))

            wins = 0
            finished_rounds = 0

            # per-round state
            startup_left = 3
            stood = False
            hit_pending = False
            dealer_revealed = False
            player_sum = 0
            busted = False
            reached_21 = False

            round_active = False

            while finished_rounds < rounds:
                msg = recv_server_payload(tcp)
                if not msg:
                    print("Disconnected / timeout from server.")
                    break

                result, rank, suit = msg

                # final result: count once
                if result != RESULT_NOT_OVER:
                    if not round_active:
                        continue

                    if result == RESULT_WIN:
                        print("Round result: WIN\n")
                        wins += 1
                    elif result == RESULT_LOSS:
                        print("Round result: LOSS\n")
                    else:
                        print("Round result: TIE\n")

                    finished_rounds += 1
                    round_active = False

                    # reset next round
                    startup_left = 3
                    stood = False
                    hit_pending = False
                    dealer_revealed = False
                    player_sum = 0
                    busted = False
                    reached_21 = False
                    continue

                # NOT_OVER printing + sums
                if startup_left == 3:
                    print(f"Card: {card_to_str(rank, suit)}")
                    player_sum = card_value(rank)

                elif startup_left == 2:
                    print(f"Card: {card_to_str(rank, suit)}")
                    player_sum += card_value(rank)
                    print(f"Player sum: {player_sum}")
                    if player_sum > 21:
                        print(f"Player busts with {player_sum}")
                        busted = True
                    elif player_sum == 21:
                        print("Player hits 21!")
                        reached_21 = True

                elif startup_left == 1:
                    print(f"Dealer shows: {card_to_str(rank, suit)}")

                else:
                    if hit_pending:
                        print(f"Card: {card_to_str(rank, suit)}")
                        player_sum += card_value(rank)
                        print(f"Player sum: {player_sum}")
                        hit_pending = False

                        if player_sum > 21:
                            print(f"Player busts with {player_sum}")
                            busted = True
                        elif player_sum == 21:
                            print("Player hits 21!")
                            reached_21 = True
                    else:
                        if stood and not dealer_revealed:
                            print(f"Dealer reveals: {card_to_str(rank, suit)}")
                            dealer_revealed = True
                        elif stood:
                            print(f"Dealer draws: {card_to_str(rank, suit)}")
                        else:
                            print(f"Card: {card_to_str(rank, suit)}")

                # consume startup messages
                if startup_left > 0:
                    startup_left -= 1
                    if startup_left > 0:
                        continue
                    round_active = True

                # decisions (do NOT ask if stood/hit_pending/busted/reached_21)
                if stood or hit_pending or busted or reached_21:
                    continue

                decision = ask_decision_once()
                tcp.sendall(pack_decision(decision))

                if decision == "Stand":
                    stood = True
                    dealer_revealed = False
                else:
                    hit_pending = True

            if finished_rounds > 0:
                win_rate = wins / finished_rounds
                print(f"Finished playing {finished_rounds} rounds, win rate: {win_rate:.2f}")

        except Exception as e:
            print(f"Client error: {e}")
        finally:
            try:
                tcp.close()
            except Exception:
                pass

        print("\nReturning to offers...\n")


if __name__ == "__main__":
    main()
