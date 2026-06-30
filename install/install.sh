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

REQUIRED_FILES=(dashboard.py network.py mikrotik.py services.py index.html network.html services.html login.html chart.min.js manifest.json sw.js smartcomm-dashboard.service)
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
dpkg -s python3-yaml           >/dev/null 2>&1 || NEED="$NEED python3-yaml"
dpkg -s sqlite3                >/dev/null 2>&1 || NEED="$NEED sqlite3"
dpkg -s nmap                   >/dev/null 2>&1 || NEED="$NEED nmap"
dpkg -s snmp                   >/dev/null 2>&1 || NEED="$NEED snmp"
dpkg -s avahi-utils            >/dev/null 2>&1 || NEED="$NEED avahi-utils"
dpkg -s mmc-utils              >/dev/null 2>&1 || NEED="$NEED mmc-utils"
dpkg -s smartmontools          >/dev/null 2>&1 || NEED="$NEED smartmontools"
dpkg -s iputils-ping           >/dev/null 2>&1 || NEED="$NEED iputils-ping"
dpkg -s curl                   >/dev/null 2>&1 || NEED="$NEED curl"
dpkg -s git                    >/dev/null 2>&1 || NEED="$NEED git"
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
for f in dashboard.py network.py mikrotik.py services.py index.html network.html services.html login.html chart.min.js manifest.json sw.js; do
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
python3 -m py_compile "$APP_DIR/services.py"  && echo "  services.py — OK"

# ===== Клон каталога сервисов (v1.5.0+) =====
CATALOG_DIR="/opt/smartcomm-services-catalog"
echo ""
echo "[5.5] Каталог сервисов (для магазина)"
if [ -d "$CATALOG_DIR/.git" ]; then
  echo "  $CATALOG_DIR уже есть, git pull"
  git -C "$CATALOG_DIR" pull --quiet || echo "  warn: git pull failed (продолжаю)"
else
  echo "  клонирую smartcomm-services-catalog → $CATALOG_DIR"
  git clone --depth=1 https://github.com/moshonkinaa/smartcomm-services-catalog.git "$CATALOG_DIR" || \
    echo "  warn: clone failed — каталог пустой, обновится через UI"
fi
mkdir -p /var/lib/smartcomm-services
chown -R "$SVC_USER:$SVC_GROUP" /var/lib/smartcomm-services

# ===== 6. Platform-specific fixes (опциональные, по DMI vendor/product) =====
# База: blacklist Intel DPTF на MSI Cubi 5 — там BIOS-баг с _SB.PC00.SEN1._TMP.MPAG
# каждые ~10с генерит "ACPI BIOS Error". int3400_thermal family дёргает битые
# методы. Coretemp (MSR) и BIOS fan-control продолжают работать без них.
echo ""
echo "[6/7] Platform-specific fixes"
DMI_VENDOR="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo unknown)"
DMI_PRODUCT="$(cat /sys/class/dmi/id/product_name 2>/dev/null || echo unknown)"
echo "  hardware: $DMI_VENDOR / $DMI_PRODUCT"

if [[ "$DMI_VENDOR" == *"Micro-Star"* ]] && [[ "$DMI_PRODUCT" == *"Cubi"* ]]; then
  BLFILE="/etc/modprobe.d/blacklist-int3400.conf"
  if [ -f "$BLFILE" ]; then
    echo "  ✓ MSI Cubi — int3400 blacklist уже применён ($BLFILE)"
  else
    echo "  → MSI Cubi обнаружен — применяю ACPI int3400 blacklist (см. CHANGELOG v1.4.3+)"
    cat > "$BLFILE" <<EOF
# Auto-applied by smartcomm-dashboard installer for MSI $DMI_PRODUCT.
# Reason: MSI BIOS has missing ACPI symbol _SB.PC00.SEN1._TMP.MPAG which
# causes "ACPI BIOS Error" every ~10s. Intel DPTF drivers below dereference
# those missing symbols. Blacklisting stops the noise.
# Safe: CPU temp continues via coretemp (MSR), fan control continues via BIOS.
blacklist int3400_thermal
blacklist int340x_thermal_zone
blacklist int3402_thermal
blacklist int3403_thermal
blacklist intel_pch_thermal
EOF
    chmod 644 "$BLFILE"
    update-initramfs -u 2>&1 | tail -1
    PLATFORM_NEEDS_REBOOT=1
    echo "  ⚠ blacklist применится после reboot — добавлю напоминание в финале"
  fi
else
  echo "  (нет известных платформенных фиксов для этого hardware — skip)"
fi

# ===== 7. Systemd =====
echo ""
echo "[7/7] Systemd unit + старт"
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
if [ "${PLATFORM_NEEDS_REBOOT:-0}" = "1" ]; then
  echo ""
  echo "  ⚠ Применён платформенный фикс (blacklist int3400) — нужен REBOOT для"
  echo "    применения, чтобы исчезли ACPI BIOS errors из dmesg:"
  echo "       sudo systemctl reboot"
fi
echo "================================================================"
