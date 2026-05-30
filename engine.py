# =====================================================================
#  TAPACCUMULATOR — engine.py
# =====================================================================

import os
import time
import wave
import tempfile
import asyncio
import threading
import subprocess
import logging

import winsdk.windows.media.playback as wmp
import winsdk.windows.media as wm
import winsdk.windows.media.core as wmc_media
import winsdk.windows.media.control as wmctl
import winsdk.windows.foundation as wf
from pynput import keyboard as pynput_keyboard, mouse as pynput_mouse

logger = logging.getLogger(__name__)


class TapEngine(threading.Thread):
    def __init__(self, data_manager, ui_callback=None):
        super().__init__(daemon=True, name="TapEngineLoop")
        self.data = data_manager
        self.is_active = True
        self.loop = None
        self.queue = None
        self.ui_callback = ui_callback

        self.player = None
        self.smtc = None

        self._session_manager = None
        self._hooked_session = None
        self._playback_token = None
        self._session_token = None
        self._k_listener = None
        self._m_listener = None

        self._last_input_time = time.time()
        self._last_tap_time = 0.0

        self._start_time = time.time()
        self._startup_grace = 5.0  # Protecting against Windows boot storm triggers

        self._mode = "mix"
        self._is_hooked = False
        self._hook_threshold = 0.22
        self._tap_debounce_ms = 280

    # ====================== BASICS ======================
    def _generate_silent_wav(self):
        path = os.path.join(tempfile.gettempdir(), "tap_accumulator_silence.wav")
        if not os.path.exists(path):
            with wave.open(path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(b'\x00' * 88200)
        return path

    def _cb(self, event, *args):
        if self.ui_callback:
            try:
                self.ui_callback(event, *args)
            except: pass

    def _push_tap(self):
        if self.loop and self.loop.is_running() and self.queue:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, 0)

    def _stamp_input(self):
        self._last_input_time = time.time()

    def _should_ignore(self, verbose=False):
        """Unified filtering engine across all operational capture states."""
        now = time.time()
        
        # 1. Boot Storm Protection
        if (now - self._start_time) < self._startup_grace:
            if verbose:
                remaining = self._startup_grace - (now - self._start_time)
                logger.warning(f"[GUARD] Trigger locked: System initializing ({remaining:.1f}s left)")
            return True
            
        # 2. Hardware Signal Debounce Protection
        if (now - self._last_tap_time) < (self._tap_debounce_ms / 1000.0):
            if verbose:
                logger.debug("[GUARD] Trigger locked: Hardware bounce protection active")
            return True

        # 3. CRITICAL HUMAN INPUT FILTER SHIELD (Applies universally to all states)
        input_delta = now - self._last_input_time
        if input_delta < self._hook_threshold:
            if verbose:
                logger.info(f"[FILTER LOCKOUT] Intercepted local hardware input device loop (Delta: {input_delta:.3f}s)")
                self._cb("hook_filtered", "local", input_delta)
            return True
            
        return False

    # ====================== INPUT TRACKING SHIELD ======================
    def _start_input_tracker(self):
        def on_input(*_):
            self._stamp_input()

        # Keyboard press/release vectors
        self._k_listener = pynput_keyboard.Listener(on_press=on_input, on_release=on_input)
        
        # Comprehensive mouse vector analysis: clicks, physical coordinate movement, and scrolling wheel ticks
        self._m_listener = pynput_mouse.Listener(
            on_click=lambda x, y, b, p: self._stamp_input(),
            on_move=lambda x, y: self._stamp_input(),
            on_scroll=lambda x, y, dx, dy: self._stamp_input()
        )

        for lst in (self._k_listener, self._m_listener):
            lst.daemon = True
            lst.start()

    def _setup_our_session(self):
        self.player = wmp.MediaPlayer()
        self.player.is_looping_enabled = True
        uri = f"file:///{self._generate_silent_wav().replace(os.sep, '/')}"
        self.player.source = wmc_media.MediaSource.create_from_uri(wf.Uri(uri))
        self.smtc = self.player.system_media_transport_controls
        self.smtc.is_play_enabled = True
        self.smtc.is_pause_enabled = True
        self.smtc.playback_status = wm.MediaPlaybackStatus.PLAYING
        self.smtc.add_button_pressed(self._on_smtc_button)
        self.player.play()
        logger.info("[SESSION] SMTC virtual endpoint registered")

    def _on_smtc_button(self, sender, args):
        if not self.is_active or self._is_hooked:
            return
        # Routed directly into unified filter tracking loop
        if self._should_ignore(verbose=True):
            return
        self._register_tap()

    # ====================== HOOK INTERCEPT ROUTINES ======================
    def _on_playback_changed(self, sender, args):
        if not self.is_active:
            return
        try:
            status = sender.get_playback_info().playback_status
            if status in (4, 5):  # Target status updates: Pause/Stop state loops
                # Routed directly into unified filter tracking loop
                if self._should_ignore(verbose=True):
                    return
                self._register_tap()
        except:
            pass

    def _register_tap(self):
        logger.info("[TAP] Confirmed hardware signal registered")
        self._cb("tap_detected", 0)
        self._last_tap_time = time.time()
        self._push_tap()

    # ====================== SESSION WATCHER ======================
    def _attach_hook(self, session):
        self._detach_hook()
        try:
            self._hooked_session = session
            self._playback_token = session.add_playback_info_changed(self._on_playback_changed)
            self._is_hooked = True
            app_id = session.source_app_user_model_id
            logger.info(f"[HOOK] Target session intercept bound → {app_id}")
            self._cb("hook_attached", app_id)
            return True
        except:
            self._is_hooked = False
            return False

    def _detach_hook(self):
        if self._hooked_session and self._playback_token:
            try:
                self._hooked_session.remove_playback_info_changed(self._playback_token)
            except: pass
        self._hooked_session = None
        self._playback_token = None
        self._is_hooked = False

    def _on_session_changed(self, sender, args):
        self._cb("session_changed")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._respond_to_session_change(), self.loop)

    async def _respond_to_session_change(self):
        await asyncio.sleep(0.15)
        try:
            new_session = self._session_manager.get_current_session()
            if new_session:
                app_id = getattr(new_session, 'source_app_user_model_id', '')
                if "TapAccumulator" in app_id or not app_id:
                    self._detach_hook()
                    self._cb("mix_own_session")
                    return
                if not self._attach_hook(new_session):
                    threading.Thread(target=self._reregister_session, daemon=True).start()
        except: pass

    async def _setup_session_watcher(self):
        try:
            self._session_manager = await wmctl.GlobalSystemMediaTransportControlsSessionManager.request_async()
            self._session_token = self._session_manager.add_current_session_changed(self._on_session_changed)

            current = self._session_manager.get_current_session()
            if current and "TapAccumulator" not in getattr(current, 'source_app_user_model_id', ''):
                self._attach_hook(current)
        except Exception as e:
            logger.error(f"Watcher init failed: {e}")

    def _reregister_session(self):
        try:
            if self.smtc:
                self.smtc.playback_status = wm.MediaPlaybackStatus.PLAYING
                self.player.play()
                self._cb("focus_recovered")
        except: pass

    # ====================== ACCUMULATOR ======================
    async def async_worker(self):
        self.queue = asyncio.Queue()
        click_count = 0

        while True:
            timeout = float(self.data.load('settings').get('timeout', 1.5))

            await self.queue.get()
            click_count = 1
            self._cb("tap_count_started", click_count)
            self.queue.task_done()

            while True:
                try:
                    await asyncio.wait_for(self.queue.get(), timeout=timeout)
                    click_count += 1
                    self._cb("tap_added", click_count)
                    self.queue.task_done()
                except asyncio.TimeoutError:
                    break

            if click_count > 0:
                self.execute_action(click_count)
            click_count = 0
            self._cb("timeout_reset")

    def execute_action(self, taps):
        macros = self.data.load('macros')
        if str(taps) in macros:
            macro = macros[str(taps)]
            if macro.get('type') == 'shell':
                try:
                    subprocess.Popen(macro['command'], shell=True)
                    self._cb("command_executed", taps, macro['command'])
                    speech_text = macro.get('speech_text', '').strip()
                    if speech_text:
                        threading.Thread(
                            target=self._execute_speech_pipeline,
                            args=(speech_text,),
                            daemon=True,
                            name="TapAccumulatorTTSWorker"
                        ).start()
                except Exception as e:
                    logger.error(f"Exec error: {e}")
        else:
            self._cb("no_macro", taps)

    # ====================== SAFE TTS EXECUTION WINDOW ======================
    def _execute_speech_pipeline(self, text):
        try:
            import pythoncom
            from win32com.client import Dispatch
            s = self.data.load('settings')
            pythoncom.CoInitialize()
            try:
                tts_engine = Dispatch("SAPI.SpVoice")
                voices = tts_engine.GetVoices()
                voice_index = int(s.get('tts_voice_index', 0))
                if voice_index < voices.Count:
                    tts_engine.Voice = voices.Item(voice_index)
                tts_engine.Rate   = int(s.get('tts_rate', 0))
                tts_engine.Volume = int(s.get('tts_volume', 100))
                tts_engine.Speak(text, 2)
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            logger.error(f"TTS Thread Failure: {e}")

    # ====================== RUN ======================
    def run(self):
        s = self.data.load('settings')
        self._mode            = s.get('capture_mode',    'mix')
        self._hook_threshold  = float(s.get('hook_threshold',  0.22))
        self._tap_debounce_ms = int(s.get('tap_debounce_ms',   280))
        self._startup_grace   = float(s.get('startup_grace',   5.0))

        self._start_time = time.time()

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        logger.info("[SYS] Initializing Core Audio Layer...")
        self._cb("engine_initializing", "Initializing virtual audio pipeline...")
        self._setup_our_session()

        logger.info("[SYS] Hooking human feedback input filters...")
        self._cb("engine_initializing", "Starting keyboard/mouse tracking filters...")
        self._start_input_tracker()

        if self._mode in ("hook", "mix"):
            logger.info("[SYS] Mounting WinRT Global Session Watcher...")
            self._cb("engine_initializing", "Binding system media hook architecture...")
            self.loop.create_task(self._setup_session_watcher())

        self.loop.create_task(self.async_worker())

        async def startup_countdown():
            total_steps = int(self._startup_grace)
            for remaining in range(total_steps, 0, -1):
                self._cb("engine_initializing", f"Shedding system startup ghosts... {remaining}s left")
                await asyncio.sleep(1)
            logger.info("[SYS] Engine online — READY to process active hardware taps")
            self._cb("engine_ready", self._mode)

        self.loop.create_task(startup_countdown())
        self.loop.run_forever()