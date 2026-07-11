#!/usr/bin/env bash
# Instala o Jarvis como serviço always-on do macOS (launchd LaunchAgent).
# Ele passa a subir sozinho no login e a se reerguer se cair — escutando o wake word.
# Uso:  bash service/install_service.sh        (a partir da pasta jarvis/)
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"          # pasta jarvis/
PY="$DIR/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
LABEL="com.leo.jarvis"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/jarvis.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ Jarvis instalado como serviço ($LABEL), escutando o wake word."
echo "  Logs:    $DIR/logs/"
echo "  Parar:   launchctl unload \"$PLIST\""
echo "  Remover: launchctl unload \"$PLIST\" && rm \"$PLIST\""
echo "Requer as libs de wake word (openwakeword, sounddevice) e permissão de Microfone."
