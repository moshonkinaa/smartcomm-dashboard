# SmartComm Dashboard — история версий

Все значимые изменения проекта. Формат — Keep a Changelog + SemVer.

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
