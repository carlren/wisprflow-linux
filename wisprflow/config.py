"""
Config management for wisprflow.
~/.config/wisprflow/config.json  (600 perms)
Env overrides: OPENROUTER_API_KEY, WISPRFLOW_MODEL
"""
import json
import os
import stat
from pathlib import Path

DEFAULT_MODEL = "openai/gpt-4o-transcribe"
DEFAULT_HOTKEY = "ctrl+shift"  # left ctrl + left shift
DEFAULT_IDLE_SILENCE_MS = 3 * 60 * 1000
DEFAULT_MAX_RECORD_SECONDS = 0  # 0 disables the fixed recording-length cap

CONFIG_DIR = Path.home() / ".config" / "wisprflow"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "api_key": "",
    "model": DEFAULT_MODEL,
    "language": None,          # ISO 639-1 or None for auto
    "hotkey": DEFAULT_HOTKEY,
    "sample_rate": 16000,
    "channels": 1,
    "auto_paste": True,
    "overlay_enabled": True,
    "sound_enabled": True,
    "max_record_seconds": DEFAULT_MAX_RECORD_SECONDS,
    "temperature": None,
    "paste_mode": "auto",  # auto | gui | terminal | primary — auto detects focused window, gui=Ctrl+V, terminal=Ctrl+Shift+V/Shift+Insert
    "vad_enabled": True,
    # VAD is only an abandoned-recording safety net. It must not act as
    # sentence endpointing, so ordinary pauses should never stop recording.
    "vad_silence_ms": DEFAULT_IDLE_SILENCE_MS,
    "vad_threshold": 500,  # RMS threshold for int16 (lower = more sensitive, 300-800 typical)
    "vad_initial_grace_ms": 800,  # grace before VAD can trigger at start (ms)
    "http_referer": "",  # optional for OpenRouter rankings
    "x_title": "wisprflow-linux",
}

def _load_file():
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    file_cfg = _load_file()
    cfg.update({k: v for k, v in file_cfg.items() if k in DEFAULT_CONFIG or k == "api_key"})
    # env overrides
    if os.environ.get("OPENROUTER_API_KEY"):
        cfg["api_key"] = os.environ["OPENROUTER_API_KEY"].strip()
    if os.environ.get("WISPRFLOW_MODEL"):
        cfg["model"] = os.environ["WISPRFLOW_MODEL"].strip()
    # also support WISPR_API_KEY etc
    if not cfg["api_key"] and os.environ.get("WISPR_API_KEY"):
        cfg["api_key"] = os.environ["WISPR_API_KEY"].strip()
    return cfg

def save_config(updates: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current = _load_file()
    # merge updates onto file (keep unknown keys)
    current.update(updates)
    # also ensure we persist defaults that were missing? only write supplied + existing
    # but ensure api_key not written as env override only
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    return load_config()

def get_api_key(cfg=None) -> str:
    if cfg is None:
        cfg = load_config()
    return (cfg.get("api_key") or "").strip()

def redact_key(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "***"
    return key[:7] + "…" + key[-4:]
