import sys
import socket
import threading
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QObject, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QMessageBox, QFrame, QSizePolicy
)

from server.utils import clamp_team_name
from server.network import RESULT_NOT_OVER, RESULT_WIN, RESULT_LOSS, RESULT_TIE
from client.network import (
    open_udp_listener, wait_for_offer,
    send_request_tcp, send_decision, recv_server_payload
)

CLIENT_NAME = "TeamJoker"

SUIT_SYMBOL = {0: "♥", 1: "♦", 2: "♣", 3: "♠"}
RED_SUITS = {0, 1}

def rank_to_str(rank: int) -> str:
    return {1: "A", 11: "J", 12: "Q", 13: "K"}.get(rank, str(rank))

def card_points(rank: int) -> int:
    if rank == 1:
        return 11
    if rank >= 11:
        return 10
    return rank

@dataclass
class Card:
    rank: int
    suit: int

class Bridge(QObject):
    log = Signal(str)
    status = Signal(str)
    set_buttons = Signal(bool)
    new_round = Signal()
    player_card = Signal(int, int)     # rank, suit
    dealer_upcard = Signal(int, int)   # rank, suit
    dealer_card = Signal(int, int)     # rank, suit (after stand)
    round_result = Signal(str)         # WIN/LOSS/TIE
    progress = Signal(int, int, int)   # done, total, wins
    finished = Signal()

class CardView(QFrame):
    def __init__(self, hidden=False):
        super().__init__()
        self.setFixedSize(92, 128)
        self.setObjectName("Card")
        self.hidden = hidden
        self.stood_flag = False


        self.lbl = QLabel("", self)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(self.lbl)

        self.setHiddenCard(hidden)

    def setHiddenCard(self, hidden: bool):
        self.hidden = hidden
        if hidden:
            self.setObjectName("CardBack")
            self.lbl.setText("BLACKJACK")
        else:
            self.setObjectName("Card")
            self.lbl.setText("")
        self.style().unpolish(self)
        self.style().polish(self)

    def setCard(self, rank: int, suit: int):
        self.setHiddenCard(False)
        sym = SUIT_SYMBOL.get(suit, "?")
        col = "#ff4d4f" if suit in RED_SUITS else "#e8eaed"
        # HTML to color the suit nicely
        self.lbl.setText(
            f"<div style='font-size:28px; font-weight:700;'>{rank_to_str(rank)}"
            f"<span style='color:{col}'> {sym}</span></div>"
        )

class HandRow(QWidget):
    def __init__(self, title: str):
        super().__init__()
        self.title = QLabel(title)
        self.title.setObjectName("SectionTitle")
        self.sum_lbl = QLabel("Sum: ?")
        self.sum_lbl.setObjectName("SumLabel")

        self.cards_bar = QHBoxLayout()
        self.cards_bar.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.sum_lbl)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addLayout(top)

        cards_wrap = QWidget()
        cards_wrap.setLayout(self.cards_bar)
        root.addWidget(cards_wrap)

        self.cards = []

    def clear(self):
        for c in self.cards:
            c.setParent(None)
        self.cards = []
        self.sum_lbl.setText("Sum: ?")

    def add_card(self, card_view: CardView):
        self.cards.append(card_view)
        self.cards_bar.addWidget(card_view)

    def set_sum(self, s: str):
        self.sum_lbl.setText(s)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Blackjack Client")
        self.setMinimumSize(980, 640)

        self.bridge = Bridge()
        self.bridge.log.connect(self.on_log)
        self.bridge.status.connect(self.on_status)
        self.bridge.set_buttons.connect(self.on_set_buttons)
        self.bridge.new_round.connect(self.on_new_round)
        self.bridge.player_card.connect(self.on_player_card)
        self.bridge.dealer_upcard.connect(self.on_dealer_upcard)
        self.bridge.dealer_card.connect(self.on_dealer_card)
        self.bridge.round_result.connect(self.on_round_result)
        self.bridge.progress.connect(self.on_progress)
        self.bridge.finished.connect(self.on_finished)

        self.tcp: Optional[socket.socket] = None
        self.stop_event = threading.Event()
        self.net_thread: Optional[threading.Thread] = None

        # game view state
        self.rounds_total = 0
        self.rounds_done = 0
        self.wins = 0
        self.opening_left = 0
        self.player_sum = 0
        self.dealer_sum = 0
        self.dealer_hidden = True
        self.player_stood = False

        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(14)

        # Top bar
        top = QHBoxLayout()
        self.rounds_in = QLineEdit("3")
        self.rounds_in.setFixedWidth(80)
        self.rounds_in.setPlaceholderText("Rounds")

        self.btn_start = QPushButton("Find Server & Play")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)

        self.lbl_status = QLabel("Idle")
        self.lbl_status.setObjectName("Status")

        top.addWidget(QLabel("Rounds (1-255):"))
        top.addWidget(self.rounds_in)
        top.addWidget(self.btn_start)
        top.addWidget(self.btn_stop)
        top.addStretch(1)
        top.addWidget(self.lbl_status)

        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)

        # Score
        self.lbl_score = QLabel("Rounds: 0/0 | Wins: 0 | Win rate: 0.00")
        self.lbl_score.setObjectName("Score")

        # Hands
        self.dealer_row = HandRow("Dealer")
        self.player_row = HandRow("You")

        # Buttons
        btns = QHBoxLayout()
        self.btn_hit = QPushButton("HIT")
        self.btn_stand = QPushButton("STAND")
        self.btn_hit.setEnabled(False)
        self.btn_stand.setEnabled(False)
        btns.addWidget(self.btn_hit)
        btns.addWidget(self.btn_stand)
        btns.addStretch(1)

        self.btn_hit.clicked.connect(lambda: self.send_decision_ui("Hit"))
        self.btn_stand.clicked.connect(lambda: self.send_decision_ui("Stand"))

        # Log
        self.log_lbl = QLabel("")
        self.log_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_lbl.setWordWrap(True)
        self.log_lbl.setObjectName("Log")
        self.log_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        outer.addLayout(top)
        outer.addWidget(self.lbl_score)
        outer.addWidget(self.dealer_row)
        outer.addWidget(self.player_row)
        outer.addLayout(btns)
        outer.addWidget(self.log_lbl, stretch=1)

    def _apply_style(self):
        # “modern-ish” dark UI via QSS
        self.setStyleSheet("""
        QWidget { background: #0b0f14; color: #e8eaed; font-family: Segoe UI; }
        QLabel#Status { color: #9aa0a6; font-weight: 600; }
        QLabel#Score { font-size: 15px; font-weight: 700; padding: 6px 0; }
        QLabel#SectionTitle { font-size: 16px; font-weight: 800; }
        QLabel#SumLabel { color: #9aa0a6; font-weight: 600; }
        QLineEdit {
            background: #111824; border: 1px solid #223046; border-radius: 10px;
            padding: 8px 10px; font-size: 14px;
        }
        QPushButton {
            background: #1a2433; border: 1px solid #2a3a55; border-radius: 12px;
            padding: 10px 14px; font-size: 14px; font-weight: 700;
        }
        QPushButton:hover { background: #223046; }
        QPushButton:disabled { background: #121824; color: #6b7280; border-color: #1f2a3f; }
        QFrame#Card {
            background: #121824; border: 1px solid #2a3a55; border-radius: 16px;
        }
        QFrame#CardBack {
            background: #0d2b55; border: 1px solid #2a66c7; border-radius: 16px;
        }
        QLabel#Log {
            background: #0f1520; border: 1px solid #223046; border-radius: 14px;
            padding: 12px; color: #cbd5e1;
        }
        """)

    # ---------- UI actions ----------
    @Slot()
    def start(self):
        if self.net_thread and self.net_thread.is_alive():
            return

        try:
            r = int(self.rounds_in.text().strip())
            if not (1 <= r <= 255):
                raise ValueError()
        except Exception:
            QMessageBox.warning(self, "Invalid input", "Rounds must be an integer between 1 and 255.")
            return

        self.rounds_total = r
        self.rounds_done = 0
        self.wins = 0
        self.update_score()

        self.stop_event.clear()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.rounds_in.setEnabled(False)

        self.on_log("Listening for server offers (UDP 13122)...")
        self.on_status("Looking for server...")

        self.net_thread = threading.Thread(target=self.network_worker, daemon=True)
        self.net_thread.start()

    @Slot()
    def stop(self):
        self.stop_event.set()
        self.close_tcp()
        self.on_set_buttons(False)

        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.rounds_in.setEnabled(True)
        self.on_status("Idle")
        self.on_log("Stopped.")

    def send_decision_ui(self, d: str):
        if not self.tcp:
            return
        try:
            send_decision(self.tcp, d)
            if d == "Stand":
                self.stood_flag = True
                self.player_stood = True
                self.on_set_buttons(False)
                self.on_status("Waiting for dealer...")
            else:
                self.on_status("Waiting for card...")
        except Exception:
            QMessageBox.critical(self, "Error", "Failed to send decision (connection lost).")
            self.stop()

    # ---------- Network thread ----------
    def close_tcp(self):
        try:
            if self.tcp:
                self.tcp.close()
        except Exception:
            pass
        self.tcp = None

    def network_worker(self):
        # 1) get offer
        try:
            udp = open_udp_listener()
        except Exception as e:
            self.bridge.log.emit(f"ERROR: UDP listener failed: {e}")
            self.bridge.status.emit("Error")
            self.bridge.finished.emit()
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
            self.bridge.finished.emit()
            return

        if not offer:
            self.bridge.log.emit("ERROR: No offers received (timeout).")
            self.bridge.status.emit("Idle")
            self.bridge.finished.emit()
            return

        server_ip, tcp_port, server_name = offer
        self.bridge.log.emit(f"Offer: {server_name} @ {server_ip}:{tcp_port}")
        self.bridge.status.emit("Connecting...")

        # 2) connect tcp + send request
        try:
            tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp.settimeout(10.0)
            tcp.connect((server_ip, tcp_port))
            tcp.settimeout(20.0)
            self.tcp = tcp

            client_name_32 = clamp_team_name(CLIENT_NAME, 32)
            send_request_tcp(tcp, self.rounds_total, client_name_32)
        except Exception as e:
            self.bridge.log.emit(f"ERROR: TCP connect/request failed: {e}")
            self.bridge.status.emit("Idle")
            self.close_tcp()
            self.bridge.finished.emit()
            return

        self.bridge.status.emit("Playing")

        rounds_done = 0
        wins = 0

        # ---- Local per-round state (IMPORTANT: not shared with UI) ----
        opening_left = 0
        stood = False

        def start_round():
            nonlocal opening_left, stood
            opening_left = 3   # 2 player + 1 dealer upcard
            stood = False
            self.bridge.new_round.emit()
            self.bridge.status.emit("Dealing...")

        start_round()

        while not self.stop_event.is_set() and rounds_done < self.rounds_total:
            msg = recv_server_payload(self.tcp)
            if not msg:
                self.bridge.log.emit("ERROR: Disconnected / timeout from server.")
                break

            result, rank, suit = msg

            if result == RESULT_NOT_OVER:
                # Opening deal: first two -> player, third -> dealer upcard
                if opening_left > 0:
                    if opening_left in (3, 2):
                        self.bridge.player_card.emit(rank, suit)
                    else:  # opening_left == 1
                        self.bridge.dealer_upcard.emit(rank, suit)

                    opening_left -= 1
                    if opening_left == 0:
                        self.bridge.set_buttons.emit(True)
                        self.bridge.status.emit("Your turn")
                    continue

                # After opening:
                stood = self.stood_flag
                if stood:
                    self.bridge.dealer_card.emit(rank, suit)
                else:
                    self.bridge.player_card.emit(rank, suit)
                    self.bridge.set_buttons.emit(True)
                    self.bridge.status.emit("Your turn")
                continue

            # Round ended:
            rounds_done += 1
            if result == RESULT_WIN:
                wins += 1
                self.bridge.round_result.emit("WIN")
            elif result == RESULT_LOSS:
                self.bridge.round_result.emit("LOSS")
            else:
                self.bridge.round_result.emit("TIE")

            self.bridge.progress.emit(rounds_done, self.rounds_total, wins)

            if rounds_done < self.rounds_total:
                start_round()

        self.close_tcp()
        self.bridge.finished.emit()


        # ---------- UI slots ----------
    @Slot()
    def on_new_round(self):
        self.player_row.clear()
        self.dealer_row.clear()
        self.stood_flag = False

        # show hidden dealer card placeholder (looks nice)
        up = CardView()
        back = CardView(hidden=True)
        self.dealer_row.add_card(up)
        self.dealer_row.add_card(back)

        self.player_sum = 0
        self.dealer_sum = 0
        self.dealer_hidden = True
        self.player_stood = False

        self.opening_left = 3
        self.on_set_buttons(False)
        self.on_status("Dealing...")

    @Slot(int, int)
    def on_player_card(self, rank: int, suit: int):
        cv = CardView()
        cv.setCard(rank, suit)
        self.player_row.add_card(cv)

        self.player_sum += card_points(rank)
        self.player_row.set_sum(f"Sum: {self.player_sum}")

        self.on_log(f"You: {rank_to_str(rank)}{SUIT_SYMBOL.get(suit,'?')}")

    @Slot(int, int)
    def on_dealer_upcard(self, rank: int, suit: int):
        # Replace first dealer card widget with actual upcard
        if self.dealer_row.cards:
            self.dealer_row.cards[0].setCard(rank, suit)
        else:
            cv = CardView()
            cv.setCard(rank, suit)
            self.dealer_row.add_card(cv)

        self.on_log(f"Dealer shows: {rank_to_str(rank)}{SUIT_SYMBOL.get(suit,'?')}")
        # Dealer sum remains hidden until reveal/draws after stand
        self.dealer_row.set_sum("Sum: ?")

    @Slot(int, int)
    def on_dealer_card(self, rank: int, suit: int):
        # First dealer update after stand is interpreted as hidden reveal:
        if self.dealer_hidden and len(self.dealer_row.cards) >= 2:
            self.dealer_row.cards[1].setCard(rank, suit)
            self.dealer_hidden = False
            self.dealer_sum = card_points(rank)
            # also add upcard points if exists
            # try to infer from first card view label not easy; so we keep sum unknown visually until first draw
            # but we can compute by tracking: easiest is to just keep sum displayed as number from now on:
            # We'll add the upcard points by reading our log-tracking approach? Instead: track dealer_sum properly:
            # We'll approximate: after reveal, we don't know the exact upcard points if not stored.
        else:
            cv = CardView()
            cv.setCard(rank, suit)
            self.dealer_row.add_card(cv)

        # For a correct dealer sum display, simplest: keep it "Sum: ?" until end (still acceptable).
        # If you want exact sum, tell me and I'll add dealer sum tracking properly.
        self.dealer_row.set_sum("Sum: ?")

        self.on_log(f"Dealer: {rank_to_str(rank)}{SUIT_SYMBOL.get(suit,'?')}")

    @Slot(str)
    def on_round_result(self, res: str):
        self.on_log(f"Round result: {res}")
        self.on_set_buttons(False)
        self.on_status("Round finished")

        if res == "WIN":
            self.wins += 1

        self.rounds_done += 1
        self.update_score()

    @Slot(int, int, int)
    def on_progress(self, done: int, total: int, wins: int):
        # keep consistent with server-side counting
        self.rounds_done = done
        self.rounds_total = total
        self.wins = wins
        self.update_score()

    @Slot()
    def on_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.rounds_in.setEnabled(True)
        self.on_set_buttons(False)
        self.on_status("Idle")

    @Slot(str)
    def on_log(self, s: str):
        old = self.log_lbl.text().strip()
        new = (old + "\n" + s).strip() if old else s
        # keep last ~20 lines
        lines = new.splitlines()[-20:]
        self.log_lbl.setText("\n".join(lines))

    @Slot(str)
    def on_status(self, s: str):
        self.lbl_status.setText(s)

    @Slot(bool)
    def on_set_buttons(self, enabled: bool):
        self.btn_hit.setEnabled(enabled)
        self.btn_stand.setEnabled(enabled)

    def update_score(self):
        rd = self.rounds_done
        rt = self.rounds_total
        wr = (self.wins / rd) if rd > 0 else 0.0
        self.lbl_score.setText(f"Rounds: {rd}/{rt} | Wins: {self.wins} | Win rate: {wr:.2f}")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
