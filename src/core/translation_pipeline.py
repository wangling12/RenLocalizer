# -*- coding: utf-8 -*-
"""
Integrated Translation Pipeline
================================

Tek tıkla çeviri: EXE → UnRen → Translate → Çeviri → Kaydet

Bu modül tüm çeviri sürecini entegre bir pipeline olarak yönetir.
"""

import os
import sys
import ast
import logging
import asyncio
import json
import re
import time
from typing import Optional, List, Dict, Callable, Tuple, Union, Any
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import shutil  # En tepeye ekleyin
from src.utils.encoding import normalize_to_utf8_sig, read_text_safely, save_text_safely
from src.core.runtime_hook_template import render_runtime_hook

from PyQt6.QtCore import QObject, pyqtSignal, QThread

from src.utils.config import ConfigManager, get_effective_batch_size, get_engine_batch_size_cap
# sdk_finder removed
from src.core.tl_parser import TLParser, TranslationFile, TranslationEntry, get_translation_stats
from src.core.parser import RenPyParser
from src.core.translator import (
    TranslationManager,
    TranslationRequest,
    TranslationEngine,
    GoogleTranslator,
    DeepLTranslator,
    LibreTranslateTranslator,
)
from src.core.ai_translator import OpenAITranslator, GeminiTranslator, LocalLLMTranslator, DeepSeekTranslator
from src.core.output_formatter import RenPyOutputFormatter
from src.core.diagnostics import DiagnosticReport
from src.core.runtime_coverage import load_runtime_miss_log, score_runtime_miss_entries, summarize_runtime_miss_scores


# Ren'Py language codes -> API language codes mapping
# Uses LanguageRegistry as the single source of truth
def _get_renpy_to_api_lang():
    """Get Ren'Py to API language mapping from LanguageRegistry."""
    try:
        from src.utils.config import ConfigManager
        config = ConfigManager()
        return config.get_renpy_to_api_map()
    except Exception:
        # Fallback to LanguageRegistry when config is unavailable
        from src.utils.language_registry import LanguageRegistry
        return LanguageRegistry.get_instance().get_renpy_to_api_map()

class _LazyRenpyToApiLangMap:
    def __init__(self):
        self._cache = {}
        self._loaded = False

    def _load(self):
        if not self._loaded:
            self._cache = _get_renpy_to_api_lang()
            self._loaded = True
        return self._cache

    def get(self, key, default=None):
        return self._load().get(key, default)

    def items(self):
        return self._load().items()

    def __iter__(self):
        return iter(self._load())

    def __len__(self):
        return len(self._load())

    def __contains__(self, key):
        return key in self._load()


# Lazily loaded on first access to avoid blocking app startup.
RENPY_TO_API_LANG = _LazyRenpyToApiLangMap()

CORE_UI_RETRY_STRINGS = {
    "About",
    "Auto",
    "Back",
    "End Replay",
    "Help",
    "History",
    "Load",
    "Load Game",
    "Main Menu",
    "Preferences",
    "Prefs",
    "Q.Load",
    "Q.Save",
    "Save",
    "Skip",
    "Start",
    "Unseen Text",
}
SEPARATOR_REMNANTS = ("|||", "RNLSEP", "SEP777", "TXTSEP")
HOTKEY_SOURCE_RE = re.compile(r"^(?P<label>.+?)\s*/\s*(?P<hotkey>[A-Za-z])$")
HOTKEY_VISIBLE_RE = re.compile(r"^(?P<label>.+?)\s*\[(?P<hotkey>[A-Za-z])\]$")
ANGLE_WRAPPED_SINGLE_RE = re.compile(r"^<(?P<label>[^<>|]+)>$")
VISIBLE_TEXT_APOSTROPHES = ("'", "’", "‘", "ʼ")
VISIBLE_TEXT_DASHES = (" - ", " – ", " — ")
VISIBLE_TEXT_SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+|$)")
VISIBLE_TEXT_BRIDGE_PREFIXES = ("And", "But", "So", "Or", "Then")
PLACEHOLDER_BRACKET_RE = re.compile(r"\[[^\]]+\]")
RENPY_TAG_RE = re.compile(r"\{/?[^}]+\}")
HTML_LEAK_RE = re.compile(r"</?(?:span|div)\b", re.IGNORECASE)
PLACEHOLDER_REMNANT_RE = re.compile(
    r"(?i)(?:R[A-Z]{0,6}LPH[0-9A-F]{3,}|XRPYX_[A-Z0-9_]+|RNPY_[A-Z0-9_]+)"
)
TRANSLATION_ID_KEY_RE = re.compile(r"^id_[0-9a-f]{16,}$")
QUOTED_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\\\']|\\.)*\'')
IMAGE_ONLY_BLOCK_RE = re.compile(r'^\s*(?P<kind>imagebutton|hotspot)\b')
TEXTUAL_UI_HINT_RE = re.compile(r'\b(?:tooltip|alt)\b|^\s*(?:text|textbutton|label|caption)\b|\bText\s*\(')
HELPER_PROPERTY_RE = re.compile(r'^\s*(?:idle|hover|selected|selected_idle|selected_hover|background|foreground|add)\b')
DYNAMIC_UI_LINE_RE = re.compile(
    r'^\s*(?:text|tooltip|label|caption)\b.*(?:\.format\(|\bf["\'])|\bText\s*\(\s*(?:[fF]["\']|.*\.format\()'
)

COVERAGE_WARNING_UI_KEYS = {
    'image_only_ui': 'coverage_warning_image_only_ui',
    'compiled_only_scripts': 'coverage_warning_compiled_only',
    'dynamic_ui_runtime': 'coverage_warning_dynamic_ui',
}

COVERAGE_AUDIT_EXCLUDE_DIRS = {
    'tl',
    'cache',
    'saves',
    'renpy',
    'python-packages',
    'lib',
    '__pycache__',
}


class PipelineStage(Enum):
    """Pipeline aşamaları"""
    IDLE = "idle"
    VALIDATING = "validating"
    UNRPA = "unrpa"
    GENERATING = "generating"
    PARSING = "parsing"
    TRANSLATING = "translating"
    SAVING = "saving"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class PipelineResult:
    """Pipeline sonucu"""
    success: bool
    message: str
    stage: PipelineStage
    stats: Optional[Dict] = None
    output_path: Optional[str] = None
    error: Optional[str] = None


class TranslationPipeline(QObject):
    """
    Entegre çeviri pipeline'ı.
    
    Akış:
    1. Proje doğrulama
    2. UnRen (gerekirse)
    3. Translate komutu ile tl/<dil>/ oluşturma
    4. tl/<dil>/*.rpy dosyalarını parse etme
    5. old "..." metinlerini çevirme
    6. new "..." alanlarına yazma ve kaydetme
    """

    def _find_rpymc_files(self, directory: str) -> list:
        """Klasörde ve alt klasörlerinde .rpymc dosyalarını bulur."""
        rpymc_files = []
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith('.rpymc'):
                    rpymc_files.append(os.path.join(root, f))
        return rpymc_files

    def _extract_strings_from_rpymc_ast(self, ast_root) -> list:
        """
        AST'den stringleri çıkarır (İteratif & Güvenli).
        Recursion yerine Stack kullanarak derin nested yapılarda çökme riskini (StackOverflow) önler.
        """
        strings = set()
        # Set kullanarak O(1) lookup performansı (Red Flag 4 Fix)
        PRIORITY_KEYS = {'text', 'content', 'value', 'caption', 'label', 'description', 'message', 'body'}
        
        # Iterative Stack Approach
        stack = [ast_root]
        
        # Safety: Aşırı derin döngüler veya milyarlarca node ihtimaline karşı bir sayaç eklenebilir
        # ancak iteratif yığın Pythonda bellek bitene kadar çökmez (Recursion limitine takılmaz).
        
        while stack:
            node = stack.pop()
            
            if isinstance(node, str):
                s = node.strip()
                # 2 karakterden uzun ve sadece boşluk olmayan metinleri al
                if len(s) > 2 and not s.isspace():
                    strings.add(s)
            
            elif isinstance(node, (list, tuple)):
                # Listeyi stack'e ekle (Ters sıra ile eklersek orijinal sırayla işleriz ama Set için sıra önemsiz)
                stack.extend(node)
                
            elif isinstance(node, dict):
                # Dict değerlerini stack'e at
                for key, value in node.items():
                    # Key 'text' gibi öncelikli bir alansa, yine de stack'e atıp işliyoruz.
                    stack.append(value)
                    
            elif hasattr(node, '__dict__'):
                # Nesne özelliklerini gez
                for value in vars(node).values():
                    stack.append(value)

        result = list(strings)
        if result:
            self.log_message.emit('debug', f".rpymc extracted {len(result)} unique strings.")
        return result
    
    # Signals
    stage_changed = pyqtSignal(str, str)  # stage, message
    progress_updated = pyqtSignal(int, int, str)  # current, total, text
    log_message = pyqtSignal(str, str)  # level, message
    finished = pyqtSignal(object)  # PipelineResult
    show_warning = pyqtSignal(str, str)  # title, message - for popup warnings
    
    def __init__(
        self,
        config: ConfigManager,
        translation_manager: TranslationManager,
        parent=None
    ):
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        
        self.config = config
        self.translation_manager = translation_manager
        self.tl_parser = TLParser()
        self.diagnostic_report = DiagnosticReport()
        # Use a less alarming name for error log, e.g. pipeline_debug.log
        self.error_log_path = Path("pipeline_debug.log")
        self.normalize_count = 0
        self._last_diagnostic_path: Optional[str] = None
        
        # State
        self.current_stage = PipelineStage.IDLE
        self.should_stop = False
        self.is_running = False
        
        # Log Buffering (v2.5.3 Optimization)
        self._log_queue = []
        self._last_log_time = 0
        self._log_throttle_interval = 0.08  # ~12 FPS limit for logs
        
        # Settings (default values; overridden via configure)
        self.game_exe_path: Optional[str] = None
        self.project_path: Optional[str] = None
        self.target_language: str = "turkish"
        self.source_language: str = "en"
        self.engine: TranslationEngine = TranslationEngine.GOOGLE
        self.auto_unren: bool = True # Legacy name, means auto extraction
        self.use_proxy: bool = False
        self._translation_guard_events: List[Dict[str, Any]] = []
        self._translation_guard_counts: Dict[str, int] = {}
        self._translation_guard_sample_limit = 200

    def _emit_scan_progress(
        self,
        label: str,
        current: int,
        total: int,
        file_path: Union[str, Path],
        step: int = 25,
    ) -> None:
        """Emit coarse-grained scan progress without flooding the log."""
        if total <= 0:
            return
        if current != 1 and current != total and current % step != 0:
            return
        file_name = Path(file_path).name
        self.log_message.emit("info", f"{label}: {current}/{total} ({file_name})")

    def _reset_translation_diagnostics(self) -> None:
        self.diagnostic_report = DiagnosticReport()
        self._last_diagnostic_path = None
        self._translation_guard_events = []
        self._translation_guard_counts = {
            'unchanged_by_engine': 0,
            'blocked_as_corrupted': 0,
            'recovered_by_retry': 0,
            'recovered_by_synthesized_variant': 0,
        }

    def _record_translation_guard_event(
        self,
        *,
        category: str,
        file_path: str,
        translation_id: str = '',
        original_text: str = '',
        translated_text: str = '',
        detail: str = '',
        line_number: int = 0,
    ) -> None:
        if category not in self._translation_guard_counts:
            self._translation_guard_counts[category] = 0
        self._translation_guard_counts[category] += 1
        if len(self._translation_guard_events) >= self._translation_guard_sample_limit:
            return
        self._translation_guard_events.append({
            'category': category,
            'file_path': file_path,
            'translation_id': translation_id,
            'line_number': line_number,
            'detail': detail,
            'original_preview': (original_text or '')[:160],
            'translated_preview': (translated_text or '')[:160],
        })

    def _extract_validation_placeholders(self, text: str, source_text: str = '') -> List[str]:
        placeholders = PLACEHOLDER_BRACKET_RE.findall(text or '')
        hotkey_match = HOTKEY_SOURCE_RE.match((source_text or '').strip())
        if hotkey_match and placeholders:
            hotkey_suffix = f"[{hotkey_match.group('hotkey').upper()}]"
            stripped_text = (text or '').strip()
            if stripped_text.endswith(hotkey_suffix):
                for idx in range(len(placeholders) - 1, -1, -1):
                    if placeholders[idx].upper() == hotkey_suffix:
                        placeholders.pop(idx)
                        break
        return sorted(re.sub(r'\s+', '', ph) for ph in placeholders)

    def _classify_translation_corruption(self, original: str, translated: str) -> Optional[str]:
        orig = (original or '').strip()
        trans = (translated or '').strip()
        if not orig or not trans:
            return None
        if any(remnant in trans for remnant in SEPARATOR_REMNANTS):
            return 'separator_remnant'
        if '⟦' in trans or '⟧' in trans or PLACEHOLDER_REMNANT_RE.search(trans):
            return 'placeholder_remnant'
        if HTML_LEAK_RE.search(trans):
            return 'html_leakage'
        if len(trans) > max(len(orig) * 4, len(orig) + 80):
            return 'length_inflation'
        if not self.validate_placeholders(original=orig, translated=trans):
            return 'placeholder_set_mismatch'
        if self._extract_validation_placeholders(orig) != self._extract_validation_placeholders(trans, source_text=orig):
            return 'placeholder_set_mismatch'
        # If the original still contains ⟦⟧ placeholder tokens, the Ren'Py
        # tags they represent will appear in the translated (restored) text but
        # not in the original string — skip the tag set check in that case to
        # avoid false-positive KORUMA blocks on legitimately restored output.
        if '⟦' not in orig and '⟧' not in orig:
            if sorted(RENPY_TAG_RE.findall(orig)) != sorted(RENPY_TAG_RE.findall(trans)):
                return 'renpy_tag_set_mismatch'
        return None

    def _get_guard_reason_text(self, reason: str) -> str:
        reason_key_map = {
            'separator_remnant': (
                'guard_reason_separator_remnant',
                'separator markers leaked into the output',
            ),
            'placeholder_remnant': (
                'guard_reason_placeholder_remnant',
                'placeholder tokens leaked into the output',
            ),
            'html_leakage': (
                'guard_reason_html_leakage',
                'HTML markup leaked into the output',
            ),
            'length_inflation': (
                'guard_reason_length_inflation',
                'translated text expanded far beyond the source',
            ),
            'placeholder_set_mismatch': (
                'guard_reason_placeholder_set_mismatch',
                'placeholder structure changed',
            ),
            'renpy_tag_set_mismatch': (
                'guard_reason_renpy_tag_set_mismatch',
                "Ren'Py text tags changed",
            ),
        }

        key, default = reason_key_map.get(
            reason,
            ('guard_reason_unknown', (reason or 'suspicious translator output').replace('_', ' ')),
        )
        return self.config.get_log_text(key, default)

    def _sanitize_translation_for_output(
        self,
        *,
        original: str,
        translated: str,
        file_path: str,
        translation_id: str,
        line_number: int = 0,
    ) -> Tuple[str, Optional[str]]:
        reason = self._classify_translation_corruption(original, translated)
        if reason is None:
            return translated, None
        self._record_translation_guard_event(
            category='blocked_as_corrupted',
            file_path=file_path,
            translation_id=translation_id,
            original_text=original,
            translated_text=translated,
            detail=reason,
            line_number=line_number,
        )
        try:
            self.diagnostic_report.mark_blocked(
                file_path,
                translation_id,
                'corrupt_blocked',
                original_text=original,
                translated_text=translated,
            )
        except Exception:
            pass
        return original, reason

    def _should_retry_unchanged_core_ui(self, original_text: str) -> bool:
        return (original_text or '').strip() in CORE_UI_RETRY_STRINGS

    def _get_requested_translation_batch_size(self) -> int:
        """Return the user-requested batch size for the active engine family."""
        if self.engine in (TranslationEngine.OPENAI, TranslationEngine.GEMINI, TranslationEngine.LOCAL_LLM):
            return getattr(self.config.translation_settings, 'ai_batch_size', 50)
        return getattr(self.config.translation_settings, 'max_batch_size', 100)

    def _get_effective_translation_batch_size(self) -> int:
        """Return the runtime-effective batch size after engine-specific caps."""
        requested = self._get_requested_translation_batch_size()
        if self.engine in (TranslationEngine.OPENAI, TranslationEngine.GEMINI, TranslationEngine.LOCAL_LLM):
            return requested
        return get_effective_batch_size(requested, self.engine)

    def _emit_batch_size_cap_notice_if_needed(self, requested: int, effective: int) -> None:
        """Log a friendly info message when an engine-specific batch cap is applied."""
        if effective == requested:
            return
        cap = get_engine_batch_size_cap(self.engine) or effective
        engine_name = getattr(self.engine, 'value', str(self.engine))
        self.log_message.emit(
            "info",
            self.config.get_log_text(
                'log_batch_size_engine_cap_applied',
                'Requested batch size {requested} exceeds the effective limit for {engine}; using {effective} (cap: {cap}).',
                requested=requested,
                engine=engine_name,
                effective=effective,
                cap=cap,
            ),
        )

    def _execute_single_request_with_retry_mode(
        self,
        loop: asyncio.AbstractEventLoop,
        translator: Any,
        request: TranslationRequest,
    ) -> Optional[Any]:
        ts = getattr(self.config, 'translation_settings', None)
        original_config_flag = getattr(ts, 'aggressive_retry_translation', False) if ts else False
        original_translator_flag = getattr(translator, 'aggressive_retry', None)
        try:
            if ts is not None:
                ts.aggressive_retry_translation = True
            if original_translator_flag is not None:
                translator.aggressive_retry = True
            return loop.run_until_complete(translator.translate_single(request))
        except Exception as exc:
            self.logger.debug("Core UI retry failed: %s", exc)
            return None
        finally:
            if ts is not None:
                ts.aggressive_retry_translation = original_config_flag
            if original_translator_flag is not None:
                translator.aggressive_retry = original_translator_flag

    def _retry_unchanged_core_ui(
        self,
        loop: asyncio.AbstractEventLoop,
        request: Optional[TranslationRequest],
        entry: TranslationEntry,
        current_text: str,
    ) -> Tuple[str, bool]:
        if request is None or not self._should_retry_unchanged_core_ui(entry.original_text):
            return current_text, False

        translator = self.translation_manager.translators.get(request.engine)
        if translator is None:
            return current_text, False

        retry_result = self._execute_single_request_with_retry_mode(loop, translator, request)
        if retry_result and getattr(retry_result, 'success', False):
            retry_text = (getattr(retry_result, 'translated_text', '') or '').strip()
            if retry_text and retry_text != entry.original_text.strip():
                return retry_text, True

        fallback_translator = getattr(translator, 'fallback_translator', None) or getattr(translator, '_fallback', None)
        if fallback_translator is not None:
            fallback_result = self._execute_single_request_with_retry_mode(loop, fallback_translator, request)
            if fallback_result and getattr(fallback_result, 'success', False):
                fallback_text = (getattr(fallback_result, 'translated_text', '') or '').strip()
                if fallback_text and fallback_text != entry.original_text.strip():
                    return fallback_text, True

        return current_text, False

    def _synthesize_hotkey_visible_variants(self, mapping: Dict[str, str]) -> Dict[str, str]:
        additions: Dict[str, str] = {}
        for original, translated in list(mapping.items()):
            match = HOTKEY_SOURCE_RE.match((original or '').strip())
            if not match:
                continue
            label = match.group('label').strip()
            hotkey = match.group('hotkey').upper()
            visible_key = f"{label} [{hotkey}]"
            translated_stripped = (translated or '').strip()
            translated_label = translated_stripped

            translated_hotkey_match = HOTKEY_SOURCE_RE.match(translated_stripped)
            if translated_hotkey_match:
                translated_label = translated_hotkey_match.group('label').strip()
            else:
                visible_match = HOTKEY_VISIBLE_RE.match(translated_stripped)
                if visible_match and visible_match.group('hotkey').upper() == hotkey:
                    translated_label = visible_match.group('label').strip()

            visible_value = f"{translated_label} [{hotkey}]"
            if (
                visible_key
                and visible_value
                and visible_key != visible_value
                and visible_key not in mapping
                and visible_key not in additions
            ):
                additions[visible_key] = visible_value
        return additions

    def _unwrap_single_angle_text(self, text: str) -> Optional[str]:
        stripped = (text or '').strip()
        if not stripped:
            return None

        match = ANGLE_WRAPPED_SINGLE_RE.match(stripped)
        if match:
            return match.group('label').strip() or None

        if stripped.startswith('<') and stripped.endswith('>') and '|' not in stripped:
            return stripped[1:-1].strip() or None
        if stripped.startswith('<') and '|' not in stripped:
            return stripped[1:].strip() or None
        if stripped.endswith('>') and '|' not in stripped:
            return stripped[:-1].strip() or None
        return None

    def _synthesize_angle_wrapper_variants(self, mapping: Dict[str, str]) -> Dict[str, str]:
        additions: Dict[str, str] = {}
        for original, translated in list(mapping.items()):
            inner_original = self._unwrap_single_angle_text(original)
            if not inner_original:
                continue

            translated_stripped = (translated or '').strip()
            inner_translated = self._unwrap_single_angle_text(translated_stripped) or translated_stripped
            inner_translated = inner_translated.strip()
            if (
                not inner_translated
                or inner_original == inner_translated
                or inner_original in mapping
                or inner_original in additions
            ):
                continue
            additions[inner_original] = inner_translated
        return additions

    def _generate_visible_text_aliases(self, text: str) -> List[str]:
        stripped = (text or '').strip()
        if not stripped:
            return []

        variants: set[str] = set()

        if any(ch in stripped for ch in VISIBLE_TEXT_APOSTROPHES):
            for apostrophe in VISIBLE_TEXT_APOSTROPHES:
                candidate = stripped
                for current in VISIBLE_TEXT_APOSTROPHES:
                    candidate = candidate.replace(current, apostrophe)
                if candidate != stripped:
                    variants.add(candidate)

        if "..." in stripped:
            variants.add(stripped.replace("...", "…"))
        if "…" in stripped:
            variants.add(stripped.replace("…", "..."))

        for dash in VISIBLE_TEXT_DASHES:
            if dash in stripped:
                for replacement in VISIBLE_TEXT_DASHES:
                    if replacement != dash:
                        variants.add(stripped.replace(dash, replacement))

        normalized_space = re.sub(r"\s+", " ", stripped.replace("\u00a0", " ")).strip()
        if normalized_space != stripped:
            variants.add(normalized_space)

        return sorted(v for v in variants if v and v != stripped)

    def _synthesize_visible_text_variants(self, mapping: Dict[str, str]) -> Dict[str, str]:
        additions: Dict[str, str] = {}
        blocked: set[str] = set()

        for original, translated in list(mapping.items()):
            translated_stripped = (translated or '').strip()
            if not translated_stripped:
                continue

            for alias in self._generate_visible_text_aliases(original):
                if alias in blocked:
                    continue
                if alias in mapping:
                    blocked.add(alias)
                    additions.pop(alias, None)
                    continue
                existing = additions.get(alias)
                if existing is not None and existing != translated_stripped:
                    blocked.add(alias)
                    additions.pop(alias, None)
                    continue
                additions[alias] = translated_stripped

        return additions

    def _split_visible_sentences(self, text: str) -> List[str]:
        stripped = (text or '').strip()
        if not stripped:
            return []
        parts = [match.group(0).strip() for match in VISIBLE_TEXT_SENTENCE_RE.finditer(stripped)]
        return [part for part in parts if part]

    def _get_extraction_mode(self) -> str:
        ts = getattr(self.config, 'translation_settings', None)
        mode = str(getattr(ts, 'extraction_mode', 'balanced') or 'balanced').strip().lower()
        if mode not in ('strict', 'balanced', 'aggressive'):
            return 'balanced'
        return mode

    def _is_aggressive_extraction_mode(self) -> bool:
        return self._get_extraction_mode() == 'aggressive'

    def _build_bridge_prefixed_variant(self, text: str, prefix: str) -> Optional[str]:
        stripped = (text or '').strip()
        if not stripped:
            return None
        if stripped.lower().startswith(prefix.lower() + ' '):
            return None
        if stripped[0].isalpha():
            stripped = stripped[0].lower() + stripped[1:]
        return f"{prefix} {stripped}"

    def _synthesize_visible_fragment_variants(self, mapping: Dict[str, str]) -> Dict[str, str]:
        additions: Dict[str, str] = {}
        blocked: set[str] = set()
        is_aggressive = self._is_aggressive_extraction_mode()
        min_source_length = 64 if is_aggressive else 80
        min_source_sentences = 2 if is_aggressive else 3
        min_target_sentences = 1 if is_aggressive else 2
        max_count_limit = 3 if is_aggressive else 2
        min_fragment_length = 36 if is_aggressive else 48
        min_fragment_words = 5 if is_aggressive else 7

        for original, translated in list(mapping.items()):
            source = (original or '').strip()
            target = (translated or '').strip()
            if not source or not target:
                continue
            if len(source) < min_source_length:
                continue
            if any(token in source for token in ('[', ']', '{', '}')):
                continue

            source_sentences = self._split_visible_sentences(source)
            target_sentences = self._split_visible_sentences(target)
            if len(source_sentences) < min_source_sentences or len(target_sentences) < min_target_sentences:
                continue

            max_count = min(max_count_limit, len(source_sentences) - 1, len(target_sentences))
            for count in range(1, max_count + 1):
                source_fragment = ' '.join(source_sentences[:count]).strip()
                target_fragment = ' '.join(target_sentences[:count]).strip()
                if len(source_fragment) < min_fragment_length or source_fragment.count(' ') < min_fragment_words:
                    continue

                candidate_keys = [source_fragment]
                for prefix in VISIBLE_TEXT_BRIDGE_PREFIXES:
                    prefixed = self._build_bridge_prefixed_variant(source_fragment, prefix)
                    if prefixed:
                        candidate_keys.append(prefixed)

                for candidate in candidate_keys:
                    if candidate in blocked:
                        continue
                    if candidate in mapping:
                        blocked.add(candidate)
                        additions.pop(candidate, None)
                        continue
                    existing = additions.get(candidate)
                    if existing is not None and existing != target_fragment:
                        blocked.add(candidate)
                        additions.pop(candidate, None)
                        continue
                    additions[candidate] = target_fragment

        return additions

    def _normalize_runtime_alias_text(self, text: str) -> str:
        normalized = (text or '').strip()
        if not normalized:
            return ''
        for current in VISIBLE_TEXT_APOSTROPHES:
            normalized = normalized.replace(current, "'")
        normalized = normalized.replace('…', '...')
        normalized = normalized.replace('–', '-').replace('—', '-').replace('−', '-')
        normalized = re.sub(r'\s+', ' ', normalized.replace('\u00a0', ' ')).strip()
        return normalized.casefold()

    def _find_runtime_alias_match_index(self, container_text: str, source_text: str) -> int:
        lowered_container = container_text.casefold()
        lowered_source = source_text.casefold()
        start = lowered_container.find(lowered_source)
        if start < 0:
            return -1
        end = start + len(source_text)
        before = container_text[start - 1] if start > 0 else ''
        after = container_text[end] if end < len(container_text) else ''
        if before and before.isalnum():
            return -1
        if after and after.isalnum():
            return -1
        return start

    def _build_runtime_observed_alias(self, observed_text: str, source_text: str, translated_text: str) -> Optional[str]:
        observed = (observed_text or '').strip()
        source = (source_text or '').strip()
        translated = (translated_text or '').strip()
        if not observed or not source or not translated:
            return None

        if self._normalize_runtime_alias_text(observed) == self._normalize_runtime_alias_text(source):
            return translated

        start = self._find_runtime_alias_match_index(observed, source)
        if start < 0:
            return None
        end = start + len(source)
        return observed[:start] + translated + observed[end:]

    def _synthesize_runtime_observed_variants(self, mapping: Dict[str, str], lang_dir: str) -> Dict[str, str]:
        log_path = Path(lang_dir) / 'diagnostics' / 'runtime_missed_strings.jsonl'
        if not log_path.is_file():
            return {}

        analysis = self.analyze_runtime_miss_log(str(log_path))
        additions: Dict[str, str] = {}
        blocked: set[str] = set()
        is_aggressive = self._is_aggressive_extraction_mode()
        accepted_actions = {'promote_alias', 'review_candidate'} if is_aggressive else {'promote_alias'}
        min_source_length = 24 if is_aggressive else 32
        min_source_words = 3 if is_aggressive else 4
        normalized_mapping = {
            self._normalize_runtime_alias_text(source): (source, target)
            for source, target in mapping.items()
            if source and target
        }

        for candidate in analysis.get('top_candidates', []):
            if candidate.get('suggested_action') not in accepted_actions:
                continue
            observed_text = (candidate.get('text') or '').strip()
            if not observed_text or observed_text in mapping or observed_text in blocked:
                continue

            matched_pairs: list[tuple[str, str]] = []
            normalized_observed = self._normalize_runtime_alias_text(observed_text)
            exact_pair = normalized_mapping.get(normalized_observed)
            if exact_pair is not None:
                matched_pairs.append(exact_pair)
            else:
                for source_text, translated_text in mapping.items():
                    source_clean = (source_text or '').strip()
                    translated_clean = (translated_text or '').strip()
                    if not source_clean or not translated_clean:
                        continue
                    if len(source_clean) < min_source_length or source_clean.count(' ') < min_source_words:
                        continue
                    if any(token in source_clean for token in ('[', ']', '{', '}')):
                        continue
                    if self._find_runtime_alias_match_index(observed_text, source_clean) >= 0:
                        matched_pairs.append((source_clean, translated_clean))
                    if len(matched_pairs) > 1:
                        break

            if len(matched_pairs) != 1:
                if len(matched_pairs) > 1:
                    blocked.add(observed_text)
                    additions.pop(observed_text, None)
                continue

            source_text, translated_text = matched_pairs[0]
            alias_value = self._build_runtime_observed_alias(observed_text, source_text, translated_text)
            if not alias_value or alias_value == observed_text:
                continue

            existing = additions.get(observed_text)
            if existing is not None and existing != alias_value:
                blocked.add(observed_text)
                additions.pop(observed_text, None)
                continue
            additions[observed_text] = alias_value

        return additions

    def _reopen_stale_tl_entries(self, tl_files: List[TranslationFile]) -> Dict[str, int]:
        reopened_counts = {
            'reopened': 0,
            'corrupted': 0,
            'unchanged_core_ui': 0,
        }

        for tl_file in tl_files:
            for entry in tl_file.entries:
                translated = (entry.translated_text or '').strip()
                if not translated:
                    continue

                reason: Optional[str] = None
                corruption_reason = self._classify_translation_corruption(entry.original_text, translated)
                if corruption_reason is not None:
                    reason = 'corrupted'
                    detail = corruption_reason
                elif translated == (entry.original_text or '').strip() and self._should_retry_unchanged_core_ui(entry.original_text):
                    reason = 'unchanged_core_ui'
                    detail = 'unchanged_core_ui'
                else:
                    continue

                reopened_counts['reopened'] += 1
                reopened_counts[reason] += 1
                entry.translated_text = ''
                self._record_translation_guard_event(
                    category='reopened_for_retranslation',
                    file_path=entry.file_path or tl_file.file_path,
                    translation_id=entry.translation_id or entry.compute_id(),
                    original_text=entry.original_text,
                    translated_text=translated,
                    detail=detail,
                    line_number=entry.line_number,
                )

        return reopened_counts

    def _write_translation_reports(self, lang_dir: str) -> None:
        diag_dir = os.path.join(lang_dir, 'diagnostics')
        os.makedirs(diag_dir, exist_ok=True)
        diag_path = os.path.join(diag_dir, f'diagnostic_{self.target_language}.json')
        self.diagnostic_report.write(diag_path)
        self._last_diagnostic_path = diag_path
        self.log_message.emit('info', self.config.get_log_text('log_diagnostic_written', path=diag_path))

        report_path = os.path.join(diag_dir, 'translation_blocked_or_fallback.json')
        payload = {
            'generated_at': int(time.time()),
            'counts': dict(self._translation_guard_counts),
            'sample_limit': self._translation_guard_sample_limit,
            'samples': self._translation_guard_events,
        }
        save_text_safely(Path(report_path), json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _iter_audit_files(self, game_dir: str, extension: str):
        for root, dirs, files in os.walk(game_dir):
            dirs[:] = [
                d for d in dirs
                if d.lower() not in COVERAGE_AUDIT_EXCLUDE_DIRS
            ]
            for filename in files:
                if filename.lower().endswith(extension):
                    yield Path(root) / filename

    def _relative_audit_path(self, game_dir: str, file_path: Path) -> str:
        try:
            return file_path.relative_to(game_dir).as_posix()
        except Exception:
            return file_path.as_posix()

    def _decode_literal_candidate(self, raw_literal: str) -> str:
        try:
            value = ast.literal_eval(raw_literal)
            return value if isinstance(value, str) else ""
        except Exception:
            return raw_literal.strip('"\'')

    def _block_has_textual_hint(self, parser: RenPyParser, block_lines: List[str]) -> bool:
        for line in block_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if TEXTUAL_UI_HINT_RE.search(line):
                return True
            if not HELPER_PROPERTY_RE.match(line) and 'Notify(' not in line:
                continue
            for raw_literal in QUOTED_LITERAL_RE.findall(line):
                candidate = self._decode_literal_candidate(raw_literal)
                if candidate and parser.is_meaningful_text(candidate):
                    return True
        return False

    def _audit_image_only_ui(self, game_dir: str) -> Dict[str, Any] | None:
        parser = RenPyParser(self.config)
        samples: List[Dict[str, Any]] = []
        count = 0

        for file_path in self._iter_audit_files(game_dir, '.rpy'):
            content = read_text_safely(file_path)
            if not content:
                continue
            lines = content.splitlines()
            idx = 0
            while idx < len(lines):
                raw_line = lines[idx]
                match = IMAGE_ONLY_BLOCK_RE.match(raw_line)
                if not match:
                    idx += 1
                    continue

                start_idx = idx
                block_lines = [raw_line]
                stripped = raw_line.strip()
                base_indent = len(raw_line) - len(raw_line.lstrip())
                idx += 1

                if stripped.endswith(':'):
                    while idx < len(lines):
                        next_line = lines[idx]
                        next_stripped = next_line.strip()
                        if next_stripped and not next_stripped.startswith('#'):
                            next_indent = len(next_line) - len(next_line.lstrip())
                            if next_indent <= base_indent:
                                break
                        block_lines.append(next_line)
                        idx += 1

                if self._block_has_textual_hint(parser, block_lines):
                    continue

                count += 1
                if len(samples) < 20:
                    samples.append({
                        'file_path': self._relative_audit_path(game_dir, file_path),
                        'line_number': start_idx + 1,
                        'kind': match.group('kind'),
                    })

        if not count:
            return None
        return {
            'code': 'image_only_ui',
            'count': count,
            'samples': samples,
        }

    def _audit_compiled_only_scripts(self, game_dir: str) -> Dict[str, Any] | None:
        rpyc_enabled = bool(
            getattr(self.config.translation_settings, 'enable_rpyc_reader', False)
            or getattr(self, 'include_rpyc', False)
        )
        if rpyc_enabled:
            return None

        rpy_paths = {
            self._relative_audit_path(game_dir, path.with_suffix(''))
            for path in self._iter_audit_files(game_dir, '.rpy')
        }
        rpyc_only = sorted(
            self._relative_audit_path(game_dir, path)
            for path in self._iter_audit_files(game_dir, '.rpyc')
            if self._relative_audit_path(game_dir, path.with_suffix('')) not in rpy_paths
        )
        if not rpyc_only:
            return None

        return {
            'code': 'compiled_only_scripts',
            'count': len(rpyc_only),
            'samples': [{'file_path': path} for path in rpyc_only[:20]],
        }

    def _audit_dynamic_ui_runtime(self, game_dir: str) -> Dict[str, Any] | None:
        runtime_hook_enabled = self._is_runtime_hook_enabled()
        if runtime_hook_enabled:
            return None

        samples: List[Dict[str, Any]] = []
        count = 0
        for file_path in self._iter_audit_files(game_dir, '.rpy'):
            content = read_text_safely(file_path)
            if not content:
                continue
            for idx, raw_line in enumerate(content.splitlines(), start=1):
                stripped = raw_line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if not DYNAMIC_UI_LINE_RE.search(raw_line):
                    continue
                count += 1
                if len(samples) < 20:
                    samples.append({
                        'file_path': self._relative_audit_path(game_dir, file_path),
                        'line_number': idx,
                        'preview': stripped[:160],
                    })

        if not count:
            return None
        return {
            'code': 'dynamic_ui_runtime',
            'count': count,
            'samples': samples,
        }

    def analyze_runtime_miss_log(self, log_path: str) -> Dict[str, Any]:
        """Score runtime miss diagnostics for future alias promotion.

        This is intentionally read-only for now. It helps inspect missed
        runtime strings without changing translation outputs during gameplay.
        """
        entries = load_runtime_miss_log(log_path)
        scored = score_runtime_miss_entries(entries)
        summary = summarize_runtime_miss_scores(entries)
        return {
            'summary': summary,
            'top_candidates': [
                {
                    'text': item.text,
                    'score': item.score,
                    'confidence': item.confidence,
                    'suggested_action': item.suggested_action,
                    'risk': item.risk,
                    'reasons': item.reasons,
                    'entry': item.entry,
                }
                for item in scored[:50]
            ],
        }

    def _collect_coverage_warnings(self, game_dir: str) -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        for collector in (
            self._audit_image_only_ui,
            self._audit_compiled_only_scripts,
            self._audit_dynamic_ui_runtime,
        ):
            try:
                warning = collector(game_dir)
            except Exception as exc:
                self.logger.debug("Coverage audit '%s' failed: %s", collector.__name__, exc)
                continue
            if warning:
                warnings.append(warning)
                self.diagnostic_report.add_coverage_warning(
                    warning['code'],
                    warning['count'],
                    samples=warning.get('samples'),
                )
        return warnings

    def _emit_coverage_warning_summary(self) -> None:
        warnings = getattr(self.diagnostic_report, 'coverage_warnings', [])
        if not warnings:
            return

        for warning in warnings[:3]:
            text_key = COVERAGE_WARNING_UI_KEYS.get(warning.get('code', ''), '')
            default_text = warning.get('code', 'warning')
            localized = self.config.get_ui_text(text_key, default_text).format(count=warning.get('count', 0))
            self.log_message.emit('warning', f"⚠️ {localized}")

        if self._last_diagnostic_path:
            report_line = self.config.get_ui_text(
                'coverage_warning_report_path',
                'Diagnostics report: {path}',
            ).format(path=self._last_diagnostic_path)
            self.log_message.emit('warning', report_line)

    def _is_generated_export_file(self, file_path: str) -> bool:
        basename = os.path.basename(file_path or '')
        lowered = basename.lower()
        return lowered.startswith('zz_rl_exported_') and lowered.endswith('.rpy')

    def _is_runtime_hook_enabled(self) -> bool:
        """Single source of truth for whether runtime assets should be generated."""
        ts = getattr(self.config, 'translation_settings', None)
        if ts is None:
            return False

        enable_runtime_hook = bool(getattr(ts, 'enable_runtime_hook', True))
        auto_generate_hook = bool(getattr(ts, 'auto_generate_hook', True))
        force_runtime = bool(getattr(ts, 'force_runtime_translation', False))
        return force_runtime or (enable_runtime_hook and auto_generate_hook)

    def emit_log(self, level: str, message: str):
        """
        Send log message to UI with throttling for better performance.
        High-priority logs (error, warning) are sent immediately.
        """
        if level in ('error', 'warning'):
            self.log_message.emit(level, message)
            return

        current_time = time.time()
        if current_time - self._last_log_time > self._log_throttle_interval:
            self.log_message.emit(level, message)
            self._last_log_time = current_time

    def _log_error(self, message: str):
        """Persist errors for later inspection (not shown to user as 'fatal')."""
        # Only log if debug mode is enabled or config allows debug logs
        if getattr(self.config, 'debug_mode', False) or getattr(self, 'always_log_errors', False):
            try:
                with self.error_log_path.open("a", encoding="utf-8") as f:
                    f.write(message + "\n")
            except Exception:
                self.logger.debug(f"Error log yazılamadı: {message}")
        # Also record diagnostic-level errors
        try:
            self.diagnostic_report.mark_skipped('pipeline', f'error:{message}')
        except Exception:
            pass
    
    def configure(
        self,
        game_exe_path: str,
        target_language: str,
        source_language: str = "en",
        engine: TranslationEngine = TranslationEngine.GOOGLE,
        auto_unren: bool = True,
        use_proxy: bool = False,
        include_deep_scan: bool = False,
        include_rpyc: bool = False
    ):
        """Pipeline ayarlarını yapılandır.
        
        Args:
            game_exe_path: Can be either:
                - Path to game .exe file (GUI mode)
                - Path to game directory (CLI mode)
        """
        self.include_deep_scan = include_deep_scan
        self.include_rpyc = include_rpyc
        self.game_exe_path = game_exe_path
        
        # Determine project_path based on whether input is file or directory
        if os.path.isdir(game_exe_path):
            # Directory path provided (CLI mode) - use as project root
            candidate = game_exe_path
            # If the directory is named 'game', go up one level
            if os.path.basename(candidate).lower() == 'game':
                candidate = os.path.dirname(candidate)
            # If no 'game' subfolder, check if parent has one
            elif not os.path.isdir(os.path.join(candidate, 'game')):
                parent = os.path.dirname(candidate)
                if os.path.isdir(os.path.join(parent, 'game')):
                    candidate = parent
        else:
            # File path provided (GUI mode) - use parent directory
            candidate = os.path.dirname(game_exe_path)
            try:
                if os.path.basename(candidate).lower() == 'game':
                    # EXE located inside <project>/game/Game.exe; use project root
                    candidate = os.path.dirname(candidate)
                    self.log_message.emit('info', self.config.get_ui_text('pipeline_project_normalize_game'))
                elif not os.path.isdir(os.path.join(candidate, 'game')):
                    # If candidate lacks a game folder but parent has it, use parent
                    parent = os.path.dirname(candidate)
                    if os.path.isdir(os.path.join(parent, 'game')):
                        candidate = parent
                        self.log_message.emit('info', self.config.get_ui_text('pipeline_project_normalize_parent'))
            except Exception:
                # Defensive: if any error occurs, fall back to dirname
                candidate = os.path.dirname(game_exe_path)
        
        self.project_path = candidate
        reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
        self.target_language = reverse_lang_map.get((target_language or "").lower(), target_language)
        self.source_language = source_language
        self.engine = engine
        self.auto_unren = auto_unren
        self.use_proxy = use_proxy
    
    def stop(self):
        """Pipeline'ı durdur"""
        self.should_stop = True
        self.log_message.emit("warning", self.config.get_ui_text("stop_requested"))
    
    def _set_stage(self, stage: PipelineStage, message: str = ""):
        """Aşamayı değiştir ve sinyal gönder"""
        self.current_stage = stage
        self.stage_changed.emit(stage.value, message)
        
        # Localized stage label
        stage_label = self.config.get_log_text(f"stage_{stage.value}", stage.value.upper())
        self.log_message.emit("info", f"[{stage_label}] {message}")
    
    def run(self):
        """Pipeline'ı çalıştır"""
        self.is_running = True
        self.should_stop = False
        
        try:
            result = self._run_pipeline()
            self.finished.emit(result)
        except Exception as e:
            self.logger.exception("Pipeline hatası")
            result = PipelineResult(
                success=False,
                message=f"Beklenmeyen hata: {str(e)}",
                stage=PipelineStage.ERROR,
                error=str(e)
            )
            self.finished.emit(result)
        finally:
            self.is_running = False
    
    def _run_pipeline(self) -> PipelineResult:
        """Ana pipeline akışı"""
        self._reset_translation_diagnostics()
        
        # 1. Doğrulama
        self._set_stage(PipelineStage.VALIDATING, self.config.get_ui_text("stage_validating"))
        
        # game_exe_path can be either:
        # 1. An .exe file path (traditional GUI usage)
        # 2. A directory path (CLI usage with --mode full)
        if not self.game_exe_path:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_invalid_exe"),
                stage=PipelineStage.ERROR
            )
        
        # Accept both file and directory paths
        is_file = os.path.isfile(self.game_exe_path)
        is_dir = os.path.isdir(self.game_exe_path)
        
        if not is_file and not is_dir:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_invalid_exe") + f" (path does not exist: {self.game_exe_path})",
                stage=PipelineStage.ERROR
            )
        
        # Ensure project_path is normalized in case the user selected an EXE
        # inside a 'game' subfolder or in a nested path.
        project_path = self.project_path
        try:
            # If project_path currently points to a 'game' folder, normalize up one level
            if os.path.basename(project_path).lower() == 'game':
                self.log_message.emit('info', self.config.get_ui_text('pipeline_project_normalize_game'))
                project_path = os.path.dirname(project_path)
            # If project_path doesn't have a 'game' folder but parent does, normalize up
            elif not os.path.isdir(os.path.join(project_path, 'game')):
                parent = os.path.dirname(project_path)
                if os.path.isdir(os.path.join(parent, 'game')):
                    self.log_message.emit('info', self.config.get_ui_text('pipeline_project_normalize_parent'))
                    project_path = parent
        except Exception:
            # on failure, leave project_path as-is
            pass
        game_dir = os.path.join(project_path, 'game')
        
        if not os.path.isdir(game_dir):
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_game_folder_missing"),
                stage=PipelineStage.ERROR
            )
        
        # .rpy dosyası kontrolü
        has_rpy = self._has_rpy_files(game_dir)
        has_rpyc = self._has_rpyc_files(game_dir)
        has_rpa = self._has_rpa_files(game_dir)  # Arşiv dosyası kontrolü

        # .rpymc dosyalarını bul ve gerçek AST tabanlı okuyucuyu kullan
        self.rpymc_entries = []
        should_scan_rpym = getattr(self.config.translation_settings, 'scan_rpym_files', False)
        
        if should_scan_rpym:
            rpymc_files = self._find_rpymc_files(game_dir)
            if rpymc_files:
                from src.core.rpyc_reader import extract_texts_from_rpyc
                for rpymc_path in rpymc_files:
                    try:
                        texts = extract_texts_from_rpyc(rpymc_path, config_manager=self.config)
                        for t in texts:
                            text_val = t.get('text') or ""
                            if not text_val:
                                continue
                            ctx_path = t.get('context_path') or []
                            if isinstance(ctx_path, str):
                                ctx_path = [ctx_path]
                            entry = TranslationEntry(
                                original_text=text_val,
                                translated_text="",
                                file_path=str(rpymc_path),
                                line_number=t.get('line_number', 0) or 0,
                                entry_type="rpymc",
                                character=t.get('character'),
                                source_comment=None,
                                block_id=None,
                                context_path=ctx_path,
                                translation_id=TLParser.make_translation_id(
                                        str(rpymc_path), t.get('line_number', 0) or 0, text_val, ctx_path, t.get('raw_text')
                                    )
                            )
                            self.rpymc_entries.append(entry)
                    except Exception as e:
                        msg = f".rpymc extraction failed: {rpymc_path} ({e})"
                        self.log_message.emit('warning', msg)
                        self._log_error(msg)

                # Log .rpymc entry count
                self.log_message.emit('debug', self.config.get_log_text('rpymc_entry_count', count=len(self.rpymc_entries)))
        else:
            self.log_message.emit('debug', "Skipping .rpymc scan (scan_rpym_files disabled)")
        
        if self.should_stop:
            return self._stopped_result()
        
        # 2. UnRen/UnRPA (gerekirse) - .rpyc VEYA .rpa dosyası varsa çalıştır
        # Platform-aware: Windows uses UnRen batch, Linux/macOS uses unrpa
        # DÜZELTME: .rpy olsa bile .rpa varsa (ve auto_unren açıksa) extraction yapılmalı.
        # Çünkü dışarıdaki .rpy dosyaları eksik/yardımcı olabilir, asıl veri .rpa içindedir.
        needs_extraction = has_rpa and self.auto_unren
        needs_decompile = not has_rpy and has_rpyc and self.auto_unren
        
        if needs_extraction or needs_decompile:
            self.log_message.emit("info", self.config.get_log_text('rpa_extraction_needed'))
            self._set_stage(PipelineStage.UNRPA, self.config.get_ui_text("stage_unren"))
            
            # Decompile/Extract
            success = self._run_extraction(project_path)
            
            if not success:
                # On non-Windows, if unrpa failed but we have rpyc files, we can still continue
                import os as _os
                if _os.name != "nt" and has_rpyc:
                    self.log_message.emit("warning", self.config.get_log_text('log_rpa_failed_rpyc_continue'))
                else:
                    return PipelineResult(
                        success=False,
                        message=self.config.get_ui_text("unren_launch_failed").format(error=""),
                        stage=PipelineStage.ERROR
                    )
            
            # CRITICAL: Clean up engine-level translations if they were accidentally created
            # This prevents technical common scripts from breaking the game
            tl_path = os.path.join(game_dir, 'tl')
            if os.path.exists(tl_path):
                for root, dirs, files in os.walk(tl_path):
                     if 'common' in root.replace('\\', '/').split('/'):
                          for f in files:
                               try: os.remove(os.path.join(root, f))
                               except Exception as e:
                                   self.logger.warning(f"Failed to clean up common file {f}: {e}")
            
            # Tekrar kontrol
            has_rpy = self._has_rpy_files(game_dir)
        
        # RPYC-only mode: If no .rpy but has .rpyc and RPYC Reader is enabled
        rpyc_only_mode = False
        if not has_rpy and has_rpyc:
            # Check if RPYC reader is enabled
            rpyc_enabled = getattr(self.config.translation_settings, 'enable_rpyc_reader', False) or getattr(self, 'include_rpyc', False)
            if rpyc_enabled:
                self.log_message.emit("info", self.config.get_ui_text("pipeline_rpyc_only_mode", "RPYC-only mode: No .rpy files found, reading .rpyc files directly."))
                rpyc_only_mode = True
            else:
                return PipelineResult(
                    success=False,
                    message=self.config.get_ui_text("pipeline_no_rpy_files") + " " + self.config.get_ui_text("pipeline_enable_rpyc_hint", "(Try enabling RPYC Reader)"),
                    stage=PipelineStage.ERROR
                )
        
        if self.should_stop:
            return self._stopped_result()
        
        # 2.5. Kaynak dosyaları çevrilebilir hale getir
        self._set_stage(PipelineStage.GENERATING, self.config.get_ui_text("stage_generating"))
        self._make_source_translatable(game_dir)
        
        if self.should_stop:
            return self._stopped_result()
        
        # 3. Translate komutu
        self._set_stage(PipelineStage.GENERATING, f"{self.config.get_ui_text('stage_generating')} ({self.target_language})")
        
        tl_dir = os.path.join(game_dir, 'tl', self.target_language)
        
        # Zaten varsa atla - Fakat kaynak dosyalar güncellenmişse tekrar çıkar
        needs_extract = False
        if not os.path.isdir(tl_dir) or not self._has_rpy_files(tl_dir):
            needs_extract = True
        elif self._needs_re_extraction(game_dir, tl_dir):
            self.log_message.emit("info", self.config.get_ui_text("pipeline_source_updated", "Source files updated. Re-extracting translations for {lang}...").replace("{lang}", str(self.target_language)))
            needs_extract = True
            
        if needs_extract:
            success = self._run_translate_command(project_path)
            
            if not success and not os.path.isdir(tl_dir):
                return PipelineResult(
                    success=False,
                    message=self.config.get_ui_text("pipeline_translate_failed"),
                    stage=PipelineStage.ERROR
                )
        else:
            self.log_message.emit("info", self.config.get_ui_text("pipeline_tl_exists_skip").replace("{lang}", str(self.target_language)))
        
        if self.should_stop:
            return self._stopped_result()
        
        # 4. Parse
        self._set_stage(PipelineStage.PARSING, self.config.get_ui_text("stage_parsing"))
        
        # Ren'Py klasör adı ile API/ISO kodunu eşle
        reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
        renpy_lang = reverse_lang_map.get(self.target_language.lower(), self.target_language)

        tl_path = os.path.join(game_dir, 'tl')
        tl_files = self.tl_parser.parse_directory(tl_path, renpy_lang)


        # Yaln?zca hedef dil alt?ndaki dosyalar? kabul et; di?er dil klas?rlerini hari? tut
        target_tl_dir = os.path.normcase(os.path.join(tl_path, renpy_lang))
        filtered_files: List[TranslationFile] = []
        for tl_file in tl_files:
            if self._is_generated_export_file(tl_file.file_path):
                self.log_message.emit("debug", f"[ExportFilter] Skipping generated export file: {tl_file.file_path}")
                continue
            fp_norm = os.path.normcase(tl_file.file_path)
            if fp_norm.startswith(target_tl_dir):
                tl_file.entries = [
                    e for e in tl_file.entries
                    if os.path.normcase(e.file_path or tl_file.file_path).startswith(target_tl_dir)
                ]
                filtered_files.append(tl_file)
            else:
                self.log_message.emit("info", self.config.get_log_text('other_lang_folder_skipped', path=tl_file.file_path))
        tl_files = filtered_files


        # Phase 5: Deep Scan Integration
        if getattr(self, 'include_deep_scan', False):
            self.log_message.emit("info", self.config.get_log_text('deep_scan_running'))
            try:
                parser = RenPyParser(self.config)
                # Scan source files
                scan_res = parser.extract_combined(
                    str(game_dir), include_rpy=True, include_rpyc=True, 
                    include_deep_scan=True, recursive=True,
                    exclude_dirs=['renpy', 'common', 'tl', 'lib', 'python-packages'], # Security: skip engine
                    progress_callback=lambda current, total, file_path: self._emit_scan_progress(
                        "Deep scan progress",
                        current,
                        total,
                        file_path,
                        step=25,
                    ),
                )
                
                existing = {e.original_text for t in tl_files for e in t.entries}
                missing = []
                for entries in scan_res.values():
                    for e in entries:
                        txt = e.get('text')
                        if txt and txt not in existing and len(txt) > 1:
                            missing.append(e)
                            existing.add(txt)
                
                if missing:
                     self.log_message.emit("info", self.config.get_log_text('deep_scan_found', count=len(missing)))
                     deepscan_dir = os.path.join(tl_path, renpy_lang)
                     os.makedirs(deepscan_dir, exist_ok=True)
                     d_file = os.path.join(deepscan_dir, "strings_deepscan.rpy")
                     
                     lines = ["# Deep Scan generated translations", f"translate {renpy_lang} strings:\n"]
                     for m in missing:
                         o = m['text'].replace('"', '\\"').replace('\n', '\\n')
                         if m.get('context'): lines.append(f"    # context: {m['context']}")
                         lines.append(f'    old "{o}"\n    new ""\n')
                         
                     with open(d_file, 'w', encoding="utf-8") as f:
                         f.write('\n'.join(lines))
                         
                     # Add new file to pipeline processing
                     for ntf in self.tl_parser.parse_directory(deepscan_dir, renpy_lang):
                         if os.path.normcase(ntf.file_path) == os.path.normcase(d_file):
                             tl_files.append(ntf)
                             break
            except Exception as e:
                self.log_message.emit("warning", self.config.get_log_text('deep_scan_error', error=str(e)))

        # Phase 5.5: Unrpyc Decompile Integration (rpyc_reader ile tamamlayıcı)
        # .rpyc dosyalarını geçici klasöre decompile et, regex parser'dan geçir,
        # rpyc_reader'ın bulamadığı metinleri tl_files'a ekle.
        _unrpyc_enabled = getattr(self.config.translation_settings, 'enable_unrpyc_decompile', True)
        if _unrpyc_enabled and has_rpyc:
            self.log_message.emit("debug", self.config.get_log_text(
                'unrpyc_decompile_running', default="Starting unrpyc decompile scan…"))
            try:
                from src.utils.unrpyc_adapter import UnrpycAdapter as _UnrpycAdapter
                from pathlib import Path as _Path
                import glob as _glob

                _adapter = _UnrpycAdapter()
                if _adapter.available:
                    _rpyc_files = [
                        _Path(p) for p in _glob.glob(
                            os.path.join(game_dir, '**', '*.rpyc'), recursive=True
                        )
                        if not any(
                            skip in p.replace('\\', '/').split('/')
                            for skip in ('tl', 'renpy', 'common', 'cache', '__pycache__')
                        )
                    ]
                    if _rpyc_files:
                        with _adapter.decompile_to_temp(_rpyc_files, _Path(game_dir)) as (_tmp, _decompiled):
                            if _decompiled:
                                self.log_message.emit("info", self.config.get_log_text(
                                    'unrpyc_decompile_found',
                                    default="Unrpyc: {count} file(s) decompiled.",
                                    count=len(_decompiled)))
                                _parser_uc = RenPyParser(self.config)
                                _scan_uc = _parser_uc.extract_combined(
                                    _tmp,
                                    include_rpy=True,
                                    include_rpyc=False,
                                    include_deep_scan=False,
                                    recursive=True,
                                    exclude_dirs=['tl', 'cache', '__pycache__'],
                                )
                                _existing_uc = {e.original_text for t in tl_files for e in t.entries}
                                _missing_uc = []
                                for _uc_entries in _scan_uc.values():
                                    for _uc_e in _uc_entries:
                                        _txt = _uc_e.get('text')
                                        if _txt and _txt not in _existing_uc and len(_txt) > 1:
                                            _missing_uc.append(_uc_e)
                                            _existing_uc.add(_txt)

                                if _missing_uc:
                                    self.log_message.emit("info", self.config.get_log_text(
                                        'unrpyc_decompile_new_strings',
                                        default="Unrpyc: {count} additional string(s) found.",
                                        count=len(_missing_uc)))
                                    _uc_out_dir = os.path.join(tl_path, renpy_lang)
                                    os.makedirs(_uc_out_dir, exist_ok=True)
                                    _uc_file = os.path.join(_uc_out_dir, "strings_unrpyc.rpy")
                                    _uc_lines = [
                                        "# Strings found via unrpyc decompile (complementary to RPYC reader)",
                                        f"translate {renpy_lang} strings:\n",
                                    ]
                                    for _m in _missing_uc:
                                        _o = _m['text'].replace('"', '\\"').replace('\n', '\\n')
                                        if _m.get('context'):
                                            _uc_lines.append(f"    # context: {_m['context']}")
                                        _uc_lines.append(f'    old "{_o}"\n    new ""\n')
                                    with open(_uc_file, 'w', encoding='utf-8') as _f:
                                        _f.write('\n'.join(_uc_lines))
                                    for _ntf in self.tl_parser.parse_directory(_uc_out_dir, renpy_lang):
                                        if os.path.normcase(_ntf.file_path) == os.path.normcase(_uc_file):
                                            tl_files.append(_ntf)
                                            break
                else:
                    self.log_message.emit("debug",
                        "Unrpyc decompile: no decompiler backend available — skipping. "
                        "Install unrpyc or rpycdec to enable complementary decompile scanning.")
            except Exception as _uc_exc:
                self.log_message.emit("warning",
                    self.config.get_log_text('unrpyc_decompile_error',
                                             default="Unrpyc decompile scan failed: {error}",
                                             error=str(_uc_exc)))

        # Hata raporunda görülen UnicodeDecodeError'ları engellemek için tl çıktısını
        # tümüyle UTF-8-SIG formatında normalize et (renpy loader katı UTF-8 kullanıyor).
        try:
            normalized = self._normalize_tl_encodings(os.path.join(tl_path, renpy_lang))
            if normalized:
                self.log_message.emit("info", self.config.get_log_text('log_tl_normalized', count=normalized))
                self.normalize_count = normalized
        except Exception as e:
            msg = self.config.get_log_text('encoding_normalize_failed', path="tl", error=str(e))
            self.log_message.emit("warning", msg)
            self._log_error(msg)
        
        if not tl_files:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_files_not_found_parse"),
                stage=PipelineStage.ERROR
            )

        reopened_counts = self._reopen_stale_tl_entries(tl_files)
        if reopened_counts['reopened']:
            self.log_message.emit(
                "info",
                "Reopened stale TL entries for retranslation: "
                f"{reopened_counts['reopened']} "
                f"(corrupted={reopened_counts['corrupted']}, "
                f"unchanged_core_ui={reopened_counts['unchanged_core_ui']})"
            )

        # Çevrilmemiş girişleri topla
        all_entries = []
        for tl_file in tl_files:
            all_entries.extend(tl_file.get_untranslated())

        # Initialize diagnostic report
        try:
            self.diagnostic_report.project = os.path.basename(os.path.abspath(game_dir))
            self.diagnostic_report.target_language = self.target_language
            for tl_file in tl_files:
                # record extracted counts based on entries
                for e in tl_file.entries:
                    fp = e.file_path or tl_file.file_path
                    self.diagnostic_report.add_extracted(fp, {
                        'text': e.original_text,
                        'line_number': e.line_number,
                        'context_path': getattr(e, 'context_path', [])
                    })
        except Exception:
            pass

        try:
            self._collect_coverage_warnings(game_dir)
        except Exception as exc:
            self.logger.debug(f"Coverage warning collection failed: {exc}")
        
        if not all_entries:
            stats = get_translation_stats(tl_files)
            if game_dir and os.path.isdir(game_dir):
                self._create_language_init_file(str(game_dir))
                
                # strings.json oluştur (Agresif kanca için)
                lang_dir = os.path.join(tl_path, renpy_lang)
                self._generate_strings_json(tl_files, lang_dir)
                
                self._manage_runtime_hook()
                
                # Dosya bazlı dışa aktarımı otomatik ve varsayılan yap
                try:
                    from src.core.exporter import export_strings_to_rpy
                    if export_strings_to_rpy(str(game_dir), renpy_lang):
                        self.log_message.emit("info", "Auto-exported translation strings to classic .rpy files.")
                except Exception as e:
                    self.logger.warning(f"Auto-export to RPY failed: {e}")

                try:
                    self._write_translation_reports(lang_dir)
                    self._emit_coverage_warning_summary()
                except Exception as exc:
                    self.logger.debug(f"Failed to write translation reports: {exc}")
                    
            return PipelineResult(
                success=True,
                message=self.config.get_ui_text("pipeline_all_already_translated"),
                stage=PipelineStage.COMPLETED,
                stats=stats,
                output_path=tl_dir
            )
        
        self.log_message.emit("info", self.config.get_ui_text("pipeline_entries_to_translate").replace("{count}", str(len(all_entries))))
        
        if self.should_stop:
            return self._stopped_result()
        
        # --- .rpymc entry'lerini all_entries'ye ekle ---
        if getattr(self, 'rpymc_entries', None):
            self.log_message.emit('info', self.config.get_log_text('rpymc_adding_entries', count=len(self.rpymc_entries)))
            all_entries.extend(self.rpymc_entries)
        
        # 5. Çeviri
        self._set_stage(PipelineStage.TRANSLATING, self.config.get_ui_text("stage_translating"))
        
        translations = self._translate_entries(all_entries)
        
        if self.should_stop:
            return self._stopped_result()
        
        if not translations:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_translate_failed"),
                stage=PipelineStage.ERROR
            )
        
        # 6. Kaydetme
        self._set_stage(PipelineStage.SAVING, self.config.get_ui_text("stage_saving"))
        
        saved_count = 0
        for tl_file in tl_files:
            # Bu dosyaya ait çevirileri filtrele
            file_translations = {}
            for entry in tl_file.entries:
                # original_text kullan (old_text property olarak da çalışır)
                tid = getattr(entry, 'translation_id', '') or TLParser.make_translation_id(
                    entry.file_path, entry.line_number, entry.original_text
                )
                if tid in translations:
                    file_translations[tid] = translations[tid]
                elif entry.original_text in translations:
                    file_translations[entry.original_text] = translations[entry.original_text]
            
            if file_translations:
                success = self.tl_parser.save_translations(tl_file, file_translations)
                if success:
                    saved_count += 1
                    # Diagnostics: mark written entries
                    try:
                        for tid in file_translations.keys():
                            # find file path
                            fp = tl_file.file_path
                            self.diagnostic_report.mark_written(fp, tid)
                    except Exception:
                        pass
        
        # 6.5. Atomik segment çevirileri strings.json'a zaten ekleniyor (extra_translations)
        # ve runtime hook Layer 1/2 tarafından eşleştiriliyor.
        # _rl_segments.rpy artık oluşturulmuyor (v2.7.1 hotfix):
        #   - translate XX strings: bloğu renpy.say() düzeyinde çalışmaz
        #   - play_dialogue() quote wrapping ("text") nedeniyle match yapamaz
        #   - Duplicate entry crash'lerine neden oluyordu
        # Eski _rl_segments.rpy dosyası varsa temizle
        _old_seg_path = os.path.join(tl_dir, '_rl_segments.rpy')
        if os.path.exists(_old_seg_path):
            try:
                os.remove(_old_seg_path)
                self.emit_log("info", "[AtomicSegments] Removed obsolete _rl_segments.rpy (translations handled by runtime hook)")
                # .rpyc de varsa sil
                _old_seg_rpyc = _old_seg_path + 'c'
                if os.path.exists(_old_seg_rpyc):
                    os.remove(_old_seg_rpyc)
            except Exception:
                pass
        
        # 7. Dil başlatma kodu oluştur (game/ klasörüne)
        self._create_language_init_file(game_dir)
        
        # Final istatistikler
        # Dosyaları yeniden parse et
        tl_files_updated = [
            tl_file
            for tl_file in self.tl_parser.parse_directory(tl_path, self.target_language)
            if not self._is_generated_export_file(tl_file.file_path)
        ]
        stats = get_translation_stats(tl_files_updated)

        # Hedef dil icin dil baslatici dosyasi olustur
        report_dir = tl_dir
        if game_dir and os.path.isdir(game_dir):
            self._create_language_init_file(str(game_dir))
            
            # strings.json oluştur (Agresif kanca için)
            lang_dir = os.path.join(tl_path, renpy_lang)
            report_dir = lang_dir
            self._generate_strings_json(tl_files_updated, lang_dir, extra_translations=translations)
            
            self._manage_runtime_hook()
            
            # Dosya bazlı dışa aktarımı otomatik ve varsayılan yap
            try:
                from src.core.exporter import export_strings_to_rpy
                if export_strings_to_rpy(str(game_dir), renpy_lang):
                    self.log_message.emit("info", "Auto-exported translation strings to classic .rpy files.")
            except Exception as e:
                self.logger.warning(f"Auto-export to RPY failed: {e}")

        try:
            self._write_translation_reports(report_dir)
            self._emit_coverage_warning_summary()
        except Exception as exc:
            self.logger.debug(f"Failed to write translation reports: {exc}")

        self._set_stage(PipelineStage.COMPLETED, self.config.get_ui_text("stage_completed"))
        summary = self.config.get_ui_text("pipeline_completed_summary").replace("{translated}", str(len(translations))).replace("{saved}", str(saved_count))
        if self.normalize_count:
            summary += f" | {self.config.get_log_text('log_tl_normalized', count=self.normalize_count)}"
        
        return PipelineResult(
            success=True,
            message=summary,
            stage=PipelineStage.COMPLETED,
            stats=stats,
            output_path=tl_dir
        )
    
    def _stopped_result(self) -> PipelineResult:
        """Durduruldu sonucu"""
        return PipelineResult(
            success=False,
            message=self.config.get_ui_text("pipeline_user_stopped"),
            stage=PipelineStage.IDLE
        )
    
    def _has_rpy_files(self, directory: str) -> bool:
        """Klasörde .rpy dosyası var mı?"""
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith('.rpy'):
                    return True
        return False
    
    def _has_rpyc_files(self, directory: str) -> bool:
        """Klasörde .rpyc dosyası var mı?"""
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith('.rpyc'):
                    return True
        return False
    
    def _has_rpa_files(self, directory: str) -> bool:
        """Klasörde .rpa arşiv dosyası var mı?"""
        for root, dirs, files in os.walk(directory):
            for f in files:
                if f.lower().endswith('.rpa'):
                    return True
        return False

    def _needs_re_extraction(self, game_dir: str, tl_dir: str) -> bool:
        """
        Geliştirici oyun dosyalarını (.rpy/.rpyc) güncellediğinde, tl/ klasöründeki mevcut 
        çevirilerden (genelde strings.json veya tl/*.rpy) daha yeni olup olmadığını kontrol eder.
        Eğer daha yeni kaynak dosyalar varsa True döndürür ve yeniden extract yapılmasını zorlar.
        """
        try:
            tl_mtime = 0
            # tl_dir içindeki dosyaların en yeni deðişme zamanını bul
            for root, dirs, files in os.walk(tl_dir):
                for f in files:
                    if f.lower().endswith('.rpy'):
                        fmtime = os.path.getmtime(os.path.join(root, f))
                        if fmtime > tl_mtime:
                            tl_mtime = fmtime
            
            # Nếu tl_dir boşsa klasörün mtime'ını kullan
            if tl_mtime == 0:
                tl_mtime = os.path.getmtime(tl_dir)
                
            # Şimdi game_dir içindeki (tl klasörü hariç) .rpy/.rpyc dosyalarına bak
            for root, dirs, files in os.walk(game_dir):
                # tl ve renpy klasörlerini atla
                if 'tl' in dirs:
                    dirs.remove('tl')
                dirs[:] = [d for d in dirs if d.lower() != 'renpy']
                for f in files:
                    if f.lower().endswith('.rpy') or f.lower().endswith('.rpyc'):
                        fmtime = os.path.getmtime(os.path.join(root, f))
                        # Eğer herhangi bir oyun scripti, tl dosyasından DAHA YENİ ise güncelleme gelmiştir!
                        if fmtime > tl_mtime:
                            return True
            return False
        except Exception as e:
            self.logger.debug(f"mtime check failed: {e}")
            return False

    def _normalize_tl_encodings(self, tl_dir: str) -> int:
        """
        tl/<lang> içindeki .rpy dosyalarını UTF-8-SIG'e yeniden yazar.
        Ren'Py loader'ı 'python_strict' ile okuduğu için geçersiz byte'lar
        (örn. 0xBE) oyunu düşürüyor; burada tamamını normalize ediyoruz.
        """
        tl_path = Path(tl_dir)
        if not tl_path.exists():
            return 0

        normalized = 0
        for file_path in tl_path.rglob("*.rpy"):
            try:
                if normalize_to_utf8_sig(file_path):
                    normalized += 1
            except Exception as e:
                self.log_message.emit("warning", self.config.get_log_text('encoding_normalize_failed', path=file_path, error=str(e)))
        return normalized
    
    def _generate_strings_json(self, tl_files: List[TranslationFile], lang_dir: str, extra_translations: dict = None):
        """
        Tüm çevirileri strings.json dosyasına aktarır.
        Agresif substring çeviri motoru için gereklidir.
        
        Args:
            tl_files: Çeviri dosyaları listesi
            lang_dir: Hedef dil dizini
            extra_translations: Ek çeviri çiftleri (atomik segment girişleri vb.)
        """
        try:
            mapping: Dict[str, str] = {}
            skipped_corrupt = 0
            skipped_reason_counts = {
                'separator_remnant': 0,
                'placeholder_remnant': 0,
                'html_leakage': 0,
                'length_inflation': 0,
                'placeholder_set_mismatch': 0,
                'renpy_tag_set_mismatch': 0,
                'duplicate_key_conflict': 0,
                'case_insensitive_conflict': 0,
            }
            mapping_sources: Dict[str, List[dict]] = {} # Track where each mapping comes from
            lower_to_orig: Dict[str, List[str]] = {}   # Track case fragments for CI checks
            skipped_samples = []

            def _mark_skipped(reason: str, original: str, translated: str):
                nonlocal skipped_corrupt
                skipped_corrupt += 1
                if reason in skipped_reason_counts:
                    skipped_reason_counts[reason] += 1
                if len(skipped_samples) < 200:
                    sample = {
                        'reason': reason,
                        'original': original,
                        'translated': translated,
                    }
                    if reason == 'duplicate_key_conflict' and original in mapping:
                        sample['existing_translation'] = mapping[original]
                        sample['sources'] = mapping_sources.get(original, [])
                    skipped_samples.append(sample)

            def _try_add_mapping(original: str, translated: str, source_file: str = None, line_num: int = None) -> None:
                orig = (original or '').strip()
                trans = (translated or '').strip()
                if not orig or not trans or orig == trans:
                    return
                if TRANSLATION_ID_KEY_RE.fullmatch(orig):
                    return

                reason = self._classify_translation_corruption(orig, trans)
                if reason is not None:
                    _mark_skipped(reason, orig, trans)
                    self.logger.debug(
                        "strings.json: Skipping %s in translation of: %s",
                        reason,
                        orig[:40],
                    )
                    return

                if orig in mapping:
                    if mapping[orig] != trans:
                        _mark_skipped('duplicate_key_conflict', orig, trans)
                        self.logger.debug(
                            "strings.json: Duplicate key conflict, keeping existing: "
                            "%s -> existing=%s vs new=%s",
                            orig[:40],
                            mapping[orig][:30],
                            trans[:30],
                        )
                    return

                # CI Conflict check
                lower_orig = orig.lower()
                if lower_orig in lower_to_orig:
                    # Check if any existing same-lower key has different translation
                    has_ci_conflict = False
                    for other_orig in lower_to_orig[lower_orig]:
                        if mapping[other_orig] != trans:
                            has_ci_conflict = True
                            break
                    
                    if has_ci_conflict:
                        # Report CI conflict but STILL ADD to mapping (runtime hook will handle it)
                        # We don't skip it here because exact-match should still work.
                        # But we mark it for diagnostics.
                        _mark_skipped('case_insensitive_conflict', orig, trans)
                    
                    if orig not in lower_to_orig[lower_orig]:
                        lower_to_orig[lower_orig].append(orig)
                else:
                    lower_to_orig[lower_orig] = [orig]

                mapping[orig] = trans
                if source_file:
                    if orig not in mapping_sources:
                        mapping_sources[orig] = []
                    mapping_sources[orig].append({'file': source_file, 'line': line_num})

            for tfile in tl_files:
                for entry in tfile.entries:
                    if entry.original_text and entry.translated_text:
                        _try_add_mapping(
                            entry.original_text, 
                            entry.translated_text,
                            source_file=os.path.basename(tfile.file_path),
                            line_num=entry.line_number
                        )
            
            # ── Atomik segment çevirileri ekle (v2.7.1) ──
            # Delimiter gruplarından gelen bağımsız segment çevirileri,
            # Ren'Py runtime vary() eşleşmesi için strings.json'a eklenir.
            if extra_translations:
                for orig, trans in extra_translations.items():
                    _try_add_mapping(orig, trans)
            
            # ── Delimiter grup segmentlerini ayır (v2.7.1 hotfix) ──
            # Ren'Py vary() fonksiyonu <A|B|C> bloklarını parçalayıp tek segment seçer.
            # strings.json'da birleşik blok ("old <A|B|C>": "<X|Y|Z>") var ama
            # bireysel segmentler ("A": "X") yok → vary() çıktısı eşleşmiyor.
            # Bu adım tüm mapping'i tarayıp:
            #   1) Angle-pipe gruplarını (<A|B|C>) bireysel segment çiftlerine ayırır
            #   2) Bare pipe patternlerini (A|B|C, <> olmadan) bireysel segment çiftlerine ayırır
            try:
                from src.core.syntax_guard import split_angle_pipe_groups, split_delimited_text
                _seg_additions = {}
                _seg_count = 0
                for m_orig, m_trans in list(mapping.items()):
                    # ── Yol 1: Angle-pipe grupları (<A|B|C>) ──
                    orig_split = split_angle_pipe_groups(m_orig)
                    if orig_split is not None:
                        trans_split = split_angle_pipe_groups(m_trans)
                        if trans_split is not None:
                            _, orig_groups = orig_split
                            _, trans_groups = trans_split
                            for g_idx in range(min(len(orig_groups), len(trans_groups))):
                                o_segs = orig_groups[g_idx]
                                t_segs = trans_groups[g_idx]
                                for s_idx in range(min(len(o_segs), len(t_segs))):
                                    o_s = o_segs[s_idx].strip()
                                    t_s = t_segs[s_idx].strip()
                                    if o_s and t_s and o_s != t_s and o_s not in mapping and o_s not in _seg_additions:
                                        _seg_additions[o_s] = t_s
                                        _seg_count += 1
                        continue  # Angle-pipe bulundu — bare pipe'a düşme
                    
                    # ── Yol 2: Bare pipe (A|B|C, <> olmadan) ──
                    if '|' not in m_orig:
                        continue
                    orig_delim = split_delimited_text(m_orig)
                    if orig_delim is None:
                        # split_delimited_text false-positive filtresi geçemediyse
                        # basit pipe split dene (vary() tam olarak bunu yapar)
                        if '|' in m_orig and '|' in m_trans:
                            o_parts = m_orig.split('|')
                            t_parts = m_trans.split('|')
                            # Safety: limit segment count (>6 likely CSV/data, not dialogue)
                            # and require at least 2 alpha chars per segment to filter noise.
                            if (len(o_parts) >= 2 and len(o_parts) == len(t_parts)
                                    and len(o_parts) <= 6):
                                _pipe_valid = True
                                for _p in o_parts:
                                    if sum(1 for ch in _p.strip() if ch.isalpha()) < 2:
                                        _pipe_valid = False
                                        break
                                if _pipe_valid:
                                    for o_s, t_s in zip(o_parts, t_parts):
                                        o_s = o_s.strip()
                                        t_s = t_s.strip()
                                        if o_s and t_s and o_s != t_s and o_s not in mapping and o_s not in _seg_additions:
                                            _seg_additions[o_s] = t_s
                                            _seg_count += 1
                        continue
                    
                    o_segs, _, _, _ = orig_delim
                    trans_delim = split_delimited_text(m_trans)
                    if trans_delim is not None:
                        t_segs, _, _, _ = trans_delim
                    elif '|' in m_trans:
                        # Çeviri split_delimited_text'e uymuyorsa basit pipe split
                        t_segs = m_trans.split('|')
                    else:
                        continue
                    
                    for s_idx in range(min(len(o_segs), len(t_segs))):
                        o_s = o_segs[s_idx].strip()
                        t_s = t_segs[s_idx].strip()
                        if o_s and t_s and o_s != t_s and o_s not in mapping and o_s not in _seg_additions:
                            _seg_additions[o_s] = t_s
                            _seg_count += 1
                
                if _seg_additions:
                    mapping.update(_seg_additions)
                    self.logger.info(f"strings.json: {_seg_count} individual segments extracted from delimiter groups")
            except Exception as e:
                self.logger.debug(f"strings.json segment splitting skipped: {e}")
            
            # ── v2.7.4: Tag-stripped çeviri girişleri (replace_text güvenlik ağı) ──
            # Ren'Py'ın config.replace_text callback'i metni tag tokenizasyonundan
            # SONRA alır. "{b}Hello{/b} World" → replace_text("Hello "), replace_text("World")
            # Bu parçalar strings.json'daki tam anahtarla eşleşmez.
            # Çözüm: Tag'leri çıkarılmış sürümleri de ekle.
            # Ayrıca tag'lerle SARMALANMIŞ metinlerin iç metnini ekle:
            # "{color=#f00}Error{/color}" → "Error" eklenir.
            try:
                _RENPY_TAG_RE = re.compile(
                    r'\{/?(?:b|i|u|s|plain|color|font|size|cps|nw|fast|w|p|a|'
                    r'outlinecolor|alpha|k|rt|rb|image|space|vspace)(?:=[^}]*)?\}'
                )
                _tag_stripped_additions = {}
                _tag_strip_count = 0
                for m_orig, m_trans in list(mapping.items()):
                    # Sadece Ren'Py tag'i içeren girdileri işle
                    if not _RENPY_TAG_RE.search(m_orig):
                        continue
                    # Tag'leri çıkar
                    stripped_orig = _RENPY_TAG_RE.sub('', m_orig).strip()
                    stripped_trans = _RENPY_TAG_RE.sub('', m_trans).strip()
                    # Anlamlı metin olmalı (en az 2 harf)
                    if (stripped_orig and stripped_trans
                            and stripped_orig != stripped_trans
                            and len(stripped_orig) >= 2
                            and any(c.isalpha() for c in stripped_orig)
                            and stripped_orig not in mapping
                            and stripped_orig not in _tag_stripped_additions):
                        _tag_stripped_additions[stripped_orig] = stripped_trans
                        _tag_strip_count += 1
                if _tag_stripped_additions:
                    mapping.update(_tag_stripped_additions)
                    self.logger.info(
                        f"strings.json: {_tag_strip_count} tag-stripped entries added for replace_text coverage"
                    )
            except Exception as e:
                self.logger.debug(f"strings.json tag-stripping skipped: {e}")

            try:
                hotkey_additions = self._synthesize_hotkey_visible_variants(mapping)
                if hotkey_additions:
                    for visible_key, visible_value in hotkey_additions.items():
                        if visible_key in mapping:
                            continue
                        mapping[visible_key] = visible_value
                        self._record_translation_guard_event(
                            category='recovered_by_synthesized_variant',
                            file_path='strings.json',
                            translation_id=visible_key,
                            original_text=visible_key,
                            translated_text=visible_value,
                            detail='visible_hotkey_variant',
                        )
                        try:
                            self.diagnostic_report.mark_recovered(
                                'strings.json',
                                visible_key,
                                'synthesized_variant',
                                original_text=visible_key,
                                translated_text=visible_value,
                            )
                        except Exception:
                            pass
                    self.logger.info(
                        "strings.json: %s hotkey visible-form variants synthesized for runtime exact-match coverage",
                        len(hotkey_additions),
                    )
            except Exception as e:
                self.logger.debug(f"strings.json hotkey synthesis skipped: {e}")

            try:
                angle_additions = self._synthesize_angle_wrapper_variants(mapping)
                if angle_additions:
                    for inner_key, inner_value in angle_additions.items():
                        if inner_key in mapping:
                            continue
                        mapping[inner_key] = inner_value
                        self._record_translation_guard_event(
                            category='recovered_by_synthesized_variant',
                            file_path='strings.json',
                            translation_id=inner_key,
                            original_text=inner_key,
                            translated_text=inner_value,
                            detail='angle_wrapper_variant',
                        )
                        try:
                            self.diagnostic_report.mark_recovered(
                                'strings.json',
                                inner_key,
                                'synthesized_variant',
                                original_text=inner_key,
                                translated_text=inner_value,
                            )
                        except Exception:
                            pass
                    self.logger.info(
                        "strings.json: %s angle-wrapper aliases synthesized for runtime quote lookup coverage",
                        len(angle_additions),
                    )
            except Exception as e:
                self.logger.debug(f"strings.json angle-wrapper synthesis skipped: {e}")

            try:
                visible_additions = self._synthesize_visible_text_variants(mapping)
                if visible_additions:
                    for alias_key, alias_value in visible_additions.items():
                        if alias_key in mapping:
                            continue
                        mapping[alias_key] = alias_value
                        self._record_translation_guard_event(
                            category='recovered_by_synthesized_variant',
                            file_path='strings.json',
                            translation_id=alias_key,
                            original_text=alias_key,
                            translated_text=alias_value,
                            detail='visible_text_variant',
                        )
                        try:
                            self.diagnostic_report.mark_recovered(
                                'strings.json',
                                alias_key,
                                'synthesized_variant',
                                original_text=alias_key,
                                translated_text=alias_value,
                            )
                        except Exception:
                            pass
                    self.logger.info(
                        "strings.json: %s visible-text aliases synthesized for runtime exact-match coverage",
                        len(visible_additions),
                    )
            except Exception as e:
                self.logger.debug(f"strings.json visible-text synthesis skipped: {e}")

            try:
                fragment_additions = self._synthesize_visible_fragment_variants(mapping)
                if fragment_additions:
                    for alias_key, alias_value in fragment_additions.items():
                        if alias_key in mapping:
                            continue
                        mapping[alias_key] = alias_value
                        self._record_translation_guard_event(
                            category='recovered_by_synthesized_variant',
                            file_path='strings.json',
                            translation_id=alias_key,
                            original_text=alias_key,
                            translated_text=alias_value,
                            detail='visible_fragment_variant',
                        )
                        try:
                            self.diagnostic_report.mark_recovered(
                                'strings.json',
                                alias_key,
                                'synthesized_variant',
                                original_text=alias_key,
                                translated_text=alias_value,
                            )
                        except Exception:
                            pass
                    self.logger.info(
                        "strings.json: %s visible-fragment aliases synthesized for runtime exact-match coverage",
                        len(fragment_additions),
                    )
            except Exception as e:
                self.logger.debug(f"strings.json visible-fragment synthesis skipped: {e}")

            try:
                runtime_observed_additions = self._synthesize_runtime_observed_variants(mapping, lang_dir)
                if runtime_observed_additions:
                    for alias_key, alias_value in runtime_observed_additions.items():
                        if alias_key in mapping:
                            continue
                        mapping[alias_key] = alias_value
                        self._record_translation_guard_event(
                            category='recovered_by_synthesized_variant',
                            file_path='strings.json',
                            translation_id=alias_key,
                            original_text=alias_key,
                            translated_text=alias_value,
                            detail='runtime_observed_variant',
                        )
                        try:
                            self.diagnostic_report.mark_recovered(
                                'strings.json',
                                alias_key,
                                'synthesized_variant',
                                original_text=alias_key,
                                translated_text=alias_value,
                            )
                        except Exception:
                            pass
                    self.logger.info(
                        "strings.json: %s runtime-observed aliases synthesized from missed-string diagnostics",
                        len(runtime_observed_additions),
                    )
            except Exception as e:
                self.logger.debug(f"strings.json runtime-observed synthesis skipped: {e}")
            
            if skipped_corrupt > 0:
                self.logger.warning(f"strings.json: Skipped {skipped_corrupt} potentially corrupted translation(s)")
                reason_summary = ', '.join(
                    f"{name}={count}" for name, count in skipped_reason_counts.items() if count > 0
                )
                if reason_summary:
                    self.logger.info(f"strings.json: Corruption reasons -> {reason_summary}")
                try:
                    diag_dir = os.path.join(lang_dir, 'diagnostics')
                    os.makedirs(diag_dir, exist_ok=True)
                    report_path = os.path.join(diag_dir, 'strings_json_skipped_corruptions.json')
                    save_text_safely(
                        Path(report_path),
                        json.dumps({
                            'generated_at': int(time.time()),
                            'total_skipped': skipped_corrupt,
                            'reason_counts': skipped_reason_counts,
                            'sample_limit': 100,
                            'samples': skipped_samples,
                        }, ensure_ascii=False, indent=2),
                        encoding='utf-8',
                    )
                    self.logger.info(f"strings.json: Wrote skipped-corruption report -> {report_path}")
                except Exception as report_exc:
                    self.logger.debug(f"strings.json: Failed to write skipped-corruption report: {report_exc}")
            
            if mapping:
                json_path = os.path.join(lang_dir, "strings.json")
                save_text_safely(
                    Path(json_path),
                    json.dumps(mapping, ensure_ascii=False, indent=4),
                    encoding='utf-8',
                )
                self.log_message.emit('info', self.config.get_log_text('log_strings_json_generated', count=len(mapping)))
                return len(mapping)
        except Exception as e:
            self.logger.warning(f"Failed to generate strings.json: {e}")

    def _write_atomic_segments_rpy(self, tl_dir: str, renpy_lang: str):
        """
        DEPRECATED (v2.7.1 hotfix) — Bu metod artık çağrılmıyor.
        
        Neden kaldırıldı:
        1. translate XX strings: bloğu renpy.say() dinamik diyaloglarında çalışmaz
        2. play_dialogue() fonksiyonu vary() çıktısını \"...\" ile sarmalıyor,
           bu yüzden old "text" girişleri "text" (tırnaklı) ile eşleşemez
        3. strings.rpy ile duplicate entry crash'lerine neden oluyordu
        
        Atomik segment çevirileri artık:
        - strings.json'a ekleniyor (extra_translations parametresi ile)
        - Runtime hook Layer 1/2 tarafından eşleştiriliyor (quote-stripping ile)
        
        Bu metod geriye dönük uyumluluk için korunuyor ama çağrılmıyor.
        """
        self.logger.debug("_write_atomic_segments_rpy is deprecated, skipping")
        return

    def _manage_runtime_hook(self):
        """
        Manages the presence of the runtime translation hook script based on settings.
        Generated by RenLocalizer to force translation of untagged strings.
        v2.7.0: Loads ALL mappings from ALL .rpy files in tl directory.
        """
        if not self.project_path:
            return
            
        try:
            game_dir = Path(self.project_path) / "game"
            if not game_dir.exists():
                return
                
            hook_filename = "zzz_renlocalizer_runtime.rpy"
            hook_path = game_dir / hook_filename
            
            # Clean up old versions
            for old in game_dir.glob("*_renlocalizer_*.rpy"):
                if old.name != hook_filename:
                    old.unlink(missing_ok=True)

            should_exist = self._is_runtime_hook_enabled()
            
            # Hedef dili al
            target_lang = getattr(self, 'target_language', None) or getattr(self.config.translation_settings, 'target_language', 'turkish') or 'turkish'
            # ISO -> Ren'Py native
            reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
            renpy_lang = reverse_lang_map.get(target_lang.lower(), target_lang)
            
            if should_exist:
                content = render_runtime_hook(
                    renpy_lang,
                    runtime_string_diagnostics=getattr(
                        self.config.translation_settings,
                        "runtime_string_diagnostics",
                        False,
                    ),
                )
                save_text_safely(hook_path, content, encoding="utf-8")
                self.log_message.emit('info', self.config.get_ui_text("log_hook_installed").replace("{filename}", hook_filename))
            else:
                # Remove if it exists
                if hook_path.exists():
                    os.remove(hook_path)
                    self.log_message.emit('info', self.config.get_ui_text("log_hook_removed").replace("{filename}", hook_filename))
                    
        except Exception as e:
            self.logger.warning(f"Failed to manage runtime hook: {e}")

    def _create_language_init_file(self, game_dir: str):
        """
        Dil baslangic dosyasini olusturur.
        game/ klasorune yazilir, boylece oyun baslarken varsayilan dil ayarlanir.
        
        v2.6.7: Agresif aktivasyon - Bazı oyunlar basit config.default_language'ı
        görmezden geldiği için çoklu yöntem kullanıyoruz.
        """
        try:
            # Hedef dil kodunu hesapla; ISO gelirse Ren'Py adina cevir
            language_code = (getattr(self, 'target_language', None) or '').strip().lower()
            if not language_code:
                try:
                    language_code = getattr(self.config.translation_settings, 'target_language', '') or ''
                except Exception:
                    language_code = ''
            original_input = language_code
            reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
            if language_code:
                language_code = reverse_lang_map.get(language_code, language_code)
            else:
                # Hedef bilinmiyorsa tl alt klasorlerini kontrol et; yalnizca tek klasor varsa kullan
                tl_root = Path(game_dir) / "tl"
                subdirs = sorted([p.name for p in tl_root.iterdir() if p.is_dir()]) if tl_root.exists() else []
                if len(subdirs) == 1:
                    language_code = subdirs[0].lower()
                    self.log_message.emit("info", self.config.get_log_text('target_lang_auto', lang=language_code))
                else:
                    language_code = 'turkish'
                    self.log_message.emit("warning", self.config.get_log_text('target_lang_default'))

            # Once eski otomatik init dosyalarini temizle ki tek dosya aktif kalsin
            try:
                for existing in Path(game_dir).glob("*_language.rpy"):
                    if "renlocalizer" in existing.name or existing.name.startswith("a0_") or existing.name.startswith("zzz_"):
                        if existing.name != f"zzz_{language_code}_language.rpy":
                            existing.unlink(missing_ok=True)
                            self.log_message.emit("info", self.config.get_log_text('old_lang_init_deleted', name=existing.name))
            except Exception:
                pass

            # Dosya adi: zzz_[lang]_language.rpy (En son yuklenir, oyunun ayarlarini ezer)
            init_file = os.path.join(game_dir, f'zzz_{language_code}_language.rpy')

            self.log_message.emit(
                "info",
                self.config.get_ui_text("pipeline_lang_init_check").replace("{path}", init_file)
                + f" | dil={language_code} (input={original_input or 'none'})"
            )

            # Zaten varsa sil ve yeniden olustur (guncellemek icin)
            if os.path.exists(init_file):
                os.remove(init_file)
                self.log_message.emit("info", self.config.get_ui_text("pipeline_lang_init_update"))

            # Sanitize language_code for use as Python identifier (e.g. zh-CN -> zh_cn)
            safe_code = language_code.replace("-", "_").replace(" ", "_").replace(".", "_")

            # Agresif çoklu-fazlı dil aktivasyon sistemi
            # v2.7.5: Ren'Py dokümantasyonuna uygun güvenli yaklaşım
            #
            # KRİTİK GÜVENLIK KURALLARI:
            # 1. gui.init() "init offset = -2" ile çalışır ve renpy.call_in_new_context("_style_reset")
            #    çağırır. Yeni context oluşturulurken config.context_copy_remove_screens'deki
            #    ekranlar (varsayılan: ['notify', ...]) scene_lists'ten kaldırılır.
            #    Bu kaldırma screen.update() → renpy.ui.detached() → stack[-1] gerektirir.
            #    Init fazında ui.stack BOŞ'tur (ui.reset() post_init'te çalışır).
            #    Bu yüzden gui.init() ÖNCESI herhangi bir screen gösterilmemeli!
            #
            # 2. _preferences.language Ren'Py dokümantasyonunda READ-ONLY olarak belirtilir.
            #    Dil değiştirmek için renpy.change_language() kullanılmalıdır.
            #
            # 3. config.language (config.default_language DEĞİL) kullanıcı tercihini EZER.
            #    Bu "unsanctioned translations" için Ren'Py'nin resmi önerisidir.
            #
            # GÜVENLI PRİORİTE SIRASI:
            #   init -2  : gui.init() (oyunun gui.rpy dosyası)
            #   init 0   : Bizim config.language ayarımız (gui.init SONRASI, güvenli)
            #   init 999 : Runtime hook kurulumu
            content = f"""# ============================================================
# RenLocalizer - Safe Language Activation v2.7.5
# ============================================================
# Bu dosya oyunun dilini {language_code.title()}'ye ayarlar.
#
# KRİTİK: init -2'den ÖNCE (gui.init öncesi) hiçbir config/screen
# işlemi yapılmaz. Bu, IndexError crash'ini önler.
#
# Ren'Py dil seçim önceliği (dokümantasyondan):
#   1. config.language (None değilse, diğer HER ŞEYİ ezer)
#   2. Kullanıcının daha önce seçtiği dil
#   3. config.enable_language_autodetect
#   4. config.default_language
#   5. None (varsayılan dil)

# ============================================================
# PHASE 1: Safe Language Override (AFTER gui.init)
# ============================================================
# config.language kullanıcı tercihini ezer — "unsanctioned translations"
# için Ren'Py'nin resmi önerisidir. Priority 0 = gui.init (-2) SONRASI.
define config.language = "{language_code}"

# ============================================================
# PHASE 2: Runtime Enforcement (Game Start Hook)
# ============================================================
# config.start_callbacks init fazı BİTTİKTEN SONRA,
# oyun (splashscreen dahil) başlamadan HEMEN ÖNCE çalışır.
# Bu noktada ui.stack başlatılmıştır, screen göstermek güvenlidir.
init python:
    def _rl_force_{safe_code}_language():
        \"\"\"
        Oyun her başladığında dili kontrol et ve gerekirse {language_code.title()}'ye çevir.
        renpy.change_language() kullanır (_preferences.language'a doğrudan yazmaz).
        \"\"\"
        try:
            current = getattr(_preferences, 'language', None)
            if current != "{language_code}":
                renpy.change_language("{language_code}")
        except Exception:
            pass

    # Oyun başladığında bu fonksiyonu çalıştır
    if _rl_force_{safe_code}_language not in config.start_callbacks:
        config.start_callbacks.append(_rl_force_{safe_code}_language)

# ============================================================
# PHASE 3: Persistent Override (Save File Protection)
# ============================================================
# init 0'da çalışır, gui.init (-2) SONRASI — güvenli.
# Bazı oyunlar kendi persistent değişkenlerini kullanır.
init python:
    try:
        if hasattr(persistent, "language"):
            persistent.language = "{language_code}"
        if hasattr(persistent, "game_language"):
            persistent.game_language = "{language_code}"
        if hasattr(persistent, "selected_language"):
            persistent.selected_language = "{language_code}"
    except Exception:
        pass
"""


            save_text_safely(Path(init_file), content, encoding='utf-8-sig', newline='\n')

            self.log_message.emit("info", self.config.get_ui_text("pipeline_lang_init_created").replace("{path}", init_file))

        except Exception as e:
            self.log_message.emit("warning", self.config.get_ui_text("pipeline_lang_init_failed").format(error=e))







    def translate_existing_tl(
        self,
        tl_root_path: str,
        target_language: str,
        source_language: str = "auto",
        engine: TranslationEngine = TranslationEngine.GOOGLE,
        use_proxy: bool = False,
    ) -> PipelineResult:
        """
        Var olan tl/<dil> klasorundeki .rpy dosyalarini (Ren'Py SDK ile uretildi)
        dogrudan cevirir. Oyunun EXE'sine gerek yoktur.
        """
        self._reset_translation_diagnostics()
        # GUI ISO kodu (fr/en/tr) gonderir; Ren'Py klasor adi icin ters cevir
        reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
        target_iso = (target_language or "").lower()
        renpy_lang = reverse_lang_map.get(target_iso, target_iso)

        # Konfigure et
        self.target_language = renpy_lang
        self.source_language = source_language
        self.engine = engine
        self.use_proxy = use_proxy
        self.project_path = os.path.abspath(Path(tl_root_path).parent.parent) if tl_root_path else None

        # Stage: PARSING
        self._set_stage(PipelineStage.PARSING, self.config.get_ui_text("stage_parsing"))

        # tl_path / lang_dir coz
        p = Path(tl_root_path)
        lang_dir: Optional[Path] = None
        tl_path: Optional[Path] = None

        target_dir_names: List[str] = []
        for name in [renpy_lang, target_iso]:
            if name and name not in target_dir_names:
                target_dir_names.append(name)

        def matches_name(path_obj: Path) -> bool:
            return path_obj.name.lower() in target_dir_names

        # 1) Kullanici zaten tl/<lang> secmis
        if matches_name(p) and p.parent.name.lower() == "tl":
            lang_dir = p
            tl_path = p.parent
        # 2) Kullanici tl dizinini secmis (game/tl)
        elif p.name.lower() == "tl":
            tl_path = p
            for name in target_dir_names:
                candidate = tl_path / name
                if candidate.exists():
                    lang_dir = candidate
                    break
        # 3) Kullanici oyun/project root secmis
        if lang_dir is None and (p / "tl").exists():
            tl_path = p / "tl"
            for name in target_dir_names:
                candidate = tl_path / name
                if candidate.exists():
                    lang_dir = candidate
                    break
        # 4) Son care: secilen dizin altinda dil klasoru var mi?
        if lang_dir is None:
            for name in target_dir_names:
                candidate = p / name
                if candidate.exists():
                    lang_dir = candidate
                    tl_path = p if p.name.lower() == "tl" else p.parent if p.parent.name.lower() == "tl" else p
                    break
        # 5) Ad uyusmasa bile kullanici dogrudan dil klasorunu secmis olabilir
        if lang_dir is None and p.is_dir():
            try:
                has_rpy = next(p.rglob("*.rpy"), None) is not None
            except Exception:
                has_rpy = False
            if has_rpy:
                lang_dir = p
                tl_path = p.parent if p.parent else p

        if lang_dir is None:
            return PipelineResult(
                success=False,
                message=self.config.get_log_text('tl_dir_not_found', path=f"{p} ({'/'.join(target_dir_names)})"),
                stage=PipelineStage.ERROR,
            )

        if not lang_dir.exists():
            return PipelineResult(
                success=False,
                message=self.config.get_log_text('tl_dir_not_found', path=str(lang_dir)),
                stage=PipelineStage.ERROR,
            )

        # Bilgilendirici log
        self.log_message.emit(
            "info",
            self.config.get_log_text('tl_directory_info', tl_path=str(tl_path), lang_dir=lang_dir.name, input=target_language),
        )

        # Oyun dizinini tahmin et (tl/<lang> altindaysa bir ust = game)
        game_dir = None
        try:
            if lang_dir.parent.name.lower() == "tl":
                game_dir = lang_dir.parent.parent
            elif tl_path and tl_path.name.lower() == "tl":
                game_dir = tl_path.parent
        except Exception:
            game_dir = None

        tl_files = self.tl_parser.parse_directory(str(tl_path), lang_dir.name)

        # Yalnizca hedef dil altindaki dosyalari kabul et; diger dil klasorlerini haric tut
        target_tl_dir = os.path.normcase(os.path.join(str(tl_path), lang_dir.name))
        filtered_files: List[TranslationFile] = []
        for tl_file in tl_files:
            if self._is_generated_export_file(tl_file.file_path):
                self.log_message.emit("debug", f"[ExportFilter] Skipping generated export file: {tl_file.file_path}")
                continue
            fp_norm = os.path.normcase(tl_file.file_path)
            if fp_norm.startswith(target_tl_dir):
                tl_file.entries = [
                    e for e in tl_file.entries
                    if os.path.normcase(e.file_path or tl_file.file_path).startswith(target_tl_dir)
                ]
                filtered_files.append(tl_file)
            else:
                self.log_message.emit("info", self.config.get_log_text('log_other_lang_skipped', path=tl_file.file_path))
        tl_files = filtered_files

        # Encode normalizasyonu (hedef dil klasoru)
        try:
            normalized = self._normalize_tl_encodings(str(lang_dir))
            if normalized:
                self.log_message.emit("info", self.config.get_log_text('log_tl_normalized', count=normalized))
                self.normalize_count = normalized
        except Exception as e:
            msg = self.config.get_log_text('encoding_normalize_failed', path=str(lang_dir), error=str(e))
            self.log_message.emit("warning", msg)
            self._log_error(msg)

        if not tl_files:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_files_not_found_parse"),
                stage=PipelineStage.ERROR,
            )

        reopened_counts = self._reopen_stale_tl_entries(tl_files)
        if reopened_counts['reopened']:
            self.log_message.emit(
                "info",
                "Reopened stale TL entries for retranslation: "
                f"{reopened_counts['reopened']} "
                f"(corrupted={reopened_counts['corrupted']}, "
                f"unchanged_core_ui={reopened_counts['unchanged_core_ui']})"
            )

        # Cevrilecek girisleri topla
        all_entries: List[TranslationEntry] = []
        for tl_file in tl_files:
            all_entries.extend(tl_file.get_untranslated())

        # Diagnostics baslangic bilgisi
        try:
            self.diagnostic_report.project = os.path.basename(os.path.abspath(tl_root_path))
            self.diagnostic_report.target_language = self.target_language
            for tl_file in tl_files:
                for e in tl_file.entries:
                    fp = e.file_path or tl_file.file_path
                    self.diagnostic_report.add_extracted(fp, {
                        'text': e.original_text,
                        'line_number': e.line_number,
                        'context_path': getattr(e, 'context_path', [])
                    })
        except Exception:
            pass

        if not all_entries:
            stats = get_translation_stats(tl_files)
            if game_dir and game_dir.exists():
                self._create_language_init_file(str(game_dir))
                self._manage_runtime_hook()
            return PipelineResult(
                success=True,
                message=self.config.get_ui_text("pipeline_all_already_translated"),
                stage=PipelineStage.COMPLETED,
                stats=stats,
                output_path=str(lang_dir)
            )

        self.log_message.emit("info", self.config.get_ui_text("pipeline_entries_to_translate").replace("{count}", str(len(all_entries))))

        # Stage: TRANSLATING
        self._set_stage(PipelineStage.TRANSLATING, self.config.get_ui_text("stage_translating"))
        translations = self._translate_entries(all_entries)

        if not translations:
            return PipelineResult(
                success=False,
                message=self.config.get_ui_text("pipeline_translate_failed"),
                stage=PipelineStage.ERROR
            )

        # Stage: SAVING
        self._set_stage(PipelineStage.SAVING, self.config.get_ui_text("stage_saving"))
        saved_count = 0
        for tl_file in tl_files:
            file_translations: Dict[str, str] = {}
            for entry in tl_file.entries:
                tid = getattr(entry, 'translation_id', '') or TLParser.make_translation_id(
                    entry.file_path, entry.line_number, entry.original_text
                )
                if tid in translations:
                    file_translations[tid] = translations[tid]
                elif entry.original_text in translations:
                    file_translations[entry.original_text] = translations[entry.original_text]

            if file_translations:
                success = self.tl_parser.save_translations(tl_file, file_translations)
                if success:
                    saved_count += 1
                    try:
                        for tid in file_translations.keys():
                            fp = tl_file.file_path
                            self.diagnostic_report.mark_written(fp, tid)
                    except Exception:
                        pass

        # Atomik segment çevirileri strings.json'a zaten ekleniyor (extra_translations)
        # _rl_segments.rpy artık oluşturulmuyor (v2.7.1 hotfix) — runtime hook yeterli
        _old_seg_path2 = os.path.join(str(lang_dir), '_rl_segments.rpy')
        if os.path.exists(_old_seg_path2):
            try:
                os.remove(_old_seg_path2)
                self.emit_log("info", "[AtomicSegments] Removed obsolete _rl_segments.rpy")
                _old_seg_rpyc2 = _old_seg_path2 + 'c'
                if os.path.exists(_old_seg_rpyc2):
                    os.remove(_old_seg_rpyc2)
            except Exception:
                pass

        # Final istatistikler
        tl_files_updated = [
            tl_file
            for tl_file in self.tl_parser.parse_directory(str(tl_path), lang_dir.name)
            if not self._is_generated_export_file(tl_file.file_path)
        ]
        stats = get_translation_stats(tl_files_updated)

        # Hedef dil icin dil baslatici dosyasi olustur
        if game_dir and game_dir.exists():
            self._create_language_init_file(str(game_dir))

            # strings.json oluştur (Agresif kanca için) — atomik segmentler dahil
            self._generate_strings_json(tl_files_updated, str(lang_dir), extra_translations=translations)

            self._manage_runtime_hook()

        try:
            self._write_translation_reports(str(lang_dir))
        except Exception as exc:
            self.logger.debug(f"Failed to write translation reports: {exc}")

        self._set_stage(PipelineStage.COMPLETED, self.config.get_ui_text("stage_completed"))
        summary = self.config.get_ui_text("pipeline_completed_summary").replace("{translated}", str(len(translations))).replace("{saved}", str(saved_count))
        if self.normalize_count:
            summary += f" | Normalize edilen tl dosyasi: {self.normalize_count}"

        return PipelineResult(
            success=True,
            message=summary,
            stage=PipelineStage.COMPLETED,
            stats=stats,
            output_path=str(lang_dir)
        )

    def _make_source_translatable(self, game_dir: str) -> int:
        """
        Kaynak .rpy dosyalarındaki UI metinlerini çevrilebilir hale getirir.
        textbutton "Text" -> textbutton _("Text")
        textbutton 'Text' -> textbutton _('Text')
        Bu işlem Ren'Py'ın translate komutunun bu metinleri yakalamasını sağlar.
        
        Returns: Değiştirilen dosya sayısı
        """
        # Çevrilebilir yapılması gereken pattern'ler
        # Her pattern: (regex_pattern, replacement)
        # 
        # Önemli Ren'Py UI Elemanları:
        # - textbutton: Tıklanabilir metin butonu
        # - text: Ekranda gösterilen metin
        # - tooltip: Fare üzerine gelince gösterilen ipucu
        # - label: Metin etiketi (nadiren çeviri gerektirir)
        # - notify: Bildirim mesajları (renpy.notify)
        # - action Notify: Action olarak bildirim
        # - title: Pencere başlığı
        # - message: Onay/hata mesajları
        #
        # NOT: Her pattern hem tek tırnak (') hem de çift tırnak (") destekler
        # ['\"] = tek veya çift tırnak eşleşir, \\1 ile aynı tırnak kullanılır
        #
        patterns = [
            # textbutton "text" veya textbutton 'text' -> textbutton _("text")
            # Ör: textbutton "Nap": veya textbutton 'Start' action Start()
            # v2.7.4: Lookahead kullanarak takip eden keyword zorunluluğu kaldırıldı
            (r"(textbutton\s+)(['\"])([^'\"]+)\2(?=\s|$|:)", 
             r'\1_(\2\3\2)'),
            
            # text "..." veya text '...' -> text _("text")
            # v2.7.4 FIX: Artık takip eden property (size, color vb.) zorunlu DEĞİL.
            # Satır sonu, iki nokta veya boşluk yeterli. Bu sayede sade
            # text "Hello" ifadeleri de yakalanıyor (eski pattern bunları kaçırıyordu).
            # NOT: text "[variable]" → skip_patterns ile atlanır
            # NOT: text "{b}Bold{/b}" → artık yakalanıyor (Ren'Py tag'leri _() içinde geçerli)
            (r"(\btext\s+)(['\"])([^'\"\[\]]+)\2(?=\s|$|:)", 
             r'\1_(\2\3\2)'),
            
            # tooltip "text" veya tooltip 'text' -> tooltip _("text")
            # Ör: tooltip "Dev Console (Toggle)"
            (r"(tooltip\s+)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
            
            # renpy.notify("text") veya renpy.notify('text') -> renpy.notify(_("text"))
            # Ör: renpy.notify("Item added to inventory")
            (r"(renpy\.notify\s*\(\s*)(['\"])([^'\"]+)\2(\s*\))", 
             r'\1_(\2\3\2)\4'),
            
            # action Notify("text") veya Notify('text') -> action Notify(_("text"))
            # Ör: action Notify("Game saved!")
            (r"(Notify\s*\(\s*)(['\"])([^'\"]+)\2(\s*\))", 
             r'\1_(\2\3\2)\4'),
            
            # title="text" veya title='text' (screen title vb.)
            # Ör: title="Settings" veya frame title 'Options':
            (r"(title\s*=\s*)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
            
            # message="text" veya message='text' (confirm screen vb.)
            # Ör: message="Are you sure you want to quit?"
            (r"(message\s*=\s*)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
            
            # yes="text" (confirm)
            # Ör: yes="Yes" 
            (r"(\byes\s*=\s*)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
            
            # no="text" (confirm)  
            # Ör: no="No"
            (r"(\bno\s*=\s*)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
            
            # alt="text" (image alt text)
            # Ör: add "image.png" alt="A beautiful sunset"
            (r"(\balt\s*=\s*)(['\"])([^'\"]+)\2", 
             r'\1_(\2\3\2)'),
        ]
        
        # Atlanacak pattern'ler (zaten çevrilebilir veya değişken)
        # Hem tek (') hem çift (") tırnak desteklenir
        skip_patterns = [
            r'_\s*\(\s*[\'"]',    # Zaten çevrilebilir: _("text") veya _('text')
            r'[\'\"]\s*\+\s*[\'"]',    # String concatenation: "text" + "more"
            r'^\s*#',             # Yorum satırı
            r'^\s*$',             # Boş satır
            r'define\s+',         # define satırları
            r'default\s+',        # default satırları
            r'=\s*[\'"][^\'"]*[\'"]\s*$',  # Sadece atama: variable = "value"
            r'[\'"][^\'"]*\[[^\]]+\][^\'"]*[\'"]',  # Değişken içeren: "[player]"
            # v2.7.4 FIX: Ren'Py text tag'leri ({b}, {color=...} vb.) artık atlanmıyor
            # çünkü _("{b}text{/b}") Ren'Py'da gayet geçerli. Sadece Python format
            # string'leri ({}, {0}, {:d}, .format()) atlanıyor.
            r'\.format\s*\(',                                    # .format() çağrısı
            r'[\'"][^\'"]*\{\s*\}[^\'"]*[\'"]',                  # Boş brace: "{}"
            r'[\'"][^\'"]*\{\d+[^}]*\}[^\'"]*[\'"]',             # Positional: "{0}", "{1:d}"
            r'[\'"][^\'"]*\{:[^}]+\}[^\'"]*[\'"]',               # Format spec: "{:d}", "{:.2f}"
        ]
        
        modified_count = 0
        rpy_dir = os.path.join(game_dir, 'rpy')
        
        if not os.path.isdir(rpy_dir):
            # rpy alt klasörü yoksa direkt game klasörünü tara
            rpy_dir = game_dir
        
        try:
            for root, dirs, files in os.walk(rpy_dir):
                # tl klasörünü atla
                if 'tl' in dirs:
                    dirs.remove('tl')
                    
                # GÜVENLİK: 'renpy' adlı klasörleri tamamen atla (içine girme)
                dirs[:] = [d for d in dirs if d.lower() != 'renpy']
                
                for filename in files:
                    if not filename.lower().endswith('.rpy'):
                        continue

                    filepath = os.path.join(root, filename)

                    try:
                        # Skip backup creation (.bak) to save space and reduce I/O noise.
                        # Files are saved safely via save_text_safely which uses atomic writes.
                        

                        content = read_text_safely(Path(filepath))
                        if content is None:
                            self.log_message.emit('warning', f"{filename} dosyası okunamadı (encoding)")
                            continue
                        
                        original_content = content
                        
                        # Her pattern için değiştir
                        for pattern, replacement in patterns:
                            # Satır satır işle
                            lines = content.split('\n')
                            new_lines = []
                            
                            for line in lines:
                                # Atlanacak satırları kontrol et
                                should_skip = False
                                for skip in skip_patterns:
                                    if re.search(skip, line):
                                        should_skip = True
                                        break
                                
                                if not should_skip:
                                    line = re.sub(pattern, replacement, line)
                                
                                new_lines.append(line)
                            
                            content = '\n'.join(new_lines)
                        
                        # Değişiklik olduysa kaydet
                        if content != original_content:
                            save_text_safely(Path(filepath), content, encoding='utf-8-sig', newline='\n')
                            modified_count += 1
                    
                    except Exception as e:
                        msg = f"Dosya işlenemedi {filename}: {e}"
                        self.log_message.emit("warning", msg)
                        self._log_error(msg)
                        continue
            
            if modified_count > 0:
                self.log_message.emit("info", self.config.get_log_text('source_files_made_translatable', count=modified_count))
            
        except Exception as e:
            self.log_message.emit("warning", self.config.get_log_text('source_files_error', error=str(e)))
        
        return modified_count
    
    def _run_extraction(self, project_path: str) -> bool:
        """RPA arşivlerini unrpa ile aç (tüm platformlarda çalışır)."""
        try:
            self.log_message.emit("info", self.config.get_log_text('unren_starting'))
            
            # unrpa kütüphanesini kullan
            from src.utils.unrpa_adapter import UnrpaAdapter
            from pathlib import Path
            
            adapter = UnrpaAdapter()
            if not adapter.is_available():
                self.log_message.emit("error", self.config.get_log_text('log_unrpa_not_installed'))
                return False
            
            # game dizinini bul
            project_path_obj = Path(project_path)
            game_dir = project_path_obj / "game"
            
            if not game_dir.exists():
                if project_path_obj.name == "game":
                    game_dir = project_path_obj
                else:
                    game_dir = project_path_obj
            
            self.log_message.emit("info", self.config.get_log_text('log_rpa_extracting', path=game_dir))
            
            try:
                success = adapter.extract_game(game_dir)
                
                if success:
                    self.log_message.emit("info", self.config.get_log_text('unren_completed'))
                    return True
                else:
                    # RPA dosyası bulunamadı veya zaten açılmış
                    self.log_message.emit("info", self.config.get_log_text('log_rpa_not_found_or_extracted'))
                    # rpyc dosyaları varsa devam et
                    if self._has_rpyc_files(str(game_dir)):
                        self.log_message.emit("info", self.config.get_log_text('log_rpyc_continue'))
                        return True
                    return False
                    
            except Exception as e:
                self.log_message.emit("error", self.config.get_log_text('log_rpa_error', error=str(e)))
                # Son şans - rpyc dosyaları varsa devam et
                if self._has_rpyc_files(str(game_dir)):
                    self.log_message.emit("info", self.config.get_log_text('log_rpyc_fallback_continue'))
                    return True
                return False
            
        except Exception as e:
            self.log_message.emit("error", self.config.get_log_text('unren_general_error', error=str(e)))
            return False
    
    def _cleanup_legacy_mod_files(self, game_dir: str) -> int:
        """
        UnRen'in eklediği mod dosyalarını temizle.
        Bu dosyalar bazı oyunlarla uyumsuz (örn: 'Screen quick_menu is not known' hatası).
        
        Silinen dosyalar:
        - unren-console.rpy / .rpyc
        - unren-qmenu.rpy / .rpyc
        - unren-quick.rpy / .rpyc
        - unren-rollback.rpy / .rpyc
        - unren-skip.rpy / .rpyc
        
        Returns: Silinen dosya sayısı
        """
        cleanup_patterns = [
            "unren-console.rpy", "unren-console.rpyc",
            "unren-qmenu.rpy", "unren-qmenu.rpyc",
            "unren-quick.rpy", "unren-quick.rpyc",
            "unren-rollback.rpy", "unren-rollback.rpyc",
            "unren-skip.rpy", "unren-skip.rpyc",
        ]
        
        deleted_count = 0
        for filename in cleanup_patterns:
            filepath = os.path.join(game_dir, filename)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    self.log_message.emit("info", self.config.get_log_text('unren_mod_deleted', filename=filename))
                    deleted_count += 1
            except Exception as e:
                self.log_message.emit("warning", self.config.get_log_text('unren_mod_delete_failed', filename=filename, error=str(e)))
        
        if deleted_count > 0:
            self.log_message.emit("info", self.config.get_log_text('unren_mod_cleanup_done', count=deleted_count))
        
        return deleted_count
    
    def _run_translate_command(self, project_path: str) -> bool:
        """Kaynak dosyaları parse edip tl/ klasörüne çeviri şablonları oluştur
        
        ÖNEMLİ: Ren'Py String Translation sistemi kullanılıyor.
        Bu sistemde aynı string sadece BİR KERE tanımlanabilir (global tekil).
        Bu nedenle tüm stringler (diyalog + UI) tek bir dosyada toplanıyor.
        """
        try:
            self.log_message.emit("info", self.config.get_log_text('log_translation_files_creating', lang=self.target_language))
            
            # Dil ismini belirle (ISO kodu yerine klasör ismi)
            reverse_lang_map = {v.lower(): k for k, v in RENPY_TO_API_LANG.items()}
            renpy_lang = reverse_lang_map.get(self.target_language.lower(), self.target_language)
            
            game_dir = os.path.join(project_path, 'game')
            tl_dir = os.path.join(game_dir, 'tl', renpy_lang)
            
            # tl dizini oluştur
            os.makedirs(tl_dir, exist_ok=True)
            
            # Kaynak dosyaları parse et
            from src.core.parser import RenPyParser
            parser = RenPyParser(self.config)

            # Resolve feature flags once so they can be reused for source/common scanning
            use_deep = getattr(self, 'include_deep_scan', False)
            use_rpyc = getattr(self, 'include_rpyc', False)

            if self.config and hasattr(self.config, 'translation_settings'):
                settings = self.config.translation_settings
                # If explicit override wasn't set (or False), fallback to config
                if not use_deep:
                    use_deep = getattr(settings, 'enable_deep_scan', getattr(settings, 'use_deep_scan', True))

                # USER REQUEST: Force enable RPYC scanning to ensure maximum coverage
                # We always scan RPYC files to catch strings missing from decompiled RPYs
                use_rpyc = True

            # 1. Parse 'game' directory
            # Parse 'game' directory and flatten results
            self.log_message.emit("info", "Scanning source .rpy files...")
            parse_results = parser.extract_combined(
                game_dir,
                include_rpy=True,
                include_rpyc=use_rpyc,
                include_deep_scan=use_deep,
                recursive=True,
                progress_callback=lambda current, total, file_path: self._emit_scan_progress(
                    "Source scan progress",
                    current,
                    total,
                    file_path,
                    step=50,
                ),
            )
            source_texts = []
            for i, (file_path, entries) in enumerate(parse_results.items()):
                for entry in entries:
                    entry['file_path'] = str(file_path)
                    source_texts.append(entry)
                
                # Yield periodically to keep UI responsive
                if i % 50 == 0:
                    time.sleep(0.001)
            self.log_message.emit("info", f"Source scan completed. {len(parse_results)} files processed.")

            # Remove any entries that originate from game/renpy/common — we'll re-parse them with
            # a temporary parser that forces UI scanning for engine common strings.
            renpy_common_path = os.path.normpath(os.path.abspath(os.path.join(game_dir, 'renpy', 'common')))
            if os.path.isdir(renpy_common_path):
                before_len = len(source_texts)
                def abs_path(p):
                    try:
                        return os.path.normpath(os.path.abspath(str(p)))
                    except Exception:
                        return ''
                source_texts = [e for e in source_texts if not abs_path(e.get('file_path', '')).startswith(renpy_common_path)]
                after_len = len(source_texts)
                if before_len != after_len:
                    self.log_message.emit('debug', f'Removed {before_len - after_len} entries from initial game parse that belong to renpy/common to avoid duplicates')

            # Explicitly scan 'renpy/common' if it exists in project root
            renpy_dir = os.path.join(project_path, 'renpy')
            renpy_common = os.path.join(renpy_dir, 'common')

            if os.path.isdir(renpy_common):
                self.log_message.emit("info", self.config.get_log_text('log_scanning_renpy_common', path=renpy_common))
                # Parse 'renpy/common' and flatten results
                # Use temporary parser with forced UI scanning so engine UI strings are included
                from src.core.parser import RenPyParser
                from src.utils.config import ConfigManager as LocalConfig
                import copy
                temp_conf = LocalConfig()
                temp_conf.translation_settings = copy.deepcopy(self.config.translation_settings)
                temp_conf.translation_settings.translate_ui = True
                temp_parser = RenPyParser(temp_conf)
                try:
                    common_results = temp_parser.parse_directory(renpy_common)
                except Exception:
                    common_results = parser.parse_directory(renpy_common)
                
                # Filter out obvious technical entries that might have slipped through
                for file_path, entries in common_results.items():
                    valid_entries = []
                    for entry in entries:
                        txt = entry.get('text', '')
                        # Engine strings in common are usually UI: "Quit", "Are you sure?", etc.
                        # If it has heavy punctuation, glob markers, or looks like code, skip it.
                        if re.search(r'[\\#\[\](){}|*+?^$]', txt): 
                             if len(txt) > 10 or re.search(r'\*\*?/\*\*?|\.[a-z0-9]+$', txt):
                                 continue
                        
                        # Skip common technical words that are not UI
                        if txt.lower().strip() in parser.renpy_technical_terms:
                            continue
                            
                        valid_entries.append(entry)
                    
                    for entry in valid_entries:
                        entry['file_path'] = str(file_path)
                        entry['is_engine_common'] = True
                        source_texts.append(entry)
                # If engine/common ships only .rpyc files, optionally parse them too
                if use_rpyc:
                    try:
                        from src.core.rpyc_reader import extract_texts_from_rpyc_directory
                        rpyc_results = extract_texts_from_rpyc_directory(renpy_common)
                        for file_path, entries in rpyc_results.items():
                            for entry in entries:
                                txt = entry.get('text', '')
                                if re.search(r'[\\#\[\](){}|*+?^$]', txt):
                                    if len(txt) > 10 or re.search(r'\*\*?/\*\*?|\.[a-z0-9]+$', txt):
                                        continue
                                if txt.lower().strip() in parser.renpy_technical_terms:
                                    continue

                                patched = dict(entry)
                                patched['file_path'] = str(file_path)
                                patched['is_engine_common'] = True
                                if 'text_type' in patched and 'type' not in patched:
                                    patched['type'] = patched.get('text_type')
                                source_texts.append(patched)
                    except Exception as exc:
                        self.log_message.emit("warning", self.config.get_log_text('log_engine_common_scan_failed', error=str(exc)))
            # SDK scanning removed (v2.5.0)
            pass

            # --- FIX START: Initialize and Populate Results ---
            deep_results = {}
            rpyc_results = {}
            existing_texts = {e['text'] for e in source_texts} # For dedup
            deep_count = 0

            # 3. Deep Scan Execution
            # Check config (default to True if not set)
            if use_deep:
                self.log_message.emit("info", self.config.get_log_text('deep_scan_running_short'))
                deep_results = parser.extract_from_directory_with_deep_scan(
                    game_dir,
                    progress_callback=lambda current, total, file_path: self._emit_scan_progress(
                        "Deep scan progress",
                        current,
                        total,
                        file_path,
                        step=25,
                    ),
                )

            # 4. RPYC Execution
            if use_rpyc:
                self.log_message.emit("warning", "⏳ Scanning .rpyc (Binary) database... This may take time depending on file size. Please wait, program is not frozen!")
                self.log_message.emit("info", self.config.get_log_text('rpyc_scan_running'))
                # Import here to avoid circular imports if any
                try:
                    from src.core.rpyc_reader import extract_texts_from_rpyc_directory
                    rpyc_results = extract_texts_from_rpyc_directory(game_dir, config_manager=self.config)
                    self.log_message.emit("success", f"✅ .rpyc scan completed. {len(rpyc_results)} files processed.")
                except ImportError:
                    self.log_message.emit("warning", self.config.get_log_text('rpyc_module_not_found'))
            # --- FIX END ---
            
            # --- EKSİK OLAN BİRLEŞTİRME KODU BAŞLANGICI ---

            # Deep Scan Sonuçlarını Birleştir
            if deep_results:
                self.log_message.emit("info", self.config.get_log_text('deep_scan_merging'))
                for file_path, entries in deep_results.items():
                    for entry in entries:
                        if entry.get('is_deep_scan'):
                            entry['file_path'] = str(file_path)
                            source_texts.append(entry)

            # RPYC Sonuçlarını Birleştir
            if rpyc_results:
                self.log_message.emit("info", self.config.get_log_text('rpyc_data_merging'))
                # Mevcut metinleri kontrol et (tekrarı önlemek için)
                existing_texts = {e.get('text') for e in source_texts}

                for file_path, entries in rpyc_results.items():
                    for entry in entries:
                        text = entry.get('text', '')
                        if text and text not in existing_texts:
                            entry['file_path'] = str(file_path)
                            source_texts.append(entry)
                            existing_texts.add(text)

            # --- EKSİK OLAN BİRLEŞTİRME KODU BİTİŞİ ---
            
            if not source_texts:
                self.log_message.emit("warning", self.config.get_log_text('no_translatable_texts'))
                return False
            
            self.log_message.emit("info", self.config.get_log_text('texts_found_creating', count=len(source_texts)))
            
            # Check for existing translations in the tl folder to avoid duplicates
            # If a string is already in options.rpy or screens.rpy, adding it to strings.rpy causes a crash
            existing_global_strings = set()
            try:
                lang_tl_path = os.path.join(game_dir, 'tl', renpy_lang)
                if os.path.isdir(lang_tl_path):
                    # Direct scan for 'old "..."' and 'new "..."' pairs in existing .rpy files
                    # Patterns for old-new pairs in strings
                    # Improved regex to handle various indentation and optional spaces
                    string_pair_pattern = re.compile(r'^\s*old\s+"(?P<old>.*?)"\s*\n\s*new\s+"(?P<new>.*?)"\s*$', re.MULTILINE | re.DOTALL)
                    
                    # Dialogue format in tl files (comments with # and then the translation)
                    dialogue_block_pat = re.compile(r'^\s*#\s*(?:\w+\s+)?"(?P<old>.*?)"\s*\n\s*(?:\w+\s+)?"(?P<new>.*?)"\s*$', re.MULTILINE | re.DOTALL)
                    
                    for root, dirs, files in os.walk(lang_tl_path):
                        for filename in files:
                            # Skip compiled files
                            if not filename.lower().endswith('.rpy'):
                                continue
                            
                            filepath = os.path.join(root, filename)
                            try:
                                with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
                                    content = f.read()
                                
                                # Find all 'old/new' pairs
                                for match in string_pair_pattern.finditer(content):
                                    old_text = match.group('old')
                                    
                                    # FIX v2.8.6: Include ALL 'old "text"' entries in existing tl/ files,
                                    # even those with empty 'new ""'. In Ren'Py 7.5+/8.x, duplicate
                                    # 'old "text"' definitions across files cause a crash regardless
                                    # of whether the translation is empty or not.
                                    if old_text:
                                        # Normalize newlines and unescape for consistency
                                        old_text = old_text.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                                        existing_global_strings.add(old_text)
                                
                                # Dialogue check
                                for m2 in dialogue_block_pat.finditer(content):
                                    old_t = m2.group('old')
                                    if old_t:
                                        old_t = old_t.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                                        existing_global_strings.add(old_t)
                                        
                                self.logger.debug(f"Scanned {filepath}: found {len(existing_global_strings)} actively translated entries")
                            except Exception as fe:
                                self.logger.debug(f"Failed to scan {filepath}: {fe}")
                    
                    if existing_global_strings:
                        self.log_message.emit("info", f"Found {len(existing_global_strings)} existing 'old \"...\"' entries in tl/ files (including untranslated placeholders). Skipping these to prevent Ren'Py duplicate-string crash (7.5+/8.x).")
            except Exception as e:
                self.logger.warning(f"Existing TL scan failed: {e}")

            # TÜM metinleri GLOBAL olarak tekil tut
            # Ren'Py String Translation'da aynı string sadece 1 kere tanımlanabilir
            # Prefers entries marked as engine_common if duplicates occur
            seen_map = {}
            for entry in source_texts:
                text = entry.get('text', '')
                if not text:
                    continue
                
                # Skip if already exists in other .rpy files in tl/ folder
                if text in existing_global_strings:
                    continue
                    
                existing = seen_map.get(text)
                if not existing:
                    seen_map[text] = entry
                else:
                    # If the existing one is not engine_common but the new one is, prefer the new
                    if not existing.get('is_engine_common') and entry.get('is_engine_common'):
                        seen_map[text] = entry
                    # Prefer deep_scan or contextful entries over generic ones if needed
                    elif not existing.get('is_deep_scan') and entry.get('is_deep_scan'):
                        seen_map[text] = entry

            # 4. Group strings by file for separate .rpy generation
            # Ren'Py allows multiple 'translate strings:' blocks across different files.
            # To avoid duplicates (which cause Ren'Py to crash), we MUST only define each string ONCE.
            # We'll assign each unique string to the FIRST source file it was found in.
            file_groups = {} # {rel_path: [entries]}
            seen_texts = set()
            
            # Add existing global strings (found in other .rpy files) to seen_texts
            # to prevent defining them again in NEW files.
            for t in existing_global_strings:
                seen_texts.add(t)

            for entry in source_texts:
                text = entry.get('text', '')
                if not text or text in seen_texts:
                    continue
                
                # Determine relative file path for mirroring
                file_path = entry.get('file_path', '')
                try:
                    # v2.7.2: Robust path mirroring for separate .rpy generation
                    # If file is outside game(e.g. renpy/common), map it to a safe internal folder
                    if game_dir in file_path:
                        rel_path = os.path.relpath(file_path, game_dir)
                    else:
                        # Map engine common folders to internal safe mirror paths
                        # e.g. .../renpy/common/00sync.rpy -> _engine/common/00sync.rpy
                        if 'renpy' in file_path and 'common' in file_path:
                            rel_path = os.path.join('_engine', 'common', os.path.basename(file_path))
                        else:
                            rel_path = 'external_libs.rpy'
                    
                    # Convert to .rpy in tl folder
                    rel_path = os.path.splitext(rel_path)[0] + '.rpy'
                    # Strip any leading '..' or '/' to prevent path traversal outside tl directory
                    rel_path = rel_path.lstrip('./\\')
                except Exception:
                    rel_path = 'strings.rpy'
                
                if rel_path not in file_groups:
                    file_groups[rel_path] = []
                
                file_groups[rel_path].append(entry)
                seen_texts.add(text)
            
            if not file_groups:
                self.log_message.emit("info", "No new strings to generate for translation files.")
                return True

            self.log_message.emit("info", f"Generating {len(file_groups)} separate translation files for {renpy_lang}...")
            
            # 5. Generate and write each file
            generated_count = 0
            total_entries_count = 0
            
            for rel_path, entries in file_groups.items():
                if self.should_stop: return False
                
                try:
                    content = self._generate_all_strings_file(entries, game_dir, lang_name=renpy_lang)
                    if not content: continue
                    
                    full_path = os.path.normpath(os.path.join(tl_dir, rel_path))
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)
                    
                    # Atomic Write
                    temp_path = full_path + '.tmp'
                    with open(temp_path, 'w', encoding='utf-8-sig', newline='\n') as f:
                        f.write(content)
                        f.flush()
                        os.fsync(f.fileno())
                    
                    if os.path.exists(full_path):
                        # FIX v2.8.6: APPEND instead of overwrite to preserve existing translations.
                        # Multiple 'translate strings:' blocks in the same file are valid in Ren'Py.
                        # This prevents losing dialogue-format blocks and previously-translated entries.
                        translate_start = content.find(f'translate {renpy_lang} strings:')
                        if translate_start >= 0:
                            append_block = '\n\n' + content[translate_start:]
                            try:
                                with open(full_path, 'a', encoding='utf-8-sig', newline='\n') as fa:
                                    fa.write(append_block)
                                os.remove(temp_path)
                            except Exception as _append_err:
                                self.logger.warning(f"Append failed for {rel_path}, falling back to replace: {_append_err}")
                                os.replace(temp_path, full_path)
                        else:
                            # No translate block in content; nothing meaningful to append
                            os.remove(temp_path)
                    else:
                        os.rename(temp_path, full_path)
                    
                    generated_count += 1
                    total_entries_count += len(entries)
                    
                except Exception as fe:
                    self.logger.error(f"Failed to generate {rel_path}: {fe}")
                    continue
            
            self.log_message.emit("success", f"Successfully created {generated_count} translation files ({total_entries_count} unique strings total).")
            return True
                
        except Exception as e:
            self.log_message.emit("error", self.config.get_log_text('translation_file_error', error=str(e)))
            return False
    
    def _generate_all_strings_file(self, entries: List[dict], game_dir: str, lang_name: str = None) -> str:
        """
        Tüm çevrilecek metinleri (diyalog + UI) tek bir strings.rpy dosyasında topla.
        
        Ren'Py String Translation formatı kullanılır:
        translate language strings:
            old "original text"
            new "translated text"
        
        Bu format ID gerektirmez ve her yerde çalışır.
        """
        formatter = RenPyOutputFormatter()
        skipped = 0
        lines = []
        lines.append("# Translation strings file")
        lines.append("# Auto-generated by RenLocalizer")
        lines.append("# Using Ren'Py String Translation format for maximum compatibility")
        lines.append("")
        
        target_lang = lang_name if lang_name else self.target_language
        
        rel_path_cache = {}
        seen_texts = set()
        entries_added = 0
        
        for i, entry in enumerate(entries):
            text = entry.get('text', '')
            if not text or formatter._should_skip_translation(text):
                skipped += 1
                continue
                
            # Global deduplication by text content to prevent bloating
            if text in seen_texts:
                continue
            seen_texts.add(text)
            
            file_path = entry.get('file_path', '')
            line_num = entry.get('line_number', 0)
            character = entry.get('character', '')
            text_type = entry.get('text_type', 'unknown')
            is_nontranslatable_identifier = self._is_nontranslatable_identifier_entry(entry)
            
            escaped_text = self._escape_rpy_string(text)
            
            if file_path in rel_path_cache:
                rel_path = rel_path_cache[file_path]
            else:
                rel_path = 'unknown'
                if file_path:
                    try:
                        rel_path = os.path.relpath(file_path, game_dir)
                    except ValueError:
                        rel_path = os.path.abspath(file_path)
                rel_path_cache[file_path] = rel_path
            
            # Start gathering the actual strings before the header to determine if any exist
            entry_lines = []
            
            # Kaynak bilgisi ve karakter adını yorum olarak ekle
            comment_parts = [f"{rel_path}:{line_num}"]
            if character:
                comment_parts.append(f"({character})")
            if text_type and text_type != 'dialogue':
                comment_parts.append(f"[{text_type}]")
            if entry.get('is_engine_common'):
                comment_parts.append('[engine_common]')
            
            entry_lines.append(f"    # {' '.join(comment_parts)}")
            
            # Check cache for existing translation to support seamless resume
            cached_translation = ""
            if self.translation_manager and not is_nontranslatable_identifier:
                api_target = RENPY_TO_API_LANG.get(self.target_language, self.target_language)
                api_source = RENPY_TO_API_LANG.get(self.source_language, self.source_language)
                
                # Fast path: Try with current engine settings
                cache_key = (self.engine.value, api_source, api_target, text)
                cached_res = self.translation_manager._cache.get(cache_key)
                
                # If not found with exact key, try loose match (any engine, same languages)
                if not cached_res:
                    for k, v in self.translation_manager._cache.items():
                        if len(k) >= 4 and k[2] == api_target and k[3] == text:
                            cached_res = v
                            break
                            
                if cached_res and cached_res.success:
                    cached_translation = self._escape_rpy_string(cached_res.translated_text)

            if is_nontranslatable_identifier:
                cached_translation = escaped_text

            entry_lines.append(f'    old "{escaped_text}"')
            entry_lines.append(f'    new "{cached_translation}"')
            entry_lines.append("")
            
            # Add to main lines
            lines.extend(entry_lines)
            entries_added += 1
            
            # Yield GIL periodically to keep UI alive
            if i % 100 == 0:
                time.sleep(0.001)
        
        # v2.7.2 Fix: If NO translatable entries were found, do NOT return a file content.
        # This prevents "translate strings statement expects a non-empty block" errors in Ren'Py.
        if entries_added == 0:
            return None
            
        # Add the header and return
        header = [
            "# Translation strings file",
            "# Auto-generated by RenLocalizer",
            "# Using Ren'Py String Translation format for maximum compatibility",
            "",
            f"translate {target_lang} strings:",
            ""
        ]
        
        if skipped:
            try:
                self.log_message.emit("debug", self.config.get_log_text('technical_entries_skipped', count=skipped))
            except Exception:
                pass

        return '\n'.join(header + lines)
    
    def _protect_glossary_terms(self, text: str) -> Tuple[str, Dict[str, str]]:
        """Sözlük terimlerini Unicode bracket placeholder ile korur ve karşılıklarını saklar.
        
        v3.4: [[g0]] formatı yerine ⟦RLPH{ns}_gN⟧ formatına geçildi.
        Eski format Google Translate tarafından kolayca bozuluyordu çünkü
        [[ ]] çift köşeli parantezler çeviri motorları için anlamlı değildi.
        Yeni format, syntax_guard ile aynı Unicode matematiksel parantez (U+27E6/U+27E7)
        kullanır — Google bunlara "tanımsız sembol" olarak dokunmaz.
        """
        if not self.config or not hasattr(self.config, 'glossary') or not self.config.glossary:
            return text, {}
            
        import uuid
        placeholders = {}
        counter = 0
        token_namespace = uuid.uuid4().hex[:6].upper()
        # En uzun terimler önce (çakışmayı önlemek için)
        sorted_terms = sorted(self.config.glossary.items(), key=lambda x: -len(x[0]))
        
        result = text
        for src, dst in sorted_terms:
            if not src or not dst: continue
            
            # Sadece tam kelime eşleşmesi (\b)
            pattern = re.compile(r'(?i)\b' + re.escape(src) + r'\b')
            
            def replace_func(match):
                nonlocal counter
                key = f"\u27e6RLPH{token_namespace}_G{counter}\u27e7"
                placeholders[key] = dst  # Hedef çeviriyi yer tutucu sözlüğüne koy!
                counter += 1
                return key
                
            result = pattern.sub(replace_func, result)
            
        return result, placeholders

    def _escape_rpy_string(self, text: str) -> str:
        """Ren'Py string formatı için escape et"""
        if not text:
            return text
        
        # Escape sequences
        text = text.replace('\\', '\\\\')
        text = text.replace('"', '\\"')
        text = text.replace('\n', '\\n')
        text = text.replace('\t', '\\t')
        
        return text

    def _is_nontranslatable_identifier_entry(self, entry) -> bool:
        """style_prefix gibi kimlik/anahtar tipindeki girdiler çevrilmemeli."""
        try:
            if isinstance(entry, dict):
                character = (entry.get('character') or '').strip().lower()
            else:
                character = (getattr(entry, 'character', '') or '').strip().lower()
            return character == 'style_prefix'
        except Exception:
            return False
    
    def _translate_entries(self, entries: List[TranslationEntry]) -> Dict[str, str]:
        """Girişleri çevir (placeholder koruması zorunlu)."""
        from src.core.translator import protect_renpy_syntax
        from src.core.syntax_guard import split_delimited_text, rejoin_delimited_text, split_angle_pipe_groups, rejoin_angle_pipe_groups
        translations = {}
        self._last_atomic_segments = {}  # v2.7.1: Delimiter atomik segment çiftleri
        formatter = RenPyOutputFormatter()

        # Teknik/yer tutucu metinleri çeviri kuyruğundan ayıkla
        filtered_entries: List[TranslationEntry] = []
        for entry in entries:
            if self._is_nontranslatable_identifier_entry(entry):
                continue
            if formatter._should_skip_translation(entry.original_text):
                continue
            filtered_entries.append(entry)

        skipped = len(entries) - len(filtered_entries)
        if skipped:
            self.log_message.emit("debug", self.config.get_log_text('placeholder_excluded', count=skipped))

        entries = filtered_entries
        total = len(entries)

        # Connect all translators to the pipeline's log signal and stop callback
        self.translation_manager.should_stop_callback = lambda: self.should_stop
        for engine_type, translator in self.translation_manager.translators.items():
            if hasattr(translator, 'status_callback'):
                translator.status_callback = self.log_message.emit
            if hasattr(translator, 'should_stop_callback'):
                translator.should_stop_callback = lambda: self.should_stop
        if total == 0:
            # cache_file henüz tanımlanmadı — boş sonuç döndür
            return translations

        # Batch çeviri için hazırla
        requested_batch_size = self._get_requested_translation_batch_size()
        batch_size = self._get_effective_translation_batch_size()

        if self.engine in (TranslationEngine.OPENAI, TranslationEngine.GEMINI, TranslationEngine.LOCAL_LLM):
            self.log_message.emit("debug", f"AI engine detected, using batch size: {batch_size}")
            if batch_size > 1000:
                self.log_message.emit(
                    "info",
                    self.config.get_log_text(
                        'log_ai_batch_large_notice',
                        'Large AI batch size in use ({batch}). This may increase token usage, latency, or API failure risk.',
                        batch=batch_size,
                    ),
                )
        else:
            self._emit_batch_size_cap_notice_if_needed(requested_batch_size, batch_size)

        api_target_lang = RENPY_TO_API_LANG.get(self.target_language, self.target_language)
        
        # =====================================================================
        # SMART LANGUAGE DETECTION
        # =====================================================================
        # When source_language is "auto", we detect it once at the start instead
        # of letting Google guess on each request. This prevents short texts like
        # "OK", "Yes", or character names from being incorrectly detected.
        # =====================================================================
        api_source_lang = RENPY_TO_API_LANG.get(self.source_language, self.source_language)
        
        if self.source_language.lower() == "auto" and self.engine == TranslationEngine.GOOGLE:
            self.log_message.emit("info", self.config.get_log_text(
                'smart_detect_starting', 
                "[Smart Detect] Kaynak dil tespit ediliyor..."
            ))
            
            # Get text samples from entries
            text_samples = [e.original_text for e in entries]
            
            # Detect using Google Translator
            translator = self.translation_manager.translators.get(TranslationEngine.GOOGLE)
            if not translator:
                translator = GoogleTranslator(config_manager=self.config)
                self.translation_manager.add_translator(TranslationEngine.GOOGLE, translator)
            
            try:
                # Create a specialized translator just for detection to avoid session/loop conflicts
                # This prevents the 'Event loop is closed' error on the main translator
                detection_translator = GoogleTranslator(config_manager=self.config)
                
                # Create temporary event loop for detection
                detect_loop = asyncio.new_event_loop()
                
                detected_lang = detect_loop.run_until_complete(
                    detection_translator.detect_language(text_samples, target_lang=api_target_lang)
                )
                
                # Close the temporary loop and the detection translator's session
                detect_loop.run_until_complete(detection_translator.close_session())
                detect_loop.close()
                
                if detected_lang:
                    api_source_lang = detected_lang
                    self.log_message.emit("info", self.config.get_log_text(
                        'smart_detect_success',
                        f"[Smart Detect] ✓ Kaynak dil tespit edildi: {detected_lang.upper()}"
                    ))
                else:
                    self.log_message.emit("warning", self.config.get_log_text(
                        'smart_detect_fallback',
                        "[Smart Detect] Güven eşiği geçilemedi, 'auto' modunda devam ediliyor."
                    ))
                    api_source_lang = "auto"
            except Exception as e:
                self.logger.warning(f"Smart language detection failed: {e}")
                api_source_lang = "auto"

        # Cache path management (Global vs Local)
        should_use_global_cache = getattr(self.config.translation_settings, 'use_global_cache', True)
        
        if should_use_global_cache:
            # Create a project name based ID (last part of project_path)
            project_name = os.path.basename(self.project_path.rstrip('/\\'))
            if not project_name:
                project_name = "default_project"
            
            # Use data_dir from config (which accounts for portable mode)
            base_cache_dir = os.path.join(self.config.data_dir, getattr(self.config.translation_settings, 'cache_path', 'cache'))
            cache_dir = os.path.join(base_cache_dir, project_name, self.target_language)
            self.log_message.emit("info", f"Using global data cache: [{project_name}]")
        else:
            # Standard path: game/tl/<lang>/translation_cache.json
            cache_dir = os.path.join(self.project_path, 'game', 'tl', self.target_language)
            self.log_message.emit("info", "Using local project-specific cache.")

        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, "translation_cache.json")
        
        # Load existing cache for resume
        self.translation_manager.load_cache(cache_file)

        # ================================================================
        # v2.7.3: External TM — Load selected TM sources
        # ================================================================
        _external_tm = None
        _tm_hit_count = 0
        if getattr(self.config.translation_settings, 'use_external_tm', False):
            try:
                import json as _json
                tm_source_paths = _json.loads(
                    getattr(self.config.translation_settings, 'external_tm_sources', '[]')
                )
                if tm_source_paths:
                    from src.tools.external_tm import ExternalTMStore
                    tm_dir = str(os.path.join(self.config.data_dir, "tm"))
                    _external_tm = ExternalTMStore(tm_dir=tm_dir)
                    loaded = _external_tm.load_sources(tm_source_paths)
                    if loaded > 0:
                        self.log_message.emit("info", f"[ExternalTM] {loaded} entry loaded from {_external_tm.loaded_source_count} source(s)")
                    else:
                        self.log_message.emit("warning", "[ExternalTM] No entries loaded — TM lookup disabled")
                        _external_tm = None
            except Exception as _tm_err:
                self.logger.warning(f"External TM load failed: {_tm_err}")
                _external_tm = None

        if total == 0:
            # Final Cache Kaydı
            self.translation_manager.save_cache(cache_file)
            return translations

        self.log_message.emit("info", self.config.get_log_text('translation_lang_api', lang=self.target_language, api=api_target_lang))

        loop = asyncio.new_event_loop()
        
        # Ensure translator is registered; fallback to Google/DeepL defaults
        if self.engine == TranslationEngine.GOOGLE and self.engine not in self.translation_manager.translators:
            gt = GoogleTranslator(config_manager=self.config, proxy_manager=getattr(self.translation_manager, "proxy_manager", None))
            self.translation_manager.add_translator(TranslationEngine.GOOGLE, gt)
        if self.engine == TranslationEngine.DEEPL and self.engine not in self.translation_manager.translators:
            deepl_key = getattr(getattr(self.config, "api_keys", None), "deepl_api_key", "") or ""
            dt = DeepLTranslator(api_key=deepl_key, proxy_manager=getattr(self.translation_manager, "proxy_manager", None), config_manager=self.config)
            dt.status_callback = self.log_message.emit
            self.translation_manager.add_translator(TranslationEngine.DEEPL, dt)

        # AI Translators Lazy Init
        if self.engine == TranslationEngine.OPENAI and self.engine not in self.translation_manager.translators:
            # Determine correct API key based on Base URL
            # Users might enter DeepSeek key in its own field but run it via OpenAI engine (compatible mode)
            base_url = self.config.translation_settings.openai_base_url
            api_key_to_use = self.config.api_keys.openai_api_key

            if base_url and "deepseek" in base_url.lower():
                ds_key = getattr(self.config.api_keys, "deepseek_api_key", "")
                if ds_key:
                    self.log_message.emit("info", self.config.get_log_text('log_deepseek_mode'))
                    api_key_to_use = ds_key
                else:
                    self.log_message.emit("info", self.config.get_log_text('log_deepseek_fallback'))

            t = OpenAITranslator(
                api_key=api_key_to_use,
                model=self.config.translation_settings.openai_model,
                base_url=base_url,
                proxy_manager=getattr(self.translation_manager, "proxy_manager", None),
                config_manager=self.config,
                temperature=self.config.translation_settings.ai_temperature,
                timeout=self.config.translation_settings.ai_timeout,
                max_tokens=self.config.translation_settings.ai_max_tokens
            )
            t.status_callback = self.log_message.emit
            self.translation_manager.add_translator(TranslationEngine.OPENAI, t)

        if self.engine == TranslationEngine.GEMINI and self.engine not in self.translation_manager.translators:
            t = GeminiTranslator(
                api_key=self.config.api_keys.gemini_api_key,
                model=self.config.translation_settings.gemini_model,
                safety_level=self.config.translation_settings.gemini_safety_settings,
                proxy_manager=getattr(self.translation_manager, "proxy_manager", None),
                config_manager=self.config,
                temperature=self.config.translation_settings.ai_temperature,
                timeout=self.config.translation_settings.ai_timeout,
                max_tokens=self.config.translation_settings.ai_max_tokens
            )
            # Add fallback to Google
            fallback = GoogleTranslator(
                proxy_manager=getattr(self.translation_manager, "proxy_manager", None),
                config_manager=self.config
            )
            fallback.status_callback = self.log_message.emit
            t.set_fallback_translator(fallback)
            t.status_callback = self.log_message.emit
            self.translation_manager.add_translator(TranslationEngine.GEMINI, t)

        if self.engine == TranslationEngine.LOCAL_LLM and self.engine not in self.translation_manager.translators:
            t = LocalLLMTranslator(
                model=self.config.translation_settings.local_llm_model,
                base_url=self.config.translation_settings.local_llm_url,
                proxy_manager=getattr(self.translation_manager, "proxy_manager", None),
                config_manager=self.config,
                temperature=self.config.translation_settings.ai_temperature,
                timeout=self.config.translation_settings.ai_timeout,
                max_tokens=self.config.translation_settings.ai_max_tokens
            )
            t.status_callback = self.log_message.emit
            self.translation_manager.add_translator(TranslationEngine.LOCAL_LLM, t)

        if self.engine == TranslationEngine.LIBRETRANSLATE and self.engine not in self.translation_manager.translators:
            from src.core.translator import LibreTranslateTranslator
            t = LibreTranslateTranslator(
                base_url=self.config.translation_settings.libretranslate_url,
                api_key=self.config.translation_settings.libretranslate_api_key,
                proxy_manager=getattr(self.translation_manager, "proxy_manager", None),
                config_manager=self.config
            )
            t.status_callback = self.log_message.emit
            self.translation_manager.add_translator(TranslationEngine.LIBRETRANSLATE, t)

        # ================================================================
        # v2.7.1: Auto-protect character names — glossary'ye ekle
        # ================================================================
        _auto_names_added = 0
        if getattr(self.config.translation_settings, 'auto_protect_character_names', True):
            existing_glossary = self.config.glossary if hasattr(self.config, 'glossary') and self.config.glossary else {}
            existing_lower = {k.lower() for k in existing_glossary}
            char_names: set = set()
            for entry in entries:
                c = getattr(entry, 'character', '') or ''
                c = c.strip()
                # Değişken isimleri / enterpolasyon değil, gerçek isimler
                # Boşluklu isimler de kabul edilir (örn. "Mary Jane", "Old Man")
                if (c and len(c) >= 2 and not c.startswith('[') and not c.startswith('{')
                        and not c.startswith('$')
                        and c.lower() not in existing_lower
                        and c[0].isupper()):  # İsimler büyük harfle başlar
                    char_names.add(c)
            if char_names:
                # Thread-safe glossary update via config lock if available
                _lock = getattr(self.config, '_lock', None)
                if _lock:
                    _lock.acquire()
                try:
                    for name in char_names:
                        existing_glossary[name] = name  # name → name (korunur, çevrilmez)
                    self.config.glossary = existing_glossary
                finally:
                    if _lock:
                        _lock.release()
                _auto_names_added = len(char_names)
                self.log_message.emit("info", f"[AutoProtect] {_auto_names_added} character name(s) protected: {', '.join(sorted(char_names)[:10])}")

        try:
            unchanged_count = 0
            failed_entries: List[str] = []
            sample_logs: List[str] = []
            stop_quota = False
            for i in range(0, total, batch_size):
                if self.should_stop:
                    break

                batch = entries[i:i + batch_size]

                # Progress güncelle
                current = min(i + batch_size, total)
                if batch:
                    self.progress_updated.emit(current, total, batch[0].original_text[:50])

                # Çeviri istekleri oluştur (her zaman placeholder korumalı)
                requests = []
                # Delimiter-Aware Translation: Her entry için delimiter split bilgisini sakla
                # Key: request listesindeki başlangıç indexi, Value: (entry_idx, segment_count, delimiter, prefix, suffix, translation_id, original_text)
                _delimiter_groups = {}  # {batch_entry_idx: (req_start_idx, seg_count, delim, prefix, suffix, tid, orig_text)}
                # Multi-Group Angle-Pipe (v2.7.5): Çoklu <seg|seg> grupları
                _multi_group_data = {}  # {batch_entry_idx: (req_start, group_lens, tid, orig_text)}
                _delimiter_enabled = getattr(self.config.translation_settings, 'enable_delimiter_aware_translation', True)
                _tm_resolved_indices = set()  # TM ile çözülen entry index'leri — FAZ 1'de atlanacak
                
                _prev_entry_text = None  # extend context tracking — reset per batch
                _prev_entry_file = None  # track file path for cross-file boundary detection
                for entry_idx, entry in enumerate(batch):
                    translation_id = getattr(entry, 'translation_id', '') or TLParser.make_translation_id(
                        entry.file_path,
                        entry.line_number,
                        entry.original_text,
                        getattr(entry, 'context_path', []),
                        getattr(entry, 'raw_text', None)
                    )
                    
                    # ============================================================
                    # EXTERNAL TM LOOKUP (v2.7.3) — API çağrısı yapmadan çevir
                    # ============================================================
                    if _external_tm is not None:
                        _tm_result = _external_tm.get_exact(entry.original_text)
                        if _tm_result is not None:
                            translations[translation_id] = _tm_result
                            translations.setdefault(entry.original_text, _tm_result)
                            _tm_hit_count += 1
                            _tm_resolved_indices.add(entry_idx)  # FAZ 1'de atla
                            # Diagnostics: TM çevirilerini de raporla
                            try:
                                if _tm_result != entry.original_text:
                                    self.diagnostic_report.mark_translated(
                                        entry.file_path, translation_id, _tm_result,
                                        original_text=entry.original_text)
                                else:
                                    self.diagnostic_report.mark_unchanged(
                                        entry.file_path, translation_id,
                                        original_text=entry.original_text)
                            except Exception:
                                pass
                            _prev_entry_text = entry.original_text
                            _prev_entry_file = entry.file_path
                            continue  # API'ye gitmeden devam et
                    
                    # ============================================================
                    # MULTI-GROUP ANGLE-PIPE SPLIT (v2.7.5)
                    # ============================================================
                    # Metindeki TÜM <seg1|seg2|...> gruplarını bulur.
                    # Template ayrı çevrilir, her segment bağımsız çevrilir.
                    # Bu sayede:
                    #   - Çoklu gruplar korunur (GT grup sırasını bozamaz)
                    #   - Çevreleyen metin de tam çevrilir
                    #   - Kısa/tek kelimelik segmentler desteklenir
                    multi_result = split_angle_pipe_groups(entry.original_text) if _delimiter_enabled else None
                    
                    if multi_result is not None:
                        template, groups = multi_result
                        req_start_idx = len(requests)
                        group_lens = [len(g) for g in groups]
                        _multi_group_data[entry_idx] = (req_start_idx, group_lens, translation_id, entry.original_text)
                        
                        _log_preview = entry.original_text[:80].replace('<', '\u2039').replace('>', '\u203a')
                        self.log_message.emit("debug", f"[MultiGroup] {len(groups)} groups ({sum(group_lens)} segments): {_log_preview}")
                        
                        # Request 0: Template ([DGRP_N] placeholder'lı — protect_renpy_syntax korur)
                        protected_template, ph_template = protect_renpy_syntax(template)
                        protected_template, gph_template = self._protect_glossary_terms(protected_template)
                        ph_template.update(gph_template)
                        
                        requests.append(TranslationRequest(
                            text=protected_template,
                            source_lang=api_source_lang,
                            target_lang=api_target_lang,
                            engine=self.engine,
                            metadata={
                                'preprotected': True,
                                'original_text': template,
                                'entry': entry,
                                'translation_id': translation_id,
                                'file_path': entry.file_path,
                                'line_number': entry.line_number,
                                'context_path': getattr(entry, 'context_path', []),
                                'placeholders': ph_template,
                                '_multi_group_template': True,
                            }
                        ))
                        
                        # Requests 1..N: Her grubun segmentleri
                        for group in groups:
                            for seg in group:
                                seg_text = seg.strip()
                                protected_seg, ph_seg = protect_renpy_syntax(seg_text)
                                protected_seg, gph_seg = self._protect_glossary_terms(protected_seg)
                                ph_seg.update(gph_seg)
                                
                                requests.append(TranslationRequest(
                                    text=protected_seg,
                                    source_lang=api_source_lang,
                                    target_lang=api_target_lang,
                                    engine=self.engine,
                                    metadata={
                                        'preprotected': True,
                                        'original_text': seg_text,
                                        'entry': entry,
                                        'translation_id': translation_id,
                                        'file_path': entry.file_path,
                                        'line_number': entry.line_number,
                                        'context_path': getattr(entry, 'context_path', []),
                                        'placeholders': ph_seg,
                                        '_multi_group_segment': True,
                                    }
                                ))
                        _prev_entry_text = entry.original_text  # Track for extend
                        _prev_entry_file = entry.file_path
                        continue  # Multi-group eklendi — normal akışı atla
                    
                    # ============================================================
                    # DELIMITER-AWARE SPLIT (v2.7.2) — Bare pipe fallback
                    # ============================================================
                    # Angle-pipe grubu yoksa, bare pipe pattern'i dene (seg1|seg2|seg3)
                    delim_result = split_delimited_text(entry.original_text) if _delimiter_enabled else None
                    
                    if delim_result is not None:
                        segments, delimiter, d_prefix, d_suffix = delim_result
                        req_start_idx = len(requests)
                        _delimiter_groups[entry_idx] = (req_start_idx, len(segments), delimiter, d_prefix, d_suffix, translation_id, entry.original_text)
                        
                        _log_preview = entry.original_text[:80].replace('<', '\u2039').replace('>', '\u203a')
                        self.log_message.emit("debug", f"[Delimiter] Split into {len(segments)} segments: {_log_preview}")
                        
                        # Her segmenti ayrı bir request olarak ekle
                        for seg in segments:
                            seg_text = seg.strip()
                            protected_text, placeholders = protect_renpy_syntax(seg_text)
                            protected_text, glossary_placeholders = self._protect_glossary_terms(protected_text)
                            placeholders.update(glossary_placeholders)
                            
                            req = TranslationRequest(
                                text=protected_text,
                                source_lang=api_source_lang,
                                target_lang=api_target_lang,
                                engine=self.engine,
                                metadata={
                                    'preprotected': True,
                                    'original_text': seg_text,
                                    'entry': entry,
                                    'translation_id': translation_id,
                                    'file_path': entry.file_path,
                                    'line_number': entry.line_number,
                                    'context_path': getattr(entry, 'context_path', []),
                                    'placeholders': placeholders,
                                    '_delimiter_segment': True,  # İşaretçi: bu bir segment
                                }
                            )
                            requests.append(req)
                        _prev_entry_text = entry.original_text  # Track for extend
                        _prev_entry_file = entry.file_path
                        continue  # Normal akışı atla — segmentler eklendi
                    
                    # ============================================================
                    # Normal (non-delimited) entry işleme
                    # ============================================================
                    # Her metni çeviri öncesi koru (Ren'Py tagleri + Sözlük terimleri)
                    protected_text, placeholders = protect_renpy_syntax(entry.original_text)
                    
                    # Sözlük koruması uygula
                    protected_text, glossary_placeholders = self._protect_glossary_terms(protected_text)
                    placeholders.update(glossary_placeholders)
                    
                    req = TranslationRequest(
                        text=protected_text,  # KORUNMUŞ metin
                        source_lang=api_source_lang,
                        target_lang=api_target_lang,
                        engine=self.engine,
                        metadata={
                            'preprotected': True,
                            'original_text': entry.original_text,
                            'entry': entry,
                            'translation_id': translation_id,
                            'file_path': entry.file_path,
                            'line_number': entry.line_number,
                            'context_path': getattr(entry, 'context_path', []),
                            'placeholders': placeholders,
                            'context_hint': _prev_entry_text if (
                                getattr(entry, 'text_type', '') == 'extend'
                                and _prev_entry_file == entry.file_path  # Same file only
                            ) else None,
                        }
                    )
                    requests.append(req)
                    _prev_entry_text = entry.original_text  # Track for next extend
                    _prev_entry_file = entry.file_path

                # Batch çeviri
                self.translation_manager.set_proxy_enabled(self.use_proxy)
                self.translation_manager.ai_request_delay = getattr(self.config.translation_settings, 'ai_request_delay', 1.5)
                results = loop.run_until_complete(
                    self.translation_manager.translate_batch(requests)
                )

                # Sonuçları kaydet (her zaman restore ile!)
                # ============================================================
                # FAZ 1: Delimiter gruplarını birleştir
                # ============================================================
                # Önce delimiter segmentlerini birleştirip her batch entry için
                # tek bir çevrilmiş metin elde edelim.
                # _delimiter_groups: {entry_idx: (req_start_idx, seg_count, delim, prefix, suffix, tid, orig_text)}
                # _multi_group_data: {entry_idx: (req_start, group_lens, tid, orig_text)}
                
                # Request sonuçlarını entry bazında eşle
                # Normal entry: 1 request = 1 result
                # Delimited entry: N request = N result → rejoin
                # Multi-group entry: 1 template + sum(group_lens) segments → rejoin
                
                # Build a unified result list aligned with batch entries
                _entry_results = []  # List of (tid, restored_text_or_None, entry, success, error, request)
                _atomic_segments = []  # List of (original_seg, translated_seg) pairs for delimiter entries
                _req_cursor = 0  # Tracks position in results list
                
                for entry_idx, entry in enumerate(batch):
                    # TM ile çözülen entry'leri atla — bunlar için request yok
                    if entry_idx in _tm_resolved_indices:
                        continue
                    
                    if entry_idx in _multi_group_data:
                        # ── Multi-Group Angle-Pipe (v2.7.5) ──
                        req_start, group_lens, tid, orig_text = _multi_group_data[entry_idx]
                        total_reqs = 1 + sum(group_lens)  # 1 template + segments
                        
                        # Result 0: Çevrilmiş template
                        template_idx = req_start
                        all_success = True
                        seg_error = None
                        
                        if template_idx < len(results):
                            template_result = results[template_idx]
                            if not template_result.success or not template_result.translated_text:
                                all_success = False
                                seg_error = (template_result.error or "empty_template")
                                if template_result.quota_exceeded:
                                    stop_quota = True
                        else:
                            all_success = False
                            seg_error = "missing_template_result"
                        
                        translated_template = None
                        translated_groups = []
                        
                        if all_success:
                            translated_template = template_result.translated_text
                            if self.config and hasattr(self.config, 'glossary') and self.config.glossary:
                                translated_template = formatter.apply_glossary(
                                    text=translated_template, glossary=self.config.glossary,
                                    original_text=template_result.metadata.get('original_text', '')
                                )
                            
                            # Segment sonuçlarını gruplara ayır
                            seg_cursor = req_start + 1  # template'den sonra
                            for gl in group_lens:
                                group_segs = []
                                for s in range(gl):
                                    r_idx = seg_cursor + s
                                    if r_idx < len(results):
                                        result = results[r_idx]
                                        if result.success and result.translated_text:
                                            raw = result.translated_text
                                            if self.config and hasattr(self.config, 'glossary') and self.config.glossary:
                                                raw = formatter.apply_glossary(
                                                    text=raw, glossary=self.config.glossary,
                                                    original_text=result.metadata.get('original_text', '')
                                                )
                                            group_segs.append(raw)
                                        else:
                                            all_success = False
                                            seg_error = result.error or "empty_segment"
                                            if result.quota_exceeded:
                                                stop_quota = True
                                            break
                                        if result.quota_exceeded:
                                            stop_quota = True
                                    else:
                                        all_success = False
                                        seg_error = "missing_segment_result"
                                        break
                                translated_groups.append(group_segs)
                                seg_cursor += gl
                                if not all_success:
                                    break
                        
                        _req_cursor = req_start + total_reqs
                        
                        if all_success and translated_template and len(translated_groups) == len(group_lens):
                            restored = rejoin_angle_pipe_groups(translated_template, translated_groups)
                            
                            if restored is None:
                                self.log_message.emit(
                                    "guard",
                                    self.config.get_log_text(
                                        'log_guard_structural_original',
                                        '{category} guard kept original text after structural validation: {preview}',
                                        category='[MultiGroup]',
                                        preview=orig_text[:80],
                                    ),
                                )
                                _entry_results.append((tid, orig_text, entry, True, None, None))
                            else:
                                _entry_results.append((tid, restored, entry, True, None, None))
                                # ── Atomik segment kaydı (v2.7.1) ──
                                # Her segmentin orijinal→çeviri çiftini kaydet.
                                # Ren'Py runtime'da vary() ile segmentleri ayrı ayrı çağırır.
                                seg_r_cursor = req_start + 1  # template'den sonra
                                for grp_segs in translated_groups:
                                    for tr_seg in grp_segs:
                                        if seg_r_cursor < len(results):
                                            orig_seg = results[seg_r_cursor].metadata.get('original_text', '')
                                            if orig_seg and tr_seg and orig_seg != tr_seg:
                                                _atomic_segments.append((orig_seg, tr_seg))
                                            seg_r_cursor += 1
                        else:
                            _entry_results.append((tid, None, entry, False, seg_error, None))
                    
                    elif entry_idx in _delimiter_groups:
                        # Bu entry delimiter-split edilmişti
                        req_start, seg_count, delim, d_prefix, d_suffix, tid, orig_text = _delimiter_groups[entry_idx]
                        
                        translated_segments = []
                        all_success = True
                        seg_error = None
                        
                        for seg_i in range(seg_count):
                            r_idx = req_start + seg_i
                            if r_idx < len(results):
                                result = results[r_idx]
                                if result.success and result.translated_text:
                                    raw = result.translated_text
                                    if self.config and hasattr(self.config, 'glossary') and self.config.glossary:
                                        raw = formatter.apply_glossary(
                                            text=raw, glossary=self.config.glossary,
                                            original_text=result.metadata.get('original_text', '')
                                        )
                                    translated_segments.append(raw)
                                else:
                                    all_success = False
                                    seg_error = result.error or "empty"
                                    if result.quota_exceeded:
                                        stop_quota = True
                                    break
                                if result.quota_exceeded:
                                    stop_quota = True
                            else:
                                all_success = False
                                seg_error = "missing_result"
                                break
                        
                        _req_cursor = req_start + seg_count
                        
                        if all_success and len(translated_segments) == seg_count:
                            # Segmentleri geri birleştir (v2.7.3: yapısal doğrulama ile)
                            restored = rejoin_delimited_text(translated_segments, delim, d_prefix, d_suffix, original_text=orig_text)
                            
                            if restored is None:
                                # Yapısal bozulma tespit edildi — orijinal metni koru
                                self.log_message.emit(
                                    "guard",
                                    self.config.get_log_text(
                                        'log_guard_structural_original',
                                        '{category} guard kept original text after structural validation: {preview}',
                                        category='[Delimiter]',
                                        preview=orig_text[:80],
                                    ),
                                )
                                _entry_results.append((tid, orig_text, entry, True, None, None))
                            else:
                                _entry_results.append((tid, restored, entry, True, None, None))
                                # ── Atomik segment kaydı (v2.7.1) ──
                                # Bare-pipe segmentlerinin her birini ayrı çeviri girişi olarak kaydet.
                                for seg_i in range(seg_count):
                                    r_idx = req_start + seg_i
                                    if r_idx < len(results) and results[r_idx].success:
                                        orig_seg = results[r_idx].metadata.get('original_text', '')
                                        tr_seg = translated_segments[seg_i] if seg_i < len(translated_segments) else ''
                                        if orig_seg and tr_seg and orig_seg != tr_seg:
                                            _atomic_segments.append((orig_seg, tr_seg))
                        else:
                            # Herhangi bir segment başarısız ise orijinali koru
                            _entry_results.append((tid, None, entry, False, seg_error, None))
                    else:
                        # Normal (non-delimited) entry
                        if _req_cursor < len(results):
                            result_request = requests[_req_cursor]
                            result = results[_req_cursor]
                            _req_cursor += 1
                            
                            if result.quota_exceeded:
                                stop_quota = True
                            
                            if result.success:
                                translated_raw = result.translated_text
                                if self.config and hasattr(self.config, 'glossary') and self.config.glossary:
                                    translated_raw = formatter.apply_glossary(
                                        text=translated_raw, 
                                        glossary=self.config.glossary,
                                        original_text=entry.original_text
                                    )
                                restored = translated_raw if translated_raw else ""
                                _entry_results.append((result.metadata.get('translation_id') or result.original_text, restored, entry, True, None, result_request))
                            else:
                                _entry_results.append((result.metadata.get('translation_id') or result.original_text, None, entry, False, result.error or "empty", result_request))
                        else:
                            _entry_results.append(("", None, entry, False, "missing_result", None))
                
                # ============================================================
                # FAZ 2: Sonuçları translations'a yaz
                # ============================================================
                for tid, restored, entry, success, error, request in _entry_results:
                    if success and restored is not None:
                        retry_recovered = False
                        blocked_reason = None
                        if restored.strip() == entry.original_text.strip() and self._should_retry_unchanged_core_ui(entry.original_text):
                            restored, retry_recovered = self._retry_unchanged_core_ui(loop, request, entry, restored)
                            if retry_recovered and self.config and hasattr(self.config, 'glossary') and self.config.glossary:
                                restored = formatter.apply_glossary(
                                    text=restored,
                                    glossary=self.config.glossary,
                                    original_text=entry.original_text,
                                )

                        restored, blocked_reason = self._sanitize_translation_for_output(
                            original=entry.original_text,
                            translated=restored,
                            file_path=entry.file_path,
                            translation_id=tid,
                            line_number=entry.line_number,
                        )
                        if blocked_reason is not None:
                            guard_reason_text = self._get_guard_reason_text(blocked_reason)
                            self.log_message.emit(
                                "guard",
                                self.config.get_log_text(
                                    'log_guard_reverted_translation',
                                    'Guard kept original text after suspicious translator output ({reason}) in {path}:{line}',
                                    reason=guard_reason_text,
                                    path=entry.file_path,
                                    line=entry.line_number,
                                ),
                            )
                        
                        if restored:
                            translations[tid] = restored
                            translations.setdefault(entry.original_text, restored)
                            
                            # Diagnostics: record translated and unchanged
                            try:
                                file_path = entry.file_path
                                if blocked_reason is not None:
                                    pass
                                elif retry_recovered:
                                    self.diagnostic_report.mark_translated(file_path, tid, restored, original_text=entry.original_text)
                                    self.diagnostic_report.mark_recovered(
                                        file_path,
                                        tid,
                                        'retry',
                                        original_text=entry.original_text,
                                        translated_text=restored,
                                    )
                                    self._record_translation_guard_event(
                                        category='recovered_by_retry',
                                        file_path=file_path,
                                        translation_id=tid,
                                        original_text=entry.original_text,
                                        translated_text=restored,
                                        detail='core_ui_retry',
                                        line_number=entry.line_number,
                                    )
                                elif restored == entry.original_text:
                                    unchanged_reason = 'unchanged_core_ui' if self._should_retry_unchanged_core_ui(entry.original_text) else None
                                    self.diagnostic_report.mark_unchanged(
                                        file_path,
                                        tid,
                                        original_text=entry.original_text,
                                        reason=unchanged_reason,
                                    )
                                    if unchanged_reason:
                                        self._record_translation_guard_event(
                                            category='unchanged_by_engine',
                                            file_path=file_path,
                                            translation_id=tid,
                                            original_text=entry.original_text,
                                            translated_text=restored,
                                            detail=unchanged_reason,
                                            line_number=entry.line_number,
                                        )
                                else:
                                    self.diagnostic_report.mark_translated(file_path, tid, restored, original_text=entry.original_text)
                            except Exception:
                                pass
                            
                            if restored == entry.original_text and blocked_reason is None:
                                unchanged_count += 1
                                if len(sample_logs) < 5:
                                    sample_logs.append(f"UNCHANGED {entry.file_path}:{entry.line_number} -> {entry.original_text[:80]}")
                    else:
                        err = error or "empty"
                        file_info = f"{entry.file_path}:{entry.line_number}"
                        if file_info == ":":
                            err_entry = f"({err})"
                        else:
                            err_entry = f"{file_info} ({err})"
                        failed_entries.append(err_entry)
                        # Diagnostics: mark skipped/failed
                        try:
                            self.diagnostic_report.mark_skipped(entry.file_path, f"translate_failed:{err}", {'text': entry.original_text, 'line_number': entry.line_number})
                        except Exception:
                            pass
                
                # ============================================================
                # FAZ 2.5: Atomik segment girişleri (v2.7.1)
                # ============================================================
                # Delimiter gruplarının (<A|B|C> veya A|B|C) her segmentini
                # bağımsız bir çeviri girişi olarak kaydet. Ren'Py runtime'da
                # vary() veya liste indeksleme ile segmentleri ayrı ayrı
                # çağırdığından, birleşik blok yerine atomik girişler gerekir.
                if _atomic_segments:
                    _seg_added = 0
                    for orig_seg, tr_seg in _atomic_segments:
                        safe_seg, blocked_reason = self._sanitize_translation_for_output(
                            original=orig_seg,
                            translated=tr_seg,
                            file_path='strings.json',
                            translation_id=orig_seg,
                        )
                        if blocked_reason is not None or safe_seg == orig_seg:
                            continue
                        if orig_seg not in translations:
                            translations[orig_seg] = safe_seg
                            self._last_atomic_segments[orig_seg] = safe_seg
                            _seg_added += 1
                    if _seg_added:
                        self.emit_log("debug", f"[AtomicSegments] {_seg_added} individual segment translations registered from delimiter groups")
                
                # Cache kaydet (Performans için her 500 metinde bir checkpoint al)
                if current % 500 == 0:
                    self.translation_manager.save_cache(cache_file)
                    self.emit_log("debug", f"Checkpoint saved: {cache_file} (Progress: {current}/{total})")

                if stop_quota:
                    engine_name = getattr(self.engine, 'value', str(self.engine))
                    self.log_message.emit("error", self.config.get_log_text('error_api_quota', engine=engine_name))
                    self.should_stop = True
                    break
                self.emit_log("info", self.config.get_log_text('translated_count', current=current, total=total))

            if unchanged_count:
                self.log_message.emit("warning", self.config.get_log_text('unchanged_count_msg', unchanged=unchanged_count, total=len(translations)))
                for s in sample_logs:
                    self.log_message.emit("warning", s)
                self._log_error(f"UNCHANGED translations: {unchanged_count} / {len(translations)}\n" + "\n".join(sample_logs))
                
                # SMART TIP: Aggressive Retry Önerisi
                is_aggressive = getattr(self.config.translation_settings, 'aggressive_retry_translation', False)
                if not is_aggressive:
                    self.log_message.emit("info", self.config.get_log_text('log_hint_aggressive_retry'))

            if failed_entries:
                sample = "\n".join(failed_entries[:10])
                self.log_message.emit("warning", self.config.get_log_text('translation_failed_count', count=len(failed_entries), sample=sample))
                self._log_error(f"Translation failures ({len(failed_entries)}):\n{sample}")

            # Final Cache Kaydı
            self.translation_manager.save_cache(cache_file)
            self.log_message.emit("info", self.config.get_log_text('log_cache_saved', path=cache_file, count=len(translations)))

            # External TM detaylı istatistikleri (v2.7.8)
            if _external_tm is not None and _tm_hit_count > 0:
                _tm_stats = _external_tm.stats
                _source_names = _external_tm.loaded_source_names
                
                # Ana istatistik
                self.log_message.emit("info",
                    f"[ExternalTM] {_tm_hit_count} entries resolved from TM "
                    f"(hit rate: {_tm_stats['hit_rate']}%, {_tm_stats['misses']} misses)")
                
                # Kaynak detayları
                if _source_names:
                    _sources_str = ", ".join(_source_names)
                    self.log_message.emit("info",
                        f"[ExternalTM] Sources: {_sources_str}")
                
                # Toplam bellekteki entry
                self.log_message.emit("debug",
                    f"[ExternalTM] Total TM entries in memory: {_tm_stats['entries']} from {_tm_stats['sources']} source(s)")

        finally:
            # Proper cleanup to avoid Proactor errors on Windows
            try:
                if loop.is_running():
                    pass # Should not happen with run_until_complete
                
                # Close all sessions and network resources
                loop.run_until_complete(self.translation_manager.close_all())
                
                # Shutdown async generators and executor
                loop.run_until_complete(loop.shutdown_asyncgens())
                # Shutdown default executor only if supported (Python 3.9+)
                if hasattr(loop, 'shutdown_default_executor'):
                    loop.run_until_complete(loop.shutdown_default_executor())
                
                loop.close()
            except Exception as e:
                self.logger.debug(f"Loop cleanup notice: {e}")

        return translations

    def validate_placeholders(self, original, translated):
        """
        Çeviri sonrası değişkenlerin doğruluğunu kontrol eder.
        v2.7.2: Fuzzy matching - boşluklu versiyonları da kabul et (Google Translate corruption tolerance)
        """
        # Orijinaldeki [köşeli parantez] bloklarını bul
        orig_vars = re.findall(r'\[[^\]]+\]', original)

        for var in orig_vars:
            if var not in translated:
                # Fuzzy check: Boşluk eklenmiş veya çıkarılmış versiyonu ara
                # [player.name] → [player. name], [player .name], [player . name]
                var_content = var[1:-1]  # Bracket'leri çıkar
                # Normalized versiyon: boşluksuz
                var_normalized = re.sub(r'\s+', '', var_content)
                
                # Translated içindeki tüm bracket'leri kontrol et
                found = False
                for trans_var in re.findall(r'\[[^\]]+\]', translated):
                    trans_content = trans_var[1:-1]
                    trans_normalized = re.sub(r'\s+', '', trans_content)
                    if var_normalized == trans_normalized:
                        found = True
                        break
                
                if not found:
                    # HATA: Çeviri motoru değişkeni tamamen kaybetmiş veya değiştirmiş!
                    return False
        return True


class PipelineWorker(QThread):
    """Pipeline için QThread wrapper"""
    
    # Forward signals
    stage_changed = pyqtSignal(str, str)
    progress_updated = pyqtSignal(int, int, str)
    log_message = pyqtSignal(str, str)
    finished = pyqtSignal(object)
    show_warning = pyqtSignal(str, str)  # title, message - for popup warnings
    
    def __init__(self, pipeline: TranslationPipeline, parent=None):
        super().__init__(parent)
        self.pipeline = pipeline
        
        # Connect signals
        self.pipeline.stage_changed.connect(self.stage_changed)
        self.pipeline.progress_updated.connect(self.progress_updated)
        self.pipeline.log_message.connect(self.log_message)
        self.pipeline.finished.connect(self._on_finished)
        self.pipeline.show_warning.connect(self.show_warning)
    
    def _on_finished(self, result):
        self.finished.emit(result)
    
    def run(self):
        self.pipeline.run()
    
    def stop(self):
        self.pipeline.stop()
