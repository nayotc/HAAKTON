def ask_rounds() -> int:
    while True:
        s = input("How many rounds do you want to play? (1-255) ").strip()
        try:
            x = int(s)
            if 1 <= x <= 255:
                return x
        except Exception:
            pass
        print("Please enter a number between 1 and 255.")
