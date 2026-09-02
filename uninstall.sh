#!/usr/bin/env bash
set -e
echo "Uninstalling WisprFlow..."
systemctl --user disable --now wisprflow 2>/dev/null || true
rm -f ~/.config/systemd/user/wisprflow.service
systemctl --user daemon-reload 2>/dev/null || true
pip uninstall -y wisprflow-linux 2>/dev/null || pip3 uninstall -y wisprflow-linux 2>/dev/null || true
rm -f ~/.local/bin/wisprflow
echo "Removing cache..."
rm -rf ~/.cache/wisprflow
read -p "Remove config ~/.config/wisprflow ? [y/N] " ans
if [[ "$ans" =~ ^[Yy]$ ]]; then
  rm -rf ~/.config/wisprflow
  echo "Config removed."
else
  echo "Left ~/.config/wisprflow"
fi
echo "Done."
