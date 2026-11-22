"""Shared snapshot bytes and Store errors."""
from dataclasses import dataclass
import hashlib
import json


class StoreError(Exception):
    """The committed store cannot be used as requested."""


class MissingStore(StoreError):
    """There is no committed root at this location."""


class StoreConflict(StoreError):
    """The caller's expected committed root is no longer current."""


class CorruptStore(StoreError):
    """A committed root or its referenced object is missing or inconsistent."""


@dataclass(frozen=True)
class CommittedVersion:
    token: str
    snapshot: dict


def _encode(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decode(raw: bytes, what: str) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptStore(f"invalid {what}") from exc
    if not isinstance(value, dict):
        raise CorruptStore(f"invalid {what}")
    return value
