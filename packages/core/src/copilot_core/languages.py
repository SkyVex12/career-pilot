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
    question_cues: tuple[str, ...] = ()
    direction: str = "ltr"
    unspaced: bool = False


# Product coverage, not a population ranking. Provider mappings were checked on
# 2026-11-23 against their streaming language tables:
# https://developers.deepgram.com/docs/models-languages-overview
# Each entry is one spoken language, not another region/script of an existing one.
# Unsupported provider/model paths must never silently route to English.
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
    "ab": CallLanguage("Abkhazian", "Аҧсуа", "ab"),
    "ady": CallLanguage("Adyghe", "Адыгабзэ", "ady"),
    "af": CallLanguage("Afrikaans", "Afrikaans", "af", ("wat", "hoe", "waarom", "vertel")),
    "ak": CallLanguage("Akan", "Akan", "ak"),
    "sq": CallLanguage("Albanian", "Shqip", "sq", ("çfarë", "si", "pse", "shpjegoni")),
    "am": CallLanguage("Amharic", "አማርኛ", "am"),
    "hy": CallLanguage("Armenian", "Հայերեն", "hy", ("ինչ", "ինչպես", "ինչու")),
    "as": CallLanguage("Assamese", "অসমীয়া", "as"),
    "az": CallLanguage("Azerbaijani", "Azərbaycanca", "az", ("necə", "niyə", "nə")),
    "eu": CallLanguage("Basque", "Euskara", "eu", ("nola", "zergatik", "zer")),
    "be": CallLanguage("Belarusian", "Беларуская", "be", ("як", "чаму", "што")),
    "brx": CallLanguage("Bodo", "बड़ो", "brx"),
    "bs": CallLanguage("Bosnian", "Bosanski", "bs", ("kako", "zašto", "šta", "objasnite")),
    "bg": CallLanguage("Bulgarian", "Български", "bg", ("как", "защо", "какво", "обяснете")),
    "ca": CallLanguage("Catalan", "Català", "ca", ("com", "què", "per què", "explica")),
    "ckb": CallLanguage("Kurdish (Sorani)", "کوردیی ناوەندی", "ckb", direction="rtl"),
    "zh-HK": CallLanguage(
        "Chinese (Cantonese)",
        "粵語",
        "zh-HK",
        ("乜", "點解", "點樣", "邊個", "可唔可以"),
        unspaced=True,
    ),
    "hr": CallLanguage("Croatian", "Hrvatski", "hr", ("kako", "zašto", "što", "objasnite")),
    "cs": CallLanguage("Czech", "Čeština", "cs", ("jak", "proč", "co", "vysvětlete")),
    "da": CallLanguage("Danish", "Dansk", "da", ("hvordan", "hvorfor", "hvad", "forklar")),
    "dv": CallLanguage("Divehi", "ދިވެހި", "dv", direction="rtl"),
    "nl": CallLanguage("Dutch", "Nederlands", "nl", ("hoe", "waarom", "wat", "vertel", "leg uit")),
    "mhr": CallLanguage("Eastern Mari", "Олык марий", "mhr"),
    "et": CallLanguage("Estonian", "Eesti", "et", ("kuidas", "miks", "mis", "selgitage")),
    "fi": CallLanguage("Finnish", "Suomi", "fi", ("miten", "miksi", "mikä", "kerro")),
    "gl": CallLanguage("Galician", "Galego", "gl", ("como", "que", "por que", "explica")),
    "ka": CallLanguage("Georgian", "ქართული", "ka", ("როგორ", "რატომ", "რა")),
    "el": CallLanguage("Greek", "Ελληνικά", "el", ("πώς", "γιατί", "τι", "εξηγήστε")),
    "gu": CallLanguage("Gujarati", "ગુજરાતી", "gu", ("કેવી રીતે", "શા માટે", "શું")),
    "ha": CallLanguage("Hausa", "Hausa", "ha", ("yaya", "me ya sa", "menene")),
    "he": CallLanguage("Hebrew", "עברית", "he", ("איך", "למה", "מה", "הסבר"), direction="rtl"),
    "hu": CallLanguage("Hungarian", "Magyar", "hu", ("hogyan", "miért", "mi", "magyarázza")),
    "is": CallLanguage("Icelandic", "Íslenska", "is", ("hvernig", "hvers vegna", "hvað")),
    "kbd": CallLanguage("Kabardian", "Адыгэбзэ", "kbd"),
    "kab": CallLanguage("Kabyle", "Taqbaylit", "kab"),
    "kln": CallLanguage("Kalenjin", "Kalenjin", "kln"),
    "kn": CallLanguage("Kannada", "ಕನ್ನಡ", "kn", ("ಹೇಗೆ", "ಏಕೆ", "ಏನು")),
    "ks": CallLanguage("Kashmiri", "کٲشُر", "ks", direction="rtl"),
    "kk": CallLanguage("Kazakh", "Қазақша", "kk", ("қалай", "неге", "қандай")),
    "rw": CallLanguage("Kinyarwanda", "Ikinyarwanda", "rw"),
    "kok": CallLanguage("Konkani", "कोंकणी", "kok"),
    "ky": CallLanguage("Kyrgyz", "Кыргызча", "ky", ("кантип", "эмне үчүн", "эмне")),
    "lv": CallLanguage("Latvian", "Latviešu", "lv", ("kā", "kāpēc", "kas", "paskaidrojiet")),
    "lt": CallLanguage("Lithuanian", "Lietuvių", "lt", ("kaip", "kodėl", "kas", "paaiškinkite")),
    "lg": CallLanguage("Luganda", "Luganda", "lg"),
    "luo": CallLanguage("Luo", "Dholuo", "luo"),
    "mk": CallLanguage("Macedonian", "Македонски", "mk", ("како", "зошто", "што")),
    "ms": CallLanguage("Malay", "Bahasa Melayu", "ms", ("bagaimana", "mengapa", "apa", "jelaskan")),
    "ml": CallLanguage("Malayalam", "മലയാളം", "ml", ("എങ്ങനെ", "എന്തുകൊണ്ട്", "എന്ത്")),
    "mn": CallLanguage("Mongolian", "Монгол", "mn", ("хэрхэн", "яагаад", "юу")),
    "ne": CallLanguage("Nepali", "नेपाली", "ne", ("कसरी", "किन", "के", "बताउनुहोस्")),
    "kmr": CallLanguage("Kurdish (Kurmanji)", "Kurmancî", "kmr"),
    "no": CallLanguage("Norwegian", "Norsk", "no", ("hvordan", "hvorfor", "hva", "forklar")),
    "ps": CallLanguage("Pashto", "پښتو", "ps", ("څنګه", "ولې", "څه"), direction="rtl"),
    "pl": CallLanguage("Polish", "Polski", "pl", ("jak", "dlaczego", "co", "wyjaśnij")),
    "ro": CallLanguage("Romanian", "Română", "ro", ("cum", "de ce", "ce", "explică")),
    "sr": CallLanguage("Serbian", "Српски", "sr", ("како", "зашто", "шта", "kako", "zašto", "šta")),
    "sd": CallLanguage("Sindhi", "سنڌي", "sd", direction="rtl"),
    "si": CallLanguage("Sinhala", "සිංහල", "si", ("කොහොමද", "ඇයි", "කුමක්")),
    "sk": CallLanguage("Slovak", "Slovenčina", "sk", ("ako", "prečo", "čo", "vysvetlite")),
    "sl": CallLanguage("Slovenian", "Slovenščina", "sl", ("kako", "zakaj", "kaj", "pojasnite")),
    "sv": CallLanguage("Swedish", "Svenska", "sv", ("hur", "varför", "vad", "förklara")),
    "tl": CallLanguage("Tagalog", "Tagalog", "tl", ("paano", "bakit", "ano", "ipaliwanag")),
    "dav": CallLanguage("Taita", "Kidawida", "dav"),
    "tn": CallLanguage("Tswana", "Setswana", "tn"),
    "uk": CallLanguage("Ukrainian", "Українська", "uk", ("як", "чому", "що", "поясніть")),
    "ug": CallLanguage("Uyghur", "ئۇيغۇرچە", "ug", direction="rtl"),
    "uz": CallLanguage("Uzbek", "Oʻzbekcha", "uz", ("qanday", "nega", "nima")),
    "ve": CallLanguage("Venda", "Tshivenḓa", "ve"),
    "cy": CallLanguage("Welsh", "Cymraeg", "cy", ("sut", "pam", "beth", "esboniwch")),
    "yo": CallLanguage("Yoruba", "Yorùbá", "yo"),
    "zu": CallLanguage("Zulu", "IsiZulu", "zu"),
}
DEFAULT_CALL_LANGUAGES = tuple(CALL_LANGUAGES)

# Aura only supplies these six of the selected call languages. Microphone input
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


def provider_supports(language: str, model: str, provider: str) -> bool:
    """Check one explicit provider/model route before constructing a speech stream."""
    item = CALL_LANGUAGES.get(language)
    if item is None:
        return False
    if provider == "deepgram":
        return language == "en" or model in {"nova-3", "nova-3-general"}
    # The existing AssemblyAI adapter only configures its English streaming model.
    return provider == "assemblyai" and language == "en"


def recognition_supported(language: str, model: str, providers: Iterable[str]) -> bool:
    """Every configured fallback must support the selected spoken language."""
    paths = tuple(providers)
    return bool(paths) and all(provider_supports(language, model, provider) for provider in paths)


def speech_units(text: str, language: str) -> int:
    """A bounded length heuristic, not a tokenizer or word-count claim for CJK/Thai."""
    if CALL_LANGUAGES[language].unspaced:
        return sum(unicodedata.category(c).startswith(("L", "N")) for c in text) // 2
    return len(text.split())


def has_question_cue(text: str, language: str) -> bool:
    """Optional speculative-answer hint; the model detector handles uncued languages too."""
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
