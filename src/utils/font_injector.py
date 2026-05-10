# -*- coding: utf-8 -*-
"""
Font Injector Module (Google Fonts API Integration)
===================================================

Automatically downloads and injects compatible fonts for specific languages into Ren'Py projects.
Uses Google Fonts official download endpoint to ensure reliability and avoid 404 errors.
"""

import os
import requests
import logging
import zipfile
import io
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Any

from src.utils.language_registry import LanguageRegistry


class FontInjector:
    """
    Downloads and configures compatible fonts for Ren'Py games using Google Fonts.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.registry = LanguageRegistry.get_instance()

    GUI_FONT_FIELDS: Tuple[str, ...] = (
        "text_font",
        "name_text_font",
        "interface_text_font",
        "button_text_font",
        "choice_button_text_font",
        "system_font",
        "glyph_font",
    )

    STYLE_FONT_NAMES: Tuple[str, ...] = (
        "default",
        "say_dialogue",
        "say_label",
        "input",
        "button_text",
        "choice_button_text",
        "namebox",
        "notify_text",
        "history_text",
        "confirm_prompt_text",
        "navigation_button_text",
        "quick_button_text",
    )

    RTL_STYLE_NAMES: Tuple[str, ...] = (
        "default",
        "say_dialogue",
        "say_label",
        "input",
        "button_text",
        "choice_button_text",
        "history_text",
        "namebox",
        "notify_text",
    )

    # Popular Google Fonts List for Manual Selection
    POPULAR_FONTS = [
        "Roboto", "Open Sans", "Lato", "Montserrat", "Oswald", "Source Sans Pro", 
        "Slabo 27px", "Raleway", "PT Sans", "Merriweather", "Noto Sans", "Noto Serif",
        "Nunito", "Playfair Display", "Rubik", "Ubuntu", "Poppins", "Kanit", "Inter",
        "Quicksand", "Work Sans", "Fira Sans", "Barlow", "Mulish", "Inconsolata",
        "IBM Plex Sans", "Titillium Web", "DM Sans", "Oxygen", "Arimo", "Assistant",
        "Josefin Sans", "Libre Baskerville", "Anton", "Cairo", "Hind", "Bitter",
        "Vazirmatn", "Noto Sans Arabic", "Noto Sans JP", "Noto Sans SC", "Noto Sans KR",
        "Amiri", "Tajawal", "Almarai", "Harmattan", "Lalezar"
    ]

    # Known CJK font prefixes for subset selection
    _CJK_FONT_PREFIXES = {
        "noto-sans-sc", "noto-serif-sc",
        "noto-sans-tc", "noto-serif-tc",
        "noto-sans-jp", "noto-serif-jp",
        "noto-sans-kr", "noto-serif-kr",
    }

    def _get_font_subsets(self, font_id: str) -> str:
        """
        Returns appropriate font subsets based on font family.
        Ensures CJK fonts include their respective character sets.
        Uses prefix matching to avoid false positives from substring checks.
        """
        for prefix in self._CJK_FONT_PREFIXES:
            if font_id.startswith(prefix):
                if "-sc" in prefix:
                    return "latin,latin-ext,cyrillic,cyrillic-ext,chinese-simplified"
                if "-tc" in prefix:
                    return "latin,latin-ext,cyrillic,cyrillic-ext,chinese-traditional"
                if "-jp" in prefix:
                    return "latin,latin-ext,cyrillic,cyrillic-ext,japanese"
                if "-kr" in prefix:
                    return "latin,latin-ext,cyrillic,cyrillic-ext,korean"
        return "latin,latin-ext,cyrillic,cyrillic-ext,greek,greek-ext,vietnamese"

    def get_font_map_list(self) -> List[Dict[str, str]]:
        """Returns a list of default mapped fonts for UI."""
        result: List[Dict[str, str]] = []
        font_candidates = self.registry.get_all_font_candidates()
        for iso_code, candidates in font_candidates.items():
            if not candidates:
                continue
            primary_font, rtl = candidates[0]
            result.append({"lang": iso_code, "font": primary_font, "rtl": rtl})
        return result

    def inject_font(self, game_dir: str, lang_code: str, force_font_family: Optional[str] = None) -> Dict[str, Any]:
        """
        Main entry point.
        If force_font_family is provided, it skips the language mapping lookup.
        """
        # 1. Resolve Language Code and Font Family
        if force_font_family:
            # Manual Selection Mode
            font_family = force_font_family
            # Use registry for RTL detection
            is_rtl = self.registry.is_rtl(lang_code)
        else:
            # Auto Mode: normalize to Ren'Py code, then get ISO font code
            renpy_code = self.registry.normalize(lang_code)
            iso_font_code = self.registry.to_iso_font(renpy_code)
            candidates = self.registry.get_font_candidates(iso_font_code)

            if not candidates:
                return {
                    "success": False,
                    "message": f"No auto mapping for '{lang_code}' (renpy: {renpy_code}, iso: {iso_font_code})",
                    "ui_key": "font_err_no_mapping",
                    "ui_args": {"lang": lang_code}
                }
        
        try:
            # 2. Setup Directories
            game_path = Path(game_dir)
            if (game_path / "game").exists():
                game_path = game_path / "game"
                
            fonts_dir = game_path / "tl" / "renlocalizer_fonts"
            fonts_dir.mkdir(parents=True, exist_ok=True)
            
            # 3. Download & Extract
            if force_font_family:
                download_success, result_data = self._download_and_extract_google_font(font_family, fonts_dir)
                if not download_success:
                    return result_data
                font_filename = result_data
            else:
                download_success = False
                font_filename = ""
                last_error: Any = None
                font_family = candidates[0][0]
                is_rtl = candidates[0][1]

                for candidate_font, candidate_is_rtl in candidates:
                    download_success, result_data = self._download_and_extract_google_font(candidate_font, fonts_dir)
                    if download_success:
                        font_family = candidate_font
                        is_rtl = candidate_is_rtl
                        font_filename = result_data
                        break
                    last_error = result_data

                if not download_success:
                    if isinstance(last_error, dict):
                        return last_error
                    return {
                        "success": False,
                        "message": "Could not download any fallback font candidate.",
                        "ui_key": "font_err_download_fail",
                        "ui_args": {"error": "all fallback candidates failed"},
                    }

            # 4. Generate RPY Script
            # We use the ORIGINAL lang_code (e.g. 'turkish') for the Ren'Py translate block
            rpy_path = game_path / "zzz_renlocalizer_font.rpy"
            renpy_font_path = f"tl/renlocalizer_fonts/{font_filename}"
            
            self.logger.info(f"Creating font script at: {rpy_path}")
            self.logger.info(f"Font path: {renpy_font_path}, RTL: {is_rtl}, Lang: {lang_code}")
            
            already_exists = self._update_rpy_script(rpy_path, lang_code, renpy_font_path, is_rtl)

            if already_exists:
                return {
                    "success": True,
                    "message": f"Configuration for {lang_code} updated/exists.",
                    "ui_key": "font_warn_already_exists",
                    "ui_args": {"lang": lang_code},
                    "path": str(rpy_path)
                }

            return {
                "success": True, 
                "message": f"Font {font_filename} injected for {lang_code}.",
                "ui_key": "font_success_injected",
                "ui_args": {"font": font_family, "lang": lang_code},
                "path": str(rpy_path)
            }
            
        except Exception as e:
            self.logger.error(f"Injection Critical Error: {e}")
            return {
                "success": False,
                "message": str(e),
                "ui_key": "font_err_download_fail", 
                "ui_args": {"error": str(e)}
            }

    def get_available_fonts(self) -> List[str]:
        """Returns list of popular fonts sorted alphabetically."""
        return sorted(self.POPULAR_FONTS)

    def _download_and_extract_google_font(self, font_family: str, target_dir: Path) -> Tuple[bool, Any]:
        """
        Downloads ZIP using google-webfonts-helper API (more reliable for scripts).
        """
        # Font adını ID'ye çevir (Open Sans -> open-sans)
        font_id = font_family.lower().strip().replace(' ', '-')
        
        # Select appropriate subsets based on font family
        subsets = self._get_font_subsets(font_id)
        
        # API URL (Google Webfonts Helper)
        download_url = f"https://gwfh.mranftl.com/api/fonts/{font_id}?download=zip&subsets={subsets}&variants=regular,400,500,700"
        
        # Fallback URL (alternatif API)
        fallback_url = f"https://api.fontsource.org/v1/fonts/{font_id}/download"
        
        self.logger.info(f"Downloading Font ({font_id}) from: {download_url}")
        
        # Headers (Tarayıcı taklidi)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
        }

        try:
            # 1. Deneme: GWFH API
            response = requests.get(download_url, headers=headers, timeout=60)
            
            # 404 ise Fallback dene
            if response.status_code != 200:
                self.logger.warning(f"Primary API failed ({response.status_code}), trying fallback...")
                response = requests.get(fallback_url, headers=headers, timeout=60)
                
            response.raise_for_status()
            
            # İçerik kontrolü
            if len(response.content) < 1000:
                self.logger.warning(f"Downloaded file too small: {len(response.content)} bytes.")

            try:
                z = zipfile.ZipFile(io.BytesIO(response.content))
            except zipfile.BadZipFile:
                preview = response.content[:200].decode('utf-8', errors='ignore').replace('\n', ' ')
                self.logger.error(f"CRITICAL: Downloaded content is NOT a ZIP. Preview: {preview}")
                return False, {
                    "success": False,
                    "message": "Server returned invalid data (HTML error).",
                    "ui_key": "font_err_invalid_zip",
                    "ui_args": {}
                }
            
            # Find best font file (TTF > OTF)
            font_files = [f for f in z.namelist() if f.lower().endswith(('.ttf', '.otf'))]
            
            if not font_files:
                return False, {
                    "success": False,
                    "message": "No font file in ZIP",
                    "ui_key": "font_err_no_ttf",
                    "ui_args": {}
                }
            
            # Filter logic: Regular > any
            # GWFH usually returns clean names like 'roboto-v30-latin-regular.ttf'
            regulars = [f for f in font_files if "regular" in f.lower() or "-400" in f.lower()]
            best_file = regulars[0] if regulars else font_files[0]
            
            # Extract
            source = z.open(best_file)
            final_filename = os.path.basename(best_file)
            target_path = target_dir / final_filename
            
            # Eğer dosya zaten varsa boyutu farklıysa üzerine yaz
            write_file = True
            if target_path.exists():
                if target_path.stat().st_size > 0:
                    write_file = False # Zaten var, tekrar indirme (Bant genişliği tasarrufu)
            
            # Ancak kullanıcı manuel seçtiyse veya repair modundaysa zorla yazsın istiyoruz
            # Şimdilik basitlik adına overwrite edelim
            with open(target_path, "wb") as target:
                shutil.copyfileobj(source, target)
            
            self.logger.info(f"Extracted font to: {target_path}")
            return True, final_filename
            
        except requests.exceptions.HTTPError as e:
             return False, {
                "success": False,
                "message": f"Font download failed (HTTP {e.response.status_code if e.response else '?'}). Is the font name correct?",
                "ui_key": "font_err_download_fail",
                "ui_args": {"error": f"HTTP Error: {str(e)}"}
            }
        except Exception as e:
            return False, {
                "success": False,
                "message": str(e),
                "ui_key": "font_err_download_fail",
                "ui_args": {"error": str(e)}
            }

    def _update_rpy_script(self, rpy_path: Path, lang_code: str, font_path: str, is_rtl: bool) -> bool:
        """
        Updates script using Runtime Hooking.
        Intercepts ALL font loading calls, replacing them with our font safely.
        """
        
        # Bu yöntem Ren'Py'ın kendi font yükleme fonksiyonunu kancalar (hook).
        # Oyun geliştiricisi fontu kodun içine gömmüş olsa bile (hardcode),
        # oyun "Arial.ttf ver" dediğinde biz "Al sana NotoSans.ttf" deriz.
        # Bu sayede stil hataları (AttributeError) riskine girmeden font değişir.
        
        gui_font_fields_repr = repr(self.GUI_FONT_FIELDS)
        style_font_names_repr = repr(self.STYLE_FONT_NAMES)
        rtl_style_names_repr = repr(self.RTL_STYLE_NAMES)

        new_block = f"""
# --- CONFIG: {lang_code.upper()} ---
# Runtime Font Hooking

init -999 python:
    # Font ayarlarını saklayacağımız global sözlük
    if not hasattr(renpy.store, "renlocalizer_fonts"):
        renpy.store.renlocalizer_fonts = {{}}

    # Orijinal fonksiyonu sadece bir kez yedekle
    if not hasattr(renpy.store, "orig_get_font"):
        try:
            renpy.store.orig_get_font = renpy.text.font.get_font
            
            # KANCA (HOOK) FONKSİYONU
            def renlocalizer_get_font_hook(*args, **kwargs):
                # args[0] normalde istenen font dosyasıdır
                # Eğer şu anki dil bizim hedef dilimizse devreye gir

                if not args:
                    return renpy.store.orig_get_font(*args, **kwargs)

                current_lang = _preferences.language
                font_store = renpy.store.renlocalizer_fonts

                # Eğer o dil için bir font tanımlamışsak
                if current_lang in font_store and "Default" in font_store[current_lang]:
                    target_font = font_store[current_lang]["Default"]

                    # Sonsuz döngü koruması: Zaten bizim font isteniyorsa dokunma
                    if args[0] != target_font:
                        # Argümanları değiştir: (EskiFont, Boyut, ...) -> (YeniFont, Boyut, ...)
                        # Tuple olduğu için yeniden oluşturuyoruz
                        args = (target_font,) + args[1:]

                # Orijinal (veya modifiye edilmiş) çağrıyı yap
                return renpy.store.orig_get_font(*args, **kwargs)
                
            # Ren'Py'ın font yükleyicisini değiştir
            renpy.text.font.get_font = renlocalizer_get_font_hook
            
        except Exception as e:
            # Bir şeyler ters giderse konsola yaz ama oyunu çökertme
            print("RenLocalizer Font Hook Error: " + str(e))

translate {lang_code} python:
    # 1. Fontumuzu Hook sistemine kaydet
    if not hasattr(renpy.store, "renlocalizer_fonts"):
        renpy.store.renlocalizer_fonts = {{}}

    renpy.store.renlocalizer_fonts["{lang_code}"] = {{
        "Default": "{font_path}"
    }}

    # 2. Standart GUI font alanlarini guncelle
    for _gui_field in {gui_font_fields_repr}:
        try:
            if hasattr(gui, _gui_field):
                setattr(gui, _gui_field, "{font_path}")
        except Exception:
            pass

    # 3. Sık kullanılan stillere de doğrudan font uygula
    for _style_name in {style_font_names_repr}:
        try:
            _style = getattr(style, _style_name, None)
            if _style is not None:
                _style.font = "{font_path}"
        except Exception:
            pass

    # 4. RTL dillere gerekli yön ayarlarini yap
    if {is_rtl!r}:
        try:
            gui.language = "unicode"
        except Exception:
            pass

        try:
            config.rtl = True
        except Exception:
            pass

        for _rtl_style_name in {rtl_style_names_repr}:
            try:
                _rtl_style = getattr(style, _rtl_style_name, None)
                if _rtl_style is not None:
                    _rtl_style.language = "unicode"
                    _rtl_style.reading_order = "wrtl"
            except Exception:
                pass

    # 5. Font cache'lerini mümkün oldugunca temizle
    try:
        if hasattr(renpy.text, "font") and hasattr(renpy.text.font, "font_cache"):
            renpy.text.font.font_cache.clear()
    except Exception:
        pass

    try:
        if hasattr(renpy.text, "font") and hasattr(renpy.text.font, "font_names"):
            renpy.text.font.font_names.clear()
    except Exception:
        pass

    # 6. Stilleri yeniden oluştur ve interaction'ı yenile
    style.rebuild()
    try:
        renpy.restart_interaction()
    except Exception:
        pass
"""
        # Dosyayı SIFIRDAN oluştur (Eski hatalı kodları temizle)
        with open(rpy_path, 'w', encoding='utf-8') as f:
            f.write(new_block)
        
        # Verify the file was created successfully
        if not rpy_path.exists():
            self.logger.error(f"Font script file was not created: {rpy_path}")
            raise OSError(f"Failed to create font script: {rpy_path}")
        
        file_size = rpy_path.stat().st_size
        if file_size < 100:
            self.logger.error(f"Font script file is too small ({file_size} bytes): {rpy_path}")
            raise OSError(f"Font script file is incomplete: {rpy_path}")
        
        self.logger.info(f"Font script created successfully: {rpy_path} ({file_size} bytes)")
        return False
