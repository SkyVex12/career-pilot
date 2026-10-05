"""Contract tests: shared fixtures round-trip through the generated Python types.

The same fixtures run through the TypeScript types in contract.test.ts.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel, ValidationError

from copilot_protocol import models
from copilot_protocol.audio import decode_frame, encode_frame

PROTOCOL = Path(__file__).resolve().parent.parent
SCHEMA: dict[str, Any] = json.loads((PROTOCOL / "protocol.schema.json").read_text("utf-8"))
FIXTURES: dict[str, Any] = json.loads((PROTOCOL / "fixtures" / "messages.json").read_text("utf-8"))

ROOTS: dict[str, type[models.ClientMessage] | type[models.ServerMessage]] = {
    "client": models.ClientMessage,
    "server": models.ServerMessage,
}


def type_to_def(side: str) -> dict[str, str]:
    """Map each wire `type` to its schema definition name for one side."""
    union = SCHEMA["$defs"][f"{side.capitalize()}Message"]["oneOf"]
    out: dict[str, str] = {}
    for ref in union:
        name = ref["$ref"].rsplit("/", 1)[1]
        out[SCHEMA["$defs"][name]["properties"]["type"]["const"]] = name
    return out


def schema_validator(side: str) -> jsonschema.Draft202012Validator:
    sub = {"$ref": f"#/$defs/{side.capitalize()}Message", "$defs": SCHEMA["$defs"]}
    return jsonschema.Draft202012Validator(sub, format_checker=jsonschema.FormatChecker())


def cases(side: str) -> list[Any]:
    return [pytest.param(m, id=f"{i}-{m['type']}") for i, m in enumerate(FIXTURES[side])]


@pytest.mark.parametrize("side", ["client", "server"])
def test_every_message_type_has_a_fixture(side: str) -> None:
    covered = {m["type"] for m in FIXTURES[side]}
    assert covered == set(type_to_def(side))


@pytest.mark.parametrize("side", ["client", "server"])
def test_fixtures_match_the_schema(side: str) -> None:
    validator = schema_validator(side)
    for message in FIXTURES[side]:
        validator.validate(message)


@pytest.mark.parametrize("message", cases("client"))
def test_client_round_trip(message: dict[str, Any]) -> None:
    assert_round_trip("client", message)


@pytest.mark.parametrize("message", cases("server"))
def test_server_round_trip(message: dict[str, Any]) -> None:
    assert_round_trip("server", message)


def assert_round_trip(side: str, message: dict[str, Any]) -> None:
    root = ROOTS[side]
    parsed = root.model_validate_json(json.dumps(message))
    # The discriminator picked the model the schema names for this type.
    inner: BaseModel = parsed.root
    assert type(inner).__name__ == type_to_def(side)[message["type"]]
    # Serializing gives back exactly the wire message.
    assert json.loads(parsed.model_dump_json(by_alias=True, exclude_unset=True)) == message
    assert parsed.model_dump(mode="json", by_alias=True, exclude_unset=True) == message


@pytest.mark.parametrize("case", [pytest.param(c, id=c["why"]) for c in FIXTURES["invalid"]])
def test_invalid_messages_are_rejected(case: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ROOTS[case["side"]].model_validate_json(json.dumps(case["message"]))
    assert not schema_validator(case["side"]).is_valid(case["message"])


def test_pass_field_uses_wire_name() -> None:
    delta = models.AnswerDelta(
        type="answer.delta",
        seq=1,
        question_id="0192f3c4-1d0a-7c55-b2e1-77c0a9d4e210",  # pyright: ignore[reportArgumentType]
        pass_="fast",
        text="hi",
    )
    assert delta.model_dump(mode="json", by_alias=True)["pass"] == "fast"


@pytest.mark.parametrize("case", [pytest.param(c, id=c["why"]) for c in FIXTURES["audio_frames"]])
def test_audio_frame_round_trip(case: dict[str, Any]) -> None:
    header = models.AudioFrameHeader.model_validate(case["header"])
    audio = bytes.fromhex(case["audio_hex"])
    frame = encode_frame(header, audio)
    assert frame.hex() == case["frame_hex"]
    decoded, payload = decode_frame(frame)
    assert decoded == header
    assert payload == audio


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["why"]) for c in FIXTURES["invalid_audio_frames"]]
)
def test_invalid_audio_frames_are_rejected(case: dict[str, Any]) -> None:
    # pydantic's ValidationError is a ValueError too.
    with pytest.raises(ValueError, match=r"audio frame is|validation error"):
        decode_frame(bytes.fromhex(case["frame_hex"]))


def test_audio_frame_size_limit() -> None:
    header = models.AudioFrameHeader(channel=0, flags=0, frame_seq=0, capture_ms=0)
    encode_frame(header, bytes(64 * 1024 - 10))
    with pytest.raises(ValueError, match="limit"):
        encode_frame(header, bytes(64 * 1024 - 9))
