"""
Transcriber: OpenRouter /audio/transcriptions (JSON base64) + local OpenAI-compatible server.

Backends (cfg["backend"]):
  openrouter: POST https://openrouter.ai/api/v1/audio/transcriptions
    JSON: { model, input_audio:{data:base64, format}, language?, temperature?, prompt? }
    Response: { text, usage? }
    Fallback: OpenAI-compatible multipart POST to same endpoint.
  local: POST {local_url}/v1/audio/transcriptions
    multipart (OpenAI Whisper style): file, model, language?, prompt?
    Response: { text }
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

def transcribe_openrouter(wav_path: str, api_key: str, model: str, language=None, temperature=None, prompt=None, cfg=None) -> str:
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
    if prompt:
        payload["prompt"] = prompt

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
                if prompt:
                    data_form["prompt"] = prompt
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

LOCAL_TIMEOUT = 120  # local GPU inference can take longer on first (warmup) call

def transcribe_local(wav_path: str, base_url: str, model: str, language=None, prompt=None, cfg=None) -> str:
    """Transcribe via a local OpenAI-compatible STT server.

    POST {base_url}/v1/audio/transcriptions  (multipart, like OpenAI Whisper API)
    Response: {"text": "..."} — servers that don't know a field simply ignore it.
    """
    cfg = cfg or {}
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError(
            "Local backend selected but local_url is not set. "
            "Run `wisprflow config --local-url http://127.0.0.1:PORT` first."
        )
    if not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
        raise RuntimeError("Empty or missing audio file")
    url = base_url + "/v1/audio/transcriptions"

    resp = None
    for attempt in range(3):
        try:
            with open(wav_path, "rb") as f:
                files = {"file": (os.path.basename(wav_path), f, "audio/wav")}
                data_form = {"model": model}
                if language:
                    data_form["language"] = language
                if prompt:
                    data_form["prompt"] = prompt
                resp = requests.post(url, files=files, data=data_form, timeout=LOCAL_TIMEOUT)
            break
        except requests.RequestException as e:
            if attempt < 2:
                wait = 1.0 * (2 ** attempt)
                print(f"[wispr] local STT network error (attempt {attempt+1}/3): {e} — retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"Network error contacting local STT at {url} after {attempt+1} attempts: {e} "
                f"(is the local service running? check `wisprflow diagnose`)"
            ) from e
    if resp is None:
        raise RuntimeError(f"No response from local STT at {url}")

    if resp.status_code == 200:
        try:
            j = resp.json()
        except Exception as e:
            raise RuntimeError(f"Local STT returned non-JSON 200: {resp.text[:500]!r}") from e
        text = j.get("text")
        if text is None:
            raise RuntimeError(f"Unexpected local STT response shape: {j}")
        return text.strip()

    err_text = ""
    try:
        err_text = resp.text[:2000]
    except Exception:
        pass
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Local STT auth failed ({resp.status_code}). Body: {err_text}")
    raise RuntimeError(f"Local transcription failed {resp.status_code}: {err_text}")

def transcribe(wav_path: str, cfg: dict) -> str:
    backend = (cfg.get("backend") or "openrouter").strip().lower()
    lang = cfg.get("language")
    if lang == "":
        lang = None
    prompt = (cfg.get("prompt") or "").strip() or None

    if backend == "local":
        model = cfg.get("local_model") or "qwen3-asr-1.7b"
        return transcribe_local(wav_path, cfg.get("local_url"), model, language=lang, prompt=prompt, cfg=cfg)

    if backend != "openrouter":
        raise RuntimeError(f"Unknown backend {backend!r}: choose 'openrouter' or 'local' (`wisprflow config --backend ...`).")

    api_key = (cfg.get("api_key") or "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OpenRouter API key. Run `wisprflow config --api-key sk-or-...` or set OPENROUTER_API_KEY.")
    model = cfg.get("model") or "openai/gpt-4o-transcribe"
    temp = cfg.get("temperature")
    return transcribe_openrouter(wav_path, api_key, model, language=lang, temperature=temp, prompt=prompt, cfg=cfg)
