#!/usr/bin/env bash
set -e
echo "=== WisprFlow for Linux — Installer ==="
echo

# System deps
echo "[1/5] Installing system packages (needs sudo)..."
sudo apt update
sudo apt install -y \
  python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
  portaudio19-dev libportaudio2 \
  xdotool wl-clipboard wtype \
  libnotify-bin \
  alsa-utils \
  pulseaudio-utils || pipewire-pulse || true

# Python deps
echo
echo "[2/5] Installing Python packages..."
PIP_USER="pip install --user --break-system-packages"
if ! pip install --help 2>&1 | grep -q "break-system-packages"; then
  PIP_USER="pip install --user"
fi
if command -v pipx >/dev/null 2>&1; then
  echo "pipx found — installing via pip --user"
fi
$PIP_USER -r requirements.txt || pip3 install --user --break-system-packages -r requirements.txt

# Editable install so `wisprflow` command exists
echo
echo "[3/5] Installing wisprflow CLI..."
$PIP_USER -e . || pip3 install --user --break-system-packages -e .

# Ensure ~/.local/bin in PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  echo "Added ~/.local/bin to PATH in ~/.bashrc (restart shell or: export PATH=\"\$HOME/.local/bin:\$PATH\")"
  export PATH="$HOME/.local/bin:$PATH"
fi

# Systemd user service
echo
echo "[4/5] Installing systemd user service..."
mkdir -p ~/.config/systemd/user
cp systemd/wisprflow.service ~/.config/systemd/user/wisprflow.service
systemctl --user daemon-reload || true
echo "To enable autostart: systemctl --user enable --now wisprflow"

# Config placeholder
echo
echo "[5/5] Checking config..."
mkdir -p ~/.config/wisprflow
if [ ! -f ~/.config/wisprflow/config.json ]; then
  echo '{"api_key":"","model":"openai/gpt-4o-transcribe","language":null,"hotkey":"f9","sample_rate":16000,"channels":1,"auto_paste":true,"overlay_enabled":true}' > ~/.config/wisprflow/config.json
  chmod 600 ~/.config/wisprflow/config.json
  echo "Created ~/.config/wisprflow/config.json (chmod 600)"
fi

echo
echo "=== Done ==="
echo
echo "Next steps:"
echo "  1. wisprflow config --api-key sk-or-v1-...   # from https://openrouter.ai/keys"
echo "  2. wisprflow diagnose                         # check mic, clipboard, deps"
echo "  3. wisprflow daemon &                         # run once to test"
echo "  4. Press F9, speak, press F9 again -> text at cursor"
echo
echo "For Wayland/GNOME, bind a shortcut:"
echo "  Settings -> Keyboard -> Custom Shortcuts -> +"
echo "  Name: Wispr Toggle   Command: $HOME/.local/bin/wisprflow toggle   Shortcut: F9"
echo
echo "Or run: wisprflow install-hotkey"
echo
echo "Enable autostart: systemctl --user enable --now wisprflow"
