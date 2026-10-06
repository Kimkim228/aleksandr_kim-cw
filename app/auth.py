import base64
import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from .config import SECRET_KEY, TOKEN_TTL_SECONDS


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def make_token(user_id: int, role: str) -> str:
    payload = f"{user_id}|{role}|{int(time.time()) + TOKEN_TTL_SECONDS}"
    sig = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def parse_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user_id, role, exp, sig = raw.split("|")
    except Exception:
        return None
    payload = f"{user_id}|{role}|{exp}"
    good = hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or int(exp) < time.time():
        return None
    return {"id": int(user_id), "role": role}


def current_user(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="authorization required")
    user = parse_token(authorization[7:])
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user
