"""Stable call-language identifiers; candidates require deployment verification."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CallLanguage:
    name: str
    deepgram_code: str


CALL_LANGUAGES = {
    "en": CallLanguage("English", "en"),
    "es": CallLanguage("Spanish", "es"),
    "vi": CallLanguage("Vietnamese", "vi"),
}


def validate_language(value: str, verified: Iterable[str] = ("en",)) -> str:
    if value not in CALL_LANGUAGES:
        raise ValueError("Unsupported call language. Choose a language from call settings.")
    if value not in verified:
        raise ValueError(
            "This call language is not enabled on this deployment. Choose an available language."
        )
    return value


def language_instruction(value: str) -> str:
    name = CALL_LANGUAGES[value].name
    return (
        f"\nThe selected spoken call language is {name}. Recognize questions and requests "
        f"in {name}, including indirect requests; English question keywords are not required. "
        f"Write answers, summaries and feedback in {name}. Preserve proper names, code, "
        "source identifiers, citation markers and quoted evidence. Do not translate JSON keys "
        "or enum values. Keep the requested answer format. Never invent evidence or experience."
    )
