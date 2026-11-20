"""Stable, provider-backed spoken-language capabilities (not device certification)."""
# ruff: noqa: RUF001 -- Native language punctuation and letters are intentional.

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class CallLanguage:
    name: str
    native_name: str
    deepgram_code: str
    question_cues: tuple[str, ...]
    direction: str = "ltr"
    unspaced: bool = False


# Product coverage, not a population ranking. Nova-3 monolingual mappings verified
# against the provider catalog on 2026-11-20. Do not silently route to English.
CALL_LANGUAGES = {
    "en": CallLanguage("English", "English", "en", ("what", "how", "tell me")),
    "es": CallLanguage(
        "Spanish",
        "Español",
        "es",
        (
            "qué",
            "cómo",
            "cuál",
            "cuándo",
            "dónde",
            "quién",
            "puedes",
            "podrías",
            "háblame",
            "cuéntame",
            "explica",
        ),
    ),
    "vi": CallLanguage(
        "Vietnamese",
        "Tiếng Việt",
        "vi",
        ("gì", "sao", "nào", "ai", "đâu", "hãy", "có thể", "bao nhiêu"),
    ),
    "zh": CallLanguage(
        "Chinese (Mandarin)",
        "中文（普通话）",
        "zh",
        ("什么", "怎么", "如何", "为什么", "请介绍", "请解释", "请描述", "能否", "吗"),
        unspaced=True,
    ),
    "hi": CallLanguage(
        "Hindi", "हिन्दी", "hi", ("क्या", "कैसे", "क्यों", "कब", "कहाँ", "बताइए", "समझाइए")
    ),
    "ar": CallLanguage(
        "Arabic",
        "العربية",
        "ar",
        ("ما", "ماذا", "كيف", "لماذا", "متى", "أين", "اشرح", "حدثني", "هل"),
        direction="rtl",
    ),
    "fr": CallLanguage(
        "French",
        "Français",
        "fr",
        ("comment", "pourquoi", "quel", "quelle", "quand", "où", "décrivez", "expliquez", "parlez"),
    ),
    "bn": CallLanguage(
        "Bengali", "বাংলা", "bn", ("কী", "কি", "কীভাবে", "কিভাবে", "কেন", "কখন", "কোথায়", "বলুন", "ব্যাখ্যা")
    ),
    "pt": CallLanguage(
        "Portuguese",
        "Português",
        "pt",
        ("como", "qual", "quais", "quando", "onde", "por que", "descreva", "explique", "conte"),
    ),
    "ru": CallLanguage(
        "Russian",
        "Русский",
        "ru",
        ("как", "почему", "что", "когда", "где", "расскажите", "объясните", "опишите"),
    ),
    "ur": CallLanguage(
        "Urdu",
        "اردو",
        "ur",
        ("کیا", "کیسے", "کیوں", "کب", "کہاں", "بتائیں", "وضاحت"),
        direction="rtl",
    ),
    "id": CallLanguage(
        "Indonesian",
        "Bahasa Indonesia",
        "id",
        ("apa", "bagaimana", "mengapa", "kapan", "mana", "jelaskan", "ceritakan"),
    ),
    "de": CallLanguage(
        "German",
        "Deutsch",
        "de",
        ("wie", "warum", "was", "wann", "wo", "welche", "beschreiben", "erklären", "erzählen"),
    ),
    "ja": CallLanguage(
        "Japanese",
        "日本語",
        "ja",
        ("どう", "なぜ", "何", "どの", "説明", "教えて", "ください", "ですか", "ますか"),
        unspaced=True,
    ),
    "tr": CallLanguage(
        "Turkish",
        "Türkçe",
        "tr",
        ("nasıl", "neden", "niçin", "hangi", "açıklayın", "anlatın", "nedir"),
    ),
    "ta": CallLanguage(
        "Tamil", "தமிழ்", "ta", ("எப்படி", "என்ன", "ஏன்", "எப்போது", "எங்கே", "விளக்குங்கள்", "கூறுங்கள்")
    ),
    "te": CallLanguage(
        "Telugu", "తెలుగు", "te", ("ఎలా", "ఏమి", "ఎందుకు", "ఎప్పుడు", "ఎక్కడ", "వివరించండి", "చెప్పండి")
    ),
    "mr": CallLanguage(
        "Marathi", "मराठी", "mr", ("कसे", "कशी", "काय", "का", "कधी", "कुठे", "सांगा", "स्पष्ट")
    ),
    "ko": CallLanguage(
        "Korean",
        "한국어",
        "ko",
        ("어떻게", "왜", "무엇", "언제", "어디", "설명해", "알려", "말씀해"),
    ),
    "it": CallLanguage(
        "Italian",
        "Italiano",
        "it",
        ("come", "perché", "quale", "quando", "dove", "descrivi", "spiega", "racconta"),
    ),
    "fa": CallLanguage(
        "Persian",
        "فارسی",
        "fa",
        ("چگونه", "چطور", "چرا", "چه", "کجا", "توضیح", "بگویید"),
        direction="rtl",
    ),
    "th": CallLanguage(
        "Thai",
        "ไทย",
        "th",
        ("อย่างไร", "ทำไม", "อะไร", "เมื่อไร", "ที่ไหน", "อธิบาย", "เล่า", "ไหม"),
        unspaced=True,
    ),
    "sw": CallLanguage(
        "Swahili",
        "Kiswahili",
        "sw",
        ("nini", "vipi", "kwa nini", "lini", "wapi", "eleza", "fafanua"),
    ),
    "pa": CallLanguage("Punjabi", "ਪੰਜਾਬੀ", "pa", ("ਕਿਵੇਂ", "ਕੀ", "ਕਿਉਂ", "ਕਦੋਂ", "ਕਿੱਥੇ", "ਦੱਸੋ", "ਸਮਝਾਓ")),
}
DEFAULT_CALL_LANGUAGES = tuple(CALL_LANGUAGES)

# Aura only supplies these six of the 24 selected call languages. Microphone input
# and text practice are independent of generated interviewer audio.
DEFAULT_PRACTICE_VOICES = {
    "en": "aura-2-thalia-en",
    "es": "aura-2-estrella-es",
    "fr": "aura-2-agathe-fr",
    "de": "aura-2-viktoria-de",
    "it": "aura-2-livia-it",
    "ja": "aura-2-izanami-ja",
}


def practice_voice_supported(language: str, model: str | None) -> bool:
    """Do not advertise an English/unknown-language voice for a non-English session."""
    return language in DEFAULT_PRACTICE_VOICES and bool(
        model and model.startswith("aura-") and model.endswith("-" + language)
    )


def validate_language(value: str, enabled: Iterable[str] = DEFAULT_CALL_LANGUAGES) -> str:
    if value not in CALL_LANGUAGES:
        raise ValueError("Unsupported call language. Choose a language from call settings.")
    if value not in enabled:
        raise ValueError(
            "This call language is not enabled on this deployment. Choose an available language."
        )
    return value


def recognition_supported(language: str, model: str, providers: Iterable[str]) -> bool:
    """The multilingual contract requires every configured fallback to support it."""
    paths = tuple(providers)
    return (
        language in CALL_LANGUAGES
        and bool(paths)
        and (
            language == "en"
            or (model in {"nova-3", "nova-3-general"} and all(p == "deepgram" for p in paths))
        )
    )


def speech_units(text: str, language: str) -> int:
    """A bounded length heuristic, not a tokenizer or word-count claim for CJK/Thai."""
    if CALL_LANGUAGES[language].unspaced:
        return sum(unicodedata.category(c).startswith(("L", "N")) for c in text) // 2
    return len(text.split())


def has_question_cue(text: str, language: str) -> bool:
    normalized = unicodedata.normalize("NFC", text).casefold()
    item = CALL_LANGUAGES[language]
    for cue in item.question_cues:
        # CJK/Thai and Korean cues may be inside an unspaced utterance/inflected verb.
        if item.unspaced or language == "ko":
            if cue in normalized:
                return True
        elif re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", normalized):
            return True
    return False


def language_instruction(value: str) -> str:
    name = CALL_LANGUAGES[value].name
    return (
        f"\nThe selected spoken call language is {name}. Recognize questions and requests "
        f"in {name}, including indirect requests; English question keywords are not required. "
        f"Write answers, summaries and feedback in {name}. Preserve proper names, code, "
        "source identifiers, citation markers and quoted evidence. Do not translate JSON keys "
        "or enum values. Keep the requested answer format. Never invent evidence or experience."
    )
