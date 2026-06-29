#!/bin/bash
# SmartComm Dashboard — установщик для свежей Debian 12/13 (x86_64 или ARM).
# Запускается на целевом хосте от sudo-юзера. Идемпотентно (можно запускать повторно).
#
# Использование:
#   sudo bash install.sh
#
# Или с переопределением юзера сервиса (по умолчанию pi):
#   SVC_USER=admin sudo -E bash install.sh

set -euo pipefail

# ===== настройки =====
SVC_USER="${SVC_USER:-pi}"
SVC_GROUP="${SVC_GROUP:-$SVC_USER}"
APP_DIR="/opt/smartcomm-dashboard"
DATA_DIR="/var/lib/smartcomm-dashboard"
SVC_FILE="smartcomm-dashboard.service"
PORT="${PORT:-8080}"

# Каталог где лежит сам install.sh
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# Файлы дашборда могут лежать ЛИБО рядом со скриптом (standalone tarball),
# ЛИБО в parent dir (когда из git clone: repo/install/install.sh + repo/dashboard.py).
if [ -f "$SCRIPT_DIR/dashboard.py" ]; then
  SRC_DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../dashboard.py" ]; then
  SRC_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
else
  echo "ОШИБКА: dashboard.py не найден ни в $SCRIPT_DIR, ни в $SCRIPT_DIR/.."
  exit 1
fi

# ===== проверки =====
if [ "$(id -u)" -ne 0 ]; then
  echo "ОШИБКА: запусти от root через sudo:  sudo bash install.sh"
  exit 1
fi

if ! id "$SVC_USER" >/dev/null 2>&1; then
  echo "ОШИБКА: юзер $SVC_USER не существует."
  echo "Создай его (adduser $SVC_USER) или укажи другой через SVC_USER=имя sudo -E bash install.sh"
  exit 1
fi

REQUIRED_FILES=(dashboard.py network.py mikrotik.py index.html network.html login.html chart.min.js manifest.json sw.js smartcomm-dashboard.service)
OPTIONAL_FILES=(CHANGELOG.md marked.min.js)
for f in "${REQUIRED_FILES[@]}"; do
  if [ ! -f "$SRC_DIR/$f" ]; then
    echo "ОШИБКА: не найден файл $f в $SRC_DIR"
    exit 1
  fi
done

echo "================================================================"
echo " SmartComm Dashboard installer"
echo "   target user:  $SVC_USER"
echo "   app dir:      $APP_DIR"
echo "   data dir:     $DATA_DIR"
echo "   service:      $SVC_FILE"
echo "   port:         $PORT"
echo "================================================================"

# ===== 1. APT зависимости =====
echo ""
echo "[1/6] APT — зависимости"
NEED=""
dpkg -s python3-flask          >/dev/null 2>&1 || NEED="$NEED python3-flask"
dpkg -s python3-flask-compress >/dev/null 2>&1 || NEED="$NEED python3-flask-compress"
dpkg -s python3-waitress       >/dev/null 2>&1 || NEED="$NEED python3-waitress"
dpkg -s nmap                   >/dev/null 2>&1 || NEED="$NEED nmap"
dpkg -s snmp                   >/dev/null 2>&1 || NEED="$NEED snmp"
dpkg -s avahi-utils            >/dev/null 2>&1 || NEED="$NEED avahi-utils"
dpkg -s mmc-utils              >/dev/null 2>&1 || NEED="$NEED mmc-utils"
dpkg -s iputils-ping           >/dev/null 2>&1 || NEED="$NEED iputils-ping"
dpkg -s curl                   >/dev/null 2>&1 || NEED="$NEED curl"
if [ -n "$NEED" ]; then
  echo "  ставим:$NEED"
  apt-get update -qq
  apt-get install -y -qq $NEED
else
  echo "  все пакеты уже установлены"
fi

# ===== 2. NOPASSWD sudo для $SVC_USER =====
# Дашборд читает ss/journalctl/ping/nmap/systemctl через sudo. Без NOPASSWD
# каждый запрос упрётся в пароль и зависнет.
echo ""
echo "[2/6] sudoers — NOPASSWD для $SVC_USER (нужно для systemctl/ss/journalctl)"
SUDOERS_FILE="/etc/sudoers.d/smartcomm-dashboard"
cat > "$SUDOERS_FILE" <<EOF
# Автогенерация smartcomm-dashboard installer
$SVC_USER ALL=(ALL) NOPASSWD: ALL
EOF
chmod 0440 "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null && echo "  sudoers OK" || { echo "  ОШИБКА в sudoers"; rm -f "$SUDOERS_FILE"; exit 1; }

# ===== 3. Каталоги =====
echo ""
echo "[3/6] Каталоги"
mkdir -p "$APP_DIR"
mkdir -p "$DATA_DIR/photos" "$DATA_DIR/backups"
chown -R "$SVC_USER:$SVC_GROUP" "$DATA_DIR"
echo "  $APP_DIR + $DATA_DIR готовы"

# ===== 4. Копирование файлов =====
echo ""
echo "[4/6] Копируем файлы дашборда → $APP_DIR/"
for f in dashboard.py network.py mikrotik.py index.html network.html login.html chart.min.js manifest.json sw.js; do
  cp "$SRC_DIR/$f" "$APP_DIR/$f"
  chmod 644 "$APP_DIR/$f"
done
# Опциональные — копируем если есть в source
for f in "${OPTIONAL_FILES[@]}"; do
  if [ -f "$SRC_DIR/$f" ]; then
    cp "$SRC_DIR/$f" "$APP_DIR/$f"
    chmod 644 "$APP_DIR/$f"
    echo "  скопирован опциональный: $f"
  fi
done
chmod +x "$APP_DIR/dashboard.py"
echo "  всего файлов в $APP_DIR: $(ls -1 $APP_DIR | wc -l)"

# ===== 5. Sanity check Python =====
echo ""
echo "[5/6] Проверка синтаксиса Python"
python3 -m py_compile "$APP_DIR/dashboard.py" && echo "  dashboard.py — OK"
python3 -m py_compile "$APP_DIR/network.py"   && echo "  network.py — OK"
python3 -m py_compile "$APP_DIR/mikrotik.py"  && echo "  mikrotik.py — OK"

# ===== 6. Systemd =====
echo ""
echo "[6/6] Systemd unit + старт"
cp "$SRC_DIR/$SVC_FILE" "/etc/systemd/system/$SVC_FILE"
# Если хочешь другого юзера, заменяем User=/Group= в unit'е
if [ "$SVC_USER" != "pi" ]; then
  sed -i "s/^User=pi$/User=$SVC_USER/" "/etc/systemd/system/$SVC_FILE"
  sed -i "s/^Group=pi$/Group=$SVC_GROUP/" "/etc/systemd/system/$SVC_FILE"
fi
chmod 644 "/etc/systemd/system/$SVC_FILE"
systemctl daemon-reload
systemctl enable smartcomm-dashboard.service
systemctl restart smartcomm-dashboard.service
sleep 3

echo ""
echo "=== Статус сервиса ==="
systemctl is-active smartcomm-dashboard
systemctl status smartcomm-dashboard --no-pager | head -10

echo ""
echo "=== HTTP probe ==="
sleep 1
# /login публичный (без авторизации), используем как health-check
curl -fsS -o /dev/null -w "  GET /login    : HTTP %{http_code} %{time_total}s (должно 200)\n" "http://127.0.0.1:$PORT/login" || echo "  ✗ /login"
# / без cookie → 302 redirect на /login — это ожидаемо, означает что auth работает
curl -sS -o /dev/null -w "  GET /         : HTTP %{http_code} (302 = redirect на /login, всё ок)\n" "http://127.0.0.1:$PORT/"
curl -sS -o /dev/null -w "  GET /api/status: HTTP %{http_code} (401 = auth работает, всё ок)\n" "http://127.0.0.1:$PORT/api/status"

IP=$(hostname -I | awk '{print $1}')
echo ""
echo "================================================================"
echo " ГОТОВО"
echo ""
echo "  Открой в браузере:  http://$IP:$PORT/"
echo "  Карта сети:         http://$IP:$PORT/network"
echo ""
echo "  Дальше:"
echo "    1) Зайди на /network → ⚙️ Настройки → секция «MikroTik (REST API)»,"
echo "       введи IP/логин/пароль роутера → «Сохранить и проверить»."
echo "    2) На главной → плитка «iRidium · детали» → ссылка ⚙ API,"
echo "       введи пароль iRidium → плитка «iRidium · все данные API» оживёт."
echo "    3) Нажми «🔗 Sync MikroTik» в карте сети — подтянутся имена устройств."
echo ""
echo "  Логи:     sudo journalctl -u smartcomm-dashboard -f"
echo "  Рестарт:  sudo systemctl restart smartcomm-dashboard"
echo "  Стоп:     sudo systemctl stop smartcomm-dashboard"
echo "================================================================"
