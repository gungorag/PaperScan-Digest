#!/bin/bash
# ─────────────────────────────────────────────
#  PaperScan — Anacron Setup Script
#  Runs the weekly digest once a week.
#  If your laptop was off, it runs on next boot.
# ─────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=$(which python3)
TRACKER="$SCRIPT_DIR/tracker.py"
LOG="$SCRIPT_DIR/logs/anacron.log"
ANACRONTAB="$HOME/.anacrontab"
SPOOL_DIR="$HOME/.anacron/spool"
WRAPPER="$SCRIPT_DIR/run.sh"

# ── 1. Check anacron is installed ─────────────────────────────────────────────
if ! command -v anacron &>/dev/null; then
    echo "📦 anacron not found — installing..."
    sudo apt-get install -y anacron
    if ! command -v anacron &>/dev/null; then
        echo "❌ Could not install anacron. Please run: sudo apt-get install anacron"
        exit 1
    fi
    echo "✅ anacron installed."
fi

# ── 2. Create dirs ─────────────────────────────────────────────────────────────
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SPOOL_DIR"

# ── 3. Create wrapper script ───────────────────────────────────────────────────
# anacron needs a single executable command, so we wrap the python call
cat > "$WRAPPER" << EOF
#!/bin/bash
cd "$SCRIPT_DIR"
$PYTHON "$TRACKER" >> "$LOG" 2>&1
EOF
chmod +x "$WRAPPER"

# ── 4. Write user anacrontab ───────────────────────────────────────────────────
# Format: period  delay  job-id       command
#   period  = 7        → run every 7 days
#   delay   = 5        → wait 5 min after boot before running (avoids boot congestion)
#   job-id  = paperscan-weekly (used to track last run in spool dir)

ANACRON_LINE="7	5	paperscan-weekly	$WRAPPER"

# Create or update anacrontab
if [ ! -f "$ANACRONTAB" ]; then
    echo "# User anacrontab — managed by PaperScan" > "$ANACRONTAB"
fi

if grep -q "paperscan-weekly" "$ANACRONTAB"; then
    echo "⚠️  PaperScan anacron job already exists — updating it..."
    sed -i '/paperscan-weekly/d' "$ANACRONTAB"
fi

echo "$ANACRON_LINE" >> "$ANACRONTAB"

# ── 5. Add anacron to user login (runs on boot/login) ─────────────────────────
ANACRON_CMD="anacron -s -t $ANACRONTAB -S $SPOOL_DIR"
LOGIN_FILE="$HOME/.profile"

if ! grep -q "paperscan anacron" "$LOGIN_FILE" 2>/dev/null; then
    echo "" >> "$LOGIN_FILE"
    echo "# PaperScan anacron — runs missed weekly digest on boot" >> "$LOGIN_FILE"
    echo "$ANACRON_CMD &" >> "$LOGIN_FILE"
    echo "✅ Added anacron trigger to $LOGIN_FILE"
fi

# ── 6. Done ────────────────────────────────────────────────────────────────────
echo ""
echo "✅ PaperScan scheduled with anacron!"
echo ""
echo "  Frequency : every 7 days"
echo "  On boot   : runs automatically if the weekly job was missed"
echo "  Delay     : 5 minutes after login (to let the system settle)"
echo "  Logs      : $LOG"
echo "  Spool dir : $SPOOL_DIR"
echo ""
echo "To remove:  bash unschedule.sh"
echo "To test now: bash run.sh"
