# iot_integrations.py
import os, time, threading, requests
from dataclasses import dataclass, field
from typing import Dict, Any, Optional

THINGSPEAK_URL = "https://api.thingspeak.com/update"

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name, default)
    if val is not None:
        val = val.strip()
    return val

@dataclass
class ThingSpeakClient:
    write_key: str
    min_interval_sec: int = 15
    _last_push: float = field(default=0.0, init=False)

    def push(self, **fields) -> bool:
        """
        Pushes up to 8 fields: field1..field8 (ThingSpeak constraint).
        Skips if called faster than min_interval_sec.
        """
        now = time.time()
        if now - self._last_push < self.min_interval_sec:
            return False  # rate limited

        payload = {"api_key": self.write_key}
        # Map arbitrary kwargs to ThingSpeak fields deterministically
        # e.g. temp -> field1, humidity -> field2, etc.
        # Adjust mapping below to match your channel’s field naming.
        mapping = [
            "temp_c",       # field1
            "humidity",     # field2
            "water_height", # field3
            "soil_dry",     # field4 (1/0)
            "pir_active",   # field5 (1/0)
            "distance_cm",  # field6
            "motor_on",     # field7 (1/0)
            "reserved"      # field8
        ]
        for idx, key in enumerate(mapping, start=1):
            if key in fields and fields[key] is not None:
                payload[f"field{idx}"] = fields[key]

        try:
            r = requests.get(THINGSPEAK_URL, params=payload, timeout=10)
            self._last_push = now
            return r.status_code == 200 and r.text.strip().isdigit()
        except requests.RequestException:
            return False

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        try:
            r = requests.post(f"{self.base}/sendMessage",
                              data={"chat_id": self.chat_id, "text": text},
                              timeout=10)
            return r.ok
        except requests.RequestException:
            return False

class TelegramBotThread(threading.Thread):
    """
    Minimal polling bot to handle /status (and a few simple commands).
    """
    def __init__(self, token: str, chat_id: str, get_status_cb):
        super().__init__(daemon=True)
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.get_status_cb = get_status_cb
        self._running = True
        self._offset = 0

    def run(self):
        while self._running:
            try:
                r = requests.get(f"{self.base}/getUpdates",
                                 params={"timeout": 20, "offset": self._offset},
                                 timeout=30)
                if not r.ok:
                    time.sleep(2); continue
                data = r.json()
                for upd in data.get("result", []):
                    self._offset = max(self._offset, upd["update_id"] + 1)
                    msg = upd.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(self.chat_id):  # ignore other chats
                        continue
                    text = (msg.get("text") or "").strip().lower()
                    if text in ("/start", "/help"):
                        self._send("Hi! Try /status to see current readings.")
                    elif text == "/status":
                        self._send(self.get_status_cb())
                    elif text == "/motor_on":
                        self._send("Use keypad/logic; remote motor control is disabled in this bot.")
                    elif text == "/motor_off":
                        self._send("Use keypad/logic; remote motor control is disabled in this bot.")
            except requests.RequestException:
                time.sleep(3)

    def stop(self):
        self._running = False

    def _send(self, text: str):
        try:
            requests.post(f"{self.base}/sendMessage",
                          data={"chat_id": self.chat_id, "text": text},
                          timeout=10)
        except requests.RequestException:
            pass

@dataclass
class AlertGate:
    """
    Sends a Telegram alert once per state transition with cooldown.
    """
    tg: Telegram
    cooldown_sec: int = 60
    last_sent: Dict[str, float] = field(default_factory=dict)
    last_state: Dict[str, Any] = field(default_factory=dict)

    def maybe_send(self, key: str, state: Any, msg_on_change: str):
        now = time.time()
        changed = (self.last_state.get(key) != state)
        recently = (now - self.last_sent.get(key, 0)) < self.cooldown_sec
        if changed and not recently:
            if self.tg.send(msg_on_change):
                self.last_sent[key] = now
            self.last_state[key] = state
