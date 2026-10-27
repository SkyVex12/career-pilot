//! The client against the real Copilot API and workers on the local stack
//! (`scripts/desktop_test_stack.py`: Claude and Deepgram answer from the recorded fixtures,
//! the gateway uses scripted STT and Claude). Needs `docker compose up -d --wait` and
//! `uv sync`.
//!
//! The whole path the desktop app takes: documents up through the presigned URLs, the digest
//! and the packs built, an interview scheduled, a call through the gateway with the user's
//! audio recorded and encrypted, the upload after the socket closes (first failing, as with
//! the API down, then retried as at the next app start), the post-call outputs and the
//! transcript, history, usage, deleting a session and the account.

mod support;

use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use copilot_client::api::{ApiClient, ApiError};
use copilot_client::audio::aec::NoEchoCancellation;
use copilot_client::audio::source::FakeSource;
use copilot_client::audio::vad::{ChannelVad, Silero, VadParams};
use copilot_client::auth::StaticTokens;
use copilot_client::engine::{AudioParts, Engine, UiEvent};
use copilot_client::net::client::{ClientConfig, StartParams};
use copilot_client::net::close::StopReason;
use copilot_client::protocol::{Mode, Pass, ServerMessage};
use serde_json::{Value, json};
use support::Stack;
use uuid::Uuid;

fn worker_input(name: &str) -> Vec<u8> {
    std::fs::read(support::repo_root().join("apps/worker/tests/fixtures/inputs").join(name)).unwrap()
}

fn wav(name: &str) -> Vec<f32> {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    hound::WavReader::open(path).unwrap().samples::<i16>().map(|s| f32::from(s.unwrap()) / 32768.0).collect()
}

/// Poll `f` every 300 ms until it returns Some, or fail after `secs`.
async fn until<T, F, Fut>(what: &str, secs: u64, mut f: F) -> T
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Option<T>>,
{
    let deadline = Instant::now() + Duration::from_secs(secs);
    loop {
        if let Some(v) = f().await {
            return v;
        }
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

struct Call {
    session_id: Uuid,
    events: Vec<UiEvent>,
}

/// One call through the gateway: the user's audio plays on the mic (the gateway transcribes it
/// live), a typed question gets an answer, then the session ends.
async fn call(stack: &Stack, params: StartParams) -> Call {
    let me = wav("me.wav");
    let vad = || ChannelVad::new(Box::new(Silero::new().unwrap()), VadParams::default());
    let parts = AudioParts {
        source: Box::new(FakeSource::new(vec![], me.clone())),
        aec: Box::new(NoEchoCancellation),
        them_vad: vad(),
        me_vad: vad(),
    };
    let events: Arc<Mutex<Vec<UiEvent>>> = Arc::default();
    let sink = {
        let events = events.clone();
        Arc::new(move |e: UiEvent| events.lock().unwrap().push(e))
    };
    let tokens = Arc::new(StaticTokens::one(&stack.token));
    let engine =
        Engine::start(ClientConfig::new(stack.gateway.clone(), params), tokens, parts, sink).unwrap();
    let session_id = until("the session to go live", 10, || async { engine.session_id() }).await;
    tokio::time::sleep(Duration::from_millis(me.len() as u64 / 16 + 500)).await; // the user speaks
    engine.force_question(Some("Tell me about a system you scaled.".into())).await;
    until("the fast answer", 10, || async {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|e| matches!(e, UiEvent::Server { message: ServerMessage::AnswerDone(d) } if d.pass == Pass::Fast))
            .then_some(())
    })
    .await;
    assert_eq!(engine.end().await, Some(StopReason::Ended));
    let events = events.lock().unwrap().clone();
    Call { session_id, events }
}

fn start_params(job_id: Option<Uuid>, interview_id: Option<Uuid>, store_transcript: bool) -> StartParams {
    StartParams {
        mode: Mode::Interview,
        job_id,
        interview_id,
        meeting_title: None,
        employer_id: None,
        agenda: None,
        material_ids: vec![],
        platform: "zoom".into(),
        client_version: "test".into(),
        store_transcript,
    }
}

fn uuid(v: &Value) -> Uuid {
    Uuid::parse_str(v.as_str().unwrap_or_else(|| panic!("not a uuid: {v}"))).unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn documents_to_interview_to_post_call_through_the_api() {
    let stack = Stack::start();
    let api = ApiClient::new(&stack.api, Arc::new(StaticTokens::one(&stack.token))).unwrap();
    let me = api.get("/me").await.unwrap(); // the first request creates the user
    assert_eq!(me["display_name"], "Alex Doe");

    // Documents: presigned upload, then ingest; the worker reads them (Claude on fixtures).
    let resume = api.upload_document("resume", "resume.md", worker_input("resume.md")).await.unwrap();
    assert_eq!(resume["status"], "ingesting");
    let jd = api.upload_document("job_description", "job.md", worker_input("job.md")).await.unwrap();
    let docs = until("both documents and the digest", 60, || async {
        let d = api.get("/documents").await.unwrap();
        let done = d["documents"].as_array().unwrap().iter().all(|x| x["status"] == "ingested");
        (done && !d["digest"].is_null()).then_some(d)
    })
    .await;
    assert!(docs["digest"]["facts"].as_u64().unwrap() > 0);
    let jd_doc = docs["documents"].as_array().unwrap().iter().find(|d| d["id"] == jd["id"]).unwrap().clone();
    let job_id = uuid(&jd_doc["job_id"]); // the job description became a job
    let err = api.upload_document("resume", "resume.docx", b"x".to_vec()).await.unwrap_err();
    assert!(matches!(err, ApiError::Invalid(_)), "{err:?}");
    // A resume tailored for the application; anything but its three target fields is refused.
    let mut bad = serde_json::Map::new();
    bad.insert("user_id".into(), Value::from("someone-else"));
    let err =
        api.upload_document_for("resume", "resume.md", worker_input("resume.md"), &bad).await.unwrap_err();
    assert!(matches!(err, ApiError::Invalid(_)), "{err:?}");
    let mut target = serde_json::Map::new();
    target.insert("application_id".into(), Value::from(job_id.to_string()));
    target.insert("label".into(), Value::from("For Globex"));
    let tailored =
        api.upload_document_for("resume", "resume.md", worker_input("resume.md"), &target).await.unwrap();
    let version = tailored["resume_version_id"].as_str().unwrap().to_string();
    let app = api.get(&format!("/applications/{job_id}")).await.unwrap();
    assert_eq!(app["resume"]["version_id"], version.as_str());
    assert_eq!(app["resume"]["label"], "For Globex");

    // Jobs: the job's pack is built.
    let job = until("the job's pack", 60, || async {
        let jobs = api.get("/jobs").await.unwrap();
        jobs["jobs"]
            .as_array()
            .unwrap()
            .iter()
            .find(|j| uuid(&j["id"]) == job_id && j["pack"]["available"] == true)
            .cloned()
    })
    .await;
    assert!(!job["company"].as_str().unwrap().is_empty());

    // Schedule an interview: its own pack for the round.
    let starts = (std::time::SystemTime::now() + Duration::from_secs(3600))
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let iso = iso_utc(starts);
    let interview = api
        .post("/interviews", &json!({ "job_id": job_id, "round": "system_design", "starts_at": iso }))
        .await
        .unwrap();
    let interview_id = uuid(&interview["id"]);
    assert_eq!(interview["company"], job["company"]);
    until("the interview's pack", 60, || async {
        let list = api.get("/interviews").await.unwrap();
        list["interviews"]
            .as_array()
            .unwrap()
            .iter()
            .any(|i| uuid(&i["id"]) == interview_id && i["pack"]["available"] == true)
            .then_some(())
    })
    .await;

    // The call, with consent: the user's own words reach the gateway live; nothing is recorded.
    let c = call(&stack, start_params(Some(job_id), Some(interview_id), true)).await;
    assert!(
        c.events.iter().any(|e| matches!(e, UiEvent::Server { message: ServerMessage::SessionReady(_) }))
    );

    // Post-call: the app polls GET /sessions/{id} until the outputs are ready.
    let path = format!("/sessions/{}", c.session_id);
    let session = until("the post-call outputs", 120, || async {
        let s = api.get(&path).await.unwrap();
        (s["status"] == "ready").then_some(s)
    })
    .await;
    for kind in ["summary", "feedback", "follow_up_email"] {
        assert!(!session["outputs"][kind].as_str().unwrap_or("").is_empty(), "{kind}: {session}");
    }
    assert_eq!(uuid(&session["interview_id"]), interview_id);
    assert_eq!(uuid(&session["job_id"]), job_id);
    assert_eq!(session["me_audio_status"], "live");
    let transcript = api.get(&format!("{path}/transcript")).await.unwrap();
    let channels: Vec<&str> =
        transcript["segments"].as_array().unwrap().iter().filter_map(|s| s["channel"].as_str()).collect();
    assert!(channels.contains(&"me"), "the user's side was transcribed live: {transcript}");

    // History and usage.
    let list = api.get("/sessions").await.unwrap();
    assert!(list["sessions"].as_array().unwrap().iter().any(|s| uuid(&s["id"]) == c.session_id));
    let usage = api.get("/usage").await.unwrap();
    assert!(usage["sessions"].as_u64().unwrap() >= 1);

    // Without consent: no words stored, either side's, and a metadata summary only.
    let c2 = call(&stack, start_params(Some(job_id), None, false)).await;
    let s2 = until("the no-consent summary", 120, || async {
        let s = api.get(&format!("/sessions/{}", c2.session_id)).await.unwrap();
        (s["status"] == "ready").then_some(s)
    })
    .await;
    assert_eq!(s2["store_transcript"], false);
    assert!(!s2["outputs"]["summary"].as_str().unwrap().is_empty());
    let t2 = api.get(&format!("/sessions/{}/transcript", c2.session_id)).await.unwrap();
    assert!(t2["segments"].as_array().unwrap().is_empty());

    // Delete a session.
    api.delete(&path).await.unwrap();
    assert_eq!(api.get(&path).await.unwrap_err().status(), Some(404));

    // Delete the account: the worker removes everything (a later request starts afresh).
    let deleting = api.delete("/me").await.unwrap();
    assert_eq!(deleting["status"], "deleting");
    until("the account's data to go", 60, || async {
        let d = api.get("/documents").await.ok()?;
        let s = api.get("/sessions").await.ok()?;
        (d["documents"].as_array()?.is_empty() && s["sessions"].as_array()?.is_empty()).then_some(())
    })
    .await;
}

/// RFC 3339 for Unix seconds, without a date crate.
fn iso_utc(secs: u64) -> String {
    let days = secs / 86_400;
    let rem = secs % 86_400;
    // Civil from days (Howard Hinnant's algorithm).
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, rem % 3600 / 60, rem % 60)
}
