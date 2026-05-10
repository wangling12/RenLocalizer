# -*- coding: utf-8 -*-
"""
Comprehensive unit tests for LanguageRegistry module.

Covers normalization, mapping, RTL detection, font candidates,
backward compatibility, and edge cases.
"""

import unittest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.utils.language_registry import (
    LanguageRegistry,
    LanguageInfo,
    DEFAULT_LANGUAGE,
    RTL_LANGUAGES,
    FONT_CANDIDATES,
    LEGACY_ALIASES,
    LANGUAGE_DATABASE,
    normalize,
    to_api,
    to_renpy,
    to_iso_font,
    is_rtl,
    get_font_candidates,
)


class TestLanguageRegistrySingleton(unittest.TestCase):
    """Test singleton behavior."""

    def test_singleton_returns_same_instance(self):
        """Multiple calls to get_instance return the same object."""
        r1 = LanguageRegistry.get_instance()
        r2 = LanguageRegistry.get_instance()
        self.assertIs(r1, r2)

    def test_singleton_is_initialized(self):
        """Singleton has populated indexes."""
        registry = LanguageRegistry.get_instance()
        self.assertGreater(len(registry._by_renpy), 0)
        self.assertGreater(len(registry._by_api), 0)


class TestNormalize(unittest.TestCase):
    """Test normalize() method - core language code normalization."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_empty_string_returns_default(self):
        self.assertEqual(self.registry.normalize(""), DEFAULT_LANGUAGE)

    def test_none_returns_default(self):
        self.assertEqual(self.registry.normalize(None), DEFAULT_LANGUAGE)

    def test_renpy_code_passthrough(self):
        """Direct Ren'Py codes should return unchanged."""
        self.assertEqual(self.registry.normalize("schinese"), "schinese")
        self.assertEqual(self.registry.normalize("turkish"), "turkish")
        self.assertEqual(self.registry.normalize("japanese"), "japanese")

    def test_api_code_to_renpy(self):
        """API codes should resolve to Ren'Py codes."""
        self.assertEqual(self.registry.normalize("zh-CN"), "schinese")
        self.assertEqual(self.registry.normalize("tr"), "turkish")
        self.assertEqual(self.registry.normalize("ja"), "japanese")
        self.assertEqual(self.registry.normalize("ko"), "korean")

    def test_schinese_aliases(self):
        """All simplified Chinese aliases should resolve to schinese."""
        aliases = ["schinese", "chinese_s", "chinese", "zh-cn", "zh_cn", "zh-hans"]
        for alias in aliases:
            self.assertEqual(
                self.registry.normalize(alias),
                "schinese",
                f"Failed for alias: {alias}",
            )

    def test_tchinese_aliases(self):
        """All traditional Chinese aliases should resolve to tchinese."""
        aliases = ["tchinese", "chinese_t", "zh-tw", "zh_tw", "zh-hant"]
        for alias in aliases:
            self.assertEqual(
                self.registry.normalize(alias),
                "tchinese",
                f"Failed for alias: {alias}",
            )

    def test_case_insensitive(self):
        """Normalization should be case-insensitive."""
        self.assertEqual(self.registry.normalize("CHINESE_S"), "schinese")
        self.assertEqual(self.registry.normalize("Zh-CN"), "schinese")
        self.assertEqual(self.registry.normalize("TURKISH"), "turkish")

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace should be stripped."""
        self.assertEqual(self.registry.normalize("  schinese  "), "schinese")
        self.assertEqual(self.registry.normalize(" zh-CN "), "schinese")

    def test_unknown_code_returned_as_is(self):
        """Unknown codes should be returned unchanged."""
        self.assertEqual(self.registry.normalize("xyz"), "xyz")
        self.assertEqual(self.registry.normalize("unknown-lang"), "unknown-lang")

    def test_all_legacy_aliases_resolve(self):
        """Every legacy alias should resolve to a valid Ren'Py code."""
        for alias, expected in LEGACY_ALIASES.items():
            result = self.registry.normalize(alias)
            self.assertEqual(result, expected, f"Legacy alias '{alias}' failed")


class TestToApi(unittest.TestCase):
    """Test to_api() method - Ren'Py code to API code conversion."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_schinese(self):
        self.assertEqual(self.registry.to_api("schinese"), "zh-CN")

    def test_tchinese(self):
        self.assertEqual(self.registry.to_api("tchinese"), "zh-TW")

    def test_turkish(self):
        self.assertEqual(self.registry.to_api("turkish"), "tr")

    def test_japanese(self):
        self.assertEqual(self.registry.to_api("japanese"), "ja")

    def test_accepts_legacy_alias(self):
        """Should normalize input before conversion."""
        self.assertEqual(self.registry.to_api("schinese"), "zh-CN")
        self.assertEqual(self.registry.to_api("zh-cn"), "zh-CN")

    def test_unknown_code_returned_as_is(self):
        self.assertEqual(self.registry.to_api("unknown"), "unknown")


class TestToRenpy(unittest.TestCase):
    """Test to_renpy() method - API code to Ren'Py code conversion."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_zh_cn(self):
        self.assertEqual(self.registry.to_renpy("zh-CN"), "schinese")

    def test_zh_tw(self):
        self.assertEqual(self.registry.to_renpy("zh-TW"), "tchinese")

    def test_tr(self):
        self.assertEqual(self.registry.to_renpy("tr"), "turkish")

    def test_ja(self):
        self.assertEqual(self.registry.to_renpy("ja"), "japanese")

    def test_unknown_code_returned_as_is(self):
        self.assertEqual(self.registry.to_renpy("xx"), "xx")


class TestToIsoFont(unittest.TestCase):
    """Test to_iso_font() method - Ren'Py code to ISO/font code conversion."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_schinese_to_zh(self):
        """Simplified Chinese should map to 'zh' for font injection."""
        self.assertEqual(self.registry.to_iso_font("schinese"), "zh")

    def test_tchinese_to_zh_tw(self):
        """Traditional Chinese should map to 'zh_tw' for font injection."""
        self.assertEqual(self.registry.to_iso_font("tchinese"), "zh_tw")

    def test_turkish(self):
        self.assertEqual(self.registry.to_iso_font("turkish"), "tr")

    def test_japanese(self):
        self.assertEqual(self.registry.to_iso_font("japanese"), "ja")

    def test_korean(self):
        self.assertEqual(self.registry.to_iso_font("korean"), "ko")

    def test_accepts_legacy_alias(self):
        """Should normalize input before conversion."""
        self.assertEqual(self.registry.to_iso_font("schinese"), "zh")
        self.assertEqual(self.registry.to_iso_font("zh-cn"), "zh")


class TestIsRtl(unittest.TestCase):
    """Test is_rtl() method - right-to-left language detection."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_arabic_is_rtl(self):
        self.assertTrue(self.registry.is_rtl("arabic"))

    def test_persian_is_rtl(self):
        self.assertTrue(self.registry.is_rtl("persian"))

    def test_hebrew_is_rtl(self):
        self.assertTrue(self.registry.is_rtl("hebrew"))

    def test_urdu_is_rtl(self):
        self.assertTrue(self.registry.is_rtl("urdu"))

    def test_schinese_not_rtl(self):
        self.assertFalse(self.registry.is_rtl("schinese"))

    def test_turkish_not_rtl(self):
        self.assertFalse(self.registry.is_rtl("turkish"))

    def test_japanese_not_rtl(self):
        self.assertFalse(self.registry.is_rtl("japanese"))

    def test_unknown_not_rtl(self):
        self.assertFalse(self.registry.is_rtl("unknown"))


class TestFontCandidates(unittest.TestCase):
    """Test get_font_candidates() method."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_zh_has_candidates(self):
        """Simplified Chinese should have font candidates."""
        candidates = self.registry.get_font_candidates("zh")
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0][0], "Noto Sans SC")
        self.assertFalse(candidates[0][1])

    def test_zh_tw_has_candidates(self):
        """Traditional Chinese should have font candidates."""
        candidates = self.registry.get_font_candidates("zh_tw")
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0][0], "Noto Sans TC")

    def test_ja_has_candidates(self):
        candidates = self.registry.get_font_candidates("ja")
        self.assertGreater(len(candidates), 0)

    def test_arabic_rtl_font(self):
        """Arabic font candidates should be marked as RTL."""
        candidates = self.registry.get_font_candidates("ar")
        self.assertGreater(len(candidates), 0)
        self.assertTrue(candidates[0][1])

    def test_persian_rtl_font(self):
        candidates = self.registry.get_font_candidates("fa")
        self.assertGreater(len(candidates), 0)
        self.assertTrue(candidates[0][1])

    def test_unknown_empty(self):
        """Unknown ISO codes should return empty list."""
        candidates = self.registry.get_font_candidates("xx")
        self.assertEqual(candidates, [])


class TestGetDisplayName(unittest.TestCase):
    """Test get_display_name() method."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_english_name(self):
        self.assertEqual(
            self.registry.get_display_name("schinese", "en"),
            "Chinese (Simplified)",
        )

    def test_native_name(self):
        self.assertEqual(
            self.registry.get_display_name("schinese", "native"),
            "简体中文",
        )

    def test_full_name(self):
        self.assertEqual(
            self.registry.get_display_name("schinese", "full"),
            "简体中文 (Chinese (Simplified))",
        )

    def test_unknown_returns_code(self):
        self.assertEqual(self.registry.get_display_name("unknown"), "unknown")


class TestBulkQueries(unittest.TestCase):
    """Test bulk query methods."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_get_all_languages_not_empty(self):
        languages = self.registry.get_all_languages()
        self.assertGreater(len(languages), 90)

    def test_all_languages_have_unique_renpy_codes(self):
        codes = [lang.renpy_code for lang in self.registry.get_all_languages()]
        self.assertEqual(len(codes), len(set(codes)))

    def test_ui_language_list_structure(self):
        ui_list = self.registry.get_ui_language_list()
        self.assertGreater(len(ui_list), 0)
        for item in ui_list:
            self.assertIn("renpy", item)
            self.assertIn("api", item)
            self.assertIn("english", item)
            self.assertIn("native", item)
            self.assertIn("display", item)

    def test_renpy_to_api_map(self):
        renpy_map = self.registry.get_renpy_to_api_map()
        self.assertIn("schinese", renpy_map)
        self.assertEqual(renpy_map["schinese"], "zh-CN")
        self.assertIn("turkish", renpy_map)
        self.assertEqual(renpy_map["turkish"], "tr")

    def test_api_to_renpy_map(self):
        api_map = self.registry.get_api_to_renpy_map()
        self.assertIn("zh-CN", api_map)
        self.assertEqual(api_map["zh-CN"], "schinese")

    def test_is_supported(self):
        self.assertTrue(self.registry.is_supported("schinese"))
        self.assertTrue(self.registry.is_supported("zh-CN"))
        self.assertTrue(self.registry.is_supported("chinese_s"))  # legacy alias
        self.assertFalse(self.registry.is_supported("xyz"))


class TestRoundtrip(unittest.TestCase):
    """Test roundtrip consistency for all languages."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_renpy_api_renpy_roundtrip(self):
        """Ren'Py -> API -> Ren'Py should be identity."""
        for lang in self.registry.get_all_languages():
            api = self.registry.to_api(lang.renpy_code)
            back = self.registry.to_renpy(api)
            self.assertEqual(
                lang.renpy_code,
                back,
                f"Roundtrip failed for {lang.renpy_code}: {lang.renpy_code} -> {api} -> {back}",
            )


class TestConvenienceFunctions(unittest.TestCase):
    """Test module-level convenience functions."""

    def test_normalize(self):
        self.assertEqual(normalize("schinese"), "schinese")
        self.assertEqual(normalize("zh-CN"), "schinese")

    def test_to_api(self):
        self.assertEqual(to_api("schinese"), "zh-CN")

    def test_to_renpy(self):
        self.assertEqual(to_renpy("zh-CN"), "schinese")

    def test_to_iso_font(self):
        self.assertEqual(to_iso_font("schinese"), "zh")

    def test_is_rtl(self):
        self.assertTrue(is_rtl("arabic"))
        self.assertFalse(is_rtl("turkish"))

    def test_get_font_candidates(self):
        candidates = get_font_candidates("zh")
        self.assertGreater(len(candidates), 0)


class TestLanguageDatabase(unittest.TestCase):
    """Test the LANGUAGE_DATABASE structure."""

    def test_all_entries_are_language_info(self):
        for entry in LANGUAGE_DATABASE:
            self.assertIsInstance(entry, LanguageInfo)

    def test_all_entries_have_required_fields(self):
        for entry in LANGUAGE_DATABASE:
            self.assertTrue(entry.renpy_code)
            self.assertTrue(entry.api_code)
            self.assertTrue(entry.iso_font_code)
            self.assertTrue(entry.english_name)
            self.assertTrue(entry.native_name)

    def test_schinese_entry(self):
        """Verify the schinese entry is correct."""
        cs = None
        for lang in LANGUAGE_DATABASE:
            if lang.renpy_code == "schinese":
                cs = lang
                break
        self.assertIsNotNone(cs)
        self.assertEqual(cs.api_code, "zh-CN")
        self.assertEqual(cs.iso_font_code, "zh")
        self.assertFalse(cs.is_rtl)

    def test_tchinese_entry(self):
        """Verify the tchinese entry is correct."""
        ct = None
        for lang in LANGUAGE_DATABASE:
            if lang.renpy_code == "tchinese":
                ct = lang
                break
        self.assertIsNotNone(ct)
        self.assertEqual(ct.api_code, "zh-TW")
        self.assertEqual(ct.iso_font_code, "zh_tw")
        self.assertFalse(ct.is_rtl)


class TestFontInjectorIntegration(unittest.TestCase):
    """Test that font_injector.py works correctly with LanguageRegistry."""

    def test_schinese_font_injection_flow(self):
        """Simulate the font injection flow for schinese."""
        registry = LanguageRegistry.get_instance()

        # Step 1: Normalize input
        renpy_code = registry.normalize("schinese")
        self.assertEqual(renpy_code, "schinese")

        # Step 2: Get ISO font code
        iso_code = registry.to_iso_font(renpy_code)
        self.assertEqual(iso_code, "zh")

        # Step 3: Get font candidates
        candidates = registry.get_font_candidates(iso_code)
        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0][0], "Noto Sans SC")

        # Step 4: Check RTL status
        is_rtl = registry.is_rtl(renpy_code)
        self.assertFalse(is_rtl)

    def test_arabic_font_injection_flow(self):
        """Simulate the font injection flow for arabic."""
        registry = LanguageRegistry.get_instance()

        renpy_code = registry.normalize("arabic")
        self.assertEqual(renpy_code, "arabic")

        iso_code = registry.to_iso_font(renpy_code)
        self.assertEqual(iso_code, "ar")

        candidates = registry.get_font_candidates(iso_code)
        self.assertGreater(len(candidates), 0)

        is_rtl = registry.is_rtl(renpy_code)
        self.assertTrue(is_rtl)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and boundary conditions."""

    def setUp(self):
        self.registry = LanguageRegistry.get_instance()

    def test_mixed_case_legacy_alias(self):
        self.assertEqual(self.registry.normalize("ScHiNeSe"), "schinese")

    def test_api_code_with_different_case(self):
        self.assertEqual(self.registry.normalize("ZH-CN"), "schinese")
        self.assertEqual(self.registry.normalize("Zh-cn"), "schinese")

    def test_empty_api_code(self):
        """Empty string normalizes to default language, returns its API code."""
        result = self.registry.to_api("")
        self.assertEqual(result, "tr")  # DEFAULT_LANGUAGE is "turkish"

    def test_empty_renpy_code(self):
        result = self.registry.to_renpy("")
        self.assertEqual(result, "")

    def test_get_language_info(self):
        info = self.registry.get_language_info("schinese")
        self.assertIsNotNone(info)
        self.assertEqual(info.renpy_code, "schinese")
        self.assertEqual(info.api_code, "zh-CN")

    def test_get_language_info_unknown(self):
        info = self.registry.get_language_info("nonexistent")
        self.assertIsNone(info)

    def test_legacy_aliases_dict(self):
        aliases = self.registry.get_legacy_aliases()
        self.assertIn("chinese_s", aliases)
        self.assertEqual(aliases["chinese_s"], "schinese")
        self.assertIn("chinese", aliases)
        self.assertEqual(aliases["chinese"], "schinese")


if __name__ == "__main__":
    unittest.main()
