"""TradeVMx desktop :: local notification extras.

Both channels are strictly optional and imported lazily:

* toasts use ``plyer`` (a tiny cross-platform wrapper),
* voice uses ``pyttsx3`` (SAPI5 on Windows, NSSpeechSynthesizer on macOS,
  espeak on Linux).

If the package is missing (or the platform has no speech backend) the call is
logged at debug level and the desk carries on - the in-app feed and Telegram
alerts are unaffected.
"""

from __future__ import annotations

import html
import logging
import re
import threading

LOGGER = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_SPEAK_LOCK = threading.Lock()


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


class LocalNotifier:
    """Toasts and speech for the desktop experience."""

    def __init__(self, *, toast: bool = True, speak: bool = False) -> None:
        self.toast_enabled = toast
        self.speak_enabled = speak
        self._toast_warned = False
        self._speak_warned = False

    def configure(self, *, toast: bool, speak: bool) -> None:
        self.toast_enabled = toast
        self.speak_enabled = speak

    def announce(self, title: str, message: str = "") -> None:
        headline = _plain(title)[:120]
        body = _plain(message)[:240]
        if self.toast_enabled:
            threading.Thread(
                target=self._toast, args=(headline, body), name="toast", daemon=True
            ).start()
        if self.speak_enabled:
            threading.Thread(
                target=self._speak, args=(headline, body), name="speak", daemon=True
            ).start()

    def _toast(self, title: str, message: str) -> None:
        try:
            from plyer import notification  # type: ignore[import-not-found]

            notification.notify(
                title=title or "TradeVMx",
                message=message or "Alert",
                app_name="TradeVMx",
                timeout=8,
            )
        except Exception as exc:  # noqa: BLE001 - optional dependency / no backend
            if not self._toast_warned:
                self._toast_warned = True
                LOGGER.debug("Desktop toasts unavailable: %s", exc)

    def _speak(self, title: str, message: str) -> None:
        try:
            import pyttsx3  # type: ignore[import-not-found]

            with _SPEAK_LOCK:
                engine = pyttsx3.init()
                engine.setProperty("rate", 175)
                engine.say(f"{title}. {message}"[:320])
                engine.runAndWait()
                engine.stop()
        except Exception as exc:  # noqa: BLE001 - optional dependency / no backend
            if not self._speak_warned:
                self._speak_warned = True
                LOGGER.debug("Voice announcements unavailable: %s", exc)
