# SmartComm Dashboard — история версий

Все значимые изменения проекта. Формат — Keep a Changelog + SemVer.

## 3.4.3 — 2026-07-22

**Глубокий аудит всего кода (4 агента: correctness / reliability / data-integrity / cross-platform) → безопасные фиксы.**

### Security
- `login.html`: open-redirect на пути «уже залогинен» — редирект шёл сырым `?next=`, а не через `safeNext()` (фикс 3.4.1 закрыл только submit-путь, но не me-check).
- `network.py` `_audit_request_meta`: убрано доверие `X-Forwarded-For` при записи IP в аудит — waitress работает напрямую, XFF можно подделать → отравление аудит-лога. Теперь только `request.remote_addr`.
- `dashboard.py`: `MAX_CONTENT_LENGTH = 8 МБ` — защита от OOM на непомерном теле запроса.

### Fixed / reliability
- `network.py`: индекс `idx_audit_ts` на `auth_audit(ts)` конфликтовал по имени с индексом `device_audit(ts)` → фактически не создавался, фильтр аудита по дате шёл full-scan. Переименован в `idx_auth_audit_ts`.
- `services.py` `_parse_docker_size`: regex без `IGNORECASE` не матчил строчные единицы (`kB`/`mB` из `docker stats`) → сетевой I/O контейнеров занижался ~в 1024×.
- `dashboard.py` samplers-запуск: 3 независимых `try/except` (discover / sampler / auto-updater) — раньше один блок, падение первого шага глушило остальные.
- `dashboard.py` бэкап-ротация: чистим и `inventory.pre-migration-*.db`, и `app-pre-*` (каталоги — `rmtree`), а не только `inventory_*.db` — старые бэкапы копились без счёта.
- SMART-плитка: NVMe теперь помечается «SSD» (по `device.type=="nvme"`, не только `rotation_rate==0`).

## 3.4.2 — 2026-07-17

**Добивка LOW-техдолга из аудита.**

### Security
- `/api/network/devices/<id>/audit` теперь **admin-only** — история правок содержала открытый `login` устройства (утечка не-админу в обход H1).
- XSS-хардеринг (непоследовательное экранирование своих данных): `c.state` (index), `t`/`act` (network), схема URL в markdown-ссылках (`http(s)`/относительные).
- `_LOGIN_FAILS`: при переполнении чистим только протухшие записи, не сбрасываем активные лок-ауты.

### Fixed / reliability
- `int()` на query-параметрах (`days`/`limit`/`since`/`to`) → **400** вместо 500.
- Гонка на глобалах `PREV_NET`/`PREV_CPU` (rate CPU/сети) → `_METRIC_LOCK`.
- SSE live-tail логов: лимит одновременных стримов (≤4) — не выжрать потоки waitress.
- `iridium_version()`: кеш неудачи на 45с — не дёргать `sudo` на каждый `/api/status` пока irserver перезапускается.
- Удалён мёртвый код (no-op в mikrotik_sync, двойной `import re`, `togglePwd`/`copyPwd`).

## 3.4.1 — 2026-07-17

**Аудит всего кода (5 агентов) → фиксы.** Все security-фиксы 3.4.0 подтверждены на месте; новые находки исправлены.

### Security
- **HIGH: обход rate-limit логина через `X-Forwarded-For`.** `_client_ip()` слепо доверял XFF, а trusted-proxy нет (waitress напрямую) → атакующий менял XFF каждый запрос, лок-аут не наступал + подделка IP в аудите. Теперь берём только `request.remote_addr`.
- **Security-заголовки** на контроллере (`after_request`): CSP, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy — defense-in-depth (раньше были только на портале).
- Минимальная длина пароля 4 → **8** (change-password, reset, login-set).
- Open-redirect в `login.html`: `?next=` теперь пропускает только локальный путь.
- XSS-хардеринг `services.html`: `safeColor` для `p.color` в модалке «Подробнее» (был сырой), `safeUrl` для href из каталога (docs_url/web_url/rel.url), `escapeJs` для id.
- `_SERVICE_ID_RE`: `\A…\Z` вместо `^…$` (Python `$` матчил хвостовой `\n`).

### Fixed
- **`install.sh` не копировал `fleet_agent.py`** → на свежей установке heartbeat молча не работал. Добавлен в REQUIRED_FILES.
- `index.html`: не определённая CSS-переменная `--info` (все ссылки рендерились цветом текста, не синим) — добавлена во все 4 блока темы.
- `mikrotik.py` sampler: `int()` → `_safe_int` на `cpu-load` (RouterOS мог отдать `""`/`"5%"` → тик sampler'а глох).

## 3.4.0 — 2026-07-16

**SECURITY** — комплексный аудит всего кода под новую модель угроз (контроллеры больше не только за MikroTik: подключены к интернет-порталу мониторинга). Мульти-агентный аудит backend/frontend/command-exec/secrets. Задеплоено на Cubi (production) с бэкапом+верификацией.

### Security — Critical / High
- **Fleet-команда `update` больше не доверяет `tarball_url`/`to_version` от портала** (`_fleet_run_command`). Раньше скомпрометированный/подделанный портал одним ответом на heartbeat скармливал агенту произвольный tarball → RCE→root на контроллере. Теперь агент обновляется ТОЛЬКО сам, тянет последний релиз с закреплённого GitHub-репо (`_check_github_release`) и применяет лишь если версия реально новее (закрыт и downgrade). Это главный вектор эскалации «интернет-портал → все контроллеры».
- **`/api/network/devices` больше не отдаёт секреты устройств** (`network.py list_devices`). Bulk-список возвращал `login`/`password`/`snmp_community` открытым текстом любому аутентифицированному (в т.ч. non-admin client), в обход admin-gated `/credentials`. Теперь только флаги `has_credentials`/`has_snmp`; сами секреты — только через admin-эндпоинт.
- **Fleet-агент требует HTTPS** для `fleet_portal_url` (`fleet_agent.py`). По http токен узла ушёл бы открытым текстом, а on-path атакующий мог бы подменить команды. Localhost разрешён для отладки.

### Security — Medium / hardening
- **`restart-service` валидирует `service_id`** (regex `^[a-z0-9][a-z0-9_-]{0,63}$`) и в `_fleet_run_command`, и в корне `services._service_dir` — закрыт path-traversal в `docker compose -f <path>`.
- **Rate-limit логина** (`/api/auth/login`) — per-IP лок-аут (8 неудач / 5 мин → блок 5 мин). Раньше только аудит-лог без throttle.
- **XSS-хардеринг фронтенда**: `escapeJs()` для значений в inline-`onclick` (username, `t.key`, service id/name — HTML-escape там обходится через HTML-декодирование атрибута до парсинга JS); `safeUrl()` режет `javascript:`/`data:`-схемы в href/`window.open`; `safeColor()` санирует `p.color` из каталога в `style=""`; экранирование `p.icon`.
- **Секреты at-rest (H3)**: `UMask=0077` в systemd-юните + `chmod 700/600` на `inventory.db`/`metrics.db`/`backups` в `install.sh` — БД с учётками устройств и паролями MikroTik/iRidium больше не world-readable (была 0644).
- `.gitignore` расширен: `*.db`, `*.env`, `*.key`, `*.pem`, ключи — никогда в git.

### Проверено — ОК (без изменений)
- Снапшот heartbeat **чист от паролей** (проверены `build_status_payload`/`_fleet_snapshot`/`mt_status_snapshot`/`services.counts`): пароли устройств, MikroTik, iRidium и node-токен в снапшот не попадают.
- SQL-инъекции — всё параметризовано. Shell-инъекции (snmp/nmap/ping/docker/systemctl) — list-form argv, RCE из v3.2.0 подтверждённо закрыта. YAML — `safe_load`. Update-tarball — path-traversal guard корректен.

### Известный техдолг (в отчёте)
- `NOPASSWD: ALL` sudo (H4) — сужение ограничено (docker-сокет = root); основной вектор (update-RCE) закрыт. Полное сужение — отдельной задачей.
- Шифрование секрет-колонок в БД at-rest; смена команд-канала на подпись (Ed25519/HMAC); ротация захардкоженных паролей в `deploy_dashboard.ps1`/`setup_mikrotik_ssh.sh` (workstation-скрипты, вне git).

## 3.3.2 — 2026-07-15

**Fleet-паритет** — heartbeat-снапшот теперь несёт данные MikroTik и магазина сервисов, чтобы detail-экран сводного портала показывал те же панели, что и локальный дашборд.

### Added
- `_fleet_snapshot()` — обёртка над `build_status_payload()`: добавляет в heartbeat блоки `mikrotik` (mt_status_snapshot: CPU/RAM/диск/WAN/темп/uptime роутера) и `services` (counts: catalog/installed/running/stopped/error). Только в heartbeat — живой `/api/status` не трогается (без лишней нагрузки на роутер).

## 3.3.1 — 2026-07-15

**Fleet-метаданные узла** — снапшот теперь несёт данные, которые сводный портал показывает на карточке контроллера.

### Fixed
- `build_status_payload()` добавляет top-level `version` (версия дашборда), `platform_arch` (архитектура) и `net.ip` (локальный IP). Раньше их не было в снапшоте → на fleet-портале версия/арх/IP узла отображались пустыми.
- Новый helper `primary_ip()` — основной локальный IP по исходящему маршруту (кросс-платформенно, без внешних зависимостей).

## 3.3.0 — 2026-07-15

**Fleet-агент** — контроллер может слать heartbeat на сводный портал парка (SmartComm Fleet). По умолчанию ВЫКЛЮЧЕН (opt-in).

### Added
- **`fleet_agent.py`** — фоновый heartbeat-клиент. Раз в 60с собирает полный status-снапшот и шлёт POST на fleet-портал (только исходящее соединение → проходит любой NAT). В ответ получает очередь команд, выполняет, репортит результат.
- **Command-executor** (`_fleet_run_command`) — whitelist команд от портала: restart-iridium, start/stop-iridium, restart-dashboard, reboot, restart-service, update. Портал не может запустить произвольное.
- **`GET/PUT /api/fleet/settings`** (admin) — настройка подключения к порталу: portal_url, node_id, token, enabled.
- **`build_status_payload()`** — сборка status-снапшота вынесена из `api_status()` в отдельную функцию (переиспользуется агентом и HTTP-endpoint'ом).

### Architecture
Push-heartbeat + command-queue (паттерн Portainer Edge / Beszel). Портал за белым IP, контроллеры за клиентским NAT шлют только исходящие HTTPS. Detail-экран портала = рендер того же дашборда из снапшота. Backend портала (Postgres + Flask) протестирован end-to-end: heartbeat → БД, command-queue доставка/репорт, токен-аутентификация.

### Notes
- Агент дремлет пока `fleet_enabled != 1`. Активация — после настройки внешнего доступа к порталу.
- Токен per-node (портал хранит sha256).

---

## 3.2.1 — 2026-07-15

**dmesg noise filter** — добавлены безобидные firmware/BT сообщения Raspberry Pi.

На Pi4 (Buster) плитка dmesg показывала 5 «ошибок», все разовые при boot и не actionable:
- `brcmfmac: brcmf_fw_alloc_request` — загрузка WiFi-firmware BCM4345 (инфо о версии)
- `brcmf_c_preinit_dcmds: Firmware:` — то же
- `Bluetooth: hci0: command 0x100X tx timeout` — BT-контроллер не отвечает при init

WiFi и Bluetooth на контроллере не используются (только eth0). Добавлены в noise-фильтр — плитка dmesg станет зелёной.

---

## 3.2.0 — 2026-07-14

**Security + Reliability аудит** — 11 находок (мульти-агентный дебаг на парке из 4 разных контроллеров: armv7/aarch64/x86, Buster/Trixie, Flask 1.0/3.1, SQLite 3.27/3.46).

### 🔴 CRITICAL — RCE
- **SNMP command injection → root** (`network.py` `snmp_probe`). Было: `sh(f"snmpget -c {community} {ip}")` с `shell=True` — любой залогиненный юзер слал `{"community":"x; sudo reboot #"}` → выполнение с NOPASSWD sudo ALL. Стало: list-form `subprocess.run(shell=False)` + валидация community (whitelist) + IP (`ipaddress`).

### 🟠 HIGH
- **nmap/ping arg-injection** — `subnet`/`ip` шли отдельными argv, но `-oG`/`--script` = запуск NSE от root. Добавлен `--` перед позиционными + валидация CIDR/IP.
- **DB-локи → 500 на всех endpoints** — `_presence_check` держал write-lock во время ICMP-ping (до 3с/устройство), БД без WAL, auth писал last_seen на каждый запрос. При сетевом сбое дашборд падал. Фикс: **WAL + busy_timeout=10s**, ICMP-ping вынесены из транзакции (2 короткие транзакции), `auth_get_session` в try/except.
- **`send_file(max_age=)`** — Flask 2.0+ kwarg, ломал фото устройств на **Pi4 Buster** (Flask 1.0). Версионный шим `cache_timeout`/`max_age`.
- **`copytree(dirs_exist_ok=)`** — Py3.8+, ломал авто-обновление на Pi4 (Py3.7). Убран.
- **Пароли устройств** — `/credentials` и `export.csv` отдавали пароли любому юзеру. Теперь admin-only.
- **Stored XSS** — `escapeHtml` в inline `onclick` не защищал (HTML-декод до JS). Добавлен `escapeJs()` (`\xHH`-экранирование) для url/ip/tag/credentials.

### 🟡 MEDIUM
- **Admin-gate** — non-admin («клиент») больше не может менять/удалять инвентарь и сеть. Мутации + секреты требуют `is_admin` (глобальный gate по method+path).
- **`escapeHtml` на числах** (index.html) — падал `TypeError` на `42`, терял `0`. Теперь `String(s)`.
- **`iridium_version` lru_cache** — кешировал `None` навсегда при первом фейле. Кешируем только успех.
- **`_safe_int` MikroTik** — уже в v3.1.0.

### 🟢 LOW
- `Path.unlink(missing_ok=)` (Py3.8+) → `if exists(): unlink()`.
- `tar.extractall` — path-traversal фильтр (проверка что member внутри extract_dir).
- **Waitress 8 → 16 потоков** — SSE live-tail держит поток занятым, при 8 подвисало.

### Проверено
- Синтаксис всех 4 py-модулей OK. Py3.7-compat: 0 walrus/dirs_exist_ok/missing_ok/max_age/RETURNING в коде (только комментарии).
- iRidium на production Pi4 не пострадал за весь дебаг (PID 517 живой).

---

## 3.1.0 — 2026-07-14

**Added**: температура CPU MikroTik в плитке «MikroTik · CPU».

### Backend (mikrotik.py)
- `mt_health()` — запрос `/system/health`, нормализует формат (RB5009 отдаёт
  одиночный объект, RB4011 — массив).
- `mt_cpu_temp_c()` — извлекает температуру, ищет по разным именам метрик
  (`cpu-temperature` / `temperature` / `board-temperature`). None если датчика нет.
- `mt_status_snapshot()` возвращает `cpu_temp_c`.
- **`_safe_int()`** — все `int()` на RouterOS REST-значениях защищены от ValueError
  (пустые строки / суффиксы на нестандартных прошивках). Фикс латентного 500 на
  `/api/mikrotik/status` при нестандартном ответе роутера.

### Frontend (index.html)
- Плитка «MikroTik · CPU» показывает `🌡 54°C` рядом со значением загрузки.
  Цвет: 🟢 <60 / 🟡 60-70 / 🔴 ≥70. Скрывается если модель без датчика
  (hAP lite/mini).

### Проверено на реальном железе
- RB5009 (Piwatch): `cpu-temperature` = 53°C ✓
- RB4011 (Pi4): `temperature` = 54°C ✓

---

## 3.0.6 — 2026-07-14

**Fix**: продукты лицензии показывали числовой мусор («1, 103, 105, 108…»).

iRidium кладёт в `licence.products` помимо реальных названий («AMX»,
«Modbus TCP») числовые строки-заглушки — внутренние product id без имени.
Сортировка по алфавиту вытолкнула их в начало preview.

- Фильтруем чисто-числовые записи (`/^\d+$/`).
- Убрал сортировку — порядок от iRidium осмысленный.

---

## 3.0.5 — 2026-07-14

**Fix**: iRidium licence products рендерились как `[object Object], [object Object], ...`

### Root cause
Новый iRidium 1.3.86 возвращает `licence.products` как массив объектов
`[{name: "AMX"}, {name: "Crestron"}, ...]` (101 продукт). Старый код
делал `products.join(', ')` — Array#join вызывает `Object.toString()`
на объектах = `[object Object]`.

### Fixed
- Нормализация: массив строк ИЛИ массив объектов `{name}/{title}/{id}` → массив строк.
- Sort по алфавиту (список большой).
- Показываем первые 8 + collapsible `<details>` «+ ещё N».
- Row-helper теперь принимает 3-й аргумент `raw=true` для HTML внутри.

---

## 3.0.4 — 2026-07-14

**Fix**: Network devices всегда offline на старых SQLite (Buster).

### Root cause
`_presence_check()` использовал `UPDATE ... RETURNING id` — SQL синтаксис
SQLite 3.35+ (Feb 2021). На Raspbian Buster идёт **SQLite 3.27.2**
(2019) → OperationalError: near "RETURNING", presence-loop падал при
каждом вызове → is_online НИКОГДА не устанавливался в 1.

Симптом: все network-устройства показаны offline, хотя nmap-скан ARP
находит 17+ хостов и правильно записывает MAC в БД.

### Fixed
- Заменил `UPDATE ... RETURNING id` на двухшаговый SELECT+UPDATE.
  Работает на всех версиях SQLite (Buster 3.27, Bookworm 3.40+, x86).

### Context
Развёрнут на Pi 4 клиента (192.168.23.4, Raspbian Buster). Дашборд
показывал 18 устройств все offline. Diagnostic вернул
`OperationalError: near "RETURNING"`.

---

## 3.0.3 — 2026-07-14

**Complete iRidium hide** — забыл 3 плитки в v3.0.1.

### Fixed
- При `iridium_disabled=true` скрываются ВСЕ 5 iRidium-плиток (было 2):
  - `#tile-iridium-service` (шапка)
  - `#tile-iridium-details` (шапка)
  - **`#tile-iridium-conn`** (Активные TCP-подключения)
  - **`#tile-iridium-api-full`** (все данные API)
  - **`#tile-iridium-log`** (journal 50 строк)

---

## 3.0.2 — 2026-07-14

**Compat fix** — работа на старом Raspbian Buster (Python 3.7).

### Fixed
- Убран **walrus operator (`:=`)** в `dashboard.py:2424` (update apply flow) — Python 3.7 не поддерживает. Заменён на классический `while True: chunk = r.read(); if not chunk: break`.

### Context
Развёрнут четвёртый контроллер — Raspberry Pi 4 на 192.168.23.4 с production iRidium клиента. OS: Raspbian 10 (Buster) с Python 3.7. Дашборд крашился в restart loop с `SyntaxError: invalid syntax`.

### Ministry note
Buster EOL. Долгосрочно: обновить клиентский Pi до Bookworm (стандартный dist-upgrade Buster→Bullseye→Bookworm). Пока — оставляем совместимость с Buster.

---

## 3.0.1 — 2026-07-14

**iRidium disable option** — контроллеры без iRidium (например home-lab на Pi3 с Basic-пакетом) больше не показывают тревожную красную плитку «iRidium мёртв».

### Added
- Настройка **`iridium_disabled`** в `/api/iridium/settings` (bool, default false).
- Чекбокс **«iRidium на этом контроллере не установлен»** в модалке ⚙ API.

### Frontend
- Плитки `#tile-iridium-service` и `#tile-iridium-details` скрываются через `display:none` если `iridium_disabled=true`.
- Status-dot в шапке ориентируется на общий health, а не на iRidium когда он disabled.

### Context
Развёрнут третий контроллер (Pi3 на 192.168.95.74, home-lab режим без iRidium). Дашборд отчаянно моргал красным на iRidium — теперь можно скрыть.

---

## 3.0.0 — 2026-06-30

**Magic milestone** — magazine с услугами достигает feature-parity с Portainer/CasaOS по диагностике + добавляет changelog preview и export. Это финальный релиз серии 2.x → 3.0.

### Added — Changelog preview перед update
- **`/api/services/<id>/changelog`** — последние 5 релизов upstream-проекта через GitHub Releases API. Кеш 1ч.
- **Catalog YAML**: новое поле `image_origin: { github: "owner/repo" }`. Заполнено для 11 сервисов (immich, jellyfin, nextcloud, n8n, adguardhome, vaultwarden, uptime-kuma, ollama, frigate, wg-easy, 3x-ui).
- **Frontend**: при клике «⬆ Обновить» появляется модалка с release notes (collapsible `<details>`, первый release раскрыт), pre-release badge, ссылка на GitHub. Только потом — confirm на update. Если changelog недоступен → обычный confirm.

### Added — Custom tags
- **Schema migration v4**: новая колонка `custom_tags TEXT DEFAULT '[]'` в installed_services.
- **`GET/PUT /api/services/<id>/tags`** — CRUD тегов (max 10 тегов, до 32 символов каждый).
- **Frontend**: поле «🏷 Теги» в настройках сервиса (production, testing, client-X…). Tag chips отображаются на карточке (max 3, фиолетовый цвет). Сохраняются вместе с notes/auto-update одним нажатием.

### Added — Export config
- **`GET /api/services/export`** — генерирует ZIP-архив в памяти: `manifest.json` (snapshot всех installed_services), `compose/<id>/compose.yml` (все docker-compose файлы), `README.md` с инструкцией восстановления.
- **Frontend**: кнопка «⬇ Экспорт config» в шапке. Через `<a download>` браузер сохраняет файл с именем `smartcomm-services-<timestamp>.zip`.
- ❗ Архив НЕ содержит данных сервисов (фото, базы данных) — только конфиги. Полный backup требует отдельной выгрузки `/var/lib/smartcomm-services/`.

### Dependencies (заложено для будущего)
- **`get_dependencies(service_id)`** — backend helper для cross-service deps. В текущей версии всегда возвращает `{requires: [], required_by: []}` (каждый сервис self-contained). Заложено на будущее когда появятся shared-postgres / shared-redis между сервисами.

### Audit
- `service_tags_update` — логирование изменения тегов с details `{tags: [...]}`.
- `services_export_config` — кто скачивал бэкап конфигурации.

### Catalog
- 11 манифестов получили `image_origin.github` поле → changelog preview работает out-of-the-box для всех популярных сервисов.

---

## 2.6.0 — 2026-06-30

**Services: live diagnostics + bulk operations + network introspection.**

### Added — Live-tail logs (SSE)
- **`GET /api/services/<id>/logs/stream?since=30m&level=warn`** — SSE stream от `docker compose logs --follow`. Закрывается когда клиент отключается. Keepalive каждые 15с.
- **Фильтры**: `since` (5м/15м/30м/1ч/3ч/6ч, max 6ч защита от загрузки гигабайтов), `level` (info/warn/error — regex по line).
- **Frontend**: новый UI логов — live-tail с EventSource, фильтры since/level/grep, авто-скролл toggle, буфер cap 2000 строк, статус подключения (🟢/⊘/✗).

### Added — Bulk actions
- **`POST /api/services/bulk-action`** — параллельный start/stop/restart нескольких сервисов. Body: `{action, service_ids: [...]|null}`. Возвращает per-service результаты.
- **Frontend**: панель «⚡ Массовые действия» в шапке (показывается при ≥2 установленных): «↻ Перезапустить все», «⏸ Остановить все», «▶ Запустить все». Confirm + общий toast с count'ом успех/ошибка.
- Threading: backend запускает каждый action в своём потоке, общий timeout 90с.

### Added — Network info card
- **`GET /api/services/<id>/network`** — парсит compose.yml: список опубликованных портов (host:container/proto), bind address, internal hostnames, container names.
- **Frontend**: раздел «Сеть» в модалке — порты в формате pill, кликабельные ссылки для TCP, список контейнеров и внутренних DNS-имён (для inter-container communication).

### Operational
- Audit: bulk-action залогирован через `@audit_action("services_bulk_action")` с details = тело запроса.
- SSE требует HTTP/1.1 keep-alive — для будущего reverse-proxy установлен `X-Accel-Buffering: no` header.

---

## 2.5.0 — 2026-06-30

**Services: core diagnostics** — health badges, time-series графики ресурсов, uptime/restart счётчики.

### Backend
- **Schema migration v3** (`network.py`): новая таблица `service_metrics` (ring-buffer 24ч) + новые колонки `installed_services`: `restart_count`, `last_health_check_at`, `last_health_status`, `last_health_code`, `last_health_rtt_ms`, `uptime_running_seconds`, `last_status_change_at`.
- **Time-series sampler** (`services.py:_save_metrics_sample()`): пишет CPU/RAM/Net per-service в БД каждые 30 сек. Retention 24ч, prune раз в ~6 мин.
- **Health monitor** (`services.py:_check_all_health()`): HTTP HEAD probe на `web_url` каждые 60 сек, классификация healthy/degraded/down + RTT.
- **Uptime/restart tracker** (`services.py:_update_uptime_and_restarts()`): при смене статуса `sync_statuses()` инкрементит счётчики; uptime_running_seconds накапливается за всё время.
- **`compute_uptime_pct(inst, 86400)`** — uptime % за 24ч на лету.

### API
- **`/api/services/<id>/metrics?range=86400&max_points=288`** — time-series (5 мин – 7 дней), downsample равномерно.
- `/api/services/catalog` теперь возвращает `_uptime_pct_24h`, health поля в `_installed`.

### Frontend
- **Health dot** (🟢/🟡/🔴/○/●) перед именем сервиса на карточке. Tooltip: HTTP code + RTT. Down — pulse animation.
- **Stat badges** на карточке: `↑ 99.5%` uptime, `↻ 3` restart count (только если > 0). Цвет: ok/warn/err.
- **Chart.js график** в модалке Ресурсы: CPU% (синий) + RAM MB (зелёный), 4 диапазона (1ч/6ч/24ч/7д). Двойная Y-ось.
- **Раздел «Стабильность»** в модалке: uptime % за 24ч + restart count + health status с RTT.

### Notes
- Метрики начнут накапливаться **после deploy v2.5** — первые 24ч графики будут неполными. Это нормально.
- Health-check только для сервисов с `web_url` в манифесте. Probe идёт на `127.0.0.1` (локально), не нагружает сеть.

---

## 2.4.0 — 2026-06-30

**Аудит-лог: full coverage** — закрыты все state-mutating endpoints, фронт показывает детали + фильтры.

Контекст: аудит покрытия выявил **14 state-mutating endpoint'ов в network.py без логирования**, в т.ч. credentials CRUD (security-critical). Также `audit_action()` имел параметр `log_details` который ни разу не использовался — все записи `details=NULL`.

### Added (security-critical)
- **`_audit()` helper** в `network.py` — пишет в `auth_audit` от текущего юзера. Безопасный (не падает если нет сессии).
- **14 endpoints теперь логируют действия с метаданными** (никогда не пишем пароли — только `password_changed: true/false`):
  - **Credentials CRUD** (3): `credential_create/update/delete` — кто/когда/какое устройство, без пароля
  - **Network device CRUD** (4): `network_device_create/update/delete/bulk_update`
  - **Network types CRUD** (3): `device_type_create/rename/delete`
  - **Photos** (2): `device_photo_upload/delete`
  - **Misc** (2): `network_reclassify`, `network_settings_update`, `network_bulk_tags`

### Added (фильтры)
- **`/api/auth/audit`** теперь поддерживает: `?action=...` (partial LIKE), `?result=success|fail`, `?to=...` (верхняя граница ts), плюс существующие `since`, `username`, `limit`, `offset`.

### Frontend (audit log UI)
- **Новые фильтры**: по действию (поиск частичный — «credential» найдёт create/update/delete), по результату (✓/✗), по периоду (час/24ч/7д/30д/всё). Debounce 300мс для текстовых полей.
- **Новая колонка «Детали»** — раскрывает payload запроса (mac/ip/name/label/etc, без секретов). Полный JSON в tooltip.
- **20+ новых меток действий** с эмоджи для удобной идентификации.

### Security notes
- Пароли credentials **никогда** не попадают в audit (даже маскированными). Логируется только факт изменения через флаг `password_changed`.
- Для DELETE endpoints читаем метаданные ДО удаления — иначе после делита знаем только ID.

---

## 2.3.2 — 2026-06-30

**Critical fix** — дашборд не auto-started при boot на Pi из-за systemd ordering cycle.

### Fixed
- **systemd ordering cycle с argononed.service** → systemd удалял job старта `smartcomm-dashboard.service` при boot. Цикл: `multi-user.target` wants дашборд → дашборд `After=argononed.service` → argononed `After=multi-user.target`. На Cubi проблемы не было (нет argononed). Убрал `argononed.service` из `After=` шаблона.
- **`Restart=on-failure` → `Restart=always`** — теперь systemd рестартит дашборд даже при clean exit (раньше игнорировал exit code 0).

### Added
- **Persistent journal** на Pi (`/var/log/journal`) — журнал переживает reboot, можно разбирать silent freeze post-mortem.
- **Hardware watchdog на Pi** (BCM2835 timer, `RuntimeWatchdogSec=30s`) — auto-reboot за 30с при kernel freeze.
- **dmesg noise filter**: добавлены 2 безобидные ошибки Pi
  - `brcm-pcie 1000110000.pcie: link down` — PCIe слот пустой (нет NVMe), нормально
  - `brcmf_cfg80211_reg_notifier: Firmware rejected country setting` — WiFi unused (только Ethernet)

### Operational (на устройствах, не код)
- Отключён `smartmontools.service` на Pi — был `failed` (на SD-карте нет SMART). На Cubi (с SATA SSD) — оставлен включённым.

### Context
Pi сам перезагрузился 2026-06-30 10:15 (после 6 дней uptime). После reboot дашборд не запустился. Причина reboot — НЕИЗВЕСТНА (journal был volatile, логи потеряны). Теперь journal persistent + watchdog + ordering cycle fix → следующая perturbation будет полностью диагностируема.

---

## 2.3.1 — 2026-06-30

**UI fix** — Pi-specific формулировки в подтверждениях/уведомлениях убраны.

### Changed
- Подтверждение «Остановить iRidium» — «...не перезагрузишь **Pi**» → «...не перезагрузишь **контроллер**»
- Подтверждение «Reboot» — «Перезагрузить **Pi** целиком? Контроллер пропадёт» → «Перезагрузить **контроллер** целиком? Все сервисы пропадут»
- Toast после `/api/action/reboot` — «**Pi** перезагрузится через 2 сек» → «**Контроллер** перезагрузится через 2 сек»
- Toast после `/api/action/shutdown` — аналогично
- Help-секция «Управление в шапке» — «Кнопка Argon (на **Pi** с Argon ONE V3)» → «...только на **Raspberry Pi** с Argon ONE V3 кейсом»
- Argon-modal — «Reboot Pi», «выключенном Pi», «Pi загружается» → «контроллера», «контроллере», «контроллер»

Why: на Cubi и других x86-неттопах слово «Pi» вводило в заблуждение. Платформо-нейтральная формулировка «контроллер» работает везде. Raspberry Pi осталось только там где речь конкретно про Pi-hardware (Argon-кнопка существует только на Pi-кейсе).

---

## 2.3.0 — 2026-06-30

**SMART-мониторинг диска + hardware watchdog ready** — повышение надёжности после silent freeze на Cubi 2026-06-30 07:05.

### Added
- **Плитка «Накопитель» в «Состояние системы»** с поддержкой SMART для SATA/NVMe (x86 платформы). На Pi показывает SD-карту / eMMC как раньше; на Cubi и других x86 — модель диска, статус SMART (PASSED/FAILED), температура, % оставшегося ресурса (lifespan), bad sectors, годы работы, объём записанного. Цвет: 🟢 ok / 🟡 warn (< 20% жизни или bad sectors > 0) / 🔴 err (SMART failed).
- **install.sh: добавлен smartmontools** в REQUIRED apt-пакеты — `smartctl` теперь доступен для бэкенда сразу после установки.

### Backend
- Новая функция `smart_disk_health()` в `dashboard.py` — парсит `smartctl --json` для root-диска (только SATA/NVMe, mmcblk обрабатывает `sdcard_health()`). Возвращает model, smart_passed, temp_c, lifespan_pct, reallocated_sectors, power_on_years, written_tb.
- `/api/status` теперь возвращает оба поля: `sdcard` (Pi) и `smart` (x86), фронтенд показывает то что доступно.

### Operational notes (не код, что сделано в инфре)
- **Hardware watchdog (iTCO_wdt) включён на Cubi** через systemd (`RuntimeWatchdogSec=30s`). Защита от silent freeze: если ядро зависнет на 30+ сек → железо принудительно перезагружает CPU. Identity = `iTCO_wdt`, timeout = 30s, state = active.
- **MikroTik netwatch для Cubi (101.12)**: автоматический мониторинг с интервалом 1 мин, лог UP/DOWN в RouterOS journal.
- **Catalog v0.8.0**: добавлены `mem_limit` для immich (4g/3g/256m/1g), ollama (8g/1.5g), frigate (4g). Защита от runaway containers. Применятся через manual Update в дашборде.

### Context (что произошло)
Cubi умер silent freeze в 07:05:49 (после 7ч аптайма с установленными 12 сервисами), пролежал 2.5 часа до ручного включения. В журналах нет panic/OOM/thermal — kernel так глубоко завис что не успел залогировать. Watchdog + netwatch резко сократят downtime будущих случаев.

---

## 2.2.1 — 2026-06-30

**Hotfix v2.2.0** — pre-create volume target dirs перед `docker compose up -d`.

### Fixed
- **n8n crash loop** «EACCES: permission denied, open '/home/node/.n8n/config'» — корень: docker-compose сам создавал volume bind-mount target dirs от root, контейнер от node user (UID 1000) не мог писать. Теперь `_install_worker` парсит YAML compose.yml и **pre-creates все volume dirs от service-user'а** (cubi/pi, UID 1000) до запуска compose up. Любой контейнер с user UID 1000 (n8n, immich-server, и др.) теперь пишет без проблем.

### Caveats
- Только bind mounts с путём начинающимся на `{DATA}/...` (= `/var/lib/smartcomm-services/<id>/`) pre-create'нятся. Named volumes (Docker-managed) не трогаем — docker сам с ними справляется.

### Дополнительно (в каталоге, не в дашборде)
- AdGuard манифест: убрали публикацию `:443` (был conflict с 3X-UI Reality VPN)
- Nextcloud AIO: master container `:8080` → `:8800` (был conflict с SmartComm Dashboard :8080)
- Ollama OpenWebUI: `:3000` → `:3030` (был conflict с AdGuard initial setup :3000)
- **AmneziaWG → wg-easy** (классический WireGuard) — `docker.amnezia.org` DNS не работает, image не качается. Wg-easy с web UI на Docker Hub, для DPI-обхода РКН остаётся 3X-UI Reality

## 2.2.0 — 2026-06-30

**Auto-update сервисов магазина** — закрытие оставшейся фичи v2.0.0 (тогда было только UI-настройка never/weekly/monthly без реальной логики).

### Added — Backend
- **`update_service(id, source='manual')`** в services.py — `docker compose pull` + `up -d` в фоновом потоке. Использует существующий per-service install lock — не race'ит с install/uninstall.
- **`_auto_update_loop()`** — background thread (стартовая задержка 5 мин, потом цикл раз в час). Проверяет installed services с `auto_update=weekly/monthly`, для due-сервисов вызывает `update_service(sid, source='auto')`. Между сервисами 30с задержка (Docker Hub throttling).
- **`_should_auto_update(inst)`** — bool helper: `(now - last_auto_update_at) >= interval` (7d / 30d). Пропускает сервисы в error/installing/updating.
- **Migration v2** — две новые колонки в `installed_services`:
  - `last_auto_update_at` INTEGER — Unix timestamp последнего auto-update
  - `last_auto_update_ok` INTEGER (1/0) — успешно ли прошло
- Запуск `ensure_auto_updater_started()` в dashboard init

### Added — API
- **`POST /api/services/<id>/update`** — manual «Обновить сейчас». Admin + audit. Прогресс через тот же `/install-progress` endpoint что и install.

### Added — UI
- **Кнопка «⬆ Обновить»** в модалке installed-сервиса (между «Запустить/Остановить» и «Удалить»). Confirm dialog «Контейнер будет пересоздан, сервис недоступен 10-60 сек».
- В секции «Настройки» под select'ом auto_update показывается **«Последнее обновление: DATE TIME»** если поле заполнено, или «Образ ни разу не обновлялся (стоит первоначальная версия)»
- Если `last_auto_update_ok=0` — красный ⚠ маркер

### Why
В v2.0.0 был UI-select auto_update (never/weekly/monthly) — но без реальной фоновой логики. Сейчас закрыто: настройка работает, образы реально пуллятся по расписанию, в UI видно когда последний раз обновлялся.

### Совместимость
- Migration v2 — `ALTER TABLE ADD COLUMN` с try/except (idempotent на старых БД)
- Существующим инсталляциям auto_update остаётся `never` (default) — никаких неожиданных обновлений
- Сервисы со status `error`/`updating`/`installing`/`uninstalling` auto-updater пропускает (defensive)

## 2.1.1 — 2026-06-30

`install.sh` теперь ставит `sqlite3` CLI — пригодится админу для ручных DB-операций (debugging, cleanup).

### Why
Когда устанавливали Базовый пакет на Pi через API (v1.6.0–v2.0.x), cleanup-скрипт упал с `sudo: sqlite3: command not found` потому что Pi был с минимальной установкой Debian. Пришлось делать чистку через Python `sqlite3` module. Чтобы инсталляторы не натыкались на это — добавили sqlite3 в `REQUIRED` apt-пакеты.

### Note: install_profile pre-check уже в v2.1.0
Тот же запрос про «фикс profile installer pre-check (Docker)» — уже сделан в v2.1.0 (ночной аудит): `install_profile()` теперь делает полный `install_pre_check()` для каждого сервиса и если Docker не установлен — блокирует весь batch с понятной ошибкой и инструкцией.

## 2.1.0 — 2026-06-30

**Stability release** — углублённый дебаг 4-х параллельных аудиторов нашёл 27 находок, из них критичные пофикшены. Никаких новых фич — только надёжность, безопасность и UI-полировка.

### Fixed — Threading (HIGH)
- **Race condition на `_IFACE_CACHE`** (dashboard.py) — concurrent requests могли портить timestamp. Добавлен `_IFACE_CACHE_LOCK`
- **Race condition на `_UPDATES`** (apt-upgrades) — background-thread vs request-thread. Lock + `_updates_snapshot()` helper
- **Race condition на `_HEALTH`** — health_summary read/write. Добавлен `_HEALTH_LOCK`
- **Per-service install lock** в services.py — два `install` для одного сервиса (двойной клик / install+profile-batch) больше не race'ят за compose.yml и data dir. `_INSTALL_LOCKS[sid]` non-blocking try-acquire
- **Безопасная замена `log` deque в `_progress_set`** — раньше worker мог заменить deque пока другой поток её читал из `get_progress()` → crash. Теперь `_progress_set` не принимает `log`, для очистки `_progress_reset_log()` под локом
- **Timeout в `_stream_subprocess`** — раньше timeout проверялся только после `readline()`, но readline мог блокировать вечно. Теперь через `threading.Timer` который kill'ит процесс независимо от readline-блокировки

### Fixed — Security/Safety
- **Validation pid в `process_uptime_sec`** — shell injection defence-in-depth. `pid_s.isdigit()` перед `f"ps -p {pid_s}"`
- **`safe.directory=*` wildcard** в git заменён на explicit `safe.directory={CATALOG_DIR}` — на старых git wildcard игнорировался, безопаснее явно
- **Pre-check Docker в `install_profile`** — раньше profile installer не проверял что Docker есть → silently fail с «3 fail: docker: command not found». Теперь блокер: останавливает batch и показывает инструкцию для установки

### Fixed — Detection
- **UDP ports в `_busy_ports`** — раньше ss -t (только TCP). Сервисы с UDP (AdGuard :53, AmneziaWG :51820) → port-check ложно говорил «свободно». Теперь ss -tlnH + ss -ulnH
- **Status distinction `dead` vs `exited`** — раньше оба → "stopped". Теперь dead → error, exited (нормальный stop) → stopped. Mix → "stopped"

### Fixed — Frontend
- **Z-index toast** — был 200 vs modal 100, но при открытой модалке toast прятался. Поднят до 1000
- **`autocomplete="current-password"`** на uninst-pw (раньше "off" — deprecated, не работает с password managers)
- **Disabled state для action-кнопок** — `serviceAction(id, action, btn)` принимает кнопку и блокирует её на время request (предотвращает double-click → дублирующий restart/stop)
- **Null-guards для DOM ops после fetch** — если модалка закрыта пока polling/logs шли, не падать. `pollProgress`, `showServiceLogs`
- **HTTP error checks** — `installService`, `pollProgress`, `serviceAction`, `showServiceLogs` теперь все проверяют `response.ok` перед `.json()` (раньше silent failure на 500)
- **pollProgress catch** — раньше silent (`catch(_) {}`). Теперь `console.warn` для отладки

### Fixed — UX polish (кнопки одинаковой ширины)
- В .modal-actions все кнопки теперь равной ширины через `flex: 1 1 110px`. Раньше: Открыть (большая btn-primary) vs Логи (маленькая btn-action) — визуальный мусор. Сейчас единый ряд из равных кнопок
- Унифицированы цветовые варианты `.btn-action.c-{green,amber,red,blue,mute,purple}` — раньше inline-style для каждого, теперь классы
- Добавлены `aria-label` для icon-only кнопок (a11y)
- `.btn-action:disabled` стиль (opacity 0.55)

### Removed
- Мёртвый код: `_currentService` (был установлен но не читался), `servicesCountsLoad` shim на /services странице

### What's NOT fixed (отложено — не критично или design issues)
- Credentials в plaintext в DB (mikrotik/iridium/device passwords) — это design decision, требует Fernet/keyring infrastructure
- Orphan-container discovery (контейнер без compose.yml в `/var/lib/...`) — низкая вероятность, добавим в v2.2
- Naive YAML placeholder replace `{DATA}` etc — edge case если в комментариях; пока не критично
- Cross-CSS sync (3 :root блока) — косметика, делать после большого редизайна

## 2.0.1 — 2026-06-30

UX-фиксы по запросу пользователя.

### Fixed
- **Favicon на странице `/services`** — не отображался. Добавлен inline SVG (как в index.html) — синий квадрат «SC»
- **Summary-плитки** на странице сервисов — переделаны для понятности. Раньше показывалось разрозненно «3 запущено · 0 не запущено · 12 доступно»; теперь: «3 установлено (из них 3 запущено · 0 остановлено) · M доступно к установке · K не подойдёт · 12 всего в каталоге»
- **«Доступно к установке»** теперь = compatible **минус installed** (раньше показывало все compatible, включая уже-стоящие → запутывало)

### Removed
- **`pricing_hint_rub`** из всех 12 манифестов сервисов и 3 профилей — поле и упоминания «~X ₽/мес ценность для клиента» убраны из:
  - YAML манифестов (`smartcomm-services-catalog/services/*.yaml` + `profiles/*.yaml`)
  - UI: бейдж на карточке профиля, бейдж в модалке детали профиля, бейдж в модалке сервиса
- Денежные суммы будут добавлены отдельно когда будем обсуждать monetization

## 2.0.0 — 2026-06-30

**🎉 MAJOR — Магазин Phase 4: пакеты «Базовый/Стандарт/Премиум» + ресурсы live + заметки. Конец построения магазина.**

### Added — Готовые пакеты (Profiles)
Новый раздел в каталоге `smartcomm-services-catalog/profiles/` с 3 пакетами:
- **🟢 Базовый** (600 MB RAM) — AdGuard + Vaultwarden + Uptime Kuma. Для семей в загородных домах
- **🔵 Стандарт** (6.5 GB RAM) — Базовый + 3X-UI + AmneziaWG + Nextcloud + n8n. Активные пользователи
- **🟣 Премиум** (18 GB RAM) — Стандарт + Ollama + Jellyfin + Immich + Frigate. Только мощные неттопы (Cubi 5 16GB+)

На странице `/services` сверху появилась секция «📦 Готовые пакеты» с 3 большими карточками. Progress bar показывает сколько из пакета уже стоит. Один клик — install всех недостающих сервисов последовательно (worker-thread с общим progress log).

### Added — Resource usage live (docker stats)
- Background sampler раз в 30с `docker stats --no-stream --format` → CPU%, MEM MiB, MEM%, NET RX/TX → in-memory `_STATS` dict
- В карточке installed-сервиса бейдж **«⚡ 0.4% · 145M»** (CPU · RAM)
- В модалке installed-сервиса — 4 stat-плитки: CPU, RAM, Net RX, Net TX
- Aggregate если у сервиса несколько контейнеров (Immich/Nextcloud — multi-container)
- API: `GET /api/services/<id>/stats`, поле `_stats` в catalog response

### Added — Notes per service + auto_update
В модалке installed-сервиса:
- **📝 Заметки админа** — textarea (до 2000 chars). Например «Установлено для клиента ИП Иванов, оплачено до 15.11.2026». Сохраняется в БД (`installed_services.notes`).
- **🔄 Автообновление** select: never (default) / weekly / monthly. Сохраняется в `auto_update`. *(Сам cron-pull в v2.1 — пока settings UI готов, фоновый updater подключим следующим релизом.)*
- API: `PATCH /api/services/<id>/settings` body `{notes, auto_update}`

### Fixed — dubious ownership при «Обновить каталог»
- `git pull` падал с `fatal: detected dubious ownership` (git 2.x security): каталог owned root, flask-процесс под service-user.
- Фикс: все git-команды в `refresh_catalog()` и `catalog_status()` теперь через `sudo git -c safe.directory=*` — обходим check без записи в `/etc/gitconfig` (опасно для всей системы).
- Также `git pull` заменён на `fetch + reset --hard origin/main` — устойчивее к локальным изменениям файлов (если кто-то отредактировал YAML вручную через SSH).

### API эндпоинты добавленные в v2.0.0
- `GET /api/services/profiles` — список пакетов с installed_count/total
- `POST /api/services/profiles/<id>/install` — batch-установка пакета (admin + audit)
- `GET /api/services/<id>/stats` — live ресурсы
- `PATCH /api/services/<id>/settings` — notes + auto_update

### Why MAJOR версия
v1.0.0 → v2.0.0 это закрытие большого rotation:
- v1.0-1.4: dashboard core + multi-platform + bug fixes
- v1.5: каталог + UI (Phase 0+1)
- v1.6: install/uninstall (Phase 2)
- v1.7: лайфцикл (Phase 3)
- v2.0: пакеты + ресурсы + админ-инструменты (Phase 4)

Магазин теперь **production-ready** для тиражирования по клиентам. Дальше — v2.1+ это уже эволюция (auto-update cron, backup management UI, Telegram-нотификации).

## 1.7.0 — 2026-06-30

**Магазин Phase 3 — лайфцикл сервисов + критический фикс БД.** После установки сервиса теперь видно что он установлен, можно открыть его портал, остановить, перезапустить, посмотреть логи.

### Fixed — критический баг v1.6.x: «установлено 0 хотя сервис стоит»
- `services.py` хардкодил `DB_PATH = "/var/lib/smartcomm-dashboard/network.db"`, но реальная БД — `inventory.db`. SQLite молча создавал пустой файл, все upsert'ы уходили в никуда. Корень всех проблем «магазин не помнит установленные сервисы»
- Теперь `DB_PATH = str(network.DB_PATH)` — импорт из network.py, без дублирования

### Fixed — pre-check «порт занят» для уже установленного сервиса
- Раньше: если сервис уже стоит (Uptime Kuma на :3001), pre-check показывал «порт 3001 занят» → кнопка «Установка недоступна» (хотя занят собой)
- Теперь: проверка `already_installed = get_installed(id) is not None`. Если installed — пропускаем порт-чек, показываем `«✓ Порты 3001 заняты самим сервисом (норма)»`

### Added — discovery existing services при старте
- `discover_existing()` при старте дашборда сканирует `/var/lib/smartcomm-services/*/compose.yml` и регистрирует в БД те которые не записаны (компенсирует баг v1.6.x когда установка прошла но БД не обновилась)
- Применяется автоматически — после deploy v1.7.0 уже-установленные сервисы появятся в БД как `installed`/`running`

### Added — background status sampler
- Раз в 30с `docker inspect <container>` для каждого installed сервиса
- Автоматически меняет status в БД: `running` если все контейнеры up, `stopped` если exited, `error` если контейнеры исчезли
- Запускается через `ensure_sampler_started()` при старте дашборда

### Added — лайфцикл actions
- **API**: `POST /api/services/<id>/action` body=`{action: start|stop|restart}`. Все требуют admin auth + audit. После успеха обновляет status в БД.
- **API**: `GET /api/services/<id>/logs` — последние 100 строк `docker compose logs`

### Added — UI для installed-сервиса
Модалка деталей для installed теперь показывает:
- **🔗 Открыть** — кнопка-ссылка на web_url сервиса (новая вкладка, замена `{CONTROLLER}` на текущий host)
- **📜 Логи** — попап с docker compose logs (auto-scroll к низу, кнопка обновления)
- **▶ Запустить / ⏸ Остановить** (в зависимости от текущего status)
- **↻ Перезапустить**
- **🗑 Удалить** (как раньше, с password confirm)

### Added — web_url в каждом манифесте каталога
Каталог обновлён: 12 манифестов получили поле `web_url` с `{CONTROLLER}` placeholder'ом. Frontend заменяет на текущий host. Используется для кнопки «🔗 Открыть» в модалке installed + для зелёной кнопки «Открыть» в финале успешной установки.

### Added — успех установки → кнопка «Открыть»
По завершении install в progress modal появляется большая зелёная кнопка **«🔗 Открыть NAME»** — клик открывает портал сервиса сразу в новой вкладке. Не нужно копировать URL вручную.

### Why
v1.5/1.6 строили фундамент. v1.7 закрывает базовый UX: сервис установлен → видно что установлен → можно открыть/стоп/рестарт/удалить из одного интерфейса. До этого был сырой workflow («установил → теперь ищи где открывать, как остановить»).

## 1.6.1 — 2026-06-30

**Hotfix v1.6.0** — установка падала с PermissionError.

### Fixed
- `_install_worker`: при создании папки `/var/lib/smartcomm-services/<id>/` падал с `PermissionError` потому что `/var/lib/smartcomm-services/` owned root, а python был от service-user (`cubi`/`pi`). Теперь сначала `sudo mkdir -p` + `sudo chown -R <uid>:<gid>` — после chown дальнейшая работа (запись compose.yml, etc) идёт без sudo.
- Найдено первым же smoke-test'ом установки Uptime Kuma на Cubi.

## 1.6.0 — 2026-06-30

**🛍 Магазин сервисов — Phase 2: реальная установка и удаление.**

### Added — Backend install/uninstall
- `install_service(id)` — запускает установку в **фоновом потоке**, не блокирует HTTP запрос
- `uninstall_service(id, delete_data=False)` — фоновое удаление с auto-backup данных в `_backups/`
- `_render_compose(manifest)` — генерация compose.yml с заменой placeholder'ов (`{DATA}`, `{MEDIA}`, `{TZ}`, `{CONTROLLER}`)
- `_stream_subprocess()` — запускает docker compose с live-streaming вывода → in-memory log buffer (последние 500 строк, 50 показывается в UI)
- `_PROGRESS` global state с phase-tracker: `queued → preparing → pulling → starting → running` (или `error`)
- Timeout'ы: pull=15min (immich/nextcloud занимают долго), up/down=3min

### Added — API endpoints
- **`POST /api/services/<id>/install`** — admin auth + audit log. Возвращает сразу `{ok:true, message:"установка запущена"}`. Реальный процесс в фоне.
- **`POST /api/services/<id>/uninstall`** — admin auth + audit. Body: `{password, delete_data}`. **Повторная проверка пароля** даже для admin сессии (защита от случайного клика). По умолчанию data сохраняется в `/var/lib/smartcomm-services/_backups/<id>-<ts>.tar.gz`.
- **`GET /api/services/<id>/install-progress`** — polling (UI опрашивает каждые 2с). Возвращает `phase`, `phase_label` (русский), `elapsed_sec`, `state`, last 50 log lines, `error`.

### Added — UI
- Кнопка **«Установить»** в модалке деталей сервиса теперь работает — после pre-check ✓
- **Live progress modal** — заголовок «⏳ docker compose pull», timer (`Xс`), state badge, ScrollPane с логом docker (auto-scroll к новым строкам)
- Кнопка **«🗑 Удалить»** в модалке для installed-сервисов:
  - Модалка double-confirm с password input (admin password проверяется backend'ом повторно)
  - Чекбокс «Удалить также данные сервиса» (по умолчанию OFF → данные в backup)
  - Visual warning (красный border, ⚠ иконка)
- Toast при успехе/ошибке с авто-refresh каталога

### Why
v1.5.0 был фундамент (каталог + UI). v1.6.0 даёт реальную возможность ставить и удалять сервисы из дашборда — это core-функционал «магазина для контроллера».

### Безопасность
- Все install/uninstall actions требуют **admin auth** (декоратор `@requires_admin`)
- Uninstall требует **повторную проверку пароля** — даже если сессия активна, без правильного пароля не сработает (защита от случайного нажатия)
- Удаление data — **отдельный opt-in checkbox** в UI, по умолчанию OFF
- Auto-backup перед удалением (если data сохраняется) в `_backups/<id>-<unix_ts>.tar.gz`
- `audit_action` декоратор пишет каждое install/uninstall с username + service_id в audit_log

### Известные ограничения (для v1.7.0+)
- Нет «отмены» в процессе установки (kill всё-таки можно через Portainer)
- Нет live-обновления статуса контейнера после `up` (полагаемся на `docker compose ps` — добавим в v1.7.0)
- Нет UI для start/stop/restart уже установленного — будет в v1.7.0 вместе с health-check
- Логи docker — только последние 500 строк per service, не persistent

## 1.5.0 — 2026-06-30

**🛍 Магазин сервисов** — каталог + UI + pre-install чек (Phase 0+1).
Установка/удаление будут в v1.6.0.

### Added
- **Каталог сервисов** — отдельный repo [moshonkinaa/smartcomm-services-catalog](https://github.com/moshonkinaa/smartcomm-services-catalog) с 12 YAML манифестами:
  - **VPN с обходом РКН**: 3X-UI Reality, AmneziaWG
  - **AI**: Ollama + Open WebUI (локальный ChatGPT)
  - **Медиа**: Jellyfin (Netflix дома), Immich (Google Photos дома)
  - **Облако**: Nextcloud AIO (Dropbox + Office дома)
  - **Безопасность**: AdGuard Home, Vaultwarden
  - **Умный дом**: Frigate NVR (NVR с AI), Uptime Kuma
  - **Автоматизация**: n8n (no-code + AI workflows)
  - **Админ**: Portainer (только инсталлятору)
- **Backend** `services.py` — загрузка/парсинг YAML, кеш каталога, БД installed_services
- **API endpoints** (все под auth):
  - `GET /api/services/counts` — для счётчика в шапке
  - `GET /api/services/catalog` — весь каталог + флаги совместимости с текущей платформой
  - `GET /api/services/installed` — что установлено
  - `GET /api/services/<id>/pre-check` — RAM/disk/ports/docker проверки перед установкой
  - `POST /api/services/refresh` — git pull каталога (admin only)
- **UI** — кнопка «🛍 Сервисы» в шапке дашборда с динамическим счётчиком (`N запущено · M установлено · K в магазине`)
- **Страница `/services`** — карточки каталога, поиск, фильтр по 9 категориям, модалка деталей с pre-check, badge-индикаторы статусов (running/installed/incompat)
- **Migration v1** — таблица `installed_services` в БД (id, status, installed_at, last_started_at, settings_json, notes, auto_update)
- **install.sh** обновлён: ставит `python3-yaml` + `git` через apt, клонирует каталог в `/opt/smartcomm-services-catalog/`, копирует новые файлы `services.py` + `services.html`
- В каталог сервисов фильтруется автоматически по архитектуре (Pi видит только сервисы с `arm64` в platforms; Cubi — `x86_64`), RAM, диску

### Why
Финальная цель платформы — **магазин сервисов** для тиражирования по клиентам. Контроллер MSI Cubi 5 покупается ради этого. v1.5.0 — фундамент: видеть что доступно, что установлено, что подойдёт твоему железу. v1.6.0 — реальная установка через docker compose. v2.0.0 — пакеты «Базовый/Стандарт/Премиум».

### Совместимость
Полная backward-compat. Если каталог не склонирован (`/opt/smartcomm-services-catalog/` отсутствует) — backend вернёт `services_count: 0`, кнопка покажет «🛍 Сервисы». UI работает без падений.

## 1.4.4 — 2026-06-30

`install.sh` теперь применяет платформенные фиксы автоматически по DMI vendor+product.

### Added — auto-blacklist Intel DPTF на MSI Cubi
Новый шаг **`[6/7] Platform-specific fixes`** в `install.sh`:
- Читает `/sys/class/dmi/id/sys_vendor` и `/product_name`
- Если **vendor=`*Micro-Star*` и product=`*Cubi*`** — создаёт `/etc/modprobe.d/blacklist-int3400.conf` с blacklist'ом `int3400_thermal` / `int340x_thermal_zone` / `int3402/3_thermal` / `intel_pch_thermal`
- Делает `update-initramfs -u`
- В финальном сообщении напоминает что нужен reboot
- Идемпотентно — если файл уже есть, не трогает

### Why
В v1.4.3 я (вручную) применила blacklist на конкретно нашем Cubi и это убрало 90% ACPI BIOS errors (40 → 4-6). Чтобы каждый новый MSI Cubi, который инсталлятор будет ставить у клиентов, **сразу** разворачивался чистым — а не повторял ручную процедуру — закрепил это в installer'е. Это первый «platform-specific quirk» в installer'е — задел под расширение (Intel NUC, AMD mini-PC и пр. могут иметь свои фиксы).

### Что важно
- На Pi и не-MSI hardware скрипт ничего не делает — просто говорит "(нет известных платформенных фиксов для этого hardware)"
- Безопасно: coretemp (через MSR) и fan control (через BIOS) продолжают работать после blacklist
- В CHANGELOG.md в плитке «Версия» теперь видны все три новых хотфикса v1.4.x (после v1.4.2 фикса с CHANGELOG)

## 1.4.3 — 2026-06-29

Фильтр известного firmware-шума в плитке «dmesg ошибки».

### Why
На MSI Cubi 5 (MS-B0A8) BIOS-баг: ACPI-методы `_SB.PC00.SEN1._TMP` и `_SB.PC00.TFN1._FST` ссылаются на несуществующий символ `.MPAG`. Каждые ~10 секунд Linux пытается прочитать температуру через эти методы → генерирует `ACPI BIOS Error` / `ACPI Error: Aborting method`. Дашборд показывал «dmesg: 5 ошибок» (warn-плитка) — но это **БАГ ПРОШИВКИ**, не системы, **не actionable** администратором. CPU температура читается корректно через coretemp (MSR), кулер управляется BIOS — реального ущерба нет.

### Fixed — фильтрация в `dmesg_errors()`
- Известные firmware-bug паттерны исключаются из счёта **«реальных»** ошибок:
  - `ACPI BIOS Error`
  - `ACPI Error: Aborting method`
  - `Unable to get temperature, disabling` (производная от первых двух)
  - `Disabled thermal zone with critical trip point` (Linux отключает зону когда _TMP не работает)
- Добавлен `dmesg_firmware_noise_count()` — отдельный счётчик известного шума
- Плитка теперь показывает: **«dmesg · без реальных ошибок · N известных BIOS-шумов отфильтровано»** (`ok` зелёная вместо `warn` оранжевой)
- Если придут НАСТОЯЩИЕ ошибки (например проблемы с диском, OOM-killer, RAID) — они НЕ фильтруются, показываются как раньше

### Дополнительно — для Cubi 5 и подобных MSI
На контроллере [Cubi-101.12]:
```
/etc/modprobe.d/blacklist-int3400.conf
  blacklist int3400_thermal
  blacklist int340x_thermal_zone   # подгружается через зависимости — не помогает полностью
  blacklist int3402_thermal
  blacklist int3403_thermal
  blacklist intel_pch_thermal
```
Результат: ACPI BIOS errors 40 → 4 (90% уменьшение). Оставшиеся 4 — `TFN1._FST` через зависимый `int340x_thermal_zone`. С фильтром этого релиза эти 4 не считаются ошибками.

### Совместимость
Полная backward-compat. На платформах БЕЗ firmware-шума — поведение идентично (фильтр не находит совпадений, всё ошибки показываются как раньше).

## 1.4.2 — 2026-06-29

Два cross-platform бага найдены при сравнении Pi vs Cubi на разных подсетях.

### Fixed — subnet detection ловил loopback на x86
`detect_subnet()` пробовал `ip -4 -o addr show eth0`, и если eth0 нет (Cubi: `enp45s0`) — делал fallback на `ip -4 -o addr show` (все интерфейсы). Regex `re.search` брал ПЕРВОЕ совпадение, а это loopback `lo` 127.0.0.1/8.
- Симптом: на портале Cubi показывалась подсеть **127.0.0.0/8** вместо 192.168.101.0/24
- Также все nmap-сканы шли по loopback — обнаруживалось 0 устройств
- Фикс: определяем primary interface через default route в `/proc/net/route`, читаем его IP/маску, явно пропускаем `127.*` адреса. Fallback chain: default-route iface → первый UP не-lo интерфейс из `/sys/class/net/`

### Fixed — модалка «История версий» пустая
`/api/changelog` возвращал 404 «changelog not found» потому что `CHANGELOG.md` никогда не копировался в `/opt/smartcomm-dashboard/` — `install.sh` не включал его в `REQUIRED_FILES`, и мои deploy-скрипты (cp dashboard.py + index.html) тоже его не подвозили.
- Симптом: модалка «Версия X.Y.Z» в шапке открывалась с пустым списком версий или ошибкой
- Фикс 1: `install.sh` теперь копирует CHANGELOG.md и marked.min.js если они есть в source (новая секция `OPTIONAL_FILES`)
- Фикс 2: `/api/changelog` имеет fallback на raw.githubusercontent.com — если файла нет локально, скачивает с GitHub (для legacy инсталляций где `install.sh` пробежал до v1.4.2). Ответ возвращает поле `source: "local"` или `source: "github"` для прозрачности.

### Why
Эти баги были невидимы пока работал только на Pi (где eth0 существует и `CHANGELOG.md` был залит вручную через `deploy_dashboard.ps1`). При cross-platform compare на Cubi оба всплыли мгновенно. v1.4.1 был «closes major gaps», v1.4.2 — последний хвост.

## 1.4.1 — 2026-06-29

**Hotfix v1.4.0** — auto-detect путей iRidium Server (разные версии iRidium держат данные в разных местах).

### Fixed
- **`iridium_project_info()` и `iridium_db_size()` возвращали None на Cubi 5** — потому что захардкожен путь `/var/lib/iRidium Server/Documents/` (iRidium 1.x) а Cubi с iRidium **2.3.86 .deb запускается от root** → реальный путь `/root/iRidium Server/DataBase/IridiumStorageV4.db`. Это означало что плитка «iRidium · детали» на Cubi не показывала размер БД и friendly_name проекта.
- Также фикшу `primary_iface()` который кешировал `eth0` fallback на 60с при раннем boot когда default route ещё не настроен (попал в v1.4.0 как hotfix отдельным коммитом)

### Added
- `iridium_paths()` — auto-detect через `/proc/<irserver_pid>/fd` (lsof открытых файлов с подстрокой `iRidium Server`). Кешируется на жизнь процесса дашборда (refresh при отсутствии).
- Fallback chain если auto-detect не сработал: `/root/iRidium Server` → `/var/lib/iRidium Server` → `/home/pi/iRidium Server`

### Why
Cross-platform compare между Pi (95.167, iRidium 1.3.87) и Cubi (101.12, iRidium 2.3.86) показал что 9 полей в `/api/status` различаются. Корень из 9 — этот один баг с путями. Остальные различия — нормальные (Pi имеет 4 ядра CPU vs Cubi 12, разные подсети, hostname, и т.п.). Frontend gracefully обрабатывает `null` для Pi-specific fields (sdcard, voltage, fan_pct), эти места не трогали.

### Совместимость
Полная backward-compat. На Pi auto-detect найдёт ту же `/var/lib/iRidium Server/` что и раньше через lsof, либо fallback chain. На Cubi теперь корректно находит `/root/iRidium Server/`.

## 1.4.0 — 2026-06-29

**Multi-platform support** — дашборд теперь работает не только на Raspberry Pi, но и на x86 (Intel/AMD неттопы). Связано с разворачиванием первого production-контроллера MSI Cubi 5.

### Fixed — На x86 теперь работают
- **Температура CPU** — раньше `vcgencmd measure_temp` (Pi-only) → `None` на x86. Теперь fallback на `/sys/class/hwmon/*/coretemp` → Package id 0 (или первый Core). На Pi работает как раньше через vcgencmd.
- **Частота CPU** — раньше `vcgencmd measure_clock arm` → `None`. Теперь fallback на `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq`.
- **Сетевой интерфейс** — раньше хардкод `eth0` → 0 байт на любой машине где первый интерфейс называется иначе (Cubi: `enp45s0`, Debian-Predictable-Names в общем). Теперь auto-detect через default route в `/proc/net/route`, кеш 60 сек.
- **Throttle status** — раньше `vcgencmd get_throttled` падал → broken JSON. Теперь возвращает пустой контракт (всё False, raw=0x0) на x86 — фронт не падает.

### Changed
- Плитка «Сеть» теперь динамически показывает реальное имя интерфейса (`Сеть eth0` на Pi, `Сеть enp45s0` на Cubi)
- API `/api/status` поле `net.iface` теперь возвращает реальный primary interface, не хардкод
- Voltage tile (`voltage_v` в API) на x86 возвращает None — фронт-логика покажет «—» (нет аналога vcgencmd на x86)

### Added — Detection helper
- Константа `_HAS_VCGENCMD = shutil.which("vcgencmd") is not None` — единая точка определения платформы
- Функция `primary_iface()` с кешем 60 сек — для определения активного сетевого интерфейса
- Функция `_cpu_temp_x86()` — корректное чтение coretemp через hwmon, предпочитает Package id 0

### Why
SmartComm Dashboard позиционируется как продукт для тиражирования по объектам клиентов. Production-контроллер — это **MSI Cubi 5** (x86 Intel), а не Pi. На Cubi дашборд показывал:
- «Нет температуры ЦПУ» (vcgencmd отсутствует)
- «Сеть eth0: ↓0 ↑0» (eth0 не существует, активный enp45s0)
- Заглушки в throttle/voltage

После этого релиза дашборд корректно работает на **обоих платформах** без правки конфигов — автоопределение.

### Совместимость
Полностью backward-compatible с Pi 5. На Pi всё работает как раньше (vcgencmd ветка), на x86 — fallback срабатывает прозрачно. Никаких настроек/миграций не требуется.

## 1.3.1 — 2026-06-27

CSS-housekeeping: utility-классы + design tokens.

### Added — Дизайн-токены
- CSS-переменные `--r-sm: 6px`, `--r-md: 9px`, `--r-lg: 14px` — единая шкала border-radius (раньше 10+ магических значений: 3/6/7/8/9/10/11/12/14 в разных местах)
- Все card / modal / header / pill теперь используют `border-radius: var(--r-lg)` — единый стиль закругления

### Added — Utility-классы
Добавлены в `index.html` и `network.html` (синхронно):
- **Display**: `.hidden`, `.flex`, `.flex-col`, `.flex-wrap`, `.items-center`, `.items-baseline`, `.justify-end`, `.justify-between`
- **Spacing**: `.gap-1..4` (4/8/12/16px), `.mt-1..5`, `.mb-1..4`
- **Color**: `.text-muted`, `.text-dim`, `.text-ok`, `.text-warn`, `.text-err`, `.text-info`
- **Font-size**: `.fs-xs` (11), `.fs-sm` (11.5), `.fs-md` (12.5), `.fs-lg` (14)
- **Other**: `.full-w` (width: 100%)

### Added — `.btn-action.c-mute` в index.html
- Раньше существовал только в network.html — теперь синхронно в обоих файлах
- Заменил inline-style `style="color: var(--text-muted); border-color: var(--border)"` на класс `c-mute` в 4 кнопках шапки (Users / Argon / О дашборде / Версия)

### Changed
- Кнопки шапки используют новый класс `.btn-action.c-mute` — на 4 inline-style меньше
- Все 14px border-radius (.card, .modal-box, .header, .pill, .chip, .modal) перешли на `var(--r-lg)` — централизованное управление

### Why
Аудит показал ~150 inline `style="..."` атрибутов с повторяющимися паттернами. Этот батч закладывает **базис системы** — design tokens + utility classes — чтобы новый код использовал их вместо inline. Mass-convert всех 150 inline-styles отложен (риск регрессий слишком велик), но фундамент готов. Inline-styles на `display:none` намеренно оставлены: JS активно дёргает `el.style.display = ''` для toggle, а CSS-класс этого бы не пускал.

## 1.3.0 — 2026-06-27

SSH-web с auto-password из credentials карточки устройства (Task #7).

### Added — Smart SSH button
- Кнопка «🖥 Терминал» в карточке устройства теперь умная:
  - **0 credentials** → открыть webssh с дефолтным юзером `pi`, пароль вводится руками (как раньше)
  - **1 credential** → автоматически подставить login + password в URL webssh — терминал открывается сразу с активной сессией
  - **2+ credentials** → попап-выбор «admin / viewer / root...» с показом label и notes для каждого
- Использует существующий endpoint `GET /api/network/devices/<id>/credentials` (под auth)
- Picker модалка с тем же стилем `.modal-backdrop` + `.modal-box` что и остальные

### Changed
- Кнопка теперь принимает `(deviceId, ip)` вместо `(ip, hardcoded_user)` — динамика
- Tooltip обновлён: «Открыть SSH-терминал в браузере (пароль из credentials)»

### Security note
Пароль уходит в URL → виден в browser history и логах webssh-сервера. Это приемлемо для LAN-only инструмента где сам доступ к дашборду уже под auth'ом, но **не использовать через VPN/Cloudflare-tunnel без HTTPS**. Когда добавим reverse-proxy с TLS — переключиться на POST-form-submit через backend-прокси (TODO).

### Why
Сейчас при каждом SSH-входе пользователь вручную копировал пароль из «📋 Доступы» карточки в форму webssh — это 4 клика + копирование. Особенно неудобно когда у устройства 2+ доступа (admin для управления, viewer для просмотра). Теперь — один клик «🖥 Терминал» → попап с выбором (если нужно) → готовый терминал.

## 1.2.0 — 2026-06-27

UX-аудит и унификация дизайна.

### Changed — Help-модалки → collapsible sections
- На главной (`/`) и в `/network` все help-секции (`h3` + текст) превращены в `<details>` с раскрывающимися заголовками. Раньше — длинная простыня на 2-3 экрана скролла, особенно неудобно на iPhone. Теперь сразу видны 10 разделов оглавления, кликаешь нужный — раскрывается.
- Первая секция открыта по умолчанию (обычная UX-практика для FAQ).
- Анимация стрелочки `▸` → `▾` при раскрытии. Hover на summary меняет цвет на accent.
- Единый класс `.help-sect` — стилистика синхронизирована между обоими страницами.

### Changed — Унифицированы кнопки «Закрыть» в модалках
- Новый класс `.btn-close` — единый стиль для всех dismiss-кнопок (8 модалок: iRidium settings, Version, Users, Argon help, App help, Net help, Network Settings, Add device).
- Раньше каждая модалка использовала свой класс (btn-action / btn / разные inline-стили) — теперь визуально идентичны.

### Changed — «Pi» → «Контроллер» в заголовках плиток
- Заголовки плиток на главной больше не привязаны к Raspberry Pi:
  - «Pi · Температура CPU» → «Контроллер · Температура CPU»
  - «Pi · CPU», «Pi · RAM», «Pi · Диск», «Pi · Сеть eth0» — аналогично
  - «Pi uptime» → «Uptime контроллера»
- Большие графики: «Pi · Температура CPU · 1ч» → «Контроллер · Температура CPU · 1ч»
- В шапке по-прежнему отображается точное имя платформы из `/proc/device-tree/model` (Raspberry Pi 5 / MSI Cubi 5 / etc) — динамически.

### Why
- Help-модалки требовали 2-3 экранов скролла, особенно на iPhone — пользователь искал нужный раздел листая вручную. Collapsible решает это в 1 клик.
- Cosmetic inconsistency (3 разных стиля у кнопок «Закрыть») мешала перцепции «всё одной системы».
- «Pi» в заголовках был жёстко вшит, но контроллер уже скоро будет MSI Cubi 5 — приведено в нейтральную форму.

## 1.1.0 — 2026-06-27

Обратная совместимость (Task #5). Старые порталы больше не ломаются — либо graceful upgrade, либо принудительный force-reload через UI-баннер.

### Added — Schema migrations framework
- Таблица `schema_migrations(version, applied_at, description)` — формальное отслеживание applied миграций
- `MIGRATIONS` list в `network.py` — упорядоченный список миграций для будущих schema changes
- `apply_pending_migrations()` — auto-apply при старте + SQLite-safe backup до выполнения (через `backup()` API)
- v0 baseline — все pre-1.0.0 ad-hoc CREATE/ALTER маркируются как уже применённые
- Endpoint `GET /api/version` теперь возвращает `schema.{current, target, up_to_date, applied}`

### Added — Client compatibility check
- `MIN_COMPATIBLE_CLIENT` константа в `dashboard.py` — минимальная версия PWA-клиента с которой backend ещё совместим
- `/api/version` возвращает поле `min_compatible_client`
- Frontend: periodic polling `/api/version` каждые 60 сек
- Сценарии:
  - Backend обновился без breaking changes → жёлтый toast «Backend обновился до v1.x, обновите страницу» с кнопками «Обновить» / «Позже»
  - Client старше `min_compatible_client` → **force hard-reload** автоматически (unregister SW + clear caches + reload)

### Added — Service Worker invalidation
- Bumped `CACHE = sc-v4 → sc-v5` — старые browser caches принудительно сбрасываются на каждом MINOR-релизе
- При force-reload клиент сам unregister'ит все SW и очищает все Cache Storage entries

### Why
- Без migration framework каждое schema-изменение = риск потери данных при upgrade на много контроллеров
- Без client-side version check — старый закешированный PWA пытался читать новый /api/status и крашился на missing полях
- Force-reload гарантирует консистентность: либо клиент совместим, либо обновился — третьего нет

## 1.0.0 — 2026-06-27

Первый публичный релиз. Версионирование + GitHub + автообновление.

### Added
- VERSION константа и endpoint `GET /api/version`
- CHANGELOG.md + README.md + LICENSE
- Кнопка «Версия N.N.N» в шапке дашборда → модалка с историей версий
- GitHub-репозиторий [moshonkinaa/smartcomm-dashboard](https://github.com/moshonkinaa/smartcomm-dashboard)
- Background-updater: раз в час чекает GitHub Releases, при новой версии — backup + tarball download + apply + health-check + auto-rollback при фейле
- Endpoint `GET /api/update/check` — ручная проверка обновлений
- Endpoint `POST /api/update/apply` — ручной запуск обновления

### Changed
- Все Unicode-символы из «технического» диапазона (U+23FB ⏻ POWER, U+23F3 ⏳ HOURGLASS, U+FF0B ＋ FULLWIDTH PLUS) заменены на SVG-иконки или ASCII-эквиваленты — для надёжного рендеринга на любых системах

## 0.9.0 — 2026-06-27

### Added — Авторизация и аудит
- Cookie-based аутентификация (admin/admin по умолчанию, `must_change_password=1`)
- PBKDF2-HMAC-SHA256 хеширование паролей (100 тыс итераций, без внешних зависимостей)
- Таблица `auth_users` + `auth_sessions` (30-дневный TTL) + `auth_audit` (90 дней retention)
- Декораторы `@requires_auth`, `@requires_admin`, `@audit_action`
- Global `before_request` гейт на все endpoints кроме whitelist'а
- Страница `/login` с автодетектом авторизации
- Модалка «Пользователи и аудит» — 3 вкладки: список пользователей, аудит-лог с фильтром, профиль/смена пароля
- Принудительная смена пароля при первом входе
- Logout-кнопка с SVG-иконкой выхода

### Audit log записывает
- `login_success`, `login_failed` (с username_tried)
- `logout`, `change_password`, `reset_user_password`
- `create_user`, `delete_user`
- Все `/api/action/*` (start/stop/restart iRidium, reboot/shutdown Pi)
- `settings_iridium_changed`, `settings_mikrotik_changed`
- `mikrotik_sync`

## 0.8.0 — 2026-06-27

### Renamed — нейтральное имя для тиражирования
- `pi-dashboard` → `smartcomm-dashboard` (пути, service, БД, ident waitress)
- `/opt/pi-dashboard/` → `/opt/smartcomm-dashboard/`
- `/var/lib/pi-dashboard/` → `/var/lib/smartcomm-dashboard/`
- `pi-dashboard.service` → `smartcomm-dashboard.service`
- Миграция при деплое — идемпотентная (если старая инсталляция, переносит данные)

### Added — Install-пакет
- Самодостаточный пакет `smartcomm-dashboard-install/` (573 КБ, 12 файлов)
- `install.sh` — bash-установщик для свежей Debian 12/13
- `migrate_from_pi.sh` — опциональный перенос БД и фото со старого Pi
- Bundled Chart.js v4.4.1 (offline-режим, без CDN)
- README.md с пошаговой инструкцией

## 0.7.0 — 2026-06-25

### Added — IP-карта и автодетект сети
- 4 чекбокса-фильтра на карте IP: static·online, static·offline, dynamic·online, dynamic·offline
- Новая семантика цветов: фон = тип адреса, точка = статус
  - Зелёный фон = static (DHCP-резервация в MikroTik)
  - Жёлтый фон = dynamic
  - Коричневый фон = шлюз (`.1`)
  - Голубой (`#5BC5E0`) фон = этот контроллер
  - Зелёная точка = онлайн, красная = офлайн
- Карта IP продублирована в самом низу дашборда (читает /api/network/devices, клик → /network)
- Авто-детект сети из `/proc/net/route` (gateway), убраны хардкоды `192.168.95.x`
- В `/api/network/devices` теперь поле `gateway`
- В `/api/mikrotik/settings` — поле `detected_gateway` для UI placeholder

### Improved — Sampler
- Sampler MikroTik = единственный владелец `_TRAFFIC_PREV` (убрана race condition с HTTP-endpoint)
- Warm-up: первый snapshot отбрасывается чтобы избежать нулевой delta

## 0.6.0 — 2026-06-24

### Added — Стабильность и production-уровень
- Заменён Flask dev-server на **waitress 2.1** (production WSGI, 8 threads, без bottleneck)
- Service Worker `sc-v1` → `sc-v4` (форсированный сброс browser-cache)
- gzip compression через flask-compress (13× сжатие для /api/network/devices)
- Persistent metrics history: SQLite `metrics.db` с auto-hydrate при старте (графики переживают рестарт)
- 30-дневная retention метрик
- Префикс «Pi ·» на системных плитках для отличия от MikroTik
- Жирная красная рамка + красноватый фон у офлайн-плиток мониторинга

### Added — Multi-photo per device
- Таблица `device_photos`, миграция legacy single-photo
- Endpoints: `GET/POST /api/network/devices/<id>/photos`, `GET/DELETE /api/network/photos/<pid>`
- UI: галерея с кнопкой удаления на hover, multi-select при выборе файлов

### Improved — Reliability
- Все sync HTTP-вызовы к iRidium вынесены в background samplers — больше не блокируют waitress workers
- Stale-while-revalidate cache (5 минут grace) — плитка iRidium не моргает «auth failed» при кратковременных GC pause
- Adaptive cache TTL (30 сек для успеха, 5 сек для ошибки)
- Login serialization fix — устранена race condition 6 параллельных POST на login.html
- Auto-retry: при провале всех 6 параллельных API-вызовов — повтор через 200 мс
- iRidium HTTP check вынесен в фон, обновляется раз в 10 сек

### Improved — Presence
- Гистерезис: 3 пропуска подряд → ICMP-ping fallback перед маркировкой offline
- Спасает Wirenboard / WiFi-устройства от ложных «прерываний»
- В `/api/network/scan/status` новые метрики: `miss_pending`, `ping_recovered`

### UI improvements
- В карточке устройства: не пересоздавать DOM при auto-refresh если пользователь печатает (фокус в input)
- Кнопки шапки `/network` — в стиле дашборда (← Мониторинг, ? Возможности)
- Тема (light/dark) синхронизирована между страницами через `localStorage('theme')`
- WebSSH на :8022 — кнопка «🖥 Терминал» в карточке устройства и на Pi uptime

## 0.5.0 — 2026-06-24

### Added — iRidium HTTP API
- Reverse-engineering: `POST /html/login.html` → cookie `ir-session-id` → 6+ endpoints
- Login + cookie session к iRidium на `:8888`
- 6 endpoints в parallel ThreadPoolExecutor: main, info, licence, current_project, devices, tags
- Новая плитка «iRidium · все данные API» (лицензия Pro/Lite, проект GUID, 16 устройств, 736 тегов)
- Модалка «iRidium · детали → ⚙ API» — настройка пароля
- HTTP probe `:8888` (alive/code/ms) в плитке iRidium-сервис
- Smart-статус плитки: «активен / загрузка проекта (~3 мин) / порт не отвечает / остановлен»
- 3-минутный countdown «грузит проект, ещё ~N сек» после рестарта

## 0.4.0 — 2026-06-24

### Added — MikroTik integration
- REST API клиент для MikroTik RouterOS 7+ (basic auth, без бинарных протоколов)
- 4 плитки на дашборде: CPU load, RAM%, Disk%, WAN traffic
- WAN interface auto-detected через `/ip/route` (default route)
- Real-time bps + cumulative totals
- График WAN-трафика (1ч/24ч, отдельные линии ↓ download / ↑ upload)
- График CPU MikroTik (1ч/24ч)
- Кнопка «WebFig» открывает web-интерфейс MikroTik
- DHCP comments sync → device names в карте сети (override always, MAC-match)
- Auto-decode CP1251 (стандартная кодировка WinBox на русской Windows)
- Колонка «DHCP» (static/dynamic plashka) в таблице карты сети
- Hourly background auto-sync DHCP comments
- Модалка «MikroTik (REST API)» в Настройках карты сети

## 0.3.0 — 2026-06-24

### Added — Network inventory page (`/network`)
- SQLite-backed device map (таблицы: devices, scans, settings, device_types, device_events, device_credentials, device_audit)
- nmap discovery (`-sn -PR`) автоматом раз в 4 часа (настраивается)
- Auto-classification по vendor regex (camera/network/iot/controller/panel/printer)
- Presence check каждые 60 сек (легковесный ARP-ping)
- Карточка устройства: имя, помещение, тип, URL, описание, заметки, теги, фото, доступы (login/password), мониторинг на дашборде
- 24h availability timeline per device (48 баковок по 30 мин)
- IP map view (16×16 grid = вся /24 подсеть)
- Bulk операции (помещение, тип, monitor)
- Audit log (90-дневная retention)
- mDNS-обогащение через avahi-browse (Sonos/Apple/HomeKit)
- SNMP probe для устройств типа `network`
- CSV-экспорт всей таблицы
- Auto-backup `inventory.db` с retention 30 дней

### Added — Monitored devices section на дашборде
- Сетка плиток для устройств с галочкой «Показывать на дашборде»
- 24h-полоска доступности + подпись «онлайн/офлайн с ДАТА»

## 0.2.0 — 2026-06-23

### Performance
- `/api/status` оптимизирован: **175 мс → 31 мс** (5.6× быстрее)
- Декоратор `@time_cache(N)` с per-args TTL
- `ThreadPoolExecutor(max_workers=8)` — параллелизация 9 subprocess-вызовов
- Объединённые systemctl/journalctl команды (1 вызов вместо 4)
- Cache-Control: статика max-age=86400 immutable, HTML/API no-cache

### Added — Big charts + health
- 2 больших графика (температура CPU + нагрузка CPU) с переключателем 1ч/24ч
- Динамические Y-шкалы с grace 8%
- Карточка «Состояние системы»: NTP, упавшие systemd units, нужен ли ребут, available updates (раз в 6 ч), последние ошибки dmesg, SD-card SMART, throttle история

### Added — Browser notifications
- API Notification: alert при offline-устройстве из мониторинга
- Иконка 🔔 в шапке для разрешения уведомлений

## 0.1.0 — 2026-06-23

### Initial release — Pi diagnostic dashboard

- Веб-портал на Flask, порт `:8080`
- 4 базовых метрик-плитки: температура CPU, CPU%, RAM%, Disk% — со спарклайнами 1h
- Плитка «iRidium сервис»: статус, PID, uptime + кнопки Портал/Рестарт
- Плитка «Pi uptime»: время работы, kernel + кнопки Reboot/Shutdown
- Плитка «Сеть eth0»: скорость + спарклайн
- Плитка «iRidium · детали»: версия, проект, RAM/потоки процесса, размер БД, число клиентов
- Топ-5 процессов по CPU и RAM
- Активные TCP-подключения к iRidium (через `ss -tn state established`)
- 50 последних строк журнала iRidium (новые сверху)
- PWA: manifest.json + service worker (offline-shell)
- Тема (light/dark/auto) с сохранением в localStorage
- `/client` URL — read-only view без кнопок управления

## Pre-history (vendor work — не в version control)

- Установка Raspberry Pi 5 + Argon ONE V3 на Raspbian Bookworm (Debian 12)
- iRidium Server 1.3.87 armhf
- EEPROM конфиг (POWER_OFF_ON_HALT=1, PCIE_PROBE=1, PSU_MAX_CURRENT=5000)
- Brainstorming каталога 50 третьесторонних сервисов умного дома
- PDF-каталог 30 страниц для партнёров с описанием/ценами/моделями монетизации
- Сравнение мини-ПК платформ для замены Pi (Intel Core 3/5/7 100/120/150U)
- Выбор финальной платформы: MSI Cubi 5 1M-462BRU (Core 5 120U, 16 ГБ DDR5)
