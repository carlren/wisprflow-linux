# WisprFlow for Linux (Ubuntu)

A lightweight, WisprFlow-like push-to-talk speech-to-text app for Ubuntu/Linux — press a key, speak, press again, and text appears at your cursor.

This is an independent community project and is not affiliated with Wispr Flow.

Powered by [OpenRouter](https://openrouter.ai) — bring your own API key and use any STT model (default `openai/gpt-4o-transcribe`).

## Features

- **Push-to-toggle** global hotkey (default `Ctrl+Shift`) — press once to start, again to stop & transcribe
- **Works where you type** — auto-pastes into the focused app via clipboard + `Ctrl+V` simulation
- **OpenRouter STT** — uses `POST /audio/transcriptions` with base64 audio (JSON), supports `openai/gpt-4o-transcribe` and friends
- **Floating overlay** — shows Listening / Transcribing / Done states
- **System tray** (Ayatana/AppIndicator)
- **Wayland + X11** — auto-detects session and picks `wtype` / `xdotool` / `ydotool` + `wl-clipboard` / `xclip`
- **Systemd user service** — auto-start on login
- **No browser needed** — native GTK, no Electron

## Quick Start

```bash
# 1. Install system deps + Python deps
chmod +x install.sh
./install.sh

# 2. Set your OpenRouter API key (get one at https://openrouter.ai/keys)
wisprflow config --api-key sk-or-v1-...

# Optional: pick model / language
wisprflow config --model openai/gpt-4o-transcribe --language en

# 3. Start the daemon
wisprflow daemon &

# 4. Bind a GNOME shortcut to `wisprflow toggle` (recommended for Wayland/GNOME)
#   Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → +
#   Name: Wispr Toggle   Command: wisprflow toggle   Shortcut: Ctrl+Shift
```

Now press `Ctrl+Shift`, speak, then press it again — text appears at your cursor.

## Installation (manual)

```bash
sudo apt update
sudo apt install -y \
  python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
  portaudio19-dev xdotool wl-clipboard wtype \
  libportaudio2 python3-pip

pip install --user -r requirements.txt

# Make CLI available
pip install --user -e .
# or: ln -s $(pwd)/wisprflow/__main__.py ~/.local/bin/wisprflow
```

## Usage

```
wisprflow daemon              # run in foreground (shows overlay + tray)
wisprflow toggle              # toggle recording (call from hotkey)
wisprflow start               # background daemon via systemd (--user)
wisprflow stop
wisprflow status
wisprflow config --show
wisprflow config --api-key KEY --model MODEL --language LANG --hotkey f9
wisprflow diagnose            # check deps, mic, clipboard, session type
wisprflow test-mic            # 3-sec mic test
```

## Configuration

Config file: `~/.config/wisprflow/config.json` (chmod 600)

Env overrides:
- `OPENROUTER_API_KEY` — API key
- `WISPRFLOW_MODEL` — model id

Example `config.json`:
```json
{
  "api_key": "sk-or-v1-...",
  "model": "openai/gpt-4o-transcribe",
  "language": null,
  "hotkey": "ctrl+shift",
  "sample_rate": 16000,
  "channels": 1,
  "auto_paste": true,
  "overlay_enabled": true,
  "sound_enabled": true,
  "max_record_seconds": 0,
  "vad_enabled": true,
  "vad_silence_ms": 180000
}
```

VAD is used only as an abandoned-recording safeguard: three continuous minutes
of silence stops recording. It does not treat short pauses as the end of a
sentence. `max_record_seconds: 0` disables the fixed recording-length cap.

STT models known to work via OpenRouter:
- `openai/gpt-4o-transcribe` (default, best compatibility)
- `openai/gpt-4o-transcribe-turbo`

> If OpenRouter adds `openai/gpt-4o-transcribe` etc. for transcription, you can try them — just set `--model`.

## How It Works

1. Daemon listens on `~/.cache/wisprflow/daemon.sock` + `SIGUSR1` for `toggle`.
2. On toggle → `recorder` starts `sounddevice` stream (16 kHz mono) → purple pulsing overlay.
3. On second toggle → stops, writes WAV to temp, sends to OpenRouter:
   `POST https://openrouter.ai/api/v1/audio/transcriptions`
   ```json
   {"model":"openai/gpt-4o-transcribe","input_audio":{"data":"<base64 wav>","format":"wav"}}
   ```
4. Text response `{"text":"..."}` → `injector` copies to clipboard and simulates `Ctrl+V` at cursor.
   - X11: `xclip`/`xsel` + `xdotool key ctrl+v`
   - Wayland: `wl-copy` + `wtype -M ctrl -k v` / `ydotool`
   - Fallback: `pynput` + `Gtk.Clipboard`

## Troubleshooting

- **No text appears**: Run `wisprflow diagnose`, check `XDG_SESSION_TYPE`. On Wayland ensure `wtype` or `ydotool` is installed and `ydotoold` is running. Try `wtype hello` manually.
- **Mic not working**: `arecord -l` to list devices, `wisprflow test-mic` to test.
- **Hotkey not working**: GNOME Wayland blocks global grabs — bind `wisprflow toggle` to a custom shortcut in Settings instead.
- **API error 401**: Check `wisprflow config --show` (key redacted) and `OPENROUTER_API_KEY`.
- **403 / model not found**: Try `openai/gpt-4o-transcribe`.
- **Paste inserts into wrong window**: The daemon briefly focuses overlay — injector saves/restores clipboard after 300 ms. Disable overlay with `wisprflow config --no-overlay`.

## Systemd

```bash
systemctl --user enable --now wisprflow
systemctl --user status wisprflow
journalctl --user -u wisprflow -f
```

## Uninstall

```bash
./uninstall.sh
# or
systemctl --user disable --now wisprflow
pip uninstall wisprflow-linux -y
rm -rf ~/.config/wisprflow ~/.cache/wisprflow
```

## Security

- The API key is read at runtime from `~/.config/wisprflow/config.json` or
  `OPENROUTER_API_KEY`; it is not part of the source tree. The config file is
  stored with user-only (`0600`) permissions.
- Audio is base64-sent to OpenRouter only when you toggle-stop; temp WAVs are deleted.

## License

[MIT](LICENSE)
