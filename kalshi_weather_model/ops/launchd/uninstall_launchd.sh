#!/bin/zsh
set -euo pipefail

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SCHEDULER_LABEL="com.kalshi-weather.scheduler"
DASHBOARD_LABEL="com.kalshi-weather.dashboard"
SCHEDULER_DST="$LAUNCH_AGENTS_DIR/${SCHEDULER_LABEL}.plist"
DASHBOARD_DST="$LAUNCH_AGENTS_DIR/${DASHBOARD_LABEL}.plist"

uid="$(id -u)"

launchctl bootout "gui/$uid/$SCHEDULER_LABEL" >/dev/null 2>&1 || true
launchctl bootout "gui/$uid/$DASHBOARD_LABEL" >/dev/null 2>&1 || true

rm -f "$SCHEDULER_DST" "$DASHBOARD_DST"

echo "Stopped and removed:"
echo "  $SCHEDULER_LABEL"
echo "  $DASHBOARD_LABEL"

