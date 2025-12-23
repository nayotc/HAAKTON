import os

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default

def clamp_team_name(name: str, n: int = 32) -> bytes:
    b = name.encode("utf-8", errors="ignore")
    if len(b) > n:
        return b[:n]
    return b + b"\x00" * (n - len(b))
