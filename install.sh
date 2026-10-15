#!/usr/bin/env bash
# Ramuzu IPTV Player (ccarneros) - Linux installer
#
# Installs the copy of the app that sits next to this script. Nothing is
# downloaded from Git: what you reviewed is what gets installed.
#
# Usage: bash install.sh [--run] [--desktop]
set -euo pipefail

RUN_AFTER=false
MAKE_DESKTOP=false
for arg in "$@"; do
  case "$arg" in
    --run) RUN_AFTER=true ;;
    --desktop) MAKE_DESKTOP=true ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="$APP_DIR/ramuzu_iptv_player.py"
VENV_DIR="$APP_DIR/.venv"

if [[ ! -f "$APP_PY" ]]; then
  echo "!! Could not find $APP_PY" >&2
  exit 1
fi

echo "==> Installing system packages..."
source /etc/os-release 2>/dev/null || true
ID_ALL="$(echo "${ID:-} ${ID_LIKE:-}" | tr '[:upper:]' '[:lower:]')"

if [[ "$ID_ALL" =~ (debian|ubuntu|mint|pop) ]]; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip python3-pyqt5 qtwayland5 || true
  sudo apt-get install -y vlc || true
elif [[ "$ID_ALL" =~ (fedora|rhel|centos|rocky|alma) ]]; then
  sudo dnf install -y python3 python3-pip python3-qt5 qt5-qtwayland || true
  sudo dnf install -y vlc || echo "   VLC is not in your repositories (Fedora needs RPM Fusion). Install a media player manually."
elif [[ "$ID_ALL" =~ (arch|manjaro|endeavouros|artix) ]]; then
  sudo pacman -S --needed --noconfirm python python-pip python-pyqt5 qt5-wayland vlc || true
elif [[ "$ID_ALL" =~ (suse) ]]; then
  sudo zypper install -y python3 python3-pip python3-qt5 libqt5-qtwayland vlc || true
else
  echo "   Unknown distribution: install Python 3, PyQt5 and VLC with your package manager."
fi

echo "==> Creating Python virtual environment..."
# --system-site-packages lets the venv fall back to the distro's PyQt5.
python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip

echo "==> Installing Python requirements..."
if ! "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"; then
  echo "   pip could not install PyQt5; using the system package instead."
  grep -viE '^pyqt5' "$APP_DIR/requirements.txt" | "$VENV_DIR/bin/python" -m pip install -r /dev/stdin
fi

echo "==> Creating launcher (run_ramuzu.sh)..."
cat > "$APP_DIR/run_ramuzu.sh" << 'EOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/ramuzu_iptv_player.py" "$@"
EOF
chmod +x "$APP_DIR/run_ramuzu.sh"

if $MAKE_DESKTOP; then
  echo "==> Adding menu entry..."
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/ramuzu-iptv-player.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Ramuzu IPTV Player
Comment=Lightweight Xtream Codes / M3U player
Exec="$APP_DIR/run_ramuzu.sh"
Icon=multimedia-video-player
Terminal=false
Categories=AudioVideo;Player;
EOF
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

echo "==> Done."
echo "    Run: $APP_DIR/run_ramuzu.sh"
echo "    Saved passwords use your desktop keyring (GNOME Keyring / KWallet)."
if $RUN_AFTER; then
  exec "$APP_DIR/run_ramuzu.sh"
fi
