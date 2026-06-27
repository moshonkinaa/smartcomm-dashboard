# SmartComm Dashboard — install pack

Самодостаточный пакет для установки дашборда на свежий контроллер
(MSI Cubi 5 / любой x86_64 / ARM с Debian 12 или 13).

## Что внутри

| Файл | Назначение |
|---|---|
| `install.sh` | Главный установщик. Один запуск — всё готово. |
| `migrate_from_pi.sh` | Опционально: тянет БД и фото с существующего Pi. |
| `dashboard.py` | Backend (Flask + waitress) |
| `network.py` | Blueprint карты сети |
| `mikrotik.py` | REST-клиент MikroTik |
| `index.html` | Главная страница `/` |
| `network.html` | Карта сети `/network` |
| `chart.min.js` | Chart.js v4.4.1 (200 КБ, оффлайн-бандл) |
| `manifest.json`, `sw.js` | PWA-обвязка |
| `smartcomm-dashboard.service` | systemd unit |

## Требования к целевому хосту

- **Linux**: Debian 12 Bookworm или Debian 13 Trixie (Ubuntu 22.04/24.04 тоже подойдёт)
- **Архитектура**: x86_64 или ARM (тестировалось на обоих)
- **Python**: 3.11+ (есть в Debian 12 из коробки)
- **Пользователь**: создан non-root юзер (по умолчанию `pi`, можно другой через `SVC_USER=имя`)
- **Сеть**: контроллер в той же подсети что устройства которые будет мониторить

## Установка

### Вариант A — целиком на USB-флешке

1. На своём компе скопируй папку `smartcomm-dashboard-install` целиком на флешку
2. Вставь флешку в Cubi 5, скопируй на него:
   ```bash
   mkdir -p ~/smartcomm-install
   cp -r /media/$USER/USB/smartcomm-dashboard-install/* ~/smartcomm-install/
   cd ~/smartcomm-install
   chmod +x install.sh
   sudo bash install.sh
   ```

### Вариант B — через scp с компа

```powershell
# С Windows:
scp -r 'D:\Claude Projects\Iridi Taureni pi5\smartcomm-dashboard-install' pi@<cubi-ip>:~/

# На Cubi:
cd ~/smartcomm-dashboard-install
chmod +x install.sh
sudo bash install.sh
```

### Вариант C — кастомный юзер

Если сервисный юзер не `pi`, а например `admin`:
```bash
SVC_USER=admin sudo -E bash install.sh
```

## Что делает install.sh

1. Проверяет sudo, существование юзера, наличие всех файлов
2. `apt install` зависимостей: `python3-flask`, `python3-flask-compress`, `python3-waitress`, `nmap`, `snmp`, `avahi-utils`, `mmc-utils`, `iputils-ping`, `curl`
3. Создаёт `/etc/sudoers.d/smartcomm-dashboard` с NOPASSWD для сервисного юзера (нужно для `systemctl restart irserver`, `ss`, `journalctl`)
4. Создаёт каталоги `/opt/smartcomm-dashboard/`, `/var/lib/smartcomm-dashboard/{photos,backups}`
5. Копирует все файлы дашборда в `/opt/smartcomm-dashboard/`
6. Проверяет синтаксис Python
7. Ставит systemd unit, enable + start
8. Probe HTTP 200 + печатает URL и следующие шаги

Скрипт **идемпотентный** — можно запускать повторно после правок.

## После установки

1. Открой `http://<ip>:8080/` — должен загрузиться дашборд
2. Зайди в **Карта сети** → ⚙️ Настройки → секция **MikroTik (REST API)** → введи IP/логин/пароль, нажми **Сохранить и проверить**. Появятся плитки MikroTik на дашборде.
3. На главной → плитка **iRidium · детали** → ссылка **⚙ API** → введи пароль iRidium-сервера. Заработает плитка **iRidium · все данные API**.
4. В карте сети нажми **🔍 Сканировать сеть** — найдутся все устройства в подсети.
5. После скана нажми **🔗 Sync MikroTik** — подтянутся имена устройств из DHCP-комментариев MikroTik.

## Опционально: миграция данных с Pi

Если хочешь перенести существующую базу устройств / историю метрик с Pi5:

```bash
cd ~/smartcomm-dashboard-install
sudo bash migrate_from_pi.sh 192.168.95.167 pi
```

Скрипт:
1. Спросит подтверждение (текущие БД будут перезаписаны, сделает бэкап)
2. Остановит сервис
3. По scp скачает inventory.db, metrics.db, photos/ с Pi
4. Запустит сервис обратно
5. Покажет статистику (сколько устройств/событий/семплов восстановилось)

## Управление

```bash
# Логи в реальном времени
sudo journalctl -u smartcomm-dashboard -f

# Рестарт
sudo systemctl restart smartcomm-dashboard

# Стоп
sudo systemctl stop smartcomm-dashboard

# Проверка статуса
sudo systemctl status smartcomm-dashboard

# Размер данных
sudo du -sh /var/lib/smartcomm-dashboard/*
```

## Структура после установки

```
/opt/smartcomm-dashboard/         ← код (8 файлов)
├── dashboard.py
├── network.py
├── mikrotik.py
├── index.html
├── network.html
├── chart.min.js
├── manifest.json
└── sw.js

/var/lib/smartcomm-dashboard/     ← данные (StateDirectory)
├── inventory.db          ← карта сети, события, доступы, фото-метаданные
├── metrics.db            ← история CPU/RAM/диск/MikroTik (30 дней)
├── photos/               ← фото устройств (multi-photo per device)
└── backups/              ← авто-бэкапы inventory.db (по 30 дней)

/etc/systemd/system/smartcomm-dashboard.service
/etc/sudoers.d/smartcomm-dashboard
```

## Удаление

```bash
sudo systemctl stop smartcomm-dashboard
sudo systemctl disable smartcomm-dashboard
sudo rm /etc/systemd/system/smartcomm-dashboard.service
sudo rm /etc/sudoers.d/smartcomm-dashboard
sudo rm -rf /opt/smartcomm-dashboard
# /var/lib/smartcomm-dashboard оставь если данные ещё нужны
sudo systemctl daemon-reload
```

## Что НЕ делает этот пакет

- НЕ ставит iRidium Server — это отдельный .deb-пакет от iridi.com
- НЕ настраивает Docker — для каталога 30-50 third-party сервисов нужен отдельный шаг
- НЕ открывает порт в файрволле — если есть `ufw`, выполни `sudo ufw allow 8080/tcp`
- НЕ ставит SSL — дашборд работает по HTTP на LAN. Для внешнего доступа нужен reverse-proxy (nginx + letsencrypt)
