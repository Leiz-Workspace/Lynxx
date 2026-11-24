#!/bin/sh
# macOS: run the standalone Lynxx gender model server (tools/build_model_server.py) in the background for this user, as a
# LaunchAgent started at login and restarted if it fails. The desktop app calls http://127.0.0.1:8765.
#   sh install-model-server.sh [/path/to/lynxx-model-server] [port]   (default: the lynxx-model-server next to this script)
#   sh install-model-server.sh --uninstall
# Not tested by the maintainers on macOS yet.
set -eu
LABEL=local.lynxx.model-server
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0
fi
EXE=${1:-$(dirname "$0")/lynxx-model-server}
PORT=${2:-8765}
case "$PORT" in *[!0-9]*|'') echo "port must be a number" >&2; exit 2 ;; esac
EXE=$(cd "$(dirname "$EXE")" && pwd)/$(basename "$EXE")
[ -x "$EXE" ] || { echo "not an executable: $EXE" >&2; exit 2; }
LOG="$HOME/Library/Logs/lynxx-model-server.log"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
xml_escape() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$EXE")</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
    <string>--quiet</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG")</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG")</string>
</dict>
</plist>
PLIST
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "started $LABEL on 127.0.0.1:$PORT (log: $LOG)"
