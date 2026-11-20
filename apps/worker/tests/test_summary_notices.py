"""Empty audio and declined retention must have distinct, factual review notices."""

import pytest

from copilot_worker.sessionlog import metadata_summary


@pytest.mark.parametrize("language", [None, "es", "vi"])
def test_empty_audio_has_a_different_notice_than_unsaved_transcript(language):
    empty = metadata_summary("interview", 1, [], language, transcript_stored=True)
    unsaved = metadata_summary("interview", 1, [], language, transcript_stored=False)
    assert empty != unsaved
    if language is None:
        assert "No conversation was captured" in empty
        assert "transcript was not stored" not in empty
        assert "1 minute." in empty


def test_metadata_counts_use_singular_without_including_transcript_text():
    result = metadata_summary("meeting", 1, ["technical"])
    assert "1 minute." in result
    assert "1 question detected" in result
