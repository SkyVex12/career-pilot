//! Contract tests: the shared fixtures round-trip through the generated Rust types, as they do
//! through the Python (test_contract.py) and TypeScript (contract.test.ts) types.

use copilot_protocol::audio::{decode_frame, encode_frame};
use copilot_protocol::{
    AudioFrameHeader, ClientMessage, CloseCode, ServerMessage, parse_client, parse_server,
};
use serde_json::Value;

const FIXTURES: &str = include_str!("../../fixtures/messages.json");
const SCHEMA: &str = include_str!("../../protocol.schema.json");

fn fixtures() -> Value {
    serde_json::from_str(FIXTURES).unwrap()
}

fn schema() -> Value {
    serde_json::from_str(SCHEMA).unwrap()
}

/// Wire `type` -> schema definition name, for one side.
fn type_to_def(side: &str) -> Vec<(String, String)> {
    let schema = schema();
    let defs = &schema["$defs"];
    let union = format!("{}{}Message", side[..1].to_uppercase(), &side[1..]);
    defs[&union]["oneOf"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let name = r["$ref"].as_str().unwrap().rsplit('/').next().unwrap().to_string();
            let tag = defs[&name]["properties"]["type"]["const"].as_str().unwrap().to_string();
            (tag, name)
        })
        .collect()
}

fn variant_name<T: std::fmt::Debug>(message: &T) -> String {
    format!("{message:?}").split('(').next().unwrap().to_string()
}

#[test]
fn every_message_type_has_a_fixture_and_a_variant() {
    let f = fixtures();
    for side in ["client", "server"] {
        let covered: std::collections::BTreeSet<String> =
            f[side].as_array().unwrap().iter().map(|m| m["type"].as_str().unwrap().to_string()).collect();
        let defined: std::collections::BTreeSet<String> =
            type_to_def(side).into_iter().map(|(t, _)| t).collect();
        assert_eq!(covered, defined, "{side}");
    }
}

#[test]
fn client_messages_round_trip() {
    let names = type_to_def("client");
    for message in fixtures()["client"].as_array().unwrap() {
        let text = message.to_string();
        let parsed: ClientMessage = parse_client(&text).unwrap_or_else(|e| panic!("{text}: {e}"));
        let tag = message["type"].as_str().unwrap();
        assert_eq!(parsed.type_name(), tag);
        let def = &names.iter().find(|(t, _)| t == tag).unwrap().1;
        assert_eq!(&variant_name(&parsed), def);
        let back: Value = serde_json::from_str(&parsed.to_json().unwrap()).unwrap();
        assert_eq!(&back, message);
    }
}

#[test]
fn server_messages_round_trip() {
    let names = type_to_def("server");
    for message in fixtures()["server"].as_array().unwrap() {
        let text = message.to_string();
        let parsed: ServerMessage = parse_server(&text).unwrap_or_else(|e| panic!("{text}: {e}"));
        let tag = message["type"].as_str().unwrap();
        assert_eq!(parsed.type_name(), tag);
        assert_eq!(parsed.seq(), message["seq"].as_u64().unwrap());
        let def = &names.iter().find(|(t, _)| t == tag).unwrap().1;
        assert_eq!(&variant_name(&parsed), def);
        let back = serde_json::to_value(&parsed).unwrap();
        assert_eq!(&back, message);
    }
}

#[test]
fn invalid_messages_are_rejected() {
    for case in fixtures()["invalid"].as_array().unwrap() {
        let text = case["message"].to_string();
        let why = case["why"].as_str().unwrap();
        let result = match case["side"].as_str().unwrap() {
            "client" => parse_client(&text).map(|_| ()),
            _ => parse_server(&text).map(|_| ()),
        };
        assert!(result.is_err(), "accepted an invalid message ({why}): {text}");
    }
}

#[test]
fn unknown_fields_are_ignored() {
    let parsed =
        parse_server(r#"{"type":"auth.expiring","seq":3,"expires_in_s":60,"new_field":[1]}"#).unwrap();
    assert_eq!(parsed.seq(), 3);
}

#[test]
fn audio_frames_encode_to_the_shared_bytes() {
    for case in fixtures()["audio_frames"].as_array().unwrap() {
        let header: AudioFrameHeader = serde_json::from_value(case["header"].clone()).unwrap();
        let audio = hex(case["audio_hex"].as_str().unwrap());
        let frame = encode_frame(&header, &audio).unwrap();
        assert_eq!(to_hex(&frame), case["frame_hex"].as_str().unwrap(), "{}", case["why"]);
        let (decoded, payload) = decode_frame(&frame).unwrap();
        assert_eq!(decoded, header);
        assert_eq!(payload, &audio[..]);
    }
}

#[test]
fn invalid_audio_frames_are_rejected() {
    for case in fixtures()["invalid_audio_frames"].as_array().unwrap() {
        let frame = hex(case["frame_hex"].as_str().unwrap());
        assert!(decode_frame(&frame).is_err(), "{}", case["why"]);
    }
}

#[test]
fn audio_frame_size_limit() {
    let header = AudioFrameHeader { channel: 0, flags: 0, frame_seq: 0, capture_ms: 0 };
    assert!(encode_frame(&header, &vec![0; 64 * 1024 - 10]).is_ok());
    assert!(encode_frame(&header, &vec![0; 64 * 1024 - 9]).is_err());
}

#[test]
fn close_codes_match_the_schema() {
    let schema = schema();
    let codes: Vec<u64> = schema["$defs"]["CloseCode"]["enum"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_u64().unwrap())
        .collect();
    let ours: Vec<u64> = CloseCode::ALL.iter().map(|c| u64::from(c.code())).collect();
    assert_eq!(ours, codes);
    for c in CloseCode::ALL {
        assert_eq!(CloseCode::from_code(c.code()), Some(c));
    }
    assert_eq!(CloseCode::from_code(1006), None);
}

fn hex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

fn to_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}
