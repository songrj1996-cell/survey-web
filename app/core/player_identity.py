"""Comparison keys for player IDs; source values remain available for audit/export."""

# Explicit formatting-only allowlist. Do not remove punctuation, internal spaces,
# joiners, leading zeros, or coerce numeric text: those can distinguish real IDs.
_INVISIBLE_FORMATTING = dict.fromkeys(map(ord, "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff"))


def player_identity_key(value: str) -> str:
    return str(value).translate(_INVISIBLE_FORMATTING).strip()
