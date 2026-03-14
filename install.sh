#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  xnxx-scraper-pi — One-Time Setup Script
#  Supports: Raspberry Pi OS Bullseye (32/64-bit)
#            Raspberry Pi OS Bookworm  (32/64-bit)
#  Run once: bash install.sh
# ═══════════════════════════════════════════════════════════
set -e

BLUE='\033[1;34m'; GREEN='\033[1;32m'; RED='\033[1;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { echo -e "${BLUE}▶  $*${NC}"; }
ok()   { echo -e "${GREEN}✓  $*${NC}"; }
warn() { echo -e "${YELLOW}⚠  $*${NC}"; }
die()  { echo -e "${RED}✗  $*${NC}"; exit 1; }

SCRIPT_DIR="$(dirname "$(realpath "$0")")"

echo ""
echo -e "${BLUE}════════════════════════════════════════════${NC}"
echo -e "${BLUE} xnxx-scraper-pi — Setup v3              ${NC}"
echo -e "${BLUE}════════════════════════════════════════════${NC}"
echo ""

# ── Detect OS / Bookworm ──────────────────────────────────────────────────────
IS_BOOKWORM=false
if grep -qi "bookworm\|VERSION_CODENAME=bookworm" /etc/os-release 2>/dev/null; then
    IS_BOOKWORM=true
    say "Detected Raspberry Pi OS Bookworm"
else
    say "Detected Raspberry Pi OS Bullseye (or compatible)"
fi

# ── Check Python ──────────────────────────────────────────────────────────────
say "Checking Python version..."
PY=$(python3 --version 2>&1)
echo "    Found: $PY"
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    die "Python 3.8+ required. Found $PY"
fi
ok "Python $PY_MAJOR.$PY_MINOR OK"

PIP_BREAK_FLAG=""
if python3 -m pip install --dry-run pip 2>&1 | grep -qi "externally.managed\|externally managed"; then
    PIP_BREAK_FLAG="--break-system-packages"
    warn "Externally-managed Python detected -- will use --break-system-packages"
elif [ "$IS_BOOKWORM" = true ]; then
    # Belt-and-suspenders: Bookworm always gets the flag even if dry-run didn't trigger
    PIP_BREAK_FLAG="--break-system-packages"
    warn "Bookworm OS -- enabling --break-system-packages as precaution"
fi
[ -z "$PIP_BREAK_FLAG" ] && ok "Standard pip environment (no --break-system-packages needed)"

# ── Update apt (skip if offline) ──────────────────────────────────────────────
say "Updating package list..."
if sudo apt-get update -qq 2>/dev/null; then
    ok "Package list updated"
else
    warn "apt update failed -- continuing anyway (may be offline)"
fi

# ── System dependencies ───────────────────────────────────────────────────────
say "Installing system dependencies..."

# On Bookworm the versioned venv package (e.g. python3.11-venv) is required
# in addition to python3-venv to avoid "ensurepip is not available" errors.
PKGS="python3-pip python3-venv libssl-dev libffi-dev"
if [ "$IS_BOOKWORM" = true ]; then
    PKGS="$PKGS python3.${PY_MINOR}-venv"
fi

for pkg in $PKGS; do
    if dpkg -s "$pkg" &>/dev/null; then
        echo "    $pkg -- already installed"
    else
        sudo apt-get install -y -qq "$pkg" \
            && echo "    $pkg -- installed" \
            || warn "    $pkg -- failed (skipping)"
    fi
done

# ── Optional: mpv for local HDMI playback ────────────────────────────────────
if ! command -v mpv &>/dev/null; then
    warn "mpv not found. Install it for local HDMI playback:"
    warn "  sudo apt install mpv"
else
    ok "mpv already installed"
fi

# ── Create virtual environment ───────────────────────────────────────────────
VENV_DIR="$HOME/xnxx-venv"
say "Creating virtual environment at $VENV_DIR..."
if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists -- skipping creation"
else
    python3 -m venv "$VENV_DIR"
    ok "Virtual environment created"
fi

# Activate venv -- all pip calls from here on are inside the venv.
source "$VENV_DIR/bin/activate"

# ── Install Python packages ───────────────────────────────────────────────────
say "Installing Python packages..."
pip install --upgrade pip --quiet $PIP_BREAK_FLAG

REQ_FILE="$SCRIPT_DIR/requirements.txt"
if [ -f "$REQ_FILE" ]; then
    pip install --quiet $PIP_BREAK_FLAG -r "$REQ_FILE"
    ok "Python packages installed from requirements.txt"
else
    pip install --quiet $PIP_BREAK_FLAG \
        flask==3.0.3 \
        requests==2.32.3 \
        "beautifulsoup4>=4.12" \
        "urllib3>=2.0,<3" \
        "werkzeug>=3.0"
    ok "Python packages installed (no requirements.txt found -- used defaults)"
fi

# Deactivate venv
deactivate

# ── Create directory structure ────────────────────────────────────────────────
say "Creating data directories..."
mkdir -p \
    ~/xnxx-scraper/cache \
    ~/xnxx-scraper/downloads/favorites \
    ~/xnxx-scraper/data \
    ~/xnxx-scraper/users
ok "Directories ready at ~/xnxx-scraper/"

# ── Copy the main script ──────────────────────────────────────────────────────
say "Installing xnxx_pi2.py..."
SCRIPT_SRC="$SCRIPT_DIR/xnxx_pi2.py"
SCRIPT_DEST="$HOME/xnxx-scraper/xnxx_pi2.py"
if [ -f "$SCRIPT_SRC" ]; then
    cp "$SCRIPT_SRC" "$SCRIPT_DEST"
    chmod +x "$SCRIPT_DEST"
    ok "Script installed to $SCRIPT_DEST"
else
    warn "xnxx_pi2.py not found next to install.sh -- copy it manually to $SCRIPT_DEST"
fi

# ── Create launcher scripts ───────────────────────────────────────────────────
say "Creating launcher scripts..."

cat > ~/xnxx-scraper/start.sh << 'EOF'
#!/usr/bin/env bash
# Normal launch (console logging only)
source "$HOME/xnxx-venv/bin/activate"
cd "$HOME/xnxx-scraper"
python3 xnxx_pi2.py "$@"
EOF

cat > ~/xnxx-scraper/start-debug.sh << 'EOF'
#!/usr/bin/env bash
# Debug launch -- writes verbose log to xnxx_pi.log
source "$HOME/xnxx-venv/bin/activate"
cd "$HOME/xnxx-scraper"
python3 xnxx_pi2.py --debug "$@"
EOF

chmod +x ~/xnxx-scraper/start.sh ~/xnxx-scraper/start-debug.sh
ok "Launchers created"

# ── Create systemd service (optional) ────────────────────────────────────────
say "Auto-start on boot via systemd? (optional)"
read -r -p "    Create systemd service? [y/N] " CREATE_SERVICE
if [[ "$CREATE_SERVICE" =~ ^[Yy]$ ]]; then
    SERVICE_FILE="/etc/systemd/system/xnxx-pi.service"
    CURRENT_USER="$(whoami)"
    sudo tee "$SERVICE_FILE" > /dev/null << SVCEOF
[Unit]
Description=xnxx-scraper-pi
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=/home/${CURRENT_USER}/xnxx-scraper
ExecStart=/home/${CURRENT_USER}/xnxx-venv/bin/python3 /home/${CURRENT_USER}/xnxx-scraper/xnxx_pi2.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
    sudo systemctl daemon-reload
    sudo systemctl enable xnxx-pi.service
    ok "Systemd service installed and enabled"
    echo "    Start now:  sudo systemctl start xnxx-pi"
    echo "    Status:     sudo systemctl status xnxx-pi"
    echo "    Logs:       journalctl -u xnxx-pi -f"
else
    warn "Skipped systemd service"
fi

# ── Remove .git directory ─────────────────────────────────────────────────────
if [ -d "$SCRIPT_DIR/.git" ]; then
    say "Removing directory $SCRIPT_DIR..."
    rm -rf "$SCRIPT_DIR/"
    ok "removed -- directory is now a standalone copy"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}   Setup complete!                        ${NC}"
echo -e "${GREEN}============================================${NC}"
echo ""
echo "  Normal start:     ~/xnxx-scraper/start.sh"
echo "  Debug start:      ~/xnxx-scraper/start-debug.sh"
echo "  Custom port:      ~/xnxx-scraper/start.sh --port 8080"
echo ""
echo "  Then open in your browser:"
try_ip=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$try_ip" ] && echo "    http://${try_ip}:5000" || echo "    http://<pi-ip>:5000"
echo "    http://$(hostname).local:5000"
echo ""
