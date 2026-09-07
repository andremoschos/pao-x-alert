#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small encrypted recipient vault for the public direct-news repository.

Telegram chat IDs must not be committed in plaintext. The bot token is already
kept as a GitHub Actions secret, so derive an authenticated stream key from it
and store only ciphertext in direct_news_state.json. No token or chat ID is
logged by this module.
"""
import base64
import hashlib
import hmac
import json
import secrets


_CONTEXT = b"pao-direct-recipient-vault-v1"


def _key(token: str) -> bytes:
    return hashlib.sha256(_CONTEXT + b"\0" + token.encode("utf-8")).digest()


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, b"stream\0" + nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def seal(token: str, primary: str, mirror: str) -> str:
    if not token or not primary or not mirror or primary == mirror:
        raise ValueError("invalid recipient pair")
    payload = json.dumps(
        {"primary": str(primary), "mirror": str(mirror)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    key = _key(token)
    nonce = secrets.token_bytes(16)
    cipher = _xor(payload, _stream(key, nonce, len(payload)))
    tag = hmac.new(key, b"tag\0" + nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + cipher + tag).decode("ascii")


def open_pair(token: str, blob: str):
    if not token or not blob:
        return None
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        if len(raw) < 49:
            return None
        nonce, rest = raw[:16], raw[16:]
        cipher, tag = rest[:-32], rest[-32:]
        key = _key(token)
        expected = hmac.new(key, b"tag\0" + nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            return None
        payload = _xor(cipher, _stream(key, nonce, len(cipher)))
        data = json.loads(payload.decode("utf-8"))
        primary = str(data.get("primary", "")).strip()
        mirror = str(data.get("mirror", "")).strip()
        if primary and mirror and primary != mirror:
            return primary, mirror
    except Exception:
        return None
    return None


def read_from_state(state: dict, token: str):
    vault = state.get("recipient_vault") or {}
    if not isinstance(vault, dict) or int(vault.get("version", 0) or 0) != 1:
        return None
    return open_pair(token, str(vault.get("blob", "") or ""))


def write_to_state(state: dict, token: str, primary: str, mirror: str) -> None:
    state["recipient_vault"] = {
        "version": 1,
        "blob": seal(token, primary, mirror),
    }
    # Defense in depth: never leave a plaintext recipient pair in public state.
    state["recipients"] = {}
