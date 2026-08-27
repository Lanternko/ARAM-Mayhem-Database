from __future__ import annotations

import json

import pytest

from aram_nn.lcu.writer_protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    decode_frame,
    encode_frame,
)


def _ping(request_id: str = "r1") -> dict[str, object]:
    return {"version": 1, "command": "ping", "request_id": request_id}


def test_round_trip_is_utf8_json_and_exact_metadata() -> None:
    frame = encode_frame(_ping())
    assert isinstance(frame, bytes)
    assert decode_frame(frame) == _ping()
    assert json.loads(frame.decode("utf-8")) == _ping()


@pytest.mark.parametrize(
    "frame",
    [
        b"not-json",
        b"\x80\x03}q\x00.",  # pickle-like bytes must never be deserialised
        b"[]",
        b'{"version":true,"command":"ping","request_id":"x"}',
        b'{"version":1,"command":"ping","request_id":"x","extra":1}',
        b'{"version":1,"command":"ping"}',
    ],
)
def test_malformed_unknown_extra_and_wrong_type_frames_rejected(frame: bytes) -> None:
    with pytest.raises(ProtocolError):
        decode_frame(frame)


def test_oversize_and_excessive_depth_rejected_without_echoing_payload() -> None:
    with pytest.raises(ProtocolError) as exc:
        decode_frame(b"{" + b"a" * MAX_FRAME_BYTES)
    assert "a" not in str(exc.value)

    nested: object = _ping()
    for _ in range(20):
        nested = [nested]
    with pytest.raises(ProtocolError):
        # The nested value is not a valid command object after the first layer,
        # but depth is checked before command fields and remains bounded.
        decode_frame(json.dumps(nested).encode())


def test_bool_is_not_accepted_for_integer_generation() -> None:
    frame = {
        "version": 1,
        "command": "release_game",
        "request_id": "x",
        "game_id": "g",
        "token": "t",
        "generation": True,
    }
    with pytest.raises(ProtocolError):
        encode_frame(frame)


def test_command_payload_unknown_field_rejected() -> None:
    with pytest.raises(ProtocolError):
        encode_frame({**_ping(), "command": "game_claim", "game_id": "g", "sql": "DROP TABLE games"})
