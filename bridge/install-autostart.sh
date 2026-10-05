#!/bin/sh
# Start the littlelx bridge automatically when you log in (and restart it if it
# ever exits). Undo with:  launchctl unload ~/Library/LaunchAgents/littlelx.plist
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/littlelx.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>littlelx</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$DIR/littlelx.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/littlelx.log</string>
  <key>StandardErrorPath</key><string>/tmp/littlelx.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "littlelx bridge will now start at login. Log: /tmp/littlelx.log"
