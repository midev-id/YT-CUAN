from cryptography.fernet import Fernet

from . import config

_f = Fernet(config.SECRET_KEY.encode()) if config.SECRET_KEY else None


def enc(text: str) -> str:
    if _f is None:
        raise RuntimeError("SECRET_KEY belum diisi di .env")
    return _f.encrypt(text.encode()).decode()


def dec(token: str) -> str:
    return _f.decrypt(token.encode()).decode()


def mask(key: str) -> str:
    return key[:4] + "…" + key[-2:] if len(key) > 8 else "…"
