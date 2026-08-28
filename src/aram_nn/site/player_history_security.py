"""Cryptographic boundary for private player-history candidates.

Protocol ``arammeta-player-history-security-v1`` deliberately keeps lookup
tokens, local identifiers, events, and export candidates in separate HMAC
domains.  Candidate plaintext is only ever present in memory and is wrapped
with randomized RSA-OAEP before it may cross the private export boundary.
This module never generates, loads, persists, logs, or discovers secrets.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import struct
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


PROTOCOL_NAME: Final[str] = "arammeta-player-history-security-v1"
NORMALIZER_ID: Final[str] = f"nfkc-casefold-v1-u{unicodedata.unidata_version}"
_FRAME_PREFIX: Final[bytes] = b"arammeta-ph\x00"
_FRAME_VERSION: Final[int] = 1
_ALLOWED_DOMAINS: Final[frozenset[str]] = frozenset(
    {"lookup", "player", "event", "candidate", "candidate-plaintext"}
)
_BANNED_BIDI: Final[frozenset[str]] = frozenset(
    {"R", "AL", "AN", "RLE", "RLO", "RLI", "LRE", "LRO", "LRI", "FSI", "PDI", "PDF", "BN"}
)
_DATASET_ID_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9._-]*\Z", re.ASCII)
_KEY_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_B64URL_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)
_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset({"v", "alg", "key_id", "ciphertext"})
_CANDIDATE_ALGORITHM: Final[str] = "RSA-OAEP-SHA256"
MAX_RSA_KEY_BITS: Final[int] = 8192
MAX_CIPHERTEXT_BYTES: Final[int] = 1024
MAX_ENVELOPE_BYTES: Final[int] = 2048
_MAX_UNPADDED_CIPHERTEXT_B64_LENGTH: Final[int] = (
    MAX_CIPHERTEXT_BYTES * 4 + 2
) // 3


class PlayerHistorySecurityError(ValueError):
    """A fail-closed error whose stable message never contains caller data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise PlayerHistorySecurityError(code)


def validate_expected_normalizer_id(expected_normalizer_id: str) -> str:
    if not isinstance(expected_normalizer_id, str) or expected_normalizer_id != NORMALIZER_ID:
        _fail("normalizer_mismatch")
    return expected_normalizer_id


def _strict_utf8(value: str, *, allow_empty: bool = True) -> bytes:
    if not isinstance(value, str):
        _fail("invalid_type")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        _fail("invalid_encoding")
    if not allow_empty and not encoded:
        _fail("invalid_length")
    return encoded


def _validate_character_policy(value: str, *, game_name: bool) -> None:
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        if category.startswith("C") or unicodedata.bidirectional(char) in _BANNED_BIDI:
            _fail("forbidden_character")
        if category.startswith("Z"):
            if not (game_name and char == " " and 0 < index < len(value) - 1):
                _fail("invalid_whitespace")


def _validate_normalized_components(game_name: str, tag: str) -> None:
    _validate_character_policy(game_name, game_name=True)
    _validate_character_policy(tag, game_name=False)
    if not 3 <= len(game_name) <= 16:
        _fail("invalid_game_name")
    if any(char == "#" or not char.isprintable() for char in game_name):
        _fail("invalid_game_name")
    if not 3 <= len(tag) <= 5 or any(
        not unicodedata.category(char).startswith(("L", "N")) for char in tag
    ):
        _fail("invalid_tag")


def normalize_riot_id_v1(value: str) -> bytes:
    """Return canonical ``game#tag`` bytes under the pinned v1 Unicode policy."""

    encoded = _strict_utf8(value)
    if not 7 <= len(encoded) <= 128:
        _fail("invalid_length")
    if value.count("#") != 1:
        _fail("invalid_separator")
    if value[0].isspace() or value[-1].isspace():
        _fail("invalid_whitespace")
    game_name, tag = value.split("#", 1)
    _validate_character_policy(game_name, game_name=True)
    _validate_character_policy(tag, game_name=False)
    if not game_name or not tag or game_name[0].isspace() or game_name[-1].isspace():
        _fail("invalid_whitespace")
    if any(char.isspace() for char in tag):
        _fail("invalid_whitespace")

    normalized_game = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", game_name).casefold())
    normalized_tag = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", tag).casefold())
    _validate_normalized_components(normalized_game, normalized_tag)
    normalized = f"{normalized_game}#{normalized_tag}"
    normalized_bytes = _strict_utf8(normalized)
    if len(normalized_bytes) > 72:
        _fail("invalid_length")
    return normalized_bytes


def _validate_normalized_riot_id(value: bytes) -> bytes:
    if type(value) is not bytes:
        _fail("invalid_type")
    try:
        decoded = value.decode("utf-8", "strict")
    except UnicodeError:
        _fail("invalid_encoding")
    if normalize_riot_id_v1(decoded) != value:
        _fail("noncanonical_riot_id")
    return value


def validate_dataset_id(dataset_id: str) -> str:
    encoded = _strict_utf8(dataset_id, allow_empty=False)
    if len(encoded) > 64 or _DATASET_ID_RE.fullmatch(dataset_id) is None:
        _fail("invalid_dataset_id")
    return dataset_id


def validate_timestamp_ms(value: int) -> int:
    if type(value) is not int or value < 0:
        _fail("invalid_timestamp")
    return value


def frame_v1(domain: str, parts: Sequence[bytes]) -> bytes:
    """Encode the unambiguous, versioned binary frame used by every role."""

    if not isinstance(domain, str) or domain not in _ALLOWED_DOMAINS:
        _fail("invalid_domain")
    try:
        domain_bytes = domain.encode("ascii", "strict")
    except UnicodeError:
        _fail("invalid_domain")
    if len(domain_bytes) > 0xFFFF:
        _fail("invalid_domain")
    framed = bytearray(_FRAME_PREFIX)
    framed.append(_FRAME_VERSION)
    framed.extend(struct.pack(">H", len(domain_bytes)))
    framed.extend(domain_bytes)
    for part in parts:
        if type(part) is not bytes:
            _fail("invalid_frame_part")
        if len(part) > 0xFFFFFFFF:
            _fail("invalid_frame_part")
        framed.extend(struct.pack(">I", len(part)))
        framed.extend(part)
    return bytes(framed)


def parse_frame_v1(data: bytes, *, expected_domain: str, expected_part_count: int) -> tuple[bytes, ...]:
    """Strictly parse a v1 frame when the caller supplies its expected shape."""

    if type(data) is not bytes or type(expected_part_count) is not int or expected_part_count < 0:
        _fail("invalid_frame")
    if expected_domain not in _ALLOWED_DOMAINS:
        _fail("invalid_domain")
    prefix_len = len(_FRAME_PREFIX)
    if len(data) < prefix_len + 3 or data[:prefix_len] != _FRAME_PREFIX:
        _fail("invalid_frame")
    if data[prefix_len] != _FRAME_VERSION:
        _fail("invalid_frame")
    offset = prefix_len + 1
    domain_length = int.from_bytes(data[offset : offset + 2], "big")
    offset += 2
    if offset + domain_length > len(data):
        _fail("invalid_frame")
    try:
        domain = data[offset : offset + domain_length].decode("ascii", "strict")
    except UnicodeError:
        _fail("invalid_frame")
    if domain != expected_domain:
        _fail("domain_mismatch")
    offset += domain_length
    parts: list[bytes] = []
    for _ in range(expected_part_count):
        if offset + 4 > len(data):
            _fail("invalid_frame")
        part_length = int.from_bytes(data[offset : offset + 4], "big")
        offset += 4
        if offset + part_length > len(data):
            _fail("invalid_frame")
        parts.append(data[offset : offset + part_length])
        offset += part_length
    if offset != len(data):
        _fail("invalid_frame")
    return tuple(parts)


def validate_secret(secret: bytes) -> bytes:
    if type(secret) is not bytes or len(secret) != 32:
        _fail("invalid_secret")
    return secret


def validate_role_secrets(
    lookup_secret: bytes,
    player_secret: bytes,
    event_secret: bytes,
    candidate_secret: bytes,
) -> None:
    secrets = [validate_secret(secret) for secret in (lookup_secret, player_secret, event_secret, candidate_secret)]
    if len(set(secrets)) != 4:
        _fail("secrets_not_distinct")


def hmac_key_id(secret: bytes) -> str:
    return hashlib.sha256(validate_secret(secret)).hexdigest()[:32]


def validate_sqlite_key(value: object) -> bytes:
    """Accept only canonical raw 32-byte SQLite BLOB key material."""

    if type(value) is not bytes or len(value) != 32:
        _fail("invalid_sqlite_key")
    return value


def _derive(secret: bytes, domain: str, parts: Sequence[bytes]) -> bytes:
    return hmac.new(validate_secret(secret), frame_v1(domain, parts), hashlib.sha256).digest()


def derive_lookup_key(
    secret: bytes, *, expected_normalizer_id: str, normalized_riot_id: bytes
) -> bytes:
    normalizer = validate_expected_normalizer_id(expected_normalizer_id)
    normalized = _validate_normalized_riot_id(normalized_riot_id)
    return _derive(secret, "lookup", (_strict_utf8(normalizer), normalized))


def derive_player_key(
    secret: bytes, *, expected_normalizer_id: str, player_local_id: str
) -> bytes:
    validate_expected_normalizer_id(expected_normalizer_id)
    return _derive(secret, "player", (_strict_utf8(player_local_id),))


def derive_event_key(
    secret: bytes, *, expected_normalizer_id: str, player_local_id: str, game_id: int
) -> bytes:
    validate_expected_normalizer_id(expected_normalizer_id)
    if type(game_id) is not int or game_id < 0:
        _fail("invalid_game_id")
    return _derive(secret, "event", (_strict_utf8(player_local_id), str(game_id).encode("ascii")))


def derive_candidate_key(
    secret: bytes,
    *,
    expected_normalizer_id: str,
    dataset_id: str,
    normalized_riot_id: bytes,
) -> bytes:
    normalizer = validate_expected_normalizer_id(expected_normalizer_id)
    dataset = validate_dataset_id(dataset_id)
    normalized = _validate_normalized_riot_id(normalized_riot_id)
    return _derive(
        secret,
        "candidate",
        (_strict_utf8(dataset), _strict_utf8(normalizer), normalized),
    )


def _validate_key_id(key_id: str) -> str:
    if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
        _fail("invalid_key_id")
    return key_id


def rsa_public_key_id(public_key: rsa.RSAPublicKey) -> str:
    key = _validate_public_key(public_key)
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:32]


def _validate_public_key(public_key: rsa.RSAPublicKey) -> rsa.RSAPublicKey:
    if not isinstance(public_key, rsa.RSAPublicKey):
        _fail("invalid_public_key")
    if public_key.key_size < 3072:
        _fail("rsa_key_too_small")
    if public_key.key_size > MAX_RSA_KEY_BITS:
        _fail("rsa_key_too_large")
    return public_key


def _validate_private_key(private_key: rsa.RSAPrivateKey) -> rsa.RSAPrivateKey:
    if not isinstance(private_key, rsa.RSAPrivateKey):
        _fail("invalid_private_key")
    if private_key.key_size < 3072:
        _fail("rsa_key_too_small")
    if private_key.key_size > MAX_RSA_KEY_BITS:
        _fail("rsa_key_too_large")
    return private_key


def _oaep(key_id: str) -> padding.OAEP:
    label = b"arammeta-ph-candidate-v1:" + _validate_key_id(key_id).encode("ascii")
    return padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=label)


@dataclass(frozen=True)
class CandidateEnvelope:
    v: int
    alg: str
    key_id: str
    ciphertext: bytes


def encode_candidate_envelope(envelope: CandidateEnvelope) -> bytes:
    if not isinstance(envelope, CandidateEnvelope) or envelope.v != 1:
        _fail("invalid_envelope")
    if envelope.alg != _CANDIDATE_ALGORITHM:
        _fail("invalid_algorithm")
    key_id = _validate_key_id(envelope.key_id)
    if (
        type(envelope.ciphertext) is not bytes
        or not 1 <= len(envelope.ciphertext) <= MAX_CIPHERTEXT_BYTES
    ):
        _fail("invalid_ciphertext")
    encoded_ciphertext = base64.urlsafe_b64encode(envelope.ciphertext).rstrip(b"=").decode("ascii")
    encoded_envelope = json.dumps(
        {"v": 1, "alg": _CANDIDATE_ALGORITHM, "key_id": key_id, "ciphertext": encoded_ciphertext},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(encoded_envelope) > MAX_ENVELOPE_BYTES:
        _fail("invalid_envelope")
    return encoded_envelope


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_envelope")
        result[key] = value
    return result


def parse_candidate_envelope(data: bytes) -> CandidateEnvelope:
    if type(data) is not bytes or not 1 <= len(data) <= MAX_ENVELOPE_BYTES:
        _fail("invalid_envelope")
    try:
        parsed = json.loads(data.decode("utf-8", "strict"), object_pairs_hook=_reject_duplicate_json_keys)
    except PlayerHistorySecurityError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        _fail("invalid_envelope")
    if type(parsed) is not dict or frozenset(parsed) != _ENVELOPE_FIELDS:
        _fail("invalid_envelope")
    if type(parsed["v"]) is not int or parsed["v"] != 1:
        _fail("invalid_envelope")
    if type(parsed["alg"]) is not str or parsed["alg"] != _CANDIDATE_ALGORITHM:
        _fail("invalid_algorithm")
    key_id = _validate_key_id(parsed["key_id"])
    encoded_ciphertext = parsed["ciphertext"]
    if (
        type(encoded_ciphertext) is not str
        or not 1 <= len(encoded_ciphertext) <= _MAX_UNPADDED_CIPHERTEXT_B64_LENGTH
        or _B64URL_RE.fullmatch(encoded_ciphertext) is None
    ):
        _fail("invalid_ciphertext")
    try:
        padding_size = (-len(encoded_ciphertext)) % 4
        ciphertext = base64.b64decode(
            encoded_ciphertext + ("=" * padding_size), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        _fail("invalid_ciphertext")
    if (
        not 1 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES
        or base64.urlsafe_b64encode(ciphertext).rstrip(b"=").decode("ascii")
        != encoded_ciphertext
    ):
        _fail("invalid_ciphertext")
    return CandidateEnvelope(v=1, alg=_CANDIDATE_ALGORITHM, key_id=key_id, ciphertext=ciphertext)


def encrypt_candidate(
    public_key: rsa.RSAPublicKey,
    *,
    expected_normalizer_id: str,
    dataset_id: str,
    normalized_riot_id: bytes,
) -> bytes:
    """Encrypt a canonical candidate using public-key operations only."""

    key = _validate_public_key(public_key)
    normalizer = validate_expected_normalizer_id(expected_normalizer_id)
    dataset = validate_dataset_id(dataset_id)
    normalized = _validate_normalized_riot_id(normalized_riot_id)
    key_id = rsa_public_key_id(key)
    plaintext = frame_v1(
        "candidate-plaintext",
        (_strict_utf8(dataset), _strict_utf8(normalizer), normalized),
    )
    ciphertext = key.encrypt(plaintext, _oaep(key_id))
    return encode_candidate_envelope(
        CandidateEnvelope(v=1, alg=_CANDIDATE_ALGORITHM, key_id=key_id, ciphertext=ciphertext)
    )


@dataclass(frozen=True)
class CandidatePlaintext:
    dataset_id: str
    normalizer_id: str
    normalized_riot_id: bytes


def decrypt_candidate(
    envelope_bytes: bytes,
    *,
    private_key: rsa.RSAPrivateKey,
    allowed_key_ids: Mapping[str, rsa.RSAPrivateKey],
    expected_dataset_id: str,
    expected_normalizer_id: str,
) -> CandidatePlaintext:
    """Decrypt only an explicitly allowed, non-retired key/dataset binding."""

    envelope = parse_candidate_envelope(envelope_bytes)
    key = _validate_private_key(private_key)
    dataset = validate_dataset_id(expected_dataset_id)
    normalizer = validate_expected_normalizer_id(expected_normalizer_id)
    if not isinstance(allowed_key_ids, Mapping) or envelope.key_id not in allowed_key_ids:
        _fail("key_not_allowed")
    mapped_key = allowed_key_ids[envelope.key_id]
    mapped_private = _validate_private_key(mapped_key)
    actual_key_id = rsa_public_key_id(key.public_key())
    mapped_key_id = rsa_public_key_id(mapped_private.public_key())
    if actual_key_id != envelope.key_id or mapped_key_id != envelope.key_id:
        _fail("key_mismatch")
    if len(envelope.ciphertext) != (key.key_size + 7) // 8:
        _fail("invalid_ciphertext")
    try:
        plaintext = key.decrypt(envelope.ciphertext, _oaep(envelope.key_id))
    except (ValueError, TypeError):
        _fail("decrypt_failed")
    parts = parse_frame_v1(
        plaintext, expected_domain="candidate-plaintext", expected_part_count=3
    )
    try:
        actual_dataset = parts[0].decode("utf-8", "strict")
        actual_normalizer = parts[1].decode("utf-8", "strict")
    except UnicodeError:
        _fail("invalid_encoding")
    if actual_dataset != dataset:
        _fail("dataset_mismatch")
    if actual_normalizer != normalizer:
        _fail("normalizer_mismatch")
    validate_dataset_id(actual_dataset)
    validate_expected_normalizer_id(actual_normalizer)
    normalized = _validate_normalized_riot_id(parts[2])
    return CandidatePlaintext(actual_dataset, actual_normalizer, normalized)
