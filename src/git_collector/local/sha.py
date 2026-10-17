"""SHA validation and hex ↔ bytes helpers (no subprocess)."""

from __future__ import annotations

import re

from ..errors import GitCollectorError

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_SHA_HEX_LENGTHS = frozenset({40, 64})


def parse_sha_hex(value: str) -> bytes:
    text = value.strip()
    if len(text) not in _SHA_HEX_LENGTHS or _HEX_RE.fullmatch(text) is None:
        raise GitCollectorError(f"invalid commit SHA hex: {value!r}")
    return bytes.fromhex(text)


def sha_to_hex(sha: bytes) -> str:
    if len(sha) not in (20, 32):
        raise GitCollectorError(f"SHA must be 20 or 32 bytes, got {len(sha)}")
    return sha.hex()
