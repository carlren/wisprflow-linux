"""
Transcriber: OpenRouter /audio/transcriptions (JSON base64) + fallback.

Primary:  POST https://openrouter.ai/api/v1/audio/transcriptions
  JSON: { model, input_audio:{data:base64, format}, language?, temperature? }
  Response: { text, usage? }

Fallback: OpenAI-compatible multipart POST to same endpoint for servers that expect multipart.
We also support generic OpenAI endpoint via config http_referer/x_title headers.
"""
import base64
import mimetypes
import os
import time
from pathlib import Path
import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
TIMEOUT = 60

AUDIO_FORMAT_MAP = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
    ".opus": "opus",
    ".m4a": "m4a",
    ".webm": "webm",
    ".mp4": "mp4",
}

def _detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    return AUDIO_FORMAT_MAP.get(ext, "wav")

def _headers(api_key: str, cfg: dict):
    h = {
        "Authorization": f"Bearer {api_key}",
    }
    if cfg.get("http_referer"):
        h["HTTP-Referer"] = cfg["http_referer"]
    if cfg.get("x_title"):
        h["X-Title"] = cfg["x_title"]
    return h

def transcribe_openrouter(wav_path: str, api_key: str, model: str, language=None, temperature=None, cfg=None) -> str:
    cfg = cfg or {}
    fmt = _detect_format(wav_path)
    with open(wav_path, "rb") as f:
        data = f.read()
    if len(data) == 0:
        raise RuntimeError("Empty audio file")
    b64 = base64.b64encode(data).decode("utf-8")

    payload = {
        "model": model,
        "input_audio": {
            "data": b64,
            "format": fmt,
        },
    }
    if language:
        payload["language"] = language
    if temperature is not None:
        payload["temperature"] = temperature

    headers = _headers(api_key, cfg)

    # First try JSON base64 endpoint (OpenRouter native) — with retry for transient DNS/network blips
    resp = None
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.post(OPENROUTER_URL, json=payload, headers=headers, timeout=TIMEOUT)
            break
        except requests.RequestException as e:
            last_exc = e
            # is transient? NameResolutionError, ConnectionError, Timeout are all RequestException
            is_transient = True  # all RequestException here is transient (DNS/conn)
            if attempt < 2 and is_transient:
                wait = 1.0 * (2 ** attempt)  # 1s, 2s
                print(f"[wispr] OpenRouter network error (attempt {attempt+1}/3): {e} — retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Network error contacting OpenRouter after {attempt+1} attempts: {e} (check internet/DNS, will retry next toggle)") from e
    if resp is None:
        raise RuntimeError(f"Network error contacting OpenRouter after 3 attempts: {last_exc}") from last_exc

    if resp.status_code == 200:
        j = resp.json()
        text = j.get("text")
        if text is None:
            # some providers return {"choices":[{"message":{"content": "..."}}]}? try to handle
            # but for /audio/transcriptions spec it should be text
            raise RuntimeError(f"Unexpected response shape: {j}")
        return text.strip()

    # If JSON fails with 400/415, try multipart fallback (for custom endpoints or older OpenRouter)
    # Also try to surface error body
    err_text = ""
    try:
        err_text = resp.text[:2000]
    except Exception:
        pass

    # Fallback: multipart like OpenAI whisper
    # Only attempt if status suggests bad request / unsupported
    if resp.status_code in (400, 415, 422, 404):
        try:
            with open(wav_path, "rb") as f:
                files = {
                    "file": (os.path.basename(wav_path), f, "audio/wav"),
                }
                data_form = {"model": model}
                if language:
                    data_form["language"] = language
                if temperature is not None:
                    data_form["temperature"] = str(temperature)
                # need to re-read? requests will stream
                resp2 = requests.post(OPENROUTER_URL, headers=headers, files=files, data=data_form, timeout=TIMEOUT)
            if resp2.status_code == 200:
                j2 = resp2.json()
                txt = j2.get("text")
                if txt is not None:
                    return txt.strip()
                # fallback: maybe OpenAI verb
                err_text += f"\nFallback response: {resp2.text[:2000]}"
            else:
                err_text += f"\nFallback {resp2.status_code}: {resp2.text[:2000]}"
        except Exception as e:
            err_text += f"\nFallback exception: {e}"

    # Map common errors
    if resp.status_code == 401:
        raise RuntimeError(f"OpenRouter auth failed (401). Check API key. Body: {err_text}")
    if resp.status_code == 402:
        raise RuntimeError(f"OpenRouter payment required / credits exhausted (402). Body: {err_text}")
    if resp.status_code == 429:
        raise RuntimeError(f"Rate limited (429). Body: {err_text}")
    raise RuntimeError(f"Transcription failed {resp.status_code}: {err_text}")

def transcribe(wav_path: str, cfg: dict) -> str:
    api_key = (cfg.get("api_key") or "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OpenRouter API key. Run `wisprflow config --api-key sk-or-...` or set OPENROUTER_API_KEY.")
    model = cfg.get("model") or "openai/gpt-4o-transcribe"
    lang = cfg.get("language")
    if lang == "":
        lang = None
    temp = cfg.get("temperature")
    return transcribe_openrouter(wav_path, api_key, model, language=lang, temperature=temp, cfg=cfg)
