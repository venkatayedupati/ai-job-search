#!/usr/bin/env bash
# Install the job-scraper as a macOS LaunchAgent (runs every 10 minutes).
# Edit com.ai-job-search.scraper.plist with your Gmail credentials first.
set -euo pipefail

PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.ai-job-search.scraper.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.ai-job-search.scraper.plist"

echo "Installing job scraper LaunchAgent..."
cp "$PLIST_SRC" "$PLIST_DST"

# Unload first in case a stale version is running
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load -w "$PLIST_DST"

echo ""
echo "Done. The scraper is now installed and will run every 10 minutes."
echo ""
echo "Useful commands:"
echo "  Stop:    launchctl unload ~/Library/LaunchAgents/com.ai-job-search.scraper.plist"
echo "  Start:   launchctl load -w ~/Library/LaunchAgents/com.ai-job-search.scraper.plist"
echo "  Status:  launchctl list | grep ai-job-search"
echo "  Logs:    tail -f $(dirname "$0")/scraper.log"
