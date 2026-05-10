# -*- coding: utf-8 -*-
"""
Settings Backend - Python-QML Bridge for Settings
==================================================

Provides settings management functionality for QML UI.
"""

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, pyqtProperty
from PyQt6.QtWidgets import QApplication

from src.utils.config import (
    ConfigManager,
    Language,
    MAX_AI_BATCH_SIZE,
    MAX_GENERAL_BATCH_SIZE,
    get_effective_batch_size,
    get_engine_batch_size_cap,
)


class SettingsBackend(QObject):
    """Settings page Python-QML bridge."""
    
    # Signals
    settingsSaved = pyqtSignal()
    languageChanged = pyqtSignal(str)
    themeChanged = pyqtSignal(str)
    systemThemeChanged = pyqtSignal()
    proxyRefreshFinished = pyqtSignal(bool, str) # success, message
    
    def __init__(self, config_manager: ConfigManager, proxy_manager=None, parent=None):
        super().__init__(parent)
        self.config = config_manager
        self._proxy_manager = proxy_manager  # Reference to app's real ProxyManager
    
    @pyqtSlot(str, str, result=str)
    def getTextWithDefault(self, key: str, default: str) -> str:
        """Get localized text with default fallback."""
        return self.config.get_ui_text(key, default)
    
    # ==================== GENERAL SETTINGS ====================
    
    @pyqtSlot(result=list)
    def getAvailableUILanguages(self) -> list:
        """Get available UI languages with native display names."""
        ui_lang_display = {
            "tr": "🇹🇷 Türkçe",
            "en": "🇬🇧 English",
            "de": "🇩🇪 Deutsch",
            "fr": "🇫🇷 Français",
            "es": "🇪🇸 Español",
            "ru": "🇷🇺 Русский",
            "fa": "🇮🇷 فارسی",
            "zh-CN": "🇨🇳 中文 (简体)",
            "ja": "🇯🇵 日本語",
        }
        return [
            {"code": lang.value, "name": ui_lang_display.get(lang.value, lang.value)}
            for lang in Language
        ]
    
    @pyqtSlot(result=str)
    def getCurrentUILanguage(self) -> str:
        """Get current UI language code."""
        return self.config.app_settings.ui_language or "en"
    
    @pyqtSlot(str)
    def setUILanguage(self, lang_code: str):
        """Set UI language."""
        try:
            lang = Language(lang_code)
            # self.config.app_settings.ui_language = lang_code # Handled inside load_locale
            self.config.load_locale(lang)
            self.config.save_config()
            self.languageChanged.emit(lang_code)
        except Exception as e:
            print(f"Error setting UI language: {e}")
    
    @pyqtSlot(result=list)
    def getAvailableThemes(self) -> list:
        """Get available themes - internal only, no system theme."""
        return [
            {"code": "dark", "name": self.config.get_ui_text("theme_dark", "🌙 Dark")},
            {"code": "light", "name": self.config.get_ui_text("theme_light", "☀️ Light")},
            {"code": "red", "name": self.config.get_ui_text("theme_red", "🔴 Red")},
            {"code": "turquoise", "name": self.config.get_ui_text("theme_turquoise", "🔵 Turquoise")},
            {"code": "green", "name": self.config.get_ui_text("theme_green", "🌿 Green")},
            {"code": "neon", "name": self.config.get_ui_text("theme_neon", "🌈 Neon")},
        ]
    
    @pyqtProperty(str, notify=themeChanged)
    def currentTheme(self):
        return self.config.app_settings.app_theme or "dark"

    @currentTheme.setter
    def currentTheme(self, value):
        self.setTheme(value)

    @pyqtProperty(str, notify=languageChanged)
    def currentLanguage(self):
        return self.config.app_settings.ui_language or "tr"

    @currentLanguage.setter
    def currentLanguage(self, value):
        self.setUILanguage(value)

    @pyqtProperty(bool, notify=systemThemeChanged)
    def isSystemDark(self):
        """Sistem temasının karanlık olup olmadığını kontrol et."""
        try:
            from darkdetect import isDark
            return isDark()
        except ImportError:
            return True # Fallback to dark

    @pyqtSlot(result=str)
    def getCurrentTheme(self) -> str:
        """Get current theme."""
        return self.currentTheme
    
    @pyqtSlot(str)
    def setTheme(self, theme: str):
        """Set application theme."""
        self.config.app_settings.app_theme = theme
        self.config.save_config()
        
        # Theme remains applied via QML Material configuration
        pass
        
        self.themeChanged.emit(theme)
    
    @pyqtSlot(result=bool)
    def getCheckUpdates(self) -> bool:
        """Get check updates setting."""
        return self.config.app_settings.check_for_updates
    
    @pyqtSlot(bool)
    def setCheckUpdates(self, enabled: bool):
        """Set check updates setting."""
        self.config.app_settings.check_for_updates = enabled
        self.config.save_config()
    
    # ==================== API KEYS ====================
    # (DeepL/OpenAI/Gemini key methods defined later in DEEPL SETTINGS section)

    @pyqtSlot(result=str)
    def getDeepSeekApiKey(self) -> str:
        return self.config.api_keys.deepseek_api_key or ""

    @pyqtSlot(str)
    def setDeepSeekApiKey(self, key: str):
        self.config.api_keys.deepseek_api_key = key.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getDeepSeekModel(self) -> str:
        return getattr(self.config.translation_settings, 'deepseek_model', 'deepseek-chat')

    @pyqtSlot(str)
    def setDeepSeekModel(self, value: str):
        self.config.translation_settings.deepseek_model = value
        self.config.save_config()

    @pyqtSlot(result=str)
    def getDeepSeekBaseUrl(self) -> str:
        return getattr(self.config.translation_settings, 'deepseek_base_url', 'https://api.deepseek.com')

    @pyqtSlot(str)
    def setDeepSeekBaseUrl(self, value: str):
        self.config.translation_settings.deepseek_base_url = value.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getOpusMTModel(self) -> str:
        return getattr(self.config.translation_settings, 'opus_mt_model', 'Helsinki-NLP/opus-mt-en-es')

    @pyqtSlot(str)
    def setOpusMTModel(self, value: str):
        self.config.translation_settings.opus_mt_model = value.strip()
        self.config.save_config()
    
    # ==================== TRANSLATION SETTINGS ====================
    
    @pyqtSlot(result=int)
    def getBatchSize(self) -> int:
        return self.config.translation_settings.max_batch_size

    @pyqtSlot(result=int)
    def getBatchSizeMax(self) -> int:
        return MAX_GENERAL_BATCH_SIZE

    @pyqtSlot(result=int)
    def getSelectedEngineBatchCap(self) -> int:
        cap = get_engine_batch_size_cap(getattr(self.config.translation_settings, 'selected_engine', 'google'))
        return int(cap or 0)

    @pyqtSlot(result=int)
    def getEffectiveBatchSizeForSelectedEngine(self) -> int:
        requested = getattr(self.config.translation_settings, 'max_batch_size', 100)
        engine = getattr(self.config.translation_settings, 'selected_engine', 'google')
        return get_effective_batch_size(requested, engine)
    
    @pyqtSlot(int)
    def setBatchSize(self, value: int):
        self.config.translation_settings.max_batch_size = max(1, min(int(value), MAX_GENERAL_BATCH_SIZE))
        self.config.save_config()
    
    @pyqtSlot(result=float)
    def getRequestDelay(self) -> float:
        return self.config.translation_settings.request_delay
    
    @pyqtSlot(float)
    def setRequestDelay(self, value: float):
        self.config.translation_settings.request_delay = value
        self.config.save_config()
    
    @pyqtSlot(result=int)
    def getConcurrentThreads(self) -> int:
        return self.config.translation_settings.max_concurrent_threads
    
    @pyqtSlot(int)
    def setConcurrentThreads(self, value: int):
        self.config.translation_settings.max_concurrent_threads = value
        self.config.save_config()
    
    @pyqtSlot(result=int)
    def getContextLimit(self) -> int:
        return self.config.translation_settings.context_limit
    
    @pyqtSlot(int)
    def setContextLimit(self, value: int):
        self.config.translation_settings.context_limit = value
        self.config.save_config()
    
    @pyqtSlot(result=int)
    def getMaxRetries(self) -> int:
        return self.config.translation_settings.max_retries
    
    @pyqtSlot(int)
    def setMaxRetries(self, value: int):
        self.config.translation_settings.max_retries = value
        self.config.save_config()

    @pyqtSlot(result=int)
    def getTimeout(self) -> int:
        return self.config.translation_settings.timeout
    
    @pyqtSlot(int)
    def setTimeout(self, value: int):
        self.config.translation_settings.timeout = value
        self.config.save_config()

    @pyqtSlot(result=int)
    def getMaxCharsPerRequest(self) -> int:
        return self.config.translation_settings.max_chars_per_request
    
    @pyqtSlot(int)
    def setMaxCharsPerRequest(self, value: int):
        self.config.translation_settings.max_chars_per_request = value
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getAggressiveRetry(self) -> bool:
        return self.config.translation_settings.aggressive_retry_translation

    @pyqtSlot(bool)
    def setAggressiveRetry(self, enabled: bool):
        self.config.translation_settings.aggressive_retry_translation = enabled
        self.config.save_config()

    @pyqtSlot(result=str)
    def getExtractionMode(self) -> str:
        return getattr(self.config.translation_settings, 'extraction_mode', 'balanced') or 'balanced'

    @pyqtSlot(str)
    def setExtractionMode(self, value: str):
        mode = (value or 'strict').strip().lower()
        if mode not in ('strict', 'balanced', 'aggressive'):
            mode = 'strict'
        self.config.translation_settings.extraction_mode = mode
        self.config.translation_settings.minimum_extraction_confidence = {
            'strict': 0.85,
            'balanced': 0.58,
            'aggressive': 0.35,
        }[mode]
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getForceRuntime(self) -> bool:
        return self.config.translation_settings.force_runtime_translation

    @pyqtSlot(bool)
    def setForceRuntime(self, enabled: bool):
        self.config.translation_settings.force_runtime_translation = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getUseMultiEndpoint(self) -> bool:
        return self.config.translation_settings.use_multi_endpoint

    @pyqtSlot(bool)
    def setUseMultiEndpoint(self, enabled: bool):
        self.config.translation_settings.use_multi_endpoint = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getEnableLingvaFallback(self) -> bool:
        return self.config.translation_settings.enable_lingva_fallback

    @pyqtSlot(bool)
    def setEnableLingvaFallback(self, enabled: bool):
        self.config.translation_settings.enable_lingva_fallback = enabled
        self.config.save_config()
    
    # ==================== AI SETTINGS ====================
    # (OpenAI/Gemini model methods defined later in AI API KEYS & MODELS section)

    @pyqtSlot(result=str)
    def getGeminiSafety(self) -> str:
        return self.config.translation_settings.gemini_safety_settings or "BLOCK_MEDIUM_AND_ABOVE"
    
    @pyqtSlot(str)
    def setGeminiSafety(self, level: str):
        self.config.translation_settings.gemini_safety_settings = level
        self.config.save_config()
    
    @pyqtSlot(result=str)
    def getLocalLLMModel(self) -> str:
        return self.config.translation_settings.local_llm_model or "llama3.2"
    
    @pyqtSlot(str)
    def setLocalLLMModel(self, text: str):
        self.config.translation_settings.local_llm_model = text
        self.config.save_config()

    # ==================== OPENAI PRESETS ====================
    @pyqtSlot(result=list)
    def getOpenAIPresets(self) -> list:
        """Get available OpenAI-compatible API presets."""
        return [
            {"name": "OpenAI (Default)", "url": "", "model": "gpt-4o-mini"},
            {"name": "OpenRouter", "url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4o-mini"},
            {"name": "DeepSeek", "url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
            {"name": "DeepSeek Reasoner", "url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner"},
            {"name": "Grok (xAI)", "url": "https://api.x.ai/v1", "model": "grok-2-latest"},
            {"name": "Qwen 2.5 (OpenRouter)", "url": "https://openrouter.ai/api/v1", "model": "qwen/qwen-2.5-72b-instruct"},
            {"name": "Together AI", "url": "https://api.together.xyz/v1", "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
            {"name": "Groq", "url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
            {"name": "Mistral", "url": "https://api.mistral.ai/v1", "model": "mistral-large-latest"},
            {"name": "Custom", "url": "", "model": ""}
        ]
    
    @pyqtSlot(str, result=str)
    def applyOpenAIPreset(self, name: str) -> str:
        """Apply selected OpenAI preset. Returns the new values as JSON for QML to consume."""
        import json
        presets = self.getOpenAIPresets()
        for p in presets:
            if p["name"] == name and name != "Custom":
                self.setOpenAIBaseUrl(p["url"])
                self.setOpenAIModel(p["model"])
                return json.dumps({"url": p["url"], "model": p["model"]})
        return json.dumps({"url": "", "model": ""})

    @pyqtSlot(result=list)
    def getLocalLLMPresets(self) -> list:
        """Get available Local LLM presets."""
        return [
            {"name": "Ollama (Default)", "url": "http://localhost:11434/v1", "model": "llama3.2"},
            {"name": "Jan.ai", "url": "http://localhost:1337/v1", "model": "local-model"},
            {"name": "LM Studio", "url": "http://localhost:1234/v1", "model": "local-model"},
            {"name": "GPT4All", "url": "http://localhost:4891/v1", "model": "local-model"},
            {"name": "KoboldCPP", "url": "http://localhost:5001/v1", "model": "local-model"},
            {"name": "Custom", "url": "", "model": ""}
        ]

    @pyqtSlot(str, result=str)
    def applyLocalLLMPreset(self, name: str) -> str:
        """Apply selected Local LLM preset. Returns the new values as JSON for QML to consume."""
        import json
        presets = self.getLocalLLMPresets()
        for p in presets:
            if p["name"] == name and name != "Custom":
                self.setLocalLLMUrl(p["url"])
                self.setLocalLLMModel(p["model"])
                return json.dumps({"url": p["url"], "model": p["model"]})
        return json.dumps({"url": "", "model": ""})
    
    @pyqtSlot(result=str)
    def getLocalLLMUrl(self) -> str:
        return self.config.translation_settings.local_llm_url or "http://localhost:11434/v1"
    
    @pyqtSlot(str)
    def setLocalLLMUrl(self, url: str):
        self.config.translation_settings.local_llm_url = url
        self.config.save_config()

    @pyqtSlot(result=int)
    def getLocalLLMTimeout(self) -> int:
        return self.config.translation_settings.local_llm_timeout or 300
    
    @pyqtSlot(int)
    def setLocalLLMTimeout(self, value: int):
        self.config.translation_settings.local_llm_timeout = value
        self.config.save_config()

    @pyqtSlot(result=str)
    def testLocalLLMConnection(self) -> str:
        """Test connection to local LLM server synchronously for QML."""
        import asyncio
        try:
            from src.core.ai_translator import LocalLLMTranslator
            ts = self.config.translation_settings
            translator = LocalLLMTranslator(
                model=ts.local_llm_model or "llama3.2",
                base_url=ts.local_llm_url or "http://localhost:11434/v1",
                config_manager=self.config
            )
            
            # Create a temporary event loop to run the async health check
            loop = asyncio.new_event_loop()
            try:
                success, message = loop.run_until_complete(translator.health_check())
                loop.run_until_complete(translator.close())
                return message
            finally:
                loop.close()
        except Exception as e:
            return f"{self.config.get_ui_text('error', 'Hata')}: {str(e)}"

    # ========== LIBRETRANSLATE SETTINGS ==========

    @pyqtSlot(result=list)
    def getLibreTranslatePresets(self) -> list:
        """Get available LibreTranslate-compatible server presets."""
        return [
            {"name": "LibreTranslate (Local)", "url": "http://localhost:5000"},
            {"name": "Apertium (Local)", "url": "http://localhost:2737"},
            {"name": "libretranslate.com", "url": "https://libretranslate.com"},
            {"name": "Custom", "url": ""}
        ]

    @pyqtSlot(str, result=str)
    def applyLibreTranslatePreset(self, name: str) -> str:
        """Apply selected LibreTranslate preset. Returns the new URL as JSON."""
        import json
        presets = self.getLibreTranslatePresets()
        for p in presets:
            if p["name"] == name and name != "Custom":
                self.setLibreTranslateUrl(p["url"])
                return json.dumps({"url": p["url"]})
        return json.dumps({"url": ""})

    @pyqtSlot(result=str)
    def getLibreTranslateUrl(self) -> str:
        return self.config.translation_settings.libretranslate_url or "http://localhost:5000"

    @pyqtSlot(str)
    def setLibreTranslateUrl(self, url: str):
        self.config.translation_settings.libretranslate_url = url.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getLibreTranslateApiKey(self) -> str:
        return self.config.translation_settings.libretranslate_api_key or ""

    @pyqtSlot(str)
    def setLibreTranslateApiKey(self, key: str):
        self.config.translation_settings.libretranslate_api_key = key.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def testLibreTranslateConnection(self) -> str:
        """Test connection to LibreTranslate server."""
        import asyncio
        import aiohttp
        try:
            url = (self.config.translation_settings.libretranslate_url or "http://localhost:5000").rstrip('/')

            async def _test():
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{url}/languages", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            lang_count = len(data) if isinstance(data, list) else 0
                            return True, f"✓ Connected! {lang_count} languages available."
                        return False, f"HTTP {resp.status}"

            loop = asyncio.new_event_loop()
            try:
                success, message = loop.run_until_complete(_test())
                return message
            finally:
                loop.close()
        except Exception as e:
            return f"{self.config.get_ui_text('error', 'Error')}: {str(e)}"

    @pyqtSlot(result=float)
    def getAITemperature(self) -> float:
        return self.config.translation_settings.ai_temperature
    
    @pyqtSlot(float)
    def setAITemperature(self, value: float):
        self.config.translation_settings.ai_temperature = value
        self.config.save_config()
    
    @pyqtSlot(result=int)
    def getAITimeout(self) -> int:
        return self.config.translation_settings.ai_timeout
    
    @pyqtSlot(int)
    def setAITimeout(self, value: int):
        self.config.translation_settings.ai_timeout = value
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getUseHtmlProtection(self) -> bool:
        return self.config.translation_settings.use_html_protection

    @pyqtSlot(bool)
    def setUseHtmlProtection(self, value: bool):
        selected_engine = getattr(self.config.translation_settings, 'selected_engine', 'google')
        if selected_engine == 'google' and value:
            self.config.translation_settings.use_html_protection = False
        else:
            self.config.translation_settings.use_html_protection = value
        self.config.save_config()

    @pyqtSlot(result=int)
    def getAIMaxTokens(self) -> int:
        return self.config.translation_settings.ai_max_tokens

    @pyqtSlot(int)
    def setAIMaxTokens(self, value: int):
        self.config.translation_settings.ai_max_tokens = value
        self.config.save_config()

    @pyqtSlot(result=int)
    def getAIBatchSize(self) -> int:
        return self.config.translation_settings.ai_batch_size

    @pyqtSlot(result=int)
    def getAIBatchSizeMax(self) -> int:
        return MAX_AI_BATCH_SIZE

    @pyqtSlot(int)
    def setAIBatchSize(self, value: int):
        self.config.translation_settings.ai_batch_size = max(1, min(int(value), MAX_AI_BATCH_SIZE))
        self.config.save_config()

    @pyqtSlot(result=int)
    def getAIRetryCount(self) -> int:
        return self.config.translation_settings.ai_retry_count

    @pyqtSlot(int)
    def setAIRetryCount(self, value: int):
        self.config.translation_settings.ai_retry_count = value
        self.config.save_config()

    @pyqtSlot(result=int)
    def getAIConcurrency(self) -> int:
        return self.config.translation_settings.ai_concurrency

    @pyqtSlot(int)
    def setAIConcurrency(self, value: int):
        self.config.translation_settings.ai_concurrency = value
        self.config.save_config()

    @pyqtSlot(result=float)
    def getAIRequestDelay(self) -> float:
        return self.config.translation_settings.ai_request_delay

    @pyqtSlot(float)
    def setAIRequestDelay(self, value: float):
        self.config.translation_settings.ai_request_delay = value
        self.config.save_config()

    @pyqtSlot(result=str)
    def getAISystemPrompt(self) -> str:
        return self.config.translation_settings.ai_custom_system_prompt or ""

    @pyqtSlot(str)
    def setAISystemPrompt(self, text: str):
        self.config.translation_settings.ai_custom_system_prompt = text
        self.config.save_config()
    
    # ==================== PROXY SETTINGS ====================
    
    @pyqtSlot(result=bool)
    def getProxyEnabled(self) -> bool:
        return getattr(self.config.proxy_settings, 'enabled', False)
    
    @pyqtSlot(bool)
    def setProxyEnabled(self, enabled: bool):
        self.config.proxy_settings.enabled = enabled
        self.config.save_config()
    
    @pyqtSlot(result=str)
    def getProxyUrl(self) -> str:
        return getattr(self.config.proxy_settings, 'proxy_url', '') or ""
    
    @pyqtSlot(str)
    def setProxyUrl(self, url: str):
        self.config.proxy_settings.proxy_url = url
        self.config.save_config()

    @pyqtSlot(result=str)
    def getManualProxies(self) -> str:
        proxies = getattr(self.config.proxy_settings, 'manual_proxies', [])
        return "\n".join(proxies)

    @pyqtSlot(str)
    def setManualProxies(self, text: str):
        proxies = [p.strip() for p in text.split("\n") if p.strip()]
        self.config.proxy_settings.manual_proxies = proxies
        self.config.save_config()

    @pyqtSlot()
    def refreshProxies(self):
        """Refresh proxy list (test connections) in background."""
        import threading
        threading.Thread(target=self._refresh_proxies_thread, daemon=True).start()

    def _refresh_proxies_thread(self):
        import asyncio
        try:
            from src.core.proxy_manager import ProxyManager

            # Use the app's real ProxyManager if available, otherwise create temporary
            pm = self._proxy_manager if self._proxy_manager is not None else ProxyManager()
            # Always reconfigure from latest settings
            pm.configure_from_settings(self.config.proxy_settings)

            async def run_refresh():
                await pm.initialize()  # Tests connections
                return pm.get_proxy_stats()

            loop = asyncio.new_event_loop()
            try:
                stats = loop.run_until_complete(run_refresh())
                working = stats['working_proxies']
                personal = stats.get('personal_proxies', 0)
                
                msg = self.config.get_ui_text("proxy_test_finished", "Connection test finished: {working} proxies are working.").format(
                    working=working
                )
                self.proxyRefreshFinished.emit(True, msg)
            finally:
                loop.close()

        except Exception as e:
            self.proxyRefreshFinished.emit(False, str(e))

    # ==================== DEEPL SETTINGS ====================

    @pyqtSlot(result=str)
    def getDeepLFormality(self) -> str:
        return getattr(self.config.translation_settings, 'deepl_formality', 'default')

    @pyqtSlot(str)
    def setDeepLFormality(self, formality: str):
        self.config.translation_settings.deepl_formality = formality
        self.config.save_config()

    @pyqtSlot(result=str)
    def getDeepLApiKey(self) -> str:
        return self.config.api_keys.deepl_api_key or ""

    @pyqtSlot(str)
    def setDeepLApiKey(self, key: str):
        self.config.api_keys.deepl_api_key = key.strip()
        self.config.save_config()

    # ==================== AI API KEYS & MODELS ====================

    @pyqtSlot(result=str)
    def getOpenAIApiKey(self) -> str:
        return self.config.api_keys.openai_api_key or ""

    @pyqtSlot(str)
    def setOpenAIApiKey(self, key: str):
        self.config.api_keys.openai_api_key = key.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getOpenAIModel(self) -> str:
        return getattr(self.config.translation_settings, 'openai_model', 'gpt-3.5-turbo')

    @pyqtSlot(str)
    def setOpenAIModel(self, model: str):
        self.config.translation_settings.openai_model = model.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getOpenAIBaseUrl(self) -> str:
        return getattr(self.config.translation_settings, 'openai_base_url', '')

    @pyqtSlot(str)
    def setOpenAIBaseUrl(self, url: str):
        self.config.translation_settings.openai_base_url = url.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getGeminiApiKey(self) -> str:
        return self.config.api_keys.gemini_api_key or ""

    @pyqtSlot(str)
    def setGeminiApiKey(self, key: str):
        self.config.api_keys.gemini_api_key = key.strip()
        self.config.save_config()

    @pyqtSlot(result=str)
    def getGeminiModel(self) -> str:
        return getattr(self.config.translation_settings, 'gemini_model', 'gemini-2.0-flash-exp')

    @pyqtSlot(str)
    def setGeminiModel(self, model: str):
        self.config.translation_settings.gemini_model = model.strip()
        self.config.save_config()

    # ==================== ADVANCED SETTINGS ====================
    
    @pyqtSlot(result=bool)
    def getShowDebugEngines(self) -> bool:
        return getattr(self.config.translation_settings, 'show_debug_engines', False)
    
    @pyqtSlot(bool)
    def setShowDebugEngines(self, enabled: bool):
        self.config.translation_settings.show_debug_engines = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getRuntimeStringDiagnostics(self) -> bool:
        return getattr(self.config.translation_settings, 'runtime_string_diagnostics', False)

    @pyqtSlot(bool)
    def setRuntimeStringDiagnostics(self, enabled: bool):
        self.config.translation_settings.runtime_string_diagnostics = enabled
        self.config.save_config()
    
    @pyqtSlot(result=bool)
    def getExcludeSystemFolders(self) -> bool:
        return getattr(self.config.translation_settings, 'exclude_system_folders', True)
    
    @pyqtSlot(bool)
    def setExcludeSystemFolders(self, enabled: bool):
        self.config.translation_settings.exclude_system_folders = enabled
        self.config.save_config()
    
    @pyqtSlot(result=bool)
    def getScanRpymFiles(self) -> bool:
        return getattr(self.config.translation_settings, 'scan_rpym_files', False)
    
    @pyqtSlot(bool)
    def setScanRpymFiles(self, enabled: bool):
        self.config.translation_settings.scan_rpym_files = enabled
        self.config.save_config()
    
    @pyqtSlot(result=bool)
    def getUseGlobalCache(self) -> bool:
        return getattr(self.config.translation_settings, 'use_global_cache', True)
    
    @pyqtSlot(bool)
    def setUseGlobalCache(self, enabled: bool):
        self.config.translation_settings.use_global_cache = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getEnableDeepScan(self) -> bool:
        return self.config.translation_settings.enable_deep_scan

    @pyqtSlot(bool)
    def setEnableDeepScan(self, enabled: bool):
        self.config.translation_settings.enable_deep_scan = enabled
        self.config.save_config()

    # DEPRECATED: Fuzzy match no longer used in v2.5.1+ (XRPYX format)
    # Kept for backward compatibility with old config files
    @pyqtSlot(result=bool)
    def getEnableFuzzyMatch(self) -> bool:
        """Deprecated: Fuzzy match is no longer used."""
        return False  # Always return False

    @pyqtSlot(bool)
    def setEnableFuzzyMatch(self, enabled: bool):
        """Deprecated: Fuzzy match is no longer used."""
        pass  # No-op

    @pyqtSlot(result=bool)
    def getEnableRpycReader(self) -> bool:
        return self.config.translation_settings.enable_rpyc_reader

    @pyqtSlot(bool)
    def setEnableRpycReader(self, enabled: bool):
        self.config.translation_settings.enable_rpyc_reader = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getEnableUnrpycDecompile(self) -> bool:
        return getattr(self.config.translation_settings, 'enable_unrpyc_decompile', True)

    @pyqtSlot(bool)
    def setEnableUnrpycDecompile(self, enabled: bool):
        self.config.translation_settings.enable_unrpyc_decompile = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getAutoUnren(self) -> bool:
        return self.config.app_settings.unren_auto_download

    @pyqtSlot(bool)
    def setAutoUnren(self, enabled: bool):
        self.config.app_settings.unren_auto_download = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getAutoHook(self) -> bool:
        return self.config.translation_settings.auto_generate_hook

    @pyqtSlot(bool)
    def setAutoHook(self, enabled: bool):
        self.config.translation_settings.auto_generate_hook = enabled
        self.config.save_config()

    @pyqtSlot(result=bool)
    def getUseCache(self) -> bool:
        return getattr(self.config.translation_settings, 'use_cache', True)

    @pyqtSlot(bool)
    def setUseCache(self, enabled: bool):
        self.config.translation_settings.use_cache = enabled
        self.config.save_config()

    # ==================== v2.7.1 NEW SETTINGS ====================

    @pyqtSlot(result=bool)
    def getAutoProtectCharNames(self) -> bool:
        return getattr(self.config.translation_settings, 'auto_protect_character_names', True)

    @pyqtSlot(bool)
    def setAutoProtectCharNames(self, enabled: bool):
        self.config.translation_settings.auto_protect_character_names = enabled
        self.config.save_config()

    @pyqtSlot(result=str)
    def getCustomFunctionParams(self) -> str:
        return getattr(self.config.translation_settings, 'custom_function_params', '{}')

    @pyqtSlot(str)
    def setCustomFunctionParams(self, text: str):
        import json
        try:
            json.loads(text)  # Validate JSON
            self.config.translation_settings.custom_function_params = text
            self.config.save_config()
        except (json.JSONDecodeError, TypeError):
            pass  # Invalid JSON — ignore silently

    # ==================== FILTER SETTINGS ====================

    @pyqtSlot(str, result=bool)
    def getFilter(self, key: str) -> bool:
        # Fuzzy match is deprecated, always return False
        if key == "fuzzy_match":
            return False
        return getattr(self.config.translation_settings, f"translate_{key}", True)

    @pyqtSlot(str, bool)
    def setFilter(self, key: str, value: bool):
        # Fuzzy match is deprecated, ignore
        if key == "fuzzy_match":
            return
        setattr(self.config.translation_settings, f"translate_{key}", value)
        self.config.save_config()
    
    @pyqtSlot()
    def restoreDefaults(self):
        """Restore all settings to defaults."""
        self.config.reset_to_defaults()
        self.config.save_config()
        self.settingsSaved.emit()

    # ==================== PATH & PORTABLE SETTINGS ====================

    @pyqtSlot(result=bool)
    def isPortableModeSupported(self) -> bool:
        """Check if Portable Mode is allowed (Not AppImage, directory is writable)."""
        import os
        from src.utils.path_manager import is_appimage, get_app_dir
        
        if is_appimage():
            return False
            
        app_dir = get_app_dir()
        return os.access(app_dir, os.W_OK)

    @pyqtSlot(result=bool)
    def getPortableMode(self) -> bool:
        """Check if we are currently running in Portable Mode."""
        import os
        from src.utils.path_manager import get_data_path, get_app_dir
        
        # If the data path matches the app installation dir, it's portable.
        return get_data_path() == get_app_dir()

    @pyqtSlot(bool, result=bool)
    def setPortableMode(self, enable: bool) -> bool:
        """
        Switch between Portable and System modes. Returns True if successful.
        If enabling, creates a .portable marker and copies data to app path.
        If disabling, removes .portable marker and copies data to system path.
        """
        import os
        import shutil
        from src.utils.path_manager import get_data_path, get_app_dir, get_system_data_dir

        app_dir = get_app_dir()
        sys_dir = get_system_data_dir()
        marker = app_dir / ".portable"
        
        # Don't do anything if not supported
        if not self.isPortableModeSupported():
            return False

        try:
            if enable:
                # Enabling portable mode
                if not os.access(app_dir, os.W_OK):
                    return False
                    
                # Create marker file
                marker.touch(exist_ok=True)
                
                # Update config path ON THE FLY so subsequent saves go to the right place
                self.config.data_dir = app_dir
                self.config.config_file = app_dir / "config.json"
                
                # Move existing data from System to App directory (except large logs)
                try:
                    if sys_dir.exists() and sys_dir != app_dir:
                        for item in ["cache", "tm", "glossary.json"]:
                            src = sys_dir / item
                            dst = app_dir / item
                            if src.exists() and not dst.exists():
                                try:
                                    if src.is_dir():
                                        shutil.copytree(src, dst)
                                        shutil.rmtree(src)
                                    else:
                                        shutil.copy2(src, dst)
                                        src.unlink()
                                except Exception as e:
                                    logging.getLogger(__name__).warning(f"Failed to move {item} from system to portable: {e}")
                except Exception:
                    pass
                
                # Explicitly save current config to new location
                self.config.save_config()
                
            else:
                # Disabling portable mode (Moving to System Mode)
                if marker.exists():
                    marker.unlink()
                
                # Ensure system directory exists
                from src.utils.path_manager import ensure_data_directories
                ensure_data_directories(sys_dir)
                
                # Update config paths dynamically
                self.config.data_dir = sys_dir
                self.config.config_file = sys_dir / "config.json"
                
                # Move existing data from App to System directory
                try:
                    if app_dir != sys_dir:
                        for item in ["cache", "tm", "glossary.json"]:
                            src = app_dir / item
                            dst = sys_dir / item
                            if src.exists() and not dst.exists():
                                try:
                                    if src.is_dir():
                                        shutil.copytree(src, dst)
                                        shutil.rmtree(src)
                                    else:
                                        shutil.copy2(src, dst)
                                        src.unlink()
                                except Exception as e:
                                    logging.getLogger(__name__).warning(f"Failed to move {item} from portable to system: {e}")
                except Exception:
                    pass
                
                # Save config to the new system location first!
                self.config.save_config()
                
                # Now safely delete config.json from app directory to prevent triggering legacy fallback on next start
                if (app_dir / "config.json").exists():
                    try:
                        (app_dir / "config.json").unlink()
                    except Exception:
                        pass
            
            return True
            
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error toggling portable mode: {e}")
            return False

    @pyqtSlot()
    def openDataFolder(self):
        """Opens the active data folder in the system file explorer."""
        import os
        import sys
        import subprocess
        from src.utils.path_manager import get_data_path
        
        data_path = str(get_data_path())
        
        try:
            if sys.platform == "win32":
                os.startfile(data_path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", data_path])
            else:
                subprocess.Popen(["xdg-open", data_path])
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error opening data folder: {e}")
