#!/bin/bash
# Опциональный миграционный скрипт.
# Запускается на НОВОМ хосте (Cubi 5) ПОСЛЕ установки через install.sh.
# Тянет с СТАРОГО Pi через scp:
#   - inventory.db (карта сети: 130+ устройств, события, теги, фото, доступы)
#   - metrics.db (история температуры/CPU/MikroTik за 30 дней)
#   - photos/* (фото устройств)
#
# При желании можно перенести и backups/ — но они большие (десятки МБ).
#
# Использование:
#   sudo bash migrate_from_pi.sh [PI_IP] [PI_USER]
# Например:
#   sudo bash migrate_from_pi.sh 192.168.95.167 pi

set -euo pipefail

PI_IP="${1:-192.168.95.167}"
PI_USER="${2:-pi}"
DATA_DIR="/var/lib/smartcomm-dashboard"
REMOTE_DATA="/var/lib/smartcomm-dashboard"

if [ "$(id -u)" -ne 0 ]; then
  echo "Запусти от root: sudo bash migrate_from_pi.sh"
  exit 1
fi

if [ ! -d "$DATA_DIR" ]; then
  echo "ОШИБКА: $DATA_DIR не найден. Сначала запусти install.sh"
  exit 1
fi

echo "================================================================"
echo " Миграция данных с Pi ($PI_USER@$PI_IP) → этот хост"
echo "================================================================"
echo ""
echo "ВНИМАНИЕ: текущие БД на этом хосте будут перезаписаны!"
echo "Сделать бэкап и продолжить? (y/N)"
read -r ANSWER
if [ "$ANSWER" != "y" ] && [ "$ANSWER" != "Y" ]; then
  echo "Отменено."
  exit 0
fi

# Останавливаем сервис чтобы БД не была залочена
systemctl stop smartcomm-dashboard.service
echo "  сервис остановлен"

# Бэкап текущих БД (если есть)
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DATA_DIR/backups"
for f in inventory.db metrics.db; do
  if [ -f "$DATA_DIR/$f" ]; then
    cp -v "$DATA_DIR/$f" "$DATA_DIR/backups/$f.pre-migrate-$TS"
  fi
done

# Тянем с Pi
echo ""
echo "  тянем БД с Pi (нужен будет пароль пользователя $PI_USER)..."
scp "$PI_USER@$PI_IP:$REMOTE_DATA/inventory.db" "$DATA_DIR/inventory.db"
scp "$PI_USER@$PI_IP:$REMOTE_DATA/metrics.db"   "$DATA_DIR/metrics.db"

# Фото — целиком, если есть
echo ""
echo "  тянем фото устройств..."
if ssh "$PI_USER@$PI_IP" "test -d $REMOTE_DATA/photos && [ \"\$(ls -A $REMOTE_DATA/photos)\" ]"; then
  scp -r "$PI_USER@$PI_IP:$REMOTE_DATA/photos/*" "$DATA_DIR/photos/" 2>/dev/null || true
  echo "  скопировано $(ls -1 $DATA_DIR/photos | wc -l) фото"
else
  echo "  фото на Pi нет — пропускаем"
fi

# Права на всё
chown -R pi:pi "$DATA_DIR"

# Запуск
echo ""
systemctl start smartcomm-dashboard.service
sleep 2
echo "=== Сервис ==="
systemctl is-active smartcomm-dashboard

echo ""
echo "=== Проверка содержимого БД ==="
sudo -u pi python3 <<PYEOF
import sqlite3
for db, tables in [
    ("$DATA_DIR/inventory.db", ['devices','device_events','device_audit','device_credentials','settings','device_photos']),
    ("$DATA_DIR/metrics.db",   ['samples','mt_samples']),
]:
    try:
        con = sqlite3.connect(db); print(db.split('/')[-1] + ':')
        for t in tables:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t:25s} {n:>8d} rows")
            except Exception: pass
        con.close()
    except Exception as e: print('  ERR:', e)
PYEOF

echo ""
echo "================================================================"
echo " Миграция завершена. Бэкап старых БД лежит в $DATA_DIR/backups/"
echo " Если что-то пошло не так — sudo cp .pre-migrate-$TS обратно."
echo "================================================================"
