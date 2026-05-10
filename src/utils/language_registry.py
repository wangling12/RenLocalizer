# -*- coding: utf-8 -*-
"""
Language Registry - Unified Language Mapping Module
===================================================

Single Source of Truth for all language mappings in RenLocalizer.

Provides three-tier mapping:
- Ren'Py codes (internal): schinese, tchinese, turkish, japanese
- API codes (translation services): zh-CN, zh-TW, tr, ja
- ISO/Font codes (font injection): zh, zh_tw, tr, ja

Usage:
    from src.utils.language_registry import LanguageRegistry

    registry = LanguageRegistry.get_instance()
    renpy_code = registry.normalize("schinese")       # -> "schinese"
    api_code = registry.to_api("schinese")            # -> "zh-CN"
    iso_code = registry.to_iso_font("schinese")       # -> "zh"
    is_rtl = registry.is_rtl("arabic")                # -> True
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import threading


@dataclass(frozen=True)
class LanguageInfo:
    """Immutable language information record."""
    renpy_code: str       # Ren'Py internal code (for tl/<lang>/ directories)
    api_code: str         # Translation API code
    iso_font_code: str    # ISO/Font code (for font injection)
    english_name: str     # English display name
    native_name: str      # Native language name
    is_rtl: bool = False  # Right-to-left writing direction


# ============================================================================
# RTL Language Set
# ============================================================================

RTL_LANGUAGES = {
    "arabic", "persian", "hebrew", "urdu", "pashto", "sindhi",
    "kurdish", "yiddish",
}


# ============================================================================
# Font Candidate Mapping (ISO font code -> ordered list of (font_family, is_rtl))
# ============================================================================

FONT_CANDIDATES: Dict[str, List[Tuple[str, bool]]] = {
    "zh": [("Noto Sans SC", False)],
    "zh_tw": [("Noto Sans TC", False)],
    "ja": [("Noto Sans JP", False), ("M PLUS 1p", False), ("Kosugi Maru", False)],
    "ko": [("Noto Sans KR", False), ("Nanum Gothic", False)],
    "ru": [("Noto Sans", False), ("PT Sans", False), ("Ubuntu", False)],
    "tr": [("Noto Sans", False), ("Inter", False), ("Open Sans", False)],
    "uk": [("Noto Sans", False), ("PT Sans", False), ("Ubuntu", False)],
    "th": [("Noto Sans Thai", False), ("Sarabun", False), ("Prompt", False)],
    "vi": [("Be Vietnam Pro", False), ("Noto Sans", False), ("Inter", False)],
    "fa": [("Vazirmatn", True), ("Noto Sans Arabic", True)],
    "ar": [("Noto Sans Arabic", True), ("Cairo", True), ("Tajawal", True)],
    "he": [("Noto Sans Hebrew", True), ("Rubik", True), ("Heebo", True)],
}


# ============================================================================
# Legacy Code Aliases (backward compatibility)
# ============================================================================

LEGACY_ALIASES: Dict[str, str] = {
    # Chinese variants (schinese/tchinese are the canonical Ren'Py codes)
    "chinese": "schinese",
    "chinese_s": "schinese",
    "chinese_t": "tchinese",
    "zh-cn": "schinese",
    "zh_cn": "schinese",
    "zh-hans": "schinese",
    "zh-tw": "tchinese",
    "zh_tw": "tchinese",
    "zh-hant": "tchinese",
    # Short codes
    "tr": "turkish",
    "en": "english",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "it": "italian",
    "pt": "portuguese",
    "ru": "russian",
    "pl": "polish",
    "nl": "dutch",
    "ja": "japanese",
    "ko": "korean",
    "ar": "arabic",
    "th": "thai",
    "vi": "vietnamese",
    "id": "indonesian",
    "ms": "malay",
    "hi": "hindi",
    "fa": "persian",
    "cs": "czech",
    "da": "danish",
    "fi": "finnish",
    "el": "greek",
    "he": "hebrew",
    "hu": "hungarian",
    "no": "norwegian",
    "ro": "romanian",
    "sv": "swedish",
    "uk": "ukrainian",
    "bg": "bulgarian",
    "ca": "catalan",
    "hr": "croatian",
    "sk": "slovak",
    "sl": "slovenian",
    "sr": "serbian",
    "af": "afrikaans",
    "sq": "albanian",
    "am": "amharic",
    "hy": "armenian",
    "az": "azerbaijani",
    "eu": "basque",
    "be": "belarusian",
    "bn": "bengali",
    "bs": "bosnian",
    "eo": "esperanto",
    "et": "estonian",
    "tl": "filipino",
    "gl": "galician",
    "ka": "georgian",
    "gu": "gujarati",
    "ht": "haitian_creole",
    "ha": "hausa",
    "is": "icelandic",
    "ig": "igbo",
    "ga": "irish",
    "jv": "javanese",
    "kn": "kannada",
    "kk": "kazakh",
    "km": "khmer",
    "ku": "kurdish",
    "ky": "kyrgyz",
    "lo": "lao",
    "lv": "latvian",
    "lt": "lithuanian",
    "lb": "luxembourgish",
    "mk": "macedonian",
    "mg": "malagasy",
    "ml": "malayalam",
    "mt": "maltese",
    "mi": "maori",
    "mr": "marathi",
    "mn": "mongolian",
    "my": "myanmar",
    "ne": "nepali",
    "ps": "pashto",
    "pa": "punjabi",
    "sm": "samoan",
    "gd": "scots_gaelic",
    "sn": "shona",
    "sd": "sindhi",
    "si": "sinhala",
    "so": "somali",
    "sw": "swahili",
    "tg": "tajik",
    "ta": "tamil",
    "te": "telugu",
    "ur": "urdu",
    "uz": "uzbek",
    "cy": "welsh",
    "xh": "xhosa",
    "yi": "yiddish",
    "yo": "yoruba",
    "zu": "zulu",
}


# ============================================================================
# Core Language Database
# ============================================================================

LANGUAGE_DATABASE: List[LanguageInfo] = [
    LanguageInfo("turkish", "tr", "tr", "Turkish", "Türkçe", False),
    LanguageInfo("english", "en", "en", "English", "English", False),
    LanguageInfo("german", "de", "de", "German", "Deutsch", False),
    LanguageInfo("french", "fr", "fr", "French", "Français", False),
    LanguageInfo("spanish", "es", "es", "Spanish", "Español", False),
    LanguageInfo("italian", "it", "it", "Italian", "Italiano", False),
    LanguageInfo("portuguese", "pt", "pt", "Portuguese", "Português", False),
    LanguageInfo("russian", "ru", "ru", "Russian", "Русский", False),
    LanguageInfo("polish", "pl", "pl", "Polish", "Polski", False),
    LanguageInfo("dutch", "nl", "nl", "Dutch", "Nederlands", False),
    LanguageInfo("japanese", "ja", "ja", "Japanese", "日本語", False),
    LanguageInfo("korean", "ko", "ko", "Korean", "한국어", False),
    LanguageInfo("schinese", "zh-CN", "zh", "Chinese (Simplified)", "简体中文", False),
    LanguageInfo("tchinese", "zh-TW", "zh_tw", "Chinese (Traditional)", "繁體中文", False),
    LanguageInfo("arabic", "ar", "ar", "Arabic", "العربية", True),
    LanguageInfo("thai", "th", "th", "Thai", "ไทย", False),
    LanguageInfo("vietnamese", "vi", "vi", "Vietnamese", "Tiếng Việt", False),
    LanguageInfo("indonesian", "id", "id", "Indonesian", "Bahasa Indonesia", False),
    LanguageInfo("malay", "ms", "ms", "Malay", "Bahasa Melayu", False),
    LanguageInfo("hindi", "hi", "hi", "Hindi", "हिन्दी", False),
    LanguageInfo("persian", "fa", "fa", "Persian (Farsi)", "فارسی", True),
    LanguageInfo("czech", "cs", "cs", "Czech", "Čeština", False),
    LanguageInfo("danish", "da", "da", "Danish", "Dansk", False),
    LanguageInfo("finnish", "fi", "fi", "Finnish", "Suomi", False),
    LanguageInfo("greek", "el", "el", "Greek", "Ελληνικά", False),
    LanguageInfo("hebrew", "he", "he", "Hebrew", "עברית", True),
    LanguageInfo("hungarian", "hu", "hu", "Hungarian", "Magyar", False),
    LanguageInfo("norwegian", "no", "no", "Norwegian", "Norsk", False),
    LanguageInfo("romanian", "ro", "ro", "Romanian", "Română", False),
    LanguageInfo("swedish", "sv", "sv", "Swedish", "Svenska", False),
    LanguageInfo("ukrainian", "uk", "uk", "Ukrainian", "Українська", False),
    LanguageInfo("bulgarian", "bg", "bg", "Bulgarian", "Български", False),
    LanguageInfo("catalan", "ca", "ca", "Catalan", "Català", False),
    LanguageInfo("croatian", "hr", "hr", "Croatian", "Hrvatski", False),
    LanguageInfo("slovak", "sk", "sk", "Slovak", "Slovenčina", False),
    LanguageInfo("slovenian", "sl", "sl", "Slovenian", "Slovenščina", False),
    LanguageInfo("serbian", "sr", "sr", "Serbian", "Српски", False),
    LanguageInfo("afrikaans", "af", "af", "Afrikaans", "Afrikaans", False),
    LanguageInfo("albanian", "sq", "sq", "Albanian", "Shqip", False),
    LanguageInfo("amharic", "am", "am", "Amharic", "አማርኛ", False),
    LanguageInfo("armenian", "hy", "hy", "Armenian", "Հայերեն", False),
    LanguageInfo("azerbaijani", "az", "az", "Azerbaijani", "Azərbaycanca", False),
    LanguageInfo("basque", "eu", "eu", "Basque", "Euskara", False),
    LanguageInfo("belarusian", "be", "be", "Belarusian", "Беларуская", False),
    LanguageInfo("bengali", "bn", "bn", "Bengali", "বাংলা", False),
    LanguageInfo("bosnian", "bs", "bs", "Bosnian", "Bosanski", False),
    LanguageInfo("esperanto", "eo", "eo", "Esperanto", "Esperanto", False),
    LanguageInfo("estonian", "et", "et", "Estonian", "Eesti", False),
    LanguageInfo("filipino", "tl", "tl", "Filipino", "Filipino", False),
    LanguageInfo("galician", "gl", "gl", "Galician", "Galego", False),
    LanguageInfo("georgian", "ka", "ka", "Georgian", "ქართული", False),
    LanguageInfo("gujarati", "gu", "gu", "Gujarati", "ગુજરાતી", False),
    LanguageInfo("haitian_creole", "ht", "ht", "Haitian Creole", "Kreyòl Ayisyen", False),
    LanguageInfo("hausa", "ha", "ha", "Hausa", "Hausa", False),
    LanguageInfo("icelandic", "is", "is", "Icelandic", "Íslenska", False),
    LanguageInfo("igbo", "ig", "ig", "Igbo", "Asụsụ Igbo", False),
    LanguageInfo("irish", "ga", "ga", "Irish", "Gaeilge", False),
    LanguageInfo("javanese", "jv", "jv", "Javanese", "Basa Jawa", False),
    LanguageInfo("kannada", "kn", "kn", "Kannada", "ಕನ್ನಡ", False),
    LanguageInfo("kazakh", "kk", "kk", "Kazakh", "Қазақ тілі", False),
    LanguageInfo("khmer", "km", "km", "Khmer", "ភាសាខ្មែរ", False),
    LanguageInfo("kurdish", "ku", "ku", "Kurdish", "Kurdî", True),
    LanguageInfo("kyrgyz", "ky", "ky", "Kyrgyz", "Кыргызча", False),
    LanguageInfo("lao", "lo", "lo", "Lao", "ພາສາລາວ", False),
    LanguageInfo("latvian", "lv", "lv", "Latvian", "Latviešu", False),
    LanguageInfo("lithuanian", "lt", "lt", "Lithuanian", "Lietuvių", False),
    LanguageInfo("luxembourgish", "lb", "lb", "Luxembourgish", "Lëtzebuergesch", False),
    LanguageInfo("macedonian", "mk", "mk", "Macedonian", "Македонски", False),
    LanguageInfo("malagasy", "mg", "mg", "Malagasy", "Malagasy", False),
    LanguageInfo("malayalam", "ml", "ml", "Malayalam", "മലയാളം", False),
    LanguageInfo("maltese", "mt", "mt", "Maltese", "Malti", False),
    LanguageInfo("maori", "mi", "mi", "Maori", "Māori", False),
    LanguageInfo("marathi", "mr", "mr", "Marathi", "मराठी", False),
    LanguageInfo("mongolian", "mn", "mn", "Mongolian", "Монгол", False),
    LanguageInfo("myanmar", "my", "my", "Myanmar (Burmese)", "ဗမာ", False),
    LanguageInfo("nepali", "ne", "ne", "Nepali", "नेपाली", False),
    LanguageInfo("pashto", "ps", "ps", "Pashto", "پښتو", True),
    LanguageInfo("punjabi", "pa", "pa", "Punjabi", "ਪੰਜਾਬੀ", False),
    LanguageInfo("samoan", "sm", "sm", "Samoan", "Gagana Sāmoa", False),
    LanguageInfo("scots_gaelic", "gd", "gd", "Scots Gaelic", "Gàidhlig", False),
    LanguageInfo("shona", "sn", "sn", "Shona", "chiShona", False),
    LanguageInfo("sindhi", "sd", "sd", "Sindhi", "سنڌي", True),
    LanguageInfo("sinhala", "si", "si", "Sinhala", "සිංහල", False),
    LanguageInfo("somali", "so", "so", "Somali", "Soomaali", False),
    LanguageInfo("swahili", "sw", "sw", "Swahili", "Kiswahili", False),
    LanguageInfo("tajik", "tg", "tg", "Tajik", "Тоҷикӣ", False),
    LanguageInfo("tamil", "ta", "ta", "Tamil", "தமிழ்", False),
    LanguageInfo("telugu", "te", "te", "Telugu", "తెలుగు", False),
    LanguageInfo("urdu", "ur", "ur", "Urdu", "اردو", True),
    LanguageInfo("uzbek", "uz", "uz", "Uzbek", "Oʻzbekcha", False),
    LanguageInfo("welsh", "cy", "cy", "Welsh", "Cymraeg", False),
    LanguageInfo("xhosa", "xh", "xh", "Xhosa", "isiXhosa", False),
    LanguageInfo("yiddish", "yi", "yi", "Yiddish", "ייִדיש", True),
    LanguageInfo("yoruba", "yo", "yo", "Yoruba", "Yorùbá", False),
    LanguageInfo("zulu", "zu", "zu", "Zulu", "isiZulu", False),
]

DEFAULT_LANGUAGE = "turkish"


class LanguageRegistry:
    """
    Unified language mapping registry (singleton).

    Provides normalization and conversion between Ren'Py codes,
    API codes, and ISO/font codes.
    """

    _instance: Optional["LanguageRegistry"] = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "LanguageRegistry":
        """Get or create the singleton instance (thread-safe)."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Index by renpy_code (lowercase key -> LanguageInfo)
        self._by_renpy: Dict[str, LanguageInfo] = {}
        # Index by api_code (lowercase key -> LanguageInfo)
        self._by_api: Dict[str, LanguageInfo] = {}
        # Legacy alias lookup (lowercase key -> renpy_code)
        self._legacy_map: Dict[str, str] = {}
        # Font candidates by ISO font code
        self._font_candidates: Dict[str, List[Tuple[str, bool]]] = {}

        if self._by_renpy:
            return  # Already initialized

        self._initialize()

    def _initialize(self) -> None:
        """Build internal indexes from LANGUAGE_DATABASE."""
        for lang in LANGUAGE_DATABASE:
            self._by_renpy[lang.renpy_code.lower()] = lang
            self._by_api[lang.api_code.lower()] = lang

        self._legacy_map = {k.lower(): v for k, v in LEGACY_ALIASES.items()}
        self._font_candidates = dict(FONT_CANDIDATES)

    # ========================================================================
    # Core Query Methods
    # ========================================================================

    def normalize(self, code: str) -> str:
        """
        Normalize any language code to its canonical Ren'Py code.

        Accepts Ren'Py codes, API codes, or legacy aliases.
        Returns DEFAULT_LANGUAGE for empty/None input.
        """
        if not code:
            return DEFAULT_LANGUAGE

        lowered = code.lower().strip()

        # Direct renpy_code match
        if lowered in self._by_renpy:
            return self._by_renpy[lowered].renpy_code

        # API code match
        if lowered in self._by_api:
            return self._by_api[lowered].renpy_code

        # Legacy alias match
        if lowered in self._legacy_map:
            return self._legacy_map[lowered]

        # Unknown code, return as-is
        return code

    def to_api(self, renpy_code: str) -> str:
        """Convert Ren'Py code to API code."""
        normalized = self.normalize(renpy_code)
        if normalized in self._by_renpy:
            return self._by_renpy[normalized].api_code
        return renpy_code

    def to_renpy(self, api_code: str) -> str:
        """Convert API code to Ren'Py code."""
        lowered = api_code.lower().strip()
        if lowered in self._by_api:
            return self._by_api[lowered].renpy_code
        return api_code

    def to_iso_font(self, renpy_code: str) -> str:
        """Convert Ren'Py code to ISO/font code for font injection."""
        normalized = self.normalize(renpy_code)
        if normalized in self._by_renpy:
            return self._by_renpy[normalized].iso_font_code
        return renpy_code

    def is_rtl(self, code: str) -> bool:
        """Check if a language uses right-to-left writing direction."""
        normalized = self.normalize(code)
        if normalized in self._by_renpy:
            return self._by_renpy[normalized].is_rtl
        # Also check against ISO font codes for font injection callers
        if normalized in self._font_candidates:
            return self._font_candidates[normalized][0][1]
        return normalized in RTL_LANGUAGES

    def get_font_candidates(self, iso_font_code: str) -> List[Tuple[str, bool]]:
        """Get ordered list of font candidates for an ISO font code."""
        return self._font_candidates.get(iso_font_code, [])

    def get_all_font_candidates(self) -> Dict[str, List[Tuple[str, bool]]]:
        """Get complete font candidate mapping (for UI listing)."""
        return dict(self._font_candidates)

    def get_display_name(self, renpy_code: str, style: str = "en") -> str:
        """
        Get display name for a language.

        style: 'en' for English name, 'native' for native name,
               'full' for 'Native (English)' format.
        """
        normalized = self.normalize(renpy_code)
        if normalized not in self._by_renpy:
            return renpy_code

        lang = self._by_renpy[normalized]
        if style == "native":
            return lang.native_name
        if style == "full":
            if lang.english_name.lower() != lang.native_name.lower():
                return f"{lang.native_name} ({lang.english_name})"
            return lang.english_name
        return lang.english_name

    def get_language_info(self, renpy_code: str) -> Optional[LanguageInfo]:
        """Get full LanguageInfo record for a Ren'Py code."""
        normalized = self.normalize(renpy_code)
        return self._by_renpy.get(normalized)

    # ========================================================================
    # Bulk Query Methods
    # ========================================================================

    def get_all_languages(self) -> List[LanguageInfo]:
        """Get all registered languages as LanguageInfo list."""
        return list(self._by_renpy.values())

    def get_ui_language_list(self) -> List[Dict[str, str]]:
        """Get language list suitable for UI dropdowns."""
        result = []
        for lang in self._by_renpy.values():
            display = (
                f"{lang.native_name} ({lang.english_name})"
                if lang.english_name.lower() != lang.native_name.lower()
                else lang.english_name
            )
            result.append({
                "renpy": lang.renpy_code,
                "api": lang.api_code,
                "english": lang.english_name,
                "native": lang.native_name,
                "display": display,
            })
        return result

    def get_renpy_to_api_map(self) -> Dict[str, str]:
        """Get Ren'Py -> API mapping dict (for backward compatibility)."""
        return {lang.renpy_code: lang.api_code for lang in self._by_renpy.values()}

    def get_api_to_renpy_map(self) -> Dict[str, str]:
        """Get API -> Ren'Py mapping dict (for backward compatibility)."""
        return {lang.api_code: lang.renpy_code for lang in self._by_api.values()}

    def is_supported(self, code: str) -> bool:
        """Check if a language code is supported."""
        normalized = self.normalize(code)
        return normalized in self._by_renpy

    def get_legacy_aliases(self) -> Dict[str, str]:
        """Get all legacy alias mappings (for debugging/testing)."""
        return dict(self._legacy_map)

    # ========================================================================
    # Module-level convenience functions
    # ========================================================================

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (mainly for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance._by_renpy.clear()
                cls._instance._by_api.clear()
                cls._instance._legacy_map.clear()
                cls._instance._font_candidates.clear()
            cls._instance = None


# Module-level convenience functions
def normalize(code: str) -> str:
    """Convenience: normalize any language code to Ren'Py code."""
    return LanguageRegistry.get_instance().normalize(code)


def to_api(renpy_code: str) -> str:
    """Convenience: Ren'Py code -> API code."""
    return LanguageRegistry.get_instance().to_api(renpy_code)


def to_renpy(api_code: str) -> str:
    """Convenience: API code -> Ren'Py code."""
    return LanguageRegistry.get_instance().to_renpy(api_code)


def to_iso_font(renpy_code: str) -> str:
    """Convenience: Ren'Py code -> ISO/font code."""
    return LanguageRegistry.get_instance().to_iso_font(renpy_code)


def is_rtl(renpy_code: str) -> bool:
    """Convenience: check if language is RTL."""
    return LanguageRegistry.get_instance().is_rtl(renpy_code)


def get_font_candidates(iso_font_code: str) -> List[Tuple[str, bool]]:
    """Convenience: get font candidates for ISO font code."""
    return LanguageRegistry.get_instance().get_font_candidates(iso_font_code)
