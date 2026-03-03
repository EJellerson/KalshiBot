#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${(%):-%N}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

SCHEDULER_LABEL="com.ericjellerson.kalshi-weather.scheduler"
DASHBOARD_LABEL="com.ericjellerson.kalshi-weather.dashboard"

SCHEDULER_DST="$LAUNCH_AGENTS_DIR/${SCHEDULER_LABEL}.plist"
DASHBOARD_DST="$LAUNCH_AGENTS_DIR/${DASHBOARD_LABEL}.plist"
SCHEDULER_RUN_SCRIPT="$PROJECT_DIR/ops/run_scheduler.sh"
DASHBOARD_RUN_SCRIPT="$PROJECT_DIR/ops/run_dashboard.sh"
LOG_DIR="$PROJECT_DIR/data/logs"

mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$LOG_DIR"

cat >"$SCHEDULER_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SCHEDULER_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${SCHEDULER_RUN_SCRIPT}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd_scheduler.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd_scheduler.err.log</string>
</dict>
</plist>
EOF

cat >"$DASHBOARD_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${DASHBOARD_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${DASHBOARD_RUN_SCRIPT}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${PROJECT_DIR}</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd_dashboard.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd_dashboard.err.log</string>
</dict>
</plist>
EOF

uid="$(id -u)"

launchctl bootout "gui/$uid/$SCHEDULER_LABEL" >/dev/null 2>&1 || true
launchctl bootout "gui/$uid/$DASHBOARD_LABEL" >/dev/null 2>&1 || true

launchctl bootstrap "gui/$uid" "$SCHEDULER_DST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$uid" "$DASHBOARD_DST" >/dev/null 2>&1 || true

launchctl enable "gui/$uid/$SCHEDULER_LABEL" >/dev/null 2>&1 || true
launchctl enable "gui/$uid/$DASHBOARD_LABEL" >/dev/null 2>&1 || true

launchctl kickstart -k "gui/$uid/$SCHEDULER_LABEL" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/$uid/$DASHBOARD_LABEL" >/dev/null 2>&1 || true

echo "Installed and started:"
echo "  $SCHEDULER_LABEL"
echo "  $DASHBOARD_LABEL"
