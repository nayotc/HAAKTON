import socket

from server.utils import clamp_team_name
from server.game_logic import SUITS
from server.network import RESULT_NOT_OVER, RESULT_WIN, RESULT_LOSS, RESULT_TIE
from .ui import ask_rounds
from .network import open_udp_listener, wait_for_offer, send_request_tcp, send_decision, recv_server_payload

CLIENT_NAME = "TeamJoker"

def card_to_str(rank: int, suit: int) -> str:
    if rank == 1:
        rr = "A"
    elif rank == 11:
        rr = "J"
    elif rank == 12:
        rr = "Q"
    elif rank == 13:
        rr = "K"
    else:
        rr = str(rank)
    suit_name = SUITS[suit] if 0 <= suit < 4 else f"Suit{ suit }"
    return f"{rr} of {suit_name}"

def ask_decision_once() -> str:
    while True:
        d = input("Hit or stand? ").strip().lower()
        if d in ("hit", "h"):
            return "Hit"
        if d in ("stand", "s"):
            return "Stand"
        print("Please type 'hit' or 'stand'.")

def main():
    client_name_32 = clamp_team_name(CLIENT_NAME, 32)

    while True:
        rounds = ask_rounds()

        print("Client started, listening for offer requests...")
        udp = open_udp_listener()
        offer = wait_for_offer(udp)
        udp.close()

        if not offer:
            print("No offers received (timeout). Trying again...\n")
            continue

        server_ip, tcp_port, server_name = offer
        print(f"Received offer from {server_ip} ({server_name}), TCP port {tcp_port}")

        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(10.0)
            tcp.connect((server_ip, tcp_port))
            tcp.settimeout(20.0)

            send_request_tcp(tcp, rounds, client_name_32)

            wins = 0
            finished_rounds = 0

            # When True: we are allowed to ask the user for a decision.
            # When False: we already stood; wait for round result.
            need_decision = True

            while finished_rounds < rounds:
                msg = recv_server_payload(tcp)
                if not msg:
                    print("Disconnected / timeout from server.")
                    break

                result, rank, suit = msg
                print(f"Card: {card_to_str(rank, suit)}")

                if result == RESULT_NOT_OVER:
                    if need_decision:
                        decision = ask_decision_once()
                        send_decision(tcp, decision)
                        if decision == "Stand":
                            need_decision = False
                    else:
                        # We already stood; ignore intermediate dealer updates
                        pass
                else:
                    finished_rounds += 1
                    need_decision = True  # new round starts with a decision again

                    if result == RESULT_WIN:
                        print("Round result: WIN\n")
                        wins += 1
                    elif result == RESULT_LOSS:
                        print("Round result: LOSS\n")
                    else:
                        print("Round result: TIE\n")

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
