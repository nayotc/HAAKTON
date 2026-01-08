import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox

from server.utils import clamp_team_name
from server.network import (
    RESULT_NOT_OVER, RESULT_WIN, RESULT_LOSS, RESULT_TIE
)
from client.network import (
    open_udp_listener,
    wait_for_offer,
    send_request_tcp,
    send_decision,
    recv_server_payload,
)

CLIENT_NAME = "TeamJoker"

# suit encoding in protocol: 0-3 = H D C S (per assignment)
SUIT_SYMBOL = {0: "♥", 1: "♦", 2: "♣", 3: "♠"}
SUIT_COLOR  = {0: "red", 1: "red", 2: "black", 3: "black"}

def rank_to_str(rank: int) -> str:
    if rank == 1:
        return "A"
    if rank == 11:
        return "J"
    if rank == 12:
        return "Q"
    if rank == 13:
        return "K"
    return str(rank)

def card_label(rank: int, suit: int) -> str:
    return f"{rank_to_str(rank)}{SUIT_SYMBOL.get(suit, '?')}"

def card_value(rank: int) -> int:
    if rank == 1:
        return 11
    if rank >= 11:
        return 10
    return rank

def clamp_rounds(s: str) -> int:
    x = int(s.strip())
    if not (1 <= x <= 255):
        raise ValueError()
    return x


class CardWidget(ttk.Frame):
    """A visual playing card: rounded-ish frame + big rank/suit."""
    def __init__(self, parent, rank=None, suit=None, hidden=False):
        super().__init__(parent)
        self.rank = rank
        self.suit = suit
        self.hidden = hidden

        # Use a Tk Canvas to draw a card-like rectangle
        self.canvas = tk.Canvas(self, width=72, height=96, highlightthickness=0)
        self.canvas.pack()
        self._draw()

    def set_card(self, rank, suit, hidden=False):
        self.rank = rank
        self.suit = suit
        self.hidden = hidden
        self._draw()

    def _draw(self):
        self.canvas.delete("all")

        # background / border
        if self.hidden:
            bg = "#2d6cdf"
            border = "#1c3f8a"
        else:
            bg = "white"
            border = "#444444"

        self.canvas.create_rectangle(4, 4, 68, 92, fill=bg, outline=border, width=2)
        self.canvas.create_rectangle(6, 6, 66, 90, fill=bg, outline=border, width=1)

        if self.hidden:
            # simple "pattern"
            for y in range(12, 88, 8):
                self.canvas.create_line(10, y, 62, y, fill="#b9d0ff")
            self.canvas.create_text(36, 48, text="BJ", fill="white", font=("Segoe UI", 16, "bold"))
            return

        r = rank_to_str(self.rank) if self.rank is not None else "?"
        s = SUIT_SYMBOL.get(self.suit, "?")
        col = SUIT_COLOR.get(self.suit, "black")

        # corners
        self.canvas.create_text(14, 16, text=r, fill=col, font=("Segoe UI", 14, "bold"))
        self.canvas.create_text(16, 32, text=s, fill=col, font=("Segoe UI", 14, "bold"))

        self.canvas.create_text(58, 80, text=r, fill=col, font=("Segoe UI", 14, "bold"))
        self.canvas.create_text(56, 64, text=s, fill=col, font=("Segoe UI", 14, "bold"))

        # center suit
        self.canvas.create_text(36, 48, text=s, fill=col, font=("Segoe UI", 28, "bold"))


class GuiClientApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Blackjack (GUI Client)")
        self.root.geometry("820x540")

        self.client_name_32 = clamp_team_name(CLIENT_NAME, 32)

        # Thread/event queue
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.net_thread = None
        self.tcp = None

        # Round state
        self.rounds_target = 0
        self.rounds_done = 0
        self.wins = 0

        # Per-round view state
        self.opening_cards_left = 0   # 3 at round start
        self.player_cards = []
        self.dealer_cards = []        # first is shown, second maybe hidden until reveal
        self.dealer_hidden_exists = False
        self.player_stood = False     # after stand, wait until result (ignore decision prompts)

        self._build_ui()
        self._poll_events()

    # ---------- UI ----------
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main = ttk.Frame(self.root, padding=12)
        main.grid(row=0, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # Top controls
        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(6, weight=1)

        ttk.Label(top, text="Rounds (1-255):").grid(row=0, column=0, sticky="w")
        self.rounds_var = tk.StringVar(value="3")
        self.rounds_entry = ttk.Entry(top, width=8, textvariable=self.rounds_var)
        self.rounds_entry.grid(row=0, column=1, padx=6)

        self.btn_start = ttk.Button(top, text="Find Server & Play", command=self.on_start)
        self.btn_start.grid(row=0, column=2, padx=6)

        self.btn_stop = ttk.Button(top, text="Stop", command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=3, padx=6)

        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(top, textvariable=self.status_var).grid(row=0, column=6, sticky="e")

        # Table area
        table = ttk.Frame(main)
        table.grid(row=1, column=0, sticky="ew", pady=(12, 6))
        table.columnconfigure(0, weight=1)

        self.score_var = tk.StringVar(value="Rounds: 0/0 | Wins: 0 | Win rate: 0.00")
        ttk.Label(table, textvariable=self.score_var, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")

        # Hands area
        hands = ttk.Frame(main)
        hands.grid(row=2, column=0, sticky="nsew")
        hands.columnconfigure(0, weight=1)
        hands.rowconfigure(0, weight=1)
        hands.rowconfigure(1, weight=1)

        # Dealer
        dealer_box = ttk.LabelFrame(hands, text="Dealer")
        dealer_box.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        dealer_box.columnconfigure(0, weight=1)

        self.dealer_sum_var = tk.StringVar(value="Sum: ?")
        ttk.Label(dealer_box, textvariable=self.dealer_sum_var).grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))

        self.dealer_cards_frame = ttk.Frame(dealer_box)
        self.dealer_cards_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        # Player
        player_box = ttk.LabelFrame(hands, text="You")
        player_box.grid(row=1, column=0, sticky="nsew")
        player_box.columnconfigure(0, weight=1)

        self.player_sum_var = tk.StringVar(value="Sum: 0")
        ttk.Label(player_box, textvariable=self.player_sum_var).grid(row=0, column=0, sticky="w", padx=10, pady=(6, 0))

        self.player_cards_frame = ttk.Frame(player_box)
        self.player_cards_frame.grid(row=1, column=0, sticky="w", padx=10, pady=10)

        # Bottom controls
        bottom = ttk.Frame(main)
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(3, weight=1)

        self.btn_hit = ttk.Button(bottom, text="HIT", command=lambda: self.send_user_decision("Hit"), state="disabled")
        self.btn_hit.grid(row=0, column=0, padx=6)

        self.btn_stand = ttk.Button(bottom, text="STAND", command=lambda: self.send_user_decision("Stand"), state="disabled")
        self.btn_stand.grid(row=0, column=1, padx=6)

        self.info_var = tk.StringVar(value="Press 'Find Server & Play' to start.")
        ttk.Label(bottom, textvariable=self.info_var).grid(row=0, column=3, sticky="e")

        # Log (small)
        log_box = ttk.LabelFrame(main, text="Log")
        log_box.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        log_box.columnconfigure(0, weight=1)

        self.log = tk.Text(log_box, height=6, wrap="word", state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")

    def append_log(self, s: str):
        self.log.configure(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, s: str):
        self.status_var.set(s)

    def set_info(self, s: str):
        self.info_var.set(s)

    def set_score(self):
        rt = self.rounds_target
        rd = self.rounds_done
        wr = (self.wins / rd) if rd > 0 else 0.0
        self.score_var.set(f"Rounds: {rd}/{rt} | Wins: {self.wins} | Win rate: {wr:.2f}")

    def enable_decisions(self, enabled: bool):
        st = "normal" if enabled else "disabled"
        self.btn_hit.configure(state=st)
        self.btn_stand.configure(state=st)

    def clear_hand_frames(self):
        for child in self.dealer_cards_frame.winfo_children():
            child.destroy()
        for child in self.player_cards_frame.winfo_children():
            child.destroy()

    def render_hands(self):
        # Clear and re-render
        self.clear_hand_frames()

        # Dealer cards
        for i, (rank, suit, hidden) in enumerate(self.dealer_cards):
            cw = CardWidget(self.dealer_cards_frame, rank=rank, suit=suit, hidden=hidden)
            cw.grid(row=0, column=i, padx=6)

        # Player cards
        for i, (rank, suit) in enumerate(self.player_cards):
            cw = CardWidget(self.player_cards_frame, rank=rank, suit=suit, hidden=False)
            cw.grid(row=0, column=i, padx=6)

        # Sums
        psum = sum(card_value(r) for (r, _) in self.player_cards)
        self.player_sum_var.set(f"Sum: {psum}")

        if self.dealer_hidden_exists:
            self.dealer_sum_var.set("Sum: ?")
        else:
            dsum = sum(card_value(r) for (r, s, h) in self.dealer_cards)
            self.dealer_sum_var.set(f"Sum: {dsum}")

    # ---------- Controls ----------
    def on_start(self):
        try:
            r = clamp_rounds(self.rounds_var.get())
        except Exception:
            messagebox.showerror("Invalid input", "Rounds must be an integer between 1 and 255.")
            return

        self.rounds_target = r
        self.rounds_done = 0
        self.wins = 0
        self.set_score()

        # Reset view state
        self.player_cards = []
        self.dealer_cards = []
        self.dealer_hidden_exists = False
        self.player_stood = False
        self.opening_cards_left = 0
        self.render_hands()

        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.rounds_entry.configure(state="disabled")
        self.enable_decisions(False)
        self.set_info("Listening for offers...")
        self.set_status("Looking for server...")

        self.append_log("Client: Listening for server offers (UDP 13122)...")

        self.net_thread = threading.Thread(target=self._network_worker, daemon=True)
        self.net_thread.start()

    def on_stop(self):
        self.stop_event.set()
        self.enable_decisions(False)
        self._close_tcp()

        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.rounds_entry.configure(state="normal")
        self.set_status("Idle.")
        self.set_info("Stopped.")
        self.append_log("Client: Stopped.")

    def _close_tcp(self):
        try:
            if self.tcp:
                self.tcp.close()
        except Exception:
            pass
        self.tcp = None

    def start_new_round_ui(self):
        self.player_cards = []
        self.dealer_cards = []
        self.dealer_hidden_exists = True     # dealer has a hidden second card (until revealed)
        self.player_stood = False
        self.opening_cards_left = 3          # 2 player + 1 dealer upcard
        self.enable_decisions(False)
        self.set_info("Dealing cards...")
        self.render_hands()

    def reveal_dealer_hidden(self, rank, suit):
        # Replace the hidden placeholder with real card, and mark dealer no longer hidden
        for idx, (r, s, hidden) in enumerate(self.dealer_cards):
            if hidden:
                self.dealer_cards[idx] = (rank, suit, False)
                break
        else:
            # if no hidden card exists in list, just add it (robust)
            self.dealer_cards.append((rank, suit, False))
        self.dealer_hidden_exists = False

    def ensure_dealer_has_hidden_placeholder(self):
        # Add a hidden card placeholder if not already there (for nicer look at start)
        if not any(hidden for (_, _, hidden) in self.dealer_cards):
            self.dealer_cards.append((None, None, True))

    def send_user_decision(self, decision: str):
        if not self.tcp:
            return
        try:
            send_decision(self.tcp, decision)
            if decision == "Stand":
                self.player_stood = True
                self.enable_decisions(False)
                self.set_info("Waiting for dealer...")
            else:
                self.set_info("Hit sent. Waiting for card...")
        except Exception:
            self.events.put(("error", "Failed to send decision (connection lost)."))

    # ---------- Networking Worker ----------
    def _network_worker(self):
        # 1) Wait for offer
        try:
            udp = open_udp_listener()
        except Exception as e:
            self.events.put(("error", f"UDP listener failed: {e}"))
            return

        offer = None
        while not self.stop_event.is_set():
            offer = wait_for_offer(udp)
            if offer:
                break

        try:
            udp.close()
        except Exception:
            pass

        if self.stop_event.is_set():
            return

        if not offer:
            self.events.put(("error", "No offers received (timeout)."))
            return

        server_ip, tcp_port, server_name = offer
        self.events.put(("log", f"Offer: {server_name} @ {server_ip}:{tcp_port}"))
        self.events.put(("status", "Connecting..."))
        self.events.put(("info", "Connecting to server..."))

        # 2) Connect TCP + send request
        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(10.0)
            tcp.connect((server_ip, tcp_port))
            tcp.settimeout(20.0)
            self.tcp = tcp
            send_request_tcp(tcp, self.rounds_target, self.client_name_32)
        except Exception as e:
            self.events.put(("error", f"TCP connect/request failed: {e}"))
            self._close_tcp()
            return

        self.events.put(("status", "Playing"))
        self.events.put(("start_round", None))

        rounds_done = 0
        wins = 0

        while not self.stop_event.is_set() and rounds_done < self.rounds_target:
            msg = recv_server_payload(self.tcp)
            if not msg:
                self.events.put(("error", "Disconnected / timeout from server."))
                break

            result, rank, suit = msg
            self.events.put(("payload", (result, rank, suit)))

            if result != RESULT_NOT_OVER:
                rounds_done += 1
                if result == RESULT_WIN:
                    wins += 1
                    self.events.put(("round_result", "WIN"))
                elif result == RESULT_LOSS:
                    self.events.put(("round_result", "LOSS"))
                else:
                    self.events.put(("round_result", "TIE"))

                self.events.put(("progress", (rounds_done, wins)))

                if rounds_done < self.rounds_target:
                    self.events.put(("start_round", None))

        self._close_tcp()
        self.events.put(("done", (rounds_done, wins)))

    # ---------- Event handling ----------
    def _poll_events(self):
        try:
            while True:
                typ, payload = self.events.get_nowait()

                if typ == "log":
                    self.append_log(str(payload))
                elif typ == "status":
                    self.set_status(str(payload))
                elif typ == "info":
                    self.set_info(str(payload))
                elif typ == "error":
                    self.append_log(f"ERROR: {payload}")
                    self.set_status("Error")
                    self.set_info("Error.")
                    messagebox.showerror("Error", str(payload))
                    self.on_stop()

                elif typ == "start_round":
                    self.append_log("New round started.")
                    self.start_new_round_ui()
                    # show dealer has 2 cards with one hidden (visual)
                    self.ensure_dealer_has_hidden_placeholder()
                    self.render_hands()

                elif typ == "payload":
                    result, rank, suit = payload

                    # Opening deal logic:
                    # Server sends 2 player cards + 1 dealer upcard at round start.
                    if result == RESULT_NOT_OVER and self.opening_cards_left > 0:
                        # First 2 are player's cards, third is dealer upcard
                        if len(self.player_cards) < 2:
                            self.player_cards.append((rank, suit))
                            self.append_log(f"You got: {card_label(rank, suit)}")
                        else:
                            # dealer upcard
                            # put it as the first visible dealer card; keep hidden placeholder as second
                            # if dealer list currently only has hidden placeholder, insert at front
                            # store dealer card as (rank, suit, hidden=False)
                            if self.dealer_cards and self.dealer_cards[0][2] is True:
                                # hidden placeholder exists at index 0; put visible before it
                                self.dealer_cards.insert(0, (rank, suit, False))
                            else:
                                # if already has some visible, append
                                self.dealer_cards.insert(0, (rank, suit, False))

                            self.append_log(f"Dealer shows: {card_label(rank, suit)}")

                        self.opening_cards_left -= 1
                        self.render_hands()

                        if self.opening_cards_left == 0:
                            self.enable_decisions(True)
                            self.set_info("Your turn: HIT or STAND")
                        continue

                    # After opening deal:
                    if result == RESULT_NOT_OVER:
                        # If player already stood, treat these as dealer updates
                        if self.player_stood:
                            # Dealer card revealed or drawn
                            if self.dealer_hidden_exists and any(h for (_, _, h) in self.dealer_cards):
                                # interpret first dealer update after stand as reveal of hidden card
                                self.reveal_dealer_hidden(rank, suit)
                                self.append_log(f"Dealer reveals: {card_label(rank, suit)}")
                            else:
                                self.dealer_cards.append((rank, suit, False))
                                self.append_log(f"Dealer draws: {card_label(rank, suit)}")
                            self.render_hands()
                            continue

                        # Player is still making decisions -> this is usually a card for the player after Hit
                        self.player_cards.append((rank, suit))
                        self.append_log(f"You drew: {card_label(rank, suit)}")
                        self.render_hands()
                        self.enable_decisions(True)
                        self.set_info("Your turn: HIT or STAND")
                        continue

                    # Round result message (WIN/LOSS/TIE)
                    if result == RESULT_WIN:
                        self.append_log("Round result: WIN\n")
                    elif result == RESULT_LOSS:
                        self.append_log("Round result: LOSS\n")
                    else:
                        self.append_log("Round result: TIE\n")

                    # When round ends, disable decisions until next round starts
                    self.enable_decisions(False)
                    self.set_info("Round finished.")

                elif typ == "round_result":
                    # already logged above; keep here for completeness
                    pass

                elif typ == "progress":
                    self.rounds_done, self.wins = payload
                    self.set_score()

                elif typ == "done":
                    rd, w = payload
                    self.rounds_done, self.wins = rd, w
                    self.set_score()
                    self.set_status("Idle.")
                    self.set_info("Session finished. You can start again.")
                    self.btn_start.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.rounds_entry.configure(state="normal")
                    self.enable_decisions(False)
                    self.append_log("Session finished.\n")

        except queue.Empty:
            pass

        self.root.after(80, self._poll_events)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    GuiClientApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
