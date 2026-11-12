"""Minimal base58 (Bitcoin alphabet) — avoids an extra dependency."""
ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


def b58decode(text: str) -> bytes:
    if not text:
        return b""
    num = 0
    for ch in text.encode():
        if ch not in _INDEX:
            raise ValueError(f"invalid base58 character: {chr(ch)!r}")
        num = num * 58 + _INDEX[ch]
    # count leading '1's as leading zero bytes
    pad = 0
    for ch in text.encode():
        if ch == ALPHABET[0]:
            pad += 1
        else:
            break
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * pad + body


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = bytearray()
    while num:
        num, rem = divmod(num, 58)
        out.append(ALPHABET[rem])
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return (ALPHABET[0:1] * pad + bytes(reversed(out))).decode()
