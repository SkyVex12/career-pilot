"""Immutable source capture and conservative interrupted-trial resumption."""

import hashlib
import json
import zipfile

import pytest
from copilot_evals.live_answer_artifacts import DispatchJournal, source_archive


def test_archive_is_exact_immutable_and_excludes_environment_files(tmp_path, monkeypatch):
    root, out = tmp_path / "repo", tmp_path / "private"
    root.mkdir()
    out.mkdir()
    (root / "engine.py").write_text('answer = "original"\n', encoding="utf-8")
    (root / ".env").write_text("API_KEY=must-not-copy", encoding="utf-8")
    monkeypatch.setattr("importlib.metadata.distributions", lambda: [])
    code = {"engine.py": hashlib.sha256((root / "engine.py").read_bytes()).hexdigest()}
    identity = source_archive(out, root, code, {"model": "offline"})
    assert identity == source_archive(out, root, code, {"model": "offline"})
    with zipfile.ZipFile(out / "source-archive.zip") as archive:
        assert set(archive.namelist()) == {
            "engine.py",
            "safe-configuration.json",
            "dependency-versions.json",
            "prompt-snapshots.json",
        }
        assert archive.read("engine.py") == (root / "engine.py").read_bytes()
    with pytest.raises(ValueError, match="only Python"):
        source_archive(out, root, {".env": "unused"}, {})
    (root / "engine.py").write_text('answer = "changed"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="changed during"):
        source_archive(out, root, code, {})
    code["engine.py"] = hashlib.sha256((root / "engine.py").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="Immutable"):
        source_archive(out, root, code, {})


def test_resume_does_not_redispatch_ambiguous_prior_requests(tmp_path):
    journal = DispatchJournal(tmp_path, ["a", "b", "c"], set())
    assert journal.start("a")
    assert not journal.start("a")
    journal.finish("a")
    assert journal.start("b")  # Process disappears before saving a result.
    restored = DispatchJournal(tmp_path, ["a", "b", "c"], {"a"})
    assert restored.states == {"a": "completed", "b": "ambiguous", "c": "not_started"}
    assert not restored.start("b")
    assert restored.start("c")
    checkpoint = json.loads((tmp_path / "dispatch-state.json").read_text())
    assert checkpoint["in_flight"] == ["c"]
    assert checkpoint["ambiguous"] == ["b"]
    with pytest.raises(ValueError, match="schedule changed"):
        DispatchJournal(tmp_path, ["a", "b"], {"a"})


def test_prior_attempt_ledger_prevents_retry_even_when_journal_was_not_saved(tmp_path):
    journal = DispatchJournal(tmp_path, ["a", "b"], set(), {"a"})
    assert journal.states["a"] == "ambiguous"
    assert not journal.start("a")
    assert journal.start("b")


def test_literal_prompt_sources_and_resolved_rules_are_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr("importlib.metadata.distributions", lambda: [])
    root = tmp_path / "repo"
    root.mkdir()
    source = 'ADAPTIVE_SYSTEM = "Exact draft"\n'
    identity = source_archive(
        tmp_path, root, {}, {}, {"D-draft:source": source, "D-draft:rules": "Exact draft"}
    )
    snapshots = identity["prompt_snapshots"]
    assert (tmp_path / snapshots["D-draft:source"]["filename"]).read_text() == source
    assert (tmp_path / snapshots["D-draft:rules"]["filename"]).read_text() == "Exact draft"
    with zipfile.ZipFile(tmp_path / "source-archive.zip") as archive:
        assert archive.read(snapshots["D-draft:source"]["filename"]).decode() == source
