import random
from dataclasses import dataclass
from typing import List, Tuple

SUITS = ["Heart", "Diamond", "Club", "Spade"]

@dataclass(frozen=True)
class Card:
    rank: int  # 1-13 (1 = Ace, 11=J, 12=Q, 13=K)
    suit: int  # 0-3

def card_value(rank: int) -> int:
    if rank == 1:
        return 11
    if rank >= 11:
        return 10
    return rank

def card_str(c: Card) -> str:
    r = c.rank
    if r == 1:
        rr = "A"
    elif r == 11:
        rr = "J"
    elif r == 12:
        rr = "Q"
    elif r == 13:
        rr = "K"
    else:
        rr = str(r)
    return f"{rr} of {SUITS[c.suit]}"

class Deck:
    def __init__(self):
        self.cards: List[Card] = [Card(rank, suit) for suit in range(4) for rank in range(1, 14)]
        random.shuffle(self.cards)

    def draw(self) -> Card:
        if not self.cards:
            # fresh shuffled deck if empty
            self.__init__()
        return self.cards.pop()

def total(hand: List[Card]) -> int:
    # Simplified: Ace always 11 (as required)
    return sum(card_value(c.rank) for c in hand)

def dealer_should_hit(dealer_sum: int) -> bool:
    return dealer_sum < 17
