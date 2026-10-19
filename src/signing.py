import hashlib
import hmac
import time


def make_sig(secret: str, token: str, exp: int) -> str:
    return hmac.new(secret.encode(), f"{token}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_query(secret: str, token: str, ttl: int = 21600, now: int | None = None) -> str:
    exp = int(now if now is not None else time.time()) + int(ttl)
    return f"exp={exp}&sig={make_sig(secret, token, exp)}"


def verify(secret: str, token: str, exp: str, sig: str, now: int | None = None) -> bool:
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_i < int(now if now is not None else time.time()):
        return False
    return hmac.compare_digest(make_sig(secret, token, exp_i), sig or "")


def stream_path(secret: str, token: str, ttl: int = 21600, ext: str = "") -> str:
    suffix = f".{ext.lstrip('.')}" if ext else ""
    return f"/s/{token}{suffix}?{signed_query(secret, token, ttl)}"
