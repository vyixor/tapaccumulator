# =====================================================================
#  TAPACCUMULATOR v1.2.0
#  Description: An Asynchronous Rolling-Timeout Hardware Macro System
#  Architecture: Windows SMTC Interceptor + Auto Focus Recovery
# =====================================================================

import os
import sys
import json
import time
import wave
import tempfile
import asyncio
import threading
import subprocess
import logging
import customtkinter as ctk
from PIL import Image
import pystray
from pystray import MenuItem as item
from winotify import Notification, audio
from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse
from engine import TapEngine
# Core Windows Runtime Subsystems
import winsdk.windows.media.playback as wmp
import winsdk.windows.media as wm
import winsdk.windows.media.core as wmc
import winsdk.windows.media.control as wmctl
import winsdk.windows.foundation as wf

# --- LOGGING SETUP ---
log_dir = os.path.join(os.getenv('APPDATA'), 'TapAccumulator')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'tapaccumulator.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# --- 1. DATA MANAGEMENT ---
class DataManager:
    """Handles thread-safe, persistent JSON storage."""

    # Every tunable value lives here. On first run these are written to
    # settings.json. Existing keys are preserved on upgrade (non-destructive merge).
    DEFAULTS = {
        "settings": {
            # ── Appearance ──────────────────────────────────────────────
            "theme":                "System",   # "Dark" | "Light" | "System"

            # ── Engine behaviour ────────────────────────────────────────
            "capture_mode":         "mix",      # "mix" | "hook" | "reclaim"
            "timeout":              1.5,        # rolling tap timeout (seconds)
            "startup_grace":        5.0,        # boot-storm guard window (seconds)
            "hook_threshold":       0.22,       # local-input filter window (seconds)
            "tap_debounce_ms":      280,        # min gap between accepted taps (ms)

            # ── Focus recovery ──────────────────────────────────────────
            "aggressive_focus_recovery": True,
            "recovery_interval":    8.0,        # reclaim watchdog interval (seconds)

            # ── TTS ─────────────────────────────────────────────────────
            "tts_voice_index":      0,          # 0=default, 1,2,3… = other installed voices
            "tts_rate":             0,          # SAPI rate: -10 (slow) to +10 (fast)
            "tts_volume":           100,        # SAPI volume: 0–100

            # ── App behaviour ───────────────────────────────────────────
            "run_in_background":    True,
            "start_minimized":      False,      # start to tray without showing window
        }
    }

    def __init__(self):
        self.data_dir = os.path.join(os.getenv('APPDATA'), 'TapAccumulator')
        os.makedirs(self.data_dir, exist_ok=True)
        self.files = {
            'settings': os.path.join(self.data_dir, 'settings.json'),
            'macros':   os.path.join(self.data_dir, 'macros.json'),
        }
        self._init_files()

    def _init_files(self):
        # Settings: non-destructive merge — adds new keys, keeps existing values
        s_path = self.files['settings']
        defaults = self.DEFAULTS['settings']
        if os.path.exists(s_path):
            try:
                with open(s_path, 'r') as f:
                    existing = json.load(f)
                merged = {**defaults, **existing}   # existing wins on conflict
                with open(s_path, 'w') as f:
                    json.dump(merged, f, indent=4)
            except Exception:
                with open(s_path, 'w') as f:
                    json.dump(defaults, f, indent=4)
        else:
            with open(s_path, 'w') as f:
                json.dump(defaults, f, indent=4)

        # Macros: create example if missing
        m_path = self.files['macros']
        if not os.path.exists(m_path):
            with open(m_path, 'w') as f:
                json.dump({
                    "2": {"type": "shell", "command": "calc.exe",
                          "speech_text": "Calculator opened"}
                }, f, indent=4)

    def load(self, key):
        try:
            with open(self.files[key], 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {key}: {e}")
            return {} if key == 'macros' else dict(self.DEFAULTS['settings'])

    def save(self, key, data):
        try:
            with open(self.files[key], 'w') as f:
                json.dump(data, f, indent=4)
            logger.info(f"Saved {key}")
        except Exception as e:
            logger.error(f"Failed to save {key}: {e}")


# --- 3. GRAPHICAL USER INTERFACE ---
# Design: Tactical Industrial Dark — deep charcoal, electric amber accents,
# monospaced readouts, sharp geometry. Built for power users.

ctk.set_default_color_theme("blue")

# ── Palette ────────────────────────────────────────────────────────────────
C_BG        = "#0D0F14"   # primary background
C_SURFACE   = "#13161E"   # card / sidebar surface
C_RAISED    = "#1A1F2B"   # elevated element (input bg, list items)
C_BORDER    = "#252B3A"   # subtle borders
C_AMBER     = "#F5A623"   # primary accent — electric amber
C_AMBER_DIM = "#7A5110"   # muted amber for inactive / secondary
C_GREEN     = "#3DDC84"   # live / active state
C_RED       = "#FF4D4D"   # danger / remove
C_TEXT      = "#E8ECF4"   # primary text
C_MUTED     = "#4E5772"   # secondary / placeholder text
C_FOCUS_REC = "#FF9A00"   # focus recovered flash

# ── Fonts ──────────────────────────────────────────────────────────────────
F_DISPLAY  = None
F_HEADING  = None
F_SUBHEAD  = None
F_LABEL    = None
F_MONO     = None
F_MONO_SM  = None
F_NAV      = None
F_BADGE    = None
F_BADGE_SM = None

def _init_fonts():
    """Instantiate all CTkFont objects after the root window exists."""
    global F_DISPLAY, F_HEADING, F_SUBHEAD, F_LABEL
    global F_MONO, F_MONO_SM, F_NAV, F_BADGE, F_BADGE_SM
    F_DISPLAY  = ctk.CTkFont(family="Segoe UI Black",    size=52, weight="bold")
    F_HEADING  = ctk.CTkFont(family="Segoe UI Semibold", size=18, weight="bold")
    F_SUBHEAD  = ctk.CTkFont(family="Segoe UI",          size=13)
    F_LABEL    = ctk.CTkFont(family="Segoe UI",          size=11)
    F_MONO     = ctk.CTkFont(family="Consolas",          size=12)
    F_MONO_SM  = ctk.CTkFont(family="Consolas",          size=11)
    F_NAV      = ctk.CTkFont(family="Segoe UI Semibold", size=13)
    F_BADGE    = ctk.CTkFont(family="Segoe UI Black",    size=15, weight="bold")
    F_BADGE_SM = ctk.CTkFont(family="Segoe UI Semibold", size=10, weight="bold")


class TapAccumulatorApp(ctk.CTk):
    def __init__(self):
        # Detect first run BEFORE DataManager creates settings.json
        _settings_path = os.path.join(os.getenv('APPDATA'), 'TapAccumulator', 'settings.json')
        self._is_first_run = not os.path.exists(_settings_path)

        # Apply theme before window creation
        _data_preview = DataManager()
        _s = _data_preview.load('settings')
        ctk.set_appearance_mode(_s.get('theme', 'System'))
        ctk.set_appearance_mode(_s.get('theme', 'System'))

        super().__init__()
        _init_fonts()
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.icon_ico = os.path.join(self.current_dir, "icons", "icon.ico")
        self.icon_png = os.path.join(self.current_dir, "icons", "icon.png")
        
        if os.path.exists(self.icon_ico):
          self.iconbitmap(self.icon_ico)
          
        self.title("TapAccumulator  ·  v1.3")
        self.geometry("1020x700")
        self.minsize(860, 600)
        self.configure(fg_color=C_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.data        = _data_preview
        self._active_nav = None
        self._ui_ready   = False
        self.tray_icon   = None

        self.log_text           = None
        self.status_label       = None
        self.focus_status       = None
        self.current_taps_label = None
        self._tap_ring          = None
        self._pulse_job         = None

        self._build_ui()
        self._ui_ready = True

        # Start tray icon immediately — always present, not only when minimized
        self._start_tray()

        # Honour start_minimized setting
        if _s.get('start_minimized', False):
            self.withdraw()

        self.engine = TapEngine(self.data, ui_callback=self.handle_engine_event)
        self.engine.start()

        logger.info("TapAccumulator v1.3 — UI initialized, engine starting")

        # Show welcome modal on first run (settings.json was absent at startup)
        if self._is_first_run:
            self.after(800, self._show_welcome_modal)

        # Show hook warning if capture mode involves hook path
        _mode = _s.get('capture_mode', 'mix')
        if _mode in ('hook', 'mix'):
            self.after(1200, lambda: self._show_hook_warning_modal(_mode))

    def _on_close(self):
        """Hide to tray on window close (don't destroy)."""
        self.withdraw()

    # ═══════════════════════════════════════════════════════════════════
    #  UI CONSTRUCTION
    # ═══════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self._build_sidebar()
        self._build_frames()
        self.show_frame("dashboard")

    # ── Sidebar ────────────────────────────────────────────────────────

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=C_SURFACE,
                          border_width=0)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(8, weight=1)
        sb.grid_columnconfigure(0, weight=1)
        self.sidebar = sb

        logo_block = ctk.CTkFrame(sb, fg_color="transparent")
        logo_block.grid(row=0, column=0, sticky="ew", padx=20, pady=(28, 4))
        ctk.CTkLabel(logo_block, text="⚡", font=ctk.CTkFont(size=22),
                     text_color=C_AMBER).pack(side="left")
        ctk.CTkLabel(logo_block, text=" TAP", font=ctk.CTkFont(family="Segoe UI Black", size=17, weight="bold"),
                     text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(logo_block, text="ACC", font=ctk.CTkFont(family="Segoe UI Black", size=17, weight="bold"),
                     text_color=C_AMBER).pack(side="left")

        ctk.CTkLabel(sb, text="v1.3.0", font=F_LABEL,
                     text_color=C_MUTED).grid(row=1, column=0, padx=20, sticky="w", pady=(0, 16))

        ctk.CTkFrame(sb, height=1, fg_color=C_BORDER).grid(
            row=2, column=0, sticky="ew", padx=16, pady=(0, 14))

        self._nav_btns = {}
        for i, (key, label) in enumerate([("dashboard", "◈  Dashboard"),
                                           ("macros",    "⌘  Tap Commands"),
                                           ("settings",  "⚙  Settings")]):
            btn = ctk.CTkButton(sb, text=label, anchor="w",
                                font=F_NAV, height=42, corner_radius=8,
                                fg_color="transparent", hover_color=C_RAISED,
                                text_color=C_MUTED, border_width=0,
                                command=lambda k=key: self.show_frame(k))
            btn.grid(row=3 + i, column=0, sticky="ew", padx=12, pady=3)
            self._nav_btns[key] = btn

        panel = ctk.CTkFrame(sb, fg_color=C_RAISED, corner_radius=10)
        panel.grid(row=9, column=0, sticky="ew", padx=12, pady=(0, 14))
        panel.grid_columnconfigure(0, weight=1)

        def status_row(parent, row, label_text):
            ctk.CTkLabel(parent, text=label_text, font=F_LABEL,
                         text_color=C_MUTED, anchor="w").grid(
                row=row, column=0, sticky="w", padx=14, pady=(6 if row == 0 else 2, 0))
            val = ctk.CTkLabel(parent, text="—", font=F_MONO_SM,
                               text_color=C_TEXT, anchor="e")
            val.grid(row=row, column=1, sticky="e", padx=14)
            return val

        panel.grid_columnconfigure(1, weight=1)

        title_row = ctk.CTkFrame(panel, fg_color="transparent")
        title_row.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 4))
        title_row.grid_columnconfigure(1, weight=1)

        self._dot = ctk.CTkLabel(title_row, text="●", font=ctk.CTkFont(size=10),
                                  text_color=C_GREEN)
        self._dot.grid(row=0, column=0)
        self.status_label = ctk.CTkLabel(title_row, text="STARTING…",
                                          font=F_BADGE_SM, text_color=C_GREEN)
        self.status_label.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ctk.CTkFrame(panel, height=1, fg_color=C_BORDER).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 0))

        self._si_mode    = status_row(panel, 2, "MODE")
        self._si_state   = status_row(panel, 3, "STATE")
        self._si_session = status_row(panel, 4, "SESSION")
        self._si_hook    = status_row(panel, 5, "HOOK")
        self._si_filter  = status_row(panel, 6, "LAST FILTER")

        ctk.CTkFrame(panel, height=1, fg_color=C_BORDER).grid(
            row=7, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 0))

        self.focus_status = ctk.CTkLabel(panel, text="INITIALISING…",
                                          font=F_LABEL, text_color=C_MUTED, anchor="w")
        self.focus_status.grid(row=8, column=0, columnspan=2,
                               sticky="w", padx=14, pady=(6, 12))

    # ── Page frames ────────────────────────────────────────────────────

    def _build_frames(self):
        self.frames = {}
        self._build_dashboard()
        self._build_macros()
        self._build_settings()

    # ── Dashboard ──────────────────────────────────────────────────────

    def _build_dashboard(self):
        dash = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.frames["dashboard"] = dash
        dash.grid_rowconfigure(1, weight=1)
        dash.grid_columnconfigure(0, weight=1)

        topbar = ctk.CTkFrame(dash, fg_color="transparent")
        topbar.grid(row=0, column=0, sticky="ew", padx=28, pady=(28, 0))
        topbar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(topbar, text="Live Activity", font=F_HEADING,
                     text_color=C_TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(topbar, text="Real-time tap detection & event stream",
                     font=F_LABEL, text_color=C_MUTED).grid(row=1, column=0, sticky="w")

        ctk.CTkButton(topbar, text="Clear Log", width=90, height=30,
                      font=F_LABEL, corner_radius=6,
                      fg_color=C_RAISED, hover_color=C_BORDER,
                      text_color=C_MUTED, border_width=1, border_color=C_BORDER,
                      command=self.clear_log).grid(row=0, column=1, rowspan=2, sticky="e")

        body = ctk.CTkFrame(dash, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=28, pady=20)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        hero = ctk.CTkFrame(body, width=260, fg_color=C_SURFACE, corner_radius=14,
                             border_width=1, border_color=C_BORDER)
        hero.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        hero.grid_propagate(False)
        hero.grid_rowconfigure(3, weight=1)
        hero.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(hero, text="TAPS IN SEQUENCE",
                     font=F_BADGE_SM, text_color=C_MUTED).grid(
            row=0, column=0, pady=(28, 4))

        self.current_taps_label = ctk.CTkLabel(
            hero, text="0", font=F_DISPLAY, text_color=C_AMBER)
        self.current_taps_label.grid(row=1, column=0, pady=(0, 4))

        self._tap_sub = ctk.CTkLabel(hero, text="waiting for input",
                                      font=F_LABEL, text_color=C_MUTED)
        self._tap_sub.grid(row=2, column=0, pady=(0, 20))

        ctk.CTkFrame(hero, height=1, fg_color=C_BORDER).grid(
            row=3, column=0, sticky="ew", padx=20, pady=8)

        stats_grid = ctk.CTkFrame(hero, fg_color="transparent")
        stats_grid.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 24))
        stats_grid.grid_columnconfigure((0, 1), weight=1)

        self._stat_triggered = self._stat_cell(stats_grid, "TRIGGERED", "0", 0, 0)
        self._stat_timeout   = self._stat_cell(stats_grid, "TIMEOUT MS",
                                                str(int(self.data.load('settings').get('timeout', 1.5) * 1000)),
                                                0, 1)
        self._trigger_count = 0

        log_card = ctk.CTkFrame(body, fg_color=C_SURFACE, corner_radius=14,
                                 border_width=1, border_color=C_BORDER)
        log_card.grid(row=0, column=1, sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 0))
        log_hdr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_hdr, text="EVENT STREAM", font=F_BADGE_SM,
                     text_color=C_MUTED).grid(row=0, column=0, sticky="w")

        self._live_dot = ctk.CTkLabel(log_hdr, text="● LIVE", font=F_BADGE_SM,
                                       text_color=C_GREEN)
        self._live_dot.grid(row=0, column=1, sticky="e")

        self.log_text = ctk.CTkTextbox(
            log_card, font=F_MONO,
            fg_color="transparent",
            text_color=C_TEXT,
            scrollbar_button_color=C_BORDER,
            scrollbar_button_hover_color=C_AMBER_DIM,
            border_width=0,
            wrap="none"
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=6, pady=(8, 12))
        self.log_text.insert("end", f"  {'─'*52}\n")
        self.log_text.insert("end", "  TapAccumulator SMTC engine initialised.\n")
        self.log_text.insert("end", f"  {'─'*52}\n\n")

        self._blink_live_dot()

    def _stat_cell(self, parent, label, value, row, col):
        cell = ctk.CTkFrame(parent, fg_color=C_RAISED, corner_radius=8)
        cell.grid(row=row, column=col, padx=4, pady=4, sticky="ew")
        ctk.CTkLabel(cell, text=label, font=F_BADGE_SM, text_color=C_MUTED).pack(pady=(8, 1))
        lbl = ctk.CTkLabel(cell, text=value, font=ctk.CTkFont(family="Consolas", size=18, weight="bold"),
                            text_color=C_TEXT)
        lbl.pack(pady=(0, 8))
        return lbl

    def _blink_live_dot(self):
        current = self._live_dot.cget("text_color")
        next_color = C_MUTED if current == C_GREEN else C_GREEN
        self._live_dot.configure(text_color=next_color)
        self.after(900, self._blink_live_dot)

    # ── Macros frame ───────────────────────────────────────────────────

    def _build_macros(self):
        macros_frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.frames["macros"] = macros_frame
        macros_frame.grid_columnconfigure(0, weight=0)
        macros_frame.grid_columnconfigure(1, weight=1)
        macros_frame.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(macros_frame, fg_color="transparent")
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew", padx=28, pady=(28, 20))
        ctk.CTkLabel(hdr, text="Tap Commands", font=F_HEADING, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(hdr, text=" — assign shell macros to tap counts",
                     font=F_SUBHEAD, text_color=C_MUTED).pack(side="left", pady=(2, 0))

        # ── Form card ──────────────────────────────────────────────────
        form_card = ctk.CTkFrame(macros_frame, width=290, fg_color=C_SURFACE,
                                  corner_radius=14, border_width=1, border_color=C_BORDER)
        form_card.grid(row=1, column=0, sticky="nsew", padx=(28, 12), pady=(0, 28))
        form_card.grid_propagate(False)

        ctk.CTkLabel(form_card, text="NEW TRIGGER", font=F_BADGE_SM,
                     text_color=C_MUTED).pack(anchor="w", padx=22, pady=(24, 14))

        ctk.CTkFrame(form_card, height=1, fg_color=C_BORDER).pack(fill="x", padx=16, pady=(0, 18))

        # Tap count picker
        ctk.CTkLabel(form_card, text="TAP COUNT", font=F_BADGE_SM,
                     text_color=C_MUTED).pack(anchor="w", padx=22)

        self.tap_var = ctk.StringVar(value="2")
        tap_row = ctk.CTkFrame(form_card, fg_color="transparent")
        tap_row.pack(fill="x", padx=22, pady=(6, 18))

        for val in ["2", "3", "4", "5", "6", "7", "8"]:
            self._make_tap_chip(tap_row, val)

        # Command entry
        ctk.CTkLabel(form_card, text="SHELL COMMAND", font=F_BADGE_SM,
                     text_color=C_MUTED).pack(anchor="w", padx=22)

        self.cmd_var = ctk.StringVar()
        cmd_entry = ctk.CTkEntry(
            form_card, textvariable=self.cmd_var,
            placeholder_text="e.g.  calc.exe  ·  start spotify",
            font=F_MONO_SM, height=40, corner_radius=8,
            fg_color=C_RAISED, border_color=C_BORDER, border_width=1,
            text_color=C_TEXT,
            placeholder_text_color=C_MUTED
        )
        cmd_entry.pack(fill="x", padx=22, pady=(6, 16))

        # Speech text entry (TTS Field)
        ctk.CTkLabel(form_card, text="SUCCESS SPEECH TEXT (TTS)", font=F_BADGE_SM,
                     text_color=C_MUTED).pack(anchor="w", padx=22)

        self.speech_var = ctk.StringVar()
        speech_entry = ctk.CTkEntry(
            form_card, textvariable=self.speech_var,
            placeholder_text="e.g.  System optimized  ·  Fired",
            font=F_MONO_SM, height=40, corner_radius=8,
            fg_color=C_RAISED, border_color=C_BORDER, border_width=1,
            text_color=C_TEXT,
            placeholder_text_color=C_MUTED
        )
        speech_entry.pack(fill="x", padx=22, pady=(6, 22))
        speech_entry.bind("<Return>", lambda e: self.attempt_save_macro())

        ctk.CTkButton(
            form_card, text="⚡  Register Command",
            font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
            height=42, corner_radius=8,
            fg_color=C_AMBER, hover_color="#D4901E",
            text_color="#0D0F14",
            command=self.attempt_save_macro
        ).pack(fill="x", padx=22, pady=(0, 24))

        # ── Macro list card ────────────────────────────────────────────
        list_card = ctk.CTkFrame(macros_frame, fg_color=C_SURFACE,
                                  corner_radius=14, border_width=1, border_color=C_BORDER)
        list_card.grid(row=1, column=1, sticky="nsew", padx=(0, 28), pady=(0, 28))
        list_card.grid_rowconfigure(1, weight=1)
        list_card.grid_columnconfigure(0, weight=1)

        list_hdr = ctk.CTkFrame(list_card, fg_color="transparent")
        list_hdr.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 12))
        list_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(list_hdr, text="ACTIVE MACROS", font=F_BADGE_SM,
                     text_color=C_MUTED).grid(row=0, column=0, sticky="w")
        self._macro_count_lbl = ctk.CTkLabel(list_hdr, text="", font=F_BADGE_SM,
                                              text_color=C_AMBER)
        self._macro_count_lbl.grid(row=0, column=1, sticky="e")

        ctk.CTkFrame(list_card, height=1, fg_color=C_BORDER).grid(
            row=1, column=0, sticky="ew", padx=16)

        self.scroll_list = ctk.CTkScrollableFrame(
            list_card, fg_color="transparent",
            scrollbar_button_color=C_BORDER,
            scrollbar_button_hover_color=C_AMBER_DIM
        )
        self.scroll_list.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        list_card.grid_rowconfigure(2, weight=1)

        self.refresh_macro_list()

    def _make_tap_chip(self, parent, val):
        def select():
            self.tap_var.set(val)
            for v, btn in self._tap_chips.items():
                if v == val:
                    btn.configure(fg_color=C_AMBER, text_color="#0D0F14",
                                  border_color=C_AMBER)
                else:
                    btn.configure(fg_color=C_RAISED, text_color=C_MUTED,
                                  border_color=C_BORDER)

        if not hasattr(self, "_tap_chips"):
            self._tap_chips = {}

        btn = ctk.CTkButton(
            parent, text=val, width=30, height=30,
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
            corner_radius=6, border_width=1,
            fg_color=C_AMBER if val == self.tap_var.get() else C_RAISED,
            hover_color=C_AMBER_DIM,
            text_color="#0D0F14" if val == self.tap_var.get() else C_MUTED,
            border_color=C_AMBER if val == self.tap_var.get() else C_BORDER,
            command=select
        )
        btn.pack(side="left", padx=2)
        self._tap_chips[val] = btn

    # ═══════════════════════════════════════════════════════════════════
    #  NAVIGATION
    # ═══════════════════════════════════════════════════════════════════

    def show_frame(self, name):
        for frame in self.frames.values():
            frame.grid_forget()
        self.frames[name].grid(row=0, column=1, sticky="nsew")

        for key, btn in self._nav_btns.items():
            if key == name:
                btn.configure(fg_color=C_RAISED, text_color=C_AMBER)
            else:
                btn.configure(fg_color="transparent", text_color=C_MUTED)
        self._active_nav = name

    # ═══════════════════════════════════════════════════════════════════
    #  ENGINE EVENT HANDLER
    # ═══════════════════════════════════════════════════════════════════

    def handle_engine_event(self, event_type, *args):
        if not self._ui_ready:
            return

        def update():
            ts = time.strftime('%H:%M:%S')
            MC = {"RECLAIM": C_AMBER, "HOOK": "#7EB8F7", "MIX": C_GREEN}

            # ── Startup sequence ────────────────────────────────────────
            if event_type == "engine_initializing":
                msg = args[0] if args else "Initializing…"
                self.status_label.configure(text="STARTING…", text_color=C_AMBER)
                self._dot.configure(text_color=C_AMBER)
                self.focus_status.configure(text=msg[:32], text_color=C_AMBER)
                self._write_log(ts, "SYS", msg)

            elif event_type == "engine_ready":
                mode = (args[0] if args else "mix").upper()
                col  = MC.get(mode, C_MUTED)
                self.status_label.configure(text="LISTENER ACTIVE", text_color=C_GREEN)
                self._dot.configure(text_color=C_GREEN)
                self._si_mode.configure(text=mode, text_color=col)
                self._si_state.configure(text="SMTC", text_color=C_AMBER)
                self._si_session.configure(text="none", text_color=C_MUTED)
                self._si_hook.configure(text="idle", text_color=C_MUTED)
                self._si_filter.configure(text="—", text_color=C_MUTED)
                self.focus_status.configure(text="SESSION ACTIVE", text_color=C_MUTED)
                desc = {"MIX": "stealth hybrid — hook-first",
                        "HOOK": "passive session listener",
                        "RECLAIM": "own the SMTC session"}.get(mode, "")
                self._write_log(ts, "SYS",
                    f"Engine READY — mode: {mode}  ({desc})")

            # ── Tap events ──────────────────────────────────────────────
            elif event_type == "tap_detected":
                self._write_log(ts, "INPUT", "Hardware tap signal received")

            elif event_type == "tap_count_started":
                count = args[0]
                self.current_taps_label.configure(text=str(count), text_color=C_AMBER)
                self._tap_sub.configure(text="accumulating…", text_color=C_AMBER)
                self._write_log(ts, "TAP", "Sequence started — tap 1")

            elif event_type == "tap_added":
                count = args[0]
                self.current_taps_label.configure(text=str(count))
                self._write_log(ts, "TAP", f"Tap {count} added to sequence")

            elif event_type == "timeout_reset":
                self.current_taps_label.configure(text="0", text_color=C_MUTED)
                self._tap_sub.configure(text="waiting for input", text_color=C_MUTED)
                self._write_log(ts, "EXEC", "Rolling timeout — dispatching action…")

            # ── Action events ───────────────────────────────────────────
            elif event_type == "command_executed":
                count = args[0]
                cmd   = args[1] if len(args) > 1 else ""
                self._trigger_count += 1
                self._stat_triggered.configure(text=str(self._trigger_count))
                self._write_log(ts, "OK", f"{count}× fired  →  {cmd}")

            elif event_type == "no_macro":
                count = args[0] if args else "?"
                self._write_log(ts, "WARN", f"{count}× tap — no macro assigned")

            # ── Focus / session events ──────────────────────────────────
            elif event_type == "focus_recovered":
                self._si_state.configure(text="SMTC", text_color=C_AMBER)
                self._si_hook.configure(text="idle", text_color=C_MUTED)
                self.focus_status.configure(text="SESSION RE-REGISTERED",
                                             text_color=C_FOCUS_REC)
                self._write_log(ts, "SYS", "SMTC session re-registered")
                self.after(2500, lambda: self.focus_status.configure(
                    text="SESSION ACTIVE", text_color=C_MUTED))

            elif event_type == "hook_attached":
                app_id = args[0] if args else "unknown"
                short  = _parse_app_id(app_id)
                self._si_state.configure(text="HOOKED", text_color="#7EB8F7")
                self._si_session.configure(text=short, text_color=C_TEXT)
                self._si_hook.configure(text="live ●", text_color=C_GREEN)
                self.focus_status.configure(
                    text=f"HOOKED  ·  {short[:18].upper()}", text_color="#7EB8F7")
                self._write_log(ts, "SYS", f"Hook attached → {app_id}")

            elif event_type == "hook_filtered":
                # args: (label_str, delta_float)
                label = args[0] if args else "local"
                delta = f"{args[1]:.3f}s" if len(args) > 1 else "?"
                self._si_filter.configure(text=f"{label}  Δ{delta}",
                                           text_color=C_MUTED)
                self._write_log(ts, "FLTR",
                    f"Filtered ({label}) — local input {delta} ago")

            elif event_type == "session_changed":
                self._si_hook.configure(text="switching…", text_color=C_AMBER)
                self._si_session.configure(text="…", text_color=C_AMBER)
                self._write_log(ts, "SYS",
                    "Windows media session changed — re-evaluating hook")

            elif event_type == "mix_own_session":
                self._si_state.configure(text="SMTC", text_color=C_AMBER)
                self._si_hook.configure(text="idle", text_color=C_MUTED)
                self._si_session.configure(text="ours", text_color=C_MUTED)
                self.focus_status.configure(text="OUR SESSION ACTIVE",
                                             text_color=C_MUTED)
                self._write_log(ts, "SYS",
                    "Mix: our session active — SMTC button path")

            self.log_text.see("end")

        self.after(0, update)

    def _write_log(self, ts, tag, message):
        tag_colors = {
            "INPUT": C_MUTED,
            "TAP":   C_AMBER,
            "EXEC":  "#7EB8F7",
            "OK":    C_GREEN,
            "SYS":   C_FOCUS_REC,
            "FLTR":  C_MUTED,
        }
        line = f"  {ts}  [{tag:<5}]  {message}\n"
        self.log_text.insert("end", line)

    def clear_log(self):
        self.log_text.delete("1.0", "end")
        ts = time.strftime('%H:%M:%S')
        self.log_text.insert("end", f"  {ts}  [SYS  ]  Log cleared.\n")

    # ═══════════════════════════════════════════════════════════════════
    #  MACRO CRUD
    # ═══════════════════════════════════════════════════════════════════

    def attempt_save_macro(self):
        taps    = self.tap_var.get()
        command = self.cmd_var.get().strip()
        speech  = self.speech_var.get().strip()
        if not command:
            return
        macros = self.data.load('macros')
        if taps in macros:
            self.show_duplicate_modal(taps, command, speech, macros[taps]['command'])
        else:
            self._commit_macro(taps, command, speech)

    def show_duplicate_modal(self, taps, new_cmd, new_speech, old_cmd):
        modal = ctk.CTkToplevel(self)
        modal.title("Conflict")
        modal.geometry("420x260")
        modal.attributes("-topmost", True)
        modal.resizable(False, False)
        modal.configure(fg_color=C_SURFACE)
        modal.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  // 2) - 210
        y = self.winfo_y() + (self.winfo_height() // 2) - 130
        modal.geometry(f"+{x}+{y}")

        ctk.CTkLabel(modal, text="TAP COLLISION",
                     font=ctk.CTkFont(family="Segoe UI Black", size=16, weight="bold"),
                     text_color=C_AMBER).pack(pady=(28, 6))
        ctk.CTkLabel(modal,
                     text=f"{taps}× is already assigned.\n\n"
                          f"Current:  {old_cmd}\nReplace:  {new_cmd}",
                     font=F_MONO_SM, text_color=C_TEXT,
                     justify="left", wraplength=360).pack(padx=28)

        btn_row = ctk.CTkFrame(modal, fg_color="transparent")
        btn_row.pack(pady=24)
        ctk.CTkButton(btn_row, text="Cancel", width=110, height=36,
                      corner_radius=8, fg_color=C_RAISED, hover_color=C_BORDER,
                      text_color=C_TEXT, border_width=1, border_color=C_BORDER,
                      command=modal.destroy).pack(side="left", padx=8)
        ctk.CTkButton(btn_row, text="Overwrite", width=110, height=36,
                      corner_radius=8, fg_color=C_RED, hover_color="#C03030",
                      text_color=C_TEXT,
                      command=lambda: (self._commit_macro(taps, new_cmd, new_speech), modal.destroy())
                      ).pack(side="left", padx=8)

    def _commit_macro(self, taps, command, speech_text):
        macros = self.data.load('macros')
        macros[str(taps)] = {"type": "shell", "command": command, "speech_text": speech_text}
        self.data.save('macros', macros)
        self.cmd_var.set("")
        self.speech_var.set("")
        self.refresh_macro_list()
        logger.info(f"Macro registered: {taps} taps → {command} | TTS: {speech_text}")

    def delete_macro(self, taps):
        macros = self.data.load('macros')
        if str(taps) in macros:
            del macros[str(taps)]
            self.data.save('macros', macros)
            self.refresh_macro_list()
            logger.info(f"Deleted macro: {taps} taps")

    def refresh_macro_list(self):
        for w in self.scroll_list.winfo_children():
            w.destroy()

        macros = self.data.load('macros')
        count  = len(macros)
        self._macro_count_lbl.configure(
            text=f"{count} registered" if count else "none")

        if not macros:
            empty = ctk.CTkFrame(self.scroll_list, fg_color="transparent")
            empty.pack(fill="both", expand=True, pady=40)
            ctk.CTkLabel(empty, text="╌╌  no macros yet  ╌╌",
                         font=F_MONO, text_color=C_MUTED).pack()
            ctk.CTkLabel(empty, text="Use the form on the left to register\nyour first tap command.",
                         font=F_LABEL, text_color=C_MUTED, justify="center").pack(pady=8)
            return

        for taps, data in sorted(macros.items(), key=lambda x: int(x[0])):
            row = ctk.CTkFrame(self.scroll_list, fg_color=C_RAISED,
                               corner_radius=10, border_width=1, border_color=C_BORDER)
            row.pack(fill="x", pady=5, padx=4)
            row.grid_columnconfigure(1, weight=1)

            # Tap badge spanning 3 rows to include TTS text layout nicely
            badge = ctk.CTkFrame(row, fg_color=C_AMBER, corner_radius=8,
                                  width=52, height=52)
            badge.grid(row=0, column=0, padx=14, pady=12, rowspan=3)
            badge.grid_propagate(False)
            ctk.CTkLabel(badge, text=f"{taps}×",
                         font=F_BADGE, text_color="#0D0F14").place(
                relx=0.5, rely=0.5, anchor="center")

            # Details
            ctk.CTkLabel(row, text="SHELL EXECUTION",
                         font=F_BADGE_SM, text_color=C_MUTED).grid(
                row=0, column=1, sticky="sw", padx=(2, 0), pady=(12, 0))
            ctk.CTkLabel(row, text=data['command'],
                         font=F_MONO_SM, text_color=C_TEXT).grid(
                row=1, column=1, sticky="nw", padx=(2, 0), pady=(2, 4))

            # Display TTS payload if populated
            speech_val = data.get('speech_text', '').strip()
            if speech_val:
                ctk.CTkLabel(row, text=f"🗣  \"{speech_val}\"",
                             font=F_MONO_SM, text_color=C_AMBER).grid(
                    row=2, column=1, sticky="nw", padx=(2, 0), pady=(0, 12))
            else:
                ctk.CTkLabel(row, text="").grid(row=2, column=1, pady=(0, 4))

            # Remove button
            ctk.CTkButton(row, text="✕", width=32, height=32,
                          corner_radius=6, font=ctk.CTkFont(size=14, weight="bold"),
                          fg_color="transparent", hover_color="#3A1515",
                          text_color=C_RED, border_width=1, border_color="#3A1515",
                          command=lambda t=taps: self.delete_macro(t)).grid(
                row=0, column=2, rowspan=3, padx=14)

    # ═══════════════════════════════════════════════════════════════════
    #  SETTINGS PAGE
    # ═══════════════════════════════════════════════════════════════════

    def _build_settings(self):
        sf = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        self.frames["settings"] = sf
        sf.grid_columnconfigure(0, weight=1)

        def section(title, row):
            ctk.CTkLabel(sf, text=title, font=F_BADGE_SM,
                         text_color=C_MUTED, anchor="w").grid(
                row=row, column=0, sticky="w", padx=32,
                pady=(24 if row == 0 else 18, 6))
            card = ctk.CTkFrame(sf, fg_color=C_SURFACE, corner_radius=12,
                                 border_width=1, border_color=C_BORDER)
            card.grid(row=row + 1, column=0, sticky="ew", padx=24)
            card.grid_columnconfigure(1, weight=1)
            return card

        def field(card, row, label, desc, widget_fn):
            ctk.CTkLabel(card, text=label, font=F_LABEL,
                         text_color=C_TEXT, anchor="w").grid(
                row=row, column=0, sticky="w", padx=20,
                pady=(14 if row == 0 else 6, 2))
            if desc:
                ctk.CTkLabel(card, text=desc, font=ctk.CTkFont(size=10),
                             text_color=C_MUTED, anchor="w").grid(
                    row=row, column=0, sticky="sw", padx=20, pady=(0, 10))
            widget_fn(card, row)

        s = self.data.load('settings')

        # ── Appearance ───────────────────────────────────────────────
        c1 = section("APPEARANCE", 0)
        self._s_theme = ctk.StringVar(value=s.get('theme', 'System'))
        field(c1, 0, "Theme", "Dark · Light · System",
              lambda p, r: ctk.CTkSegmentedButton(
                  p, values=["Dark", "Light", "System"],
                  variable=self._s_theme,
                  fg_color=C_RAISED, selected_color=C_AMBER,
                  selected_hover_color="#D4901E",
                  unselected_color=C_RAISED,
                  unselected_hover_color=C_BORDER,
                  text_color=C_TEXT,
                  command=lambda v: ctk.set_appearance_mode(v)
              ).grid(row=r, column=1, sticky="e", padx=20, pady=(14, 10)))

        # ── Engine ───────────────────────────────────────────────────
        c2 = section("ENGINE", 2)
        self._s_mode = ctk.StringVar(value=s.get('capture_mode', 'mix'))
        field(c2, 0, "Capture Mode", "mix · hook · reclaim",
              lambda p, r: ctk.CTkSegmentedButton(
                  p, values=["mix", "hook", "reclaim"],
                  variable=self._s_mode,
                  fg_color=C_RAISED, selected_color=C_AMBER,
                  selected_hover_color="#D4901E",
                  unselected_color=C_RAISED,
                  unselected_hover_color=C_BORDER,
                  text_color=C_TEXT
              ).grid(row=r, column=1, sticky="e", padx=20, pady=(14, 10)))

        self._s_timeout    = self._make_setting_entry(c2, 1, "Tap Timeout (s)",
            "Rolling window before a sequence fires",  s.get('timeout', 1.5))
        self._s_grace      = self._make_setting_entry(c2, 2, "Startup Grace (s)",
            "Boot-storm guard window",                  s.get('startup_grace', 5.0))
        self._s_threshold  = self._make_setting_entry(c2, 3, "Hook Threshold (s)",
            "Local-input filter window",                s.get('hook_threshold', 0.22))
        self._s_debounce   = self._make_setting_entry(c2, 4, "Tap Debounce (ms)",
            "Min gap between accepted taps",            s.get('tap_debounce_ms', 280))

        # ── TTS ──────────────────────────────────────────────────────
        c3 = section("TEXT-TO-SPEECH  (Windows SAPI)", 4)
        self._s_voice  = self._make_setting_entry(c3, 0, "Voice Index",
            "0 = default, 1 / 2 / 3 … = other installed voices",
            s.get('tts_voice_index', 0))
        self._s_rate   = self._make_setting_entry(c3, 1, "Speech Rate",
            "-10 (slow) to +10 (fast)",                 s.get('tts_rate', 0))
        self._s_volume = self._make_setting_entry(c3, 2, "Volume",
            "0 – 100",                                  s.get('tts_volume', 100))

        # ── App behaviour ────────────────────────────────────────────
        c4 = section("APP BEHAVIOUR", 6)
        self._s_minimized = ctk.BooleanVar(value=s.get('start_minimized', False))
        field(c4, 0, "Start Minimized to Tray", "",
              lambda p, r: ctk.CTkSwitch(
                  p, variable=self._s_minimized, text="",
                  button_color=C_AMBER, button_hover_color="#D4901E",
                  progress_color=C_AMBER_DIM
              ).grid(row=r, column=1, sticky="e", padx=20, pady=(14, 10)))

        # ── Save button ──────────────────────────────────────────────
        ctk.CTkButton(sf, text="💾  Save Settings", height=44,
                      font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
                      fg_color=C_AMBER, hover_color="#D4901E",
                      text_color="#0D0F14", corner_radius=10,
                      command=self._save_settings).grid(
            row=14, column=0, sticky="e", padx=32, pady=(20, 32))

    def _make_setting_entry(self, card, row, label, desc, default):
        ctk.CTkLabel(card, text=label, font=F_LABEL,
                     text_color=C_TEXT, anchor="w").grid(
            row=row, column=0, sticky="nw", padx=20,
            pady=(14 if row == 0 else 8, 0))
        if desc:
            ctk.CTkLabel(card, text=desc, font=ctk.CTkFont(size=10),
                         text_color=C_MUTED, anchor="w").grid(
                row=row, column=0, sticky="sw", padx=20, pady=(0, 10))
        var = ctk.StringVar(value=str(default))
        ctk.CTkEntry(card, textvariable=var, width=120, height=34,
                     font=F_MONO_SM, corner_radius=7,
                     fg_color=C_RAISED, border_color=C_BORDER, border_width=1,
                     text_color=C_TEXT).grid(
            row=row, column=1, sticky="e", padx=20,
            pady=(14 if row == 0 else 8, 10))
        return var

    def _save_settings(self):
        s = self.data.load('settings')
        s['theme']          = self._s_theme.get()
        s['capture_mode']   = self._s_mode.get()
        s['start_minimized']= self._s_minimized.get()
        for key, var, cast in [
            ('timeout',        self._s_timeout,   float),
            ('startup_grace',  self._s_grace,     float),
            ('hook_threshold', self._s_threshold, float),
            ('tap_debounce_ms',self._s_debounce,  int),
            ('tts_voice_index',self._s_voice,     int),
            ('tts_rate',       self._s_rate,      int),
            ('tts_volume',     self._s_volume,    int),
        ]:
            try:
                s[key] = cast(var.get())
            except ValueError:
                pass
        self.data.save('settings', s)
        ctk.set_appearance_mode(s['theme'])
        self._write_log_safe("SYS", "Settings saved — restart engine to apply engine changes")

    def _write_log_safe(self, tag, message):
        """Write to log from any context (no timestamp param needed)."""
        if self.log_text:
            ts = time.strftime('%H:%M:%S')
            self._write_log(ts, tag, message)

    # ═══════════════════════════════════════════════════════════════════
    #  WELCOME MODAL  (first run only)
    # ═══════════════════════════════════════════════════════════════════

    def _show_welcome_modal(self):
        m = ctk.CTkToplevel(self)
        m.title("Welcome to TapAccumulator")
        m.geometry("620x680")
        m.resizable(False, False)
        m.configure(fg_color=C_SURFACE)
        m.attributes("-topmost", True)
        m.grab_set()
        m.lift()
        m.focus_force()
        m.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  // 2) - 310
        y = self.winfo_y() + (self.winfo_height() // 2) - 340
        m.geometry(f"+{x}+{y}")

        scroll = ctk.CTkScrollableFrame(m, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=0, pady=0)
        scroll.grid_columnconfigure(0, weight=1)

        def head(text, row, color=None):
            ctk.CTkLabel(scroll, text=text,
                         font=ctk.CTkFont(family="Segoe UI Black", size=13, weight="bold"),
                         text_color=color or C_AMBER, anchor="w").grid(
                row=row, column=0, sticky="w", padx=28,
                pady=(18 if row > 0 else 32, 4))

        def body(text, row):
            ctk.CTkLabel(scroll, text=text,
                         font=ctk.CTkFont(family="Segoe UI", size=12),
                         text_color=C_TEXT, anchor="w",
                         wraplength=530, justify="left").grid(
                row=row, column=0, sticky="w", padx=28, pady=(0, 4))

        def divider(row):
            ctk.CTkFrame(scroll, height=1, fg_color=C_BORDER).grid(
                row=row, column=0, sticky="ew", padx=24, pady=(10, 0))

        # ── Logo + title ───────────────────────────────────────────────
        logo_row = ctk.CTkFrame(scroll, fg_color="transparent")
        logo_row.grid(row=0, column=0, sticky="w", padx=28, pady=(32, 0))
        ctk.CTkLabel(logo_row, text="⚡",
                     font=ctk.CTkFont(size=28), text_color=C_AMBER).pack(side="left")
        ctk.CTkLabel(logo_row, text=" TapAccumulator  v1.3",
                     font=ctk.CTkFont(family="Segoe UI Black", size=20, weight="bold"),
                     text_color=C_TEXT).pack(side="left")

        ctk.CTkLabel(scroll,
                     text="An Asynchronous Rolling-Timeout Hardware Macro System",
                     font=ctk.CTkFont(family="Segoe UI", size=11),
                     text_color=C_MUTED, anchor="w").grid(
            row=1, column=0, sticky="w", padx=28, pady=(4, 0))

        divider(2)

        head("What is TapAccumulator?", 3)
        body("TapAccumulator turns your Bluetooth earbuds or wireless headset's "
             "media button into a programmable multi-tap macro trigger. "
             "Tap once, twice, three times, or more — each pattern fires a "
             "different shell command, script, or action on your PC, completely "
             "hands-free.", 4)

        divider(5)
        head("How it works", 6)
        body("Press your earbud's play/pause button in a rapid sequence. "
             "TapAccumulator counts your taps within a configurable rolling "
             "timeout window. When you stop tapping, the matching macro fires. "
             "Example: 2 taps → lock screen, 3 taps → launch Spotify, "
             "4 taps → run a custom script.", 7)

        divider(8)
        head("Capture Modes", 9)
        body("MIX (default) — Stealth hybrid. Registers a silent media session at "
             "startup to capture button events. When another media app (YouTube, "
             "Spotify) steals focus, it hooks into that app's session instead — "
             "no interference, no pausing your content.\n\n"
             "HOOK — Purely passive. Listens to whichever app is currently "
             "playing. Uses keyboard and mouse activity timestamps to distinguish "
             "earbud presses from local input.\n\n"
             "RECLAIM — Owns the SMTC session aggressively. Best when no other "
             "media app is running.", 10)

        divider(11)
        head("Registering Macros", 12)
        body("Go to Tap Commands → select a tap count (2–8) → enter a shell "
             "command or executable path → optionally add a speech phrase that "
             "will be spoken aloud when the macro fires → click Register Command.", 13)

        divider(14)
        head("Settings", 15)
        body("All behaviour is tunable in the Settings page or directly in "
             "settings.json under %APPDATA%\\TapAccumulator. Key values include "
             "timeout (rolling window), hook_threshold (local-input filter), "
             "tap_debounce_ms (bounce guard), TTS voice/rate/volume, and "
             "capture mode.", 16)

        divider(17)
        head("System Tray", 18)
        body("TapAccumulator lives in your system tray at all times. Right-click "
             "the amber ⚡ icon to show the window, toggle the listener on/off, "
             "reclaim media focus, or exit.", 19)

        divider(20)

        ctk.CTkButton(scroll, text="Get Started  →",
                      height=42, corner_radius=10,
                      font=ctk.CTkFont(family="Segoe UI Semibold", size=13),
                      fg_color=C_AMBER, hover_color="#D4901E",
                      text_color="#0D0F14",
                      command=m.destroy).grid(
            row=21, column=0, sticky="e", padx=28, pady=(16, 28))

    # ═══════════════════════════════════════════════════════════════════
    #  HOOK WARNING MODAL
    # ═══════════════════════════════════════════════════════════════════

    def _show_hook_warning_modal(self, mode: str):
        m = ctk.CTkToplevel(self)
        m.title("Capture Mode Notice")
        m.geometry("500x330")
        m.resizable(False, False)
        m.configure(fg_color=C_SURFACE)
        m.attributes("-topmost", True)
        m.grab_set()
        m.lift()
        m.focus_force()
        m.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  // 2) - 250
        y = self.winfo_y() + (self.winfo_height() // 2) - 165
        m.geometry(f"+{x}+{y}")
        m.grid_columnconfigure(0, weight=1)

        # Icon + title
        ctk.CTkLabel(m, text="⚠",
                     font=ctk.CTkFont(size=32), text_color=C_AMBER).grid(
            row=0, column=0, pady=(28, 6))

        ctk.CTkLabel(m,
                     text=f"Hook-Based Capture Active  [{mode.upper()}]",
                     font=ctk.CTkFont(family="Segoe UI Black", size=14, weight="bold"),
                     text_color=C_TEXT).grid(row=1, column=0, padx=28)

        ctk.CTkFrame(m, height=1, fg_color=C_BORDER).grid(
            row=2, column=0, sticky="ew", padx=24, pady=(14, 0))

        msg = (
            "TapAccumulator is currently operating in "
            f"{'MIX' if mode == 'mix' else 'HOOK'} mode, which intercepts "
            "playback state changes from the active media session.\n\n"
            "While this approach works seamlessly alongside YouTube, Spotify, "
            "and most media applications, it may register unintended taps "
            "caused by browser auto-replay, media automation tools, or "
            "applications that cycle playback state programmatically.\n\n"
            "If you experience phantom triggers, switch to RECLAIM mode in "
            "Settings for direct, exclusive media button control."
        )
        ctk.CTkLabel(m, text=msg,
                     font=ctk.CTkFont(family="Segoe UI", size=12),
                     text_color=C_TEXT, wraplength=440, justify="left").grid(
            row=3, column=0, padx=28, pady=(14, 0))

        btn_row = ctk.CTkFrame(m, fg_color="transparent")
        btn_row.grid(row=4, column=0, sticky="e", padx=28, pady=(18, 24))

        ctk.CTkButton(btn_row, text="Open Settings", width=130, height=36,
                      corner_radius=8,
                      fg_color=C_RAISED, hover_color=C_BORDER,
                      text_color=C_TEXT, border_width=1, border_color=C_BORDER,
                      command=lambda: (m.destroy(),
                                       self.show_frame("settings"))).pack(
            side="left", padx=(0, 10))

        ctk.CTkButton(btn_row, text="Understood", width=130, height=36,
                      corner_radius=8,
                      fg_color=C_AMBER, hover_color="#D4901E",
                      text_color="#0D0F14",
                      font=ctk.CTkFont(family="Segoe UI Semibold", size=12),
                      command=m.destroy).pack(side="left")

    # ═══════════════════════════════════════════════════════════════════
    #  TRAY  (always present from startup)
    # ═══════════════════════════════════════════════════════════════════

    def _start_tray(self):
        """Spawn system tray icon immediately at startup — always visible."""
        #image = Image.new('RGB', (64, 64), color=(245, 166, 35))
        # Create a 64x64 transparent canvas
        image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        draw = Image.new('RGBA', (64, 64))
        
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)

        # Theme Colors matching the TapAccumulator aesthetic
        c_amber = (245, 166, 35, 255)   # Premium Core Accent
        c_white = (255, 255, 255, 230)  # High-contrast ear tip highlight
        c_dark  = (20, 20, 20, 255)     # Deep contrast speaker mesh

        # 1. Draw the downward earbud stem
        draw.rounded_rectangle([32, 26, 40, 56], radius=4, fill=c_amber)

        # 2. Draw the bulbous pod head shell
        draw.ellipse([20, 12, 44, 34], fill=c_amber)

        # 3. Draw the silicone ear-tip sticking out to the left
        draw.rounded_rectangle([12, 16, 22, 30], radius=5, fill=c_white)

        # 4. Add a tiny dark acoustic grill accent for depth
        draw.ellipse([24, 18, 30, 24], fill=c_dark)
        
        def toggle_detector(icon, menu_item):
            self.engine.is_active = not self.engine.is_active
            state = "ENABLED" if self.engine.is_active else "DISABLED"
            logger.info(f"Listener toggled: {state}")
            self.show_notification("TapAccumulator",
                                    f"Earbud listener {state.lower()}")
            def update_ui():
                col = C_GREEN if self.engine.is_active else C_RED
                txt = "LISTENER ACTIVE" if self.engine.is_active else "LISTENER PAUSED"
                self.status_label.configure(text=txt, text_color=col)
                self._dot.configure(text_color=col)
            self.after(0, update_ui)

        def reclaim_focus(icon, menu_item):
            """Re-register our SMTC session to bring media focus back to us."""
            def _do():
                try:
                    self.engine._reregister_session()
                    self.show_notification(
                        "TapAccumulator",
                        "Media focus reclaimed — button events routing to TapAccumulator.")
                    logger.info("[TRAY] Media focus reclaimed via tray action")
                except Exception as e:
                    logger.warning(f"[TRAY] Reclaim failed: {e}")
            threading.Thread(target=_do, daemon=True, name="TrayReclaim").start()

        def show_window(icon, menu_item):
            self.after(0, self.deiconify)

        def exit_app(icon, menu_item):
            icon.stop()
            logger.info("Application exiting")
            os._exit(0)

        menu = pystray.Menu(
            item('Show TapAccumulator', show_window),
            item('Toggle Earbud Listener', toggle_detector),
            item('Reclaim Media Focus', reclaim_focus),
            item('Exit', exit_app),
        )
        self.tray_icon = pystray.Icon(
            "TapAccumulator", image, "TapAccumulator", menu)
        threading.Thread(target=self.tray_icon.run,
                         daemon=True, name="TrayThread").start()

    def hide_to_tray(self):
        """Kept for compatibility — just hide the window."""
        self.withdraw()

    def show_notification(self, title, message):
        try:
            notif = Notification(app_id="TapAccumulator",
                                  title=title, msg=message, duration="short")
            notif.set_audio(audio.Default, loop=False)
            notif.show()
        except Exception as e:
            logger.warning(f"Notification failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _parse_app_id(app_id: str) -> str:
    """Turn a Windows app ID into a short readable name."""
    if "!" in app_id:
        pkg = app_id.split("!")[0]
        parts = pkg.split(".")
        return parts[-2] if len(parts) >= 2 else parts[0]
    elif "\\" in app_id:
        return app_id.split("\\")[-1].replace(".exe", "")
    else:
        return app_id.replace(".exe", "")[:22]


if __name__ == "__main__":
    app = TapAccumulatorApp()
    app.mainloop()