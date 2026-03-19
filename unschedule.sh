#!/bin/bash
# ─────────────────────────────────────────────
#  PaperScan — Unschedule Script
#  Removes the anacron job and login hook
# ─────────────────────────────────────────────

ANACRONTAB="$HOME/.anacrontab"
LOGIN_FILE="$HOME/.profile"
SPOOL_DIR="$HOME/.anacron/spool"

# Remove from anacrontab
if [ -f "$ANACRONTAB" ] && grep -q "paperscan-weekly" "$ANACRONTAB"; then
    sed -i '/paperscan-weekly/d' "$ANACRONTAB"
    echo "✅ Removed PaperScan from anacrontab."
else
    echo "ℹ️  No PaperScan anacron job found."
fi

# Remove from .profile
if grep -q "paperscan anacron" "$LOGIN_FILE" 2>/dev/null; then
    sed -i '/paperscan anacron/d;/anacron.*anacrontab/d' "$LOGIN_FILE"
    echo "✅ Removed anacron trigger from $LOGIN_FILE."
fi

# Remove spool entry (resets the 7-day timer)
if [ -f "$SPOOL_DIR/paperscan-weekly" ]; then
    rm "$SPOOL_DIR/paperscan-weekly"
    echo "✅ Cleared anacron spool."
fi

echo ""
echo "PaperScan has been fully unscheduled."
