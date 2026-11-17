"""账号密码与登录令牌。

密码用 argon2id 哈希；令牌是随机串，库里只存它的 SHA-256（认证会话表）。
"""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError

_hasher = PasswordHasher()

# 管理员建号 / 首次初始化统一用这个初始密码，首次登录强制重置
DEFAULT_PASSWORD = "666666"
MIN_PASSWORD_LEN = 6
TOKEN_TTL_DAYS = 30


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash or not password:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_new_password(password: str) -> str | None:
    """返回错误文案；None 表示通过。"""
    pw = (password or "").strip()
    if len(pw) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if len(pw) > 128:
        return "密码不能超过 128 位"
    if pw == DEFAULT_PASSWORD:
        return "不能继续使用初始密码 666666"
    return None
