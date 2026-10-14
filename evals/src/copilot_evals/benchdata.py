"""The latency benchmark's inputs: the scripted mock interview (evals/bench/mock_interview.toml),
the fictional profile it is answered from (evals/bench/profile.toml), and the timeline of the
generated audio (where each utterance sits in the WAV).
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

BENCH_DIR = Path(__file__).resolve().parents[2] / "bench"
SCRIPT = BENCH_DIR / "mock_interview.toml"
PROFILE = BENCH_DIR / "profile.toml"
AUDIO_DIR = BENCH_DIR / "audio"  # generated, gitignored
WAV = AUDIO_DIR / "mock_interview.wav"
TIMELINE = AUDIO_DIR / "mock_interview.timeline.json"

Expect = Literal["answer", "none", "either"]


@dataclass(frozen=True)
class Utterance:
    id: str
    parts: list[str]
    gap_s: float
    pause_s: float
    voice: str
    style: str
    expect: Expect
    qtype: str | None = None
    prepared: str | None = None
    force_after_s: float | None = None
    case: str = ""

    @property
    def text(self) -> str:
        return " ".join(self.parts)


@dataclass(frozen=True)
class Prepared:
    id: str
    question: str
    points: list[str]
    nodes: list[str]


@dataclass(frozen=True)
class Story:
    short_id: str
    title: str
    one_liner: str
    competencies: list[str]


@dataclass(frozen=True)
class Profile:
    user_name: str
    company: str
    role_title: str
    round: str
    job_summary: str
    digest: str
    refs: dict[str, str]
    stories: list[Story]
    prepared: list[Prepared]


@dataclass(frozen=True)
class Span:
    """Where an utterance sits in the generated WAV, in seconds."""

    id: str
    start_s: float
    end_s: float
    parts: list[tuple[float, float]] = field(default_factory=list[tuple[float, float]])


def load_script(path: Path = SCRIPT) -> list[Utterance]:
    data = tomllib.loads(path.read_text("utf-8"))
    defaults = cast(dict[str, Any], data.get("defaults", {}))
    out: list[Utterance] = []
    for raw in cast(list[dict[str, Any]], data["utterance"]):
        u = {**defaults, **raw}
        out.append(
            Utterance(
                id=str(u["id"]),
                parts=[str(p) for p in u["parts"]],
                gap_s=float(u["gap_s"]),
                pause_s=float(u.get("pause_s", 0.0)),
                voice=str(u["voice"]),
                style=str(u["style"]),
                expect=cast(Expect, u["expect"]),
                qtype=u.get("type"),
                prepared=u.get("prepared"),
                force_after_s=u.get("force_after_s"),
                case=str(u.get("case", "")),
            )
        )
    ids = [u.id for u in out]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: utterance ids must be unique")
    return out


def load_profile(path: Path = PROFILE) -> Profile:
    data = tomllib.loads(path.read_text("utf-8"))
    return Profile(
        user_name=str(data["user_name"]),
        company=str(data["company"]),
        role_title=str(data["role_title"]),
        round=str(data["round"]),
        job_summary=str(data["job_summary"]),
        digest=str(data["digest"]).strip() + "\n",
        refs={str(k): str(v) for k, v in cast(dict[str, Any], data["refs"]).items()},
        stories=[
            Story(s["short_id"], s["title"], s["one_liner"], list(s.get("competencies", [])))
            for s in cast(list[dict[str, Any]], data["story"])
        ],
        prepared=[
            Prepared(p["id"], p["question"], list(p["points"]), list(p.get("nodes", [])))
            for p in cast(list[dict[str, Any]], data["prepared"])
        ],
    )


def load_timeline(path: Path = TIMELINE) -> list[Span]:
    raw = cast(list[dict[str, Any]], json.loads(path.read_text("utf-8"))["spans"])
    return [
        Span(s["id"], s["start_s"], s["end_s"], [(a, b) for a, b in s.get("parts", [])])
        for s in raw
    ]


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()
