"""Provider routing and script handling for the distinct spoken-language catalog."""

import unicodedata

import pytest

from copilot_core.languages import (
    CALL_LANGUAGES,
    DEFAULT_CALL_LANGUAGES,
    has_question_cue,
    language_instruction,
    provider_supports,
    recognition_supported,
    speech_units,
    validate_language,
)


def test_catalog_counts_distinct_languages_without_locale_padding():
    assert len(CALL_LANGUAGES) == 96
    assert len({item.name for item in CALL_LANGUAGES.values()}) == 96
    assert all(item.deepgram_code for item in CALL_LANGUAGES.values())
    assert DEFAULT_CALL_LANGUAGES[0] == "en"
    assert {code for code in CALL_LANGUAGES if "-" in code} == {"zh-HK"}
    assert CALL_LANGUAGES["zh-HK"].name == "Chinese (Cantonese)"
    assert all(item.name and item.native_name for item in CALL_LANGUAGES.values())


@pytest.mark.parametrize("language", CALL_LANGUAGES)
def test_every_language_has_an_explicit_supported_route(language):
    item = CALL_LANGUAGES[language]
    assert validate_language(language) == language
    assert recognition_supported(language, "nova-3", ["deepgram"])
    assert provider_supports(language, "nova-3", "deepgram")
    assert not recognition_supported(language, "nova-3", [])
    assert not recognition_supported(language, "nova-3", ["unknown"])
    instruction = language_instruction(language)
    assert item.name in instruction
    assert "Preserve proper names" in instruction
    assert "Do not translate JSON keys" in instruction


def test_every_configured_fallback_must_support_the_selected_language():
    assert not recognition_supported("vi", "nova-3", ["deepgram", "assemblyai"])
    assert not provider_supports("vi", "nova-3", "assemblyai")
    assert not provider_supports("vi", "nova-3-medical", "deepgram")
    assert provider_supports("en", "nova-3-medical", "deepgram")
    assert provider_supports("en", "universal-streaming-english", "assemblyai")
    assert not provider_supports("vi", "chirp_3", "google")


@pytest.mark.parametrize(
    "language",
    ["unknown", "en-US", "fr-CA", "zh-TW", "ga", "so", "jv", "km", "lo", "my", "mt", "or"],
)
def test_undocumented_and_unoffered_languages_remain_unavailable(language):
    assert not recognition_supported(language, "nova-3", ["deepgram", "assemblyai"])
    with pytest.raises(ValueError, match="Unsupported call language"):
        validate_language(language)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("zh-HK", "你可唔可以解釋呢個設計"),
        ("nl", "Hoe heb je dat probleem opgelost"),
        ("pl", "Wyjaśnij ten projekt"),
        ("uk", "Поясніть ваше рішення"),
        ("he", "איך פתרת את הבעיה"),
    ],
)
def test_new_question_cues_keep_native_script_and_normalization(language, text):
    assert has_question_cue(text, language)
    assert has_question_cue(unicodedata.normalize("NFD", text), language)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("zh-HK", "你可唔可以解釋呢個設計"),
        ("ja", "この問題をどう解決しましたか"),
        ("th", "คุณแก้ไขปัญหานี้อย่างไร"),
    ],
)
def test_unspaced_scripts_do_not_look_like_one_word(language, text):
    assert speech_units(text, language) >= 3


def test_unreviewed_question_hints_are_optional_and_do_not_invent_english_cues():
    assert CALL_LANGUAGES["ab"].question_cues == ()
    assert not has_question_cue("how would you handle this", "ab")
    assert not has_question_cue("somehow otherwise", "nl")
