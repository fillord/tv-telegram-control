#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.tvcontrol.bot"
PLIST_NAME="${LABEL}.plist"
DEST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"
LOG_FILE="${HOME}/Library/Logs/tv_bot.log"
PYTHON_BIN="$(command -v python3 || echo '/usr/bin/python3')"
DOMAIN="gui/$(id -u)"
SERVICE_TARGET="${DOMAIN}/${LABEL}"

case "$1" in
  install)
    echo "Установка службы автозапуска в ~/Library/LaunchAgents..."
    mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/Library/Logs"

    cat <<EOF > "${DEST}"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-i</string>
        <string>${PYTHON_BIN}</string>
        <string>tv_bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>TV_BOT_LOG_DIR</key>
        <string>${HOME}/Library/Logs</string>
    </dict>
</dict>
</plist>
EOF
    plutil -lint "${DEST}" >/dev/null
    # Загрузка службы
    launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
    launchctl bootstrap "${DOMAIN}" "${DEST}"
    launchctl kickstart -k "${SERVICE_TARGET}"
    echo "✅ Служба установлена и запущена."
    echo "Логи: tail -f '${LOG_FILE}'"
    ;;

  start)
    if [ ! -f "${DEST}" ]; then
      echo "Служба не установлена. Выполните: ./service.sh install"
      exit 1
    fi
    if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
      launchctl kickstart -k "${SERVICE_TARGET}"
    else
      launchctl bootstrap "${DOMAIN}" "${DEST}"
    fi
    echo "✅ Служба запущена."
    ;;

  stop)
    if [ -f "${DEST}" ]; then
      launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
      echo "⏹ Служба остановлена."
    else
      echo "Служба не установлена."
    fi
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  uninstall)
    echo "Удаление службы..."
    if [ -f "${DEST}" ]; then
      launchctl bootout "${SERVICE_TARGET}" 2>/dev/null || true
      rm -f "${DEST}"
      echo "✅ Служба удалена из автозапуска."
    else
      echo "Служба не была установлена."
    fi
    ;;

  status)
    echo "=== Статус службы ${LABEL} ==="
    if launchctl print "${SERVICE_TARGET}" >/dev/null 2>&1; then
      echo "🟢 Служба активна в launchd."
      launchctl print "${SERVICE_TARGET}" 2>/dev/null | awk '/pid =/ {print "PID процесса: " $3; exit}'
    else
      echo "🔴 Служба не запущена в launchd."
    fi
    echo ""
    echo "=== Последние строки лога (${LOG_FILE}) ==="
    if [ -f "${LOG_FILE}" ]; then
      tail -n 10 "${LOG_FILE}"
    else
      echo "Файл логов пока не создан."
    fi
    ;;

  logs)
    touch "${LOG_FILE}"
    tail -n 30 -f "${LOG_FILE}"
    ;;

  *)
    echo "Использование: $0 {install|start|stop|restart|status|uninstall|logs}"
    exit 1
    ;;
esac
