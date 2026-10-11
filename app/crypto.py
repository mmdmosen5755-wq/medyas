import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config

_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
        _fernet = Fernet(key)
    return _fernet


def enc(value: str) -> str:
    return _f().encrypt(value.encode()).decode() if value else ""


def dec(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _f().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""
