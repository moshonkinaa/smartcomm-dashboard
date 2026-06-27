# SmartComm Dashboard

Локальный мониторинг-портал для контроллеров умного дома (iRidium Server + Raspberry Pi 5 / x86 неттоп). Веб-интерфейс на Flask + Waitress, SQLite-БД, опционально интегрируется с MikroTik по REST API.

**Не требует облака.** Все данные на устройстве. LAN-only по умолчанию.

## Что показывает

### Главная (`/`)
- **Метрики хоста** (Pi или x86): температура CPU, нагрузка CPU/ядра, RAM, диск, сеть — со спарклайнами и большими графиками 1ч/24ч
- **iRidium**: статус сервиса, портал :8888, версия, проект, число клиентов, лицензия, список 16+ устройств и тегов из HTTP API
- **MikroTik** (если настроен): CPU/RAM/диск/трафик WAN с графиками
- **Сводный health-check**: NTP, упавшие systemd, нужен ли ребут, доступные apt-обновления, последние ошибки dmesg, SD-карта, throttle
- **Мониторинг устройств**: плитки с 24h-полосками доступности для выбранных в карте сети
- **Карта IP** подсети /24 (16×16 с фильтрами)
- Топ-5 процессов, активные TCP iRidium, журнал iRidium

### Карта сети (`/network`)
- Инвентарь всех устройств (nmap auto-scan каждые 4 часа)
- Auto-classification по vendor (Hikvision→camera, MikroTik→network и т.д.)
- Карточки: имя, помещение, тип, URL, описание, заметки, **фото (несколько)**, **доступы (login/password)**, теги
- 24h timeline доступности per device
- **Sync с MikroTik**: имена устройств из DHCP-comments + флаг static/dynamic
- IP-карта /24 с 4 чекбоксами-фильтрами (static·online, static·offline, dynamic·online, dynamic·offline)
- Web-SSH к устройствам (через webssh на `:8022`)
- Bulk операции, audit log правок, CSV-экспорт

## Установка (свежий хост)

### На Debian 12 / Debian 13 / Ubuntu 22.04+

```bash
# 1. Скачать install pack
git clone https://github.com/moshonkinaa/smartcomm-dashboard.git
cd smartcomm-dashboard/install/

# 2. Установить (создаст пользователя sudoers, поставит deps, запустит systemd)
sudo bash install.sh

# 3. Открыть в браузере
# http://<ip>:8080/ — логин admin/admin (сразу сменить)
```

Альтернатива — взять архив релиза:

```bash
curl -L https://github.com/moshonkinaa/smartcomm-dashboard/releases/latest/download/install.tar.gz | tar xz
cd install && sudo bash install.sh
```

### Зависимости (apt)

`python3-flask`, `python3-flask-compress`, `python3-waitress`, `nmap`, `snmp`, `avahi-utils`, `mmc-utils`, `iputils-ping`, `curl`

## Архитектура

- **Backend**: Python 3.11+ (Flask 2.2 + Waitress 2.1 + SQLite WAL)
- **Frontend**: Vanilla JS + Chart.js (bundled, no CDN)
- **БД**:
  - `/var/lib/smartcomm-dashboard/inventory.db` — карта сети, события, audit
  - `/var/lib/smartcomm-dashboard/metrics.db` — история CPU/RAM/диск/MikroTik (30 дней)
- **Auth**: cookie-сессии 30 дней, PBKDF2-HMAC-SHA256, role admin/user, audit log 90 дней
- **Sampler-потоки**: фоновые опросы (Pi-метрики 60с, MikroTik 30с, iRidium API 30с, iRidium HTTP probe 10с, presence ARP 60с)
- **Production WSGI**: Waitress (8 потоков), без single-thread bottleneck Flask dev-server

## Деплой обновлений

В дашборде есть кнопка **«Версия 1.0.0»** в шапке → модалка с историей и кнопкой «Проверить обновления». Background-updater автоматически проверяет GitHub Releases раз в час и применяет с health-check + rollback.

Для ручного деплоя — `pwsh -File deploy_dashboard.ps1` (Posh-SSH из Windows).

## Что НЕ входит

- iRidium Server — отдельный proprietary `.deb` от [iridi.com](https://www.iridi.com)
- Docker для каталога 30-50 third-party сервисов — ставится отдельно
- HTTPS / reverse-proxy — для внешнего доступа нужен nginx + Let's Encrypt
- Firewall — по умолчанию слушает :8080 на всех интерфейсах (LAN-only)

## История изменений

См. [CHANGELOG.md](CHANGELOG.md).

## License

MIT — см. [LICENSE](LICENSE).

## Авторство

Разработка ведётся как инструмент для одного iRidium-инсталлятора на рынке СНГ. Цель — продуктовая платформа для тиражирования на объекты клиентов.
