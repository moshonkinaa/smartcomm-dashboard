"""SmartComm Services module — catalog reader + installed state.

Логика:
  1. Каталог сервисов (YAML манифесты) клонируется с GitHub в
     CATALOG_DIR = /opt/smartcomm-services-catalog/ (раз в час auto-pull).
  2. Локально установленные сервисы трекаются в SQLite (network.db,
     таблица installed_services) — добавлена миграцией v1.
  3. Этот модуль НЕ устанавливает/удаляет сервисы — это будет в v1.6.0
     (отдельный installer-thread с docker compose).

Phase 0 deliverables:
  - load_catalog() — парсит YAML, кеширует
  - list_installed() — что установлено + статусы
  - counts() — для счётчика «N/M» в шапке
  - install_pre_check() — RAM/disk/ports validation перед установкой
  - refresh_catalog() — git pull
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import deque
from functools import lru_cache
from pathlib import Path

# YAML парсер — стандартный в большинстве Debian (python3-yaml)
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# Пути
CATALOG_REPO = "https://github.com/moshonkinaa/smartcomm-services-catalog.git"
CATALOG_DIR = Path("/opt/smartcomm-services-catalog")
SERVICES_DIR = CATALOG_DIR / "services"
PROFILES_DIR = CATALOG_DIR / "profiles"

# Где хранятся данные установленных сервисов
DATA_BASE = Path("/var/lib/smartcomm-services")

# DB — общая с network.py (там миграции запускаются). НЕ дублируем хардкод —
# импортируем чтобы не было дрейфа (в v1.6.0 я случайно написала network.db
# вместо inventory.db → installed_services создавалась но в чужой пустой БД,
# отсюда «установлено 0» хотя сервис ставится).
import network as _net_mod
DB_PATH = str(_net_mod.DB_PATH)


# ============ Catalog loading ============

_CATALOG_CACHE = {"ts": 0, "data": None}
_CATALOG_TTL = 300   # 5 минут (refresh на каждый запрос — слишком)


def _platform_arch():
    """uname machine: x86_64, aarch64, armv7l. Возвращаем canonical: x86_64 или arm64."""
    try:
        m = os.uname().machine
        if m == "aarch64":
            return "arm64"
        return m  # x86_64 как есть
    except Exception:
        return "unknown"


def _system_ram_mb():
    """Доступный RAM в МБ. Берём MemTotal из /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    kb = int(ln.split()[1])
                    return kb // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _system_free_disk_gb(path="/"):
    """Свободного диска в ГБ."""
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) // (1024 ** 3)
    except OSError:
        return 0


def _load_one_manifest(path):
    """Парсит один YAML файл, возвращает dict или None."""
    if not _HAS_YAML:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "id" not in data:
            return None
        return data
    except (OSError, yaml.YAMLError):
        return None


def load_catalog(force=False):
    """Загружает все YAML из CATALOG_DIR/services/. Кеш 5 мин (force=True игнорит)."""
    now = time.time()
    if not force and _CATALOG_CACHE["data"] and (now - _CATALOG_CACHE["ts"]) < _CATALOG_TTL:
        return _CATALOG_CACHE["data"]

    services = []
    if SERVICES_DIR.exists():
        for f in sorted(SERVICES_DIR.glob("*.yaml")):
            m = _load_one_manifest(f)
            if m:
                services.append(m)
    _CATALOG_CACHE["data"] = services
    _CATALOG_CACHE["ts"] = now
    return services


_PROFILES_CACHE = {"ts": 0, "data": None}


def load_profiles(force=False):
    """Список профилей-пакетов (Базовый/Стандарт/Премиум)."""
    now = time.time()
    if not force and _PROFILES_CACHE["data"] and (now - _PROFILES_CACHE["ts"]) < _CATALOG_TTL:
        return _PROFILES_CACHE["data"]
    profiles = []
    if PROFILES_DIR.exists() and _HAS_YAML:
        for f in sorted(PROFILES_DIR.glob("*.yaml")):
            m = _load_one_manifest(f)
            if m:
                profiles.append(m)
    profiles.sort(key=lambda p: p.get("order", 999))
    _PROFILES_CACHE["data"] = profiles
    _PROFILES_CACHE["ts"] = now
    return profiles


def install_profile(profile_id):
    """Запускает установку всех сервисов профиля последовательно в фоне.
    Пропускает уже установленные. Возвращает (ok, message)."""
    profile = next((p for p in load_profiles() if p.get("id") == profile_id), None)
    if not profile:
        return False, f"профиль '{profile_id}' не найден"
    svcs = profile.get("services", [])
    if not svcs:
        return False, "профиль пустой"

    # Фильтруем: уже установленные пропускаем, несовместимые тоже
    catalog_ids = {s.get("id") for s in load_catalog()}
    to_install = []
    skipped = []
    blockers = []   # critical — Docker missing, и т.п. — должны прервать
    for sid in svcs:
        if sid not in catalog_ids:
            skipped.append(f"{sid} (нет в каталоге)")
            continue
        if get_installed(sid):
            skipped.append(f"{sid} (уже установлен)")
            continue
        # Full pre-check — включая Docker, RAM, диск, порты
        check = install_pre_check(sid)
        if check.get("error"):
            skipped.append(f"{sid} ({check['error']})")
            continue
        if not check.get("ok"):
            blocker_reasons = "; ".join(check.get("blockers", []))
            # Если корень — отсутствие Docker, это блокер для ВСЕГО профиля
            if any("Docker не установлен" in b for b in check.get("blockers", [])):
                blockers.append(f"Docker не установлен — поставь сначала: curl -fsSL https://get.docker.com | sudo sh")
                break
            skipped.append(f"{sid} ({blocker_reasons})")
            continue
        to_install.append(sid)

    if blockers:
        return False, " · ".join(blockers)

    if not to_install:
        return False, "нечего устанавливать: все либо стоят, либо несовместимы. " + "; ".join(skipped)

    # Запускаем worker-поток
    t = threading.Thread(
        target=_profile_install_worker,
        args=(profile, to_install, skipped),
        daemon=True,
        name=f"profile-install-{profile_id}"
    )
    t.start()
    return True, f"установка пакета запущена: {len(to_install)} сервисов в очереди" + (
        f", пропущено {len(skipped)}" if skipped else ""
    )


def _profile_install_worker(profile, to_install, skipped):
    """Последовательно ставит сервисы профиля. Прогресс пишет под profile_id."""
    pid = profile["id"]
    _progress_set(pid, state="running", phase="preparing",
                  started_at=time.time(), error=None)
    _progress_reset_log(pid)
    _progress_set(pid, log_line=f"=== Установка пакета «{profile.get('name')}» ===")
    _progress_set(pid, log_line=f"  к установке: {len(to_install)} сервисов")
    if skipped:
        _progress_set(pid, log_line=f"  пропущено: {', '.join(skipped)}")

    successes = []
    failures = []
    for sid in to_install:
        _progress_set(pid, phase="pulling",
                      log_line=f"--- [{sid}] Запускаю установку...")
        manifest = next((s for s in load_catalog() if s.get("id") == sid), None)
        if not manifest:
            failures.append(f"{sid} (не найден)")
            continue
        # Вызываем установку синхронно (не через install_service, чтобы дождаться)
        try:
            _install_worker(manifest)
            # Проверяем результат
            svc_prog = get_progress(sid)
            if svc_prog and svc_prog.get("phase") == "error":
                failures.append(f"{sid}: {svc_prog.get('error', 'unknown')}")
                _progress_set(pid, log_line=f"--- [{sid}] ❌ {svc_prog.get('error', '')[:100]}")
            else:
                successes.append(sid)
                _progress_set(pid, log_line=f"--- [{sid}] ✓ установлен")
        except Exception as e:
            failures.append(f"{sid}: {e}")
            _progress_set(pid, log_line=f"--- [{sid}] ❌ {e}")

    if failures:
        _progress_set(pid, state="done", phase="error",
                      error=f"{len(failures)} fail: {'; '.join(failures[:3])}",
                      log_line=f"=== ЗАВЕРШЕНО: {len(successes)} ✓, {len(failures)} ✗ ===")
    else:
        _progress_set(pid, state="done", phase="done",
                      log_line=f"=== ✓ Пакет «{profile.get('name')}» установлен — {len(successes)} сервисов ===")


def catalog_status():
    """Метаданные каталога — где он, версия HEAD, last-fetch."""
    info = {
        "repo": CATALOG_REPO,
        "local_dir": str(CATALOG_DIR),
        "exists": CATALOG_DIR.exists(),
        "yaml_parser": _HAS_YAML,
        "services_count": 0,
        "git_sha": None,
        "git_subject": None,
        "last_fetch": None,
    }
    if not _HAS_YAML:
        info["error"] = "python3-yaml не установлен (apt install python3-yaml)"
        return info
    if not CATALOG_DIR.exists():
        info["error"] = "Каталог ещё не склонирован (POST /api/services/refresh)"
        return info
    info["services_count"] = len(load_catalog())
    try:
        sha = subprocess.run(
            ["sudo", "git", "-c", f"safe.directory={CATALOG_DIR}",
             "-C", str(CATALOG_DIR), "log", "-1", "--format=%h|%s|%ct"],
            capture_output=True, text=True, timeout=5
        )
        if sha.returncode == 0 and sha.stdout.strip():
            parts = sha.stdout.strip().split("|", 2)
            info["git_sha"] = parts[0]
            if len(parts) > 1:
                info["git_subject"] = parts[1]
            if len(parts) > 2:
                info["last_fetch"] = int(parts[2])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return info


def refresh_catalog():
    """Клонирует репо если нет, или git pull. Сбрасывает кеш.
    Возвращает (ok, message).

    Использует sudo для всех git операций — каталог обычно owned root после
    install.sh, а flask-процесс крутится под service-user. Также передаёт
    `safe.directory=*` через -c чтобы обойти git 2.x dubious ownership check
    БЕЗ модификации /etc/gitconfig (опасно для общей системы)."""
    git_safe = f"safe.directory={CATALOG_DIR}"
    git = ["sudo", "git", "-c", git_safe]
    try:
        if not CATALOG_DIR.exists():
            r = subprocess.run(
                ["sudo", "mkdir", "-p", str(CATALOG_DIR.parent)],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode != 0:
                return False, f"mkdir failed: {r.stderr.strip()[:200]}"
            r = subprocess.run(
                git + ["clone", "--depth=1", CATALOG_REPO, str(CATALOG_DIR)],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode != 0:
                return False, f"git clone failed: {r.stderr.strip()[:200]}"
        else:
            r = subprocess.run(
                git + ["-C", str(CATALOG_DIR), "fetch", "--depth=1", "origin", "main"],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                return False, f"git fetch failed: {r.stderr.strip()[:200]}"
            r = subprocess.run(
                git + ["-C", str(CATALOG_DIR), "reset", "--hard", "origin/main"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode != 0:
                return False, f"git reset failed: {r.stderr.strip()[:200]}"
        _CATALOG_CACHE["data"] = None
        _CATALOG_CACHE["ts"] = 0
        return True, f"OK — {len(load_catalog(force=True))} сервисов"
    except FileNotFoundError:
        return False, "git не установлен"
    except subprocess.TimeoutExpired:
        return False, "git operation timeout"
    except Exception as e:
        return False, f"unexpected: {e}"


# ============ Filtering ============

def is_compatible_with_platform(service):
    """Подходит ли сервис под текущую платформу (Pi/Cubi)?
    Проверяет: arch, RAM, disk."""
    arch = _platform_arch()
    reqs = service.get("requirements", {})
    platforms = reqs.get("platforms", [])
    if platforms and arch not in platforms:
        return False, f"архитектура {arch} не поддерживается (нужна одна из: {', '.join(platforms)})"
    ram_mb = _system_ram_mb()
    need_ram = reqs.get("min_ram_mb", 0)
    if ram_mb and ram_mb < need_ram:
        return False, f"мало RAM: {ram_mb}MB / нужно {need_ram}MB"
    free_disk = _system_free_disk_gb("/var")
    need_disk = reqs.get("min_disk_gb", 0)
    if free_disk < need_disk:
        return False, f"мало места: {free_disk}GB / нужно {need_disk}GB"
    return True, "OK"


# ============ Installed services (DB) ============

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def list_installed():
    """Из БД — что установлено. Возвращает список dict'ов."""
    try:
        with _db() as c:
            rows = c.execute("""
                SELECT id, catalog_version, status, installed_at, last_started_at,
                       last_action_at, last_error, auto_update, notes, settings_json
                FROM installed_services
                ORDER BY installed_at DESC
            """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def update_settings(service_id, notes=None, auto_update=None):
    """Обновляет notes / auto_update для installed сервиса."""
    if get_installed(service_id) is None:
        return False, "сервис не установлен"
    fields = {}
    if notes is not None:
        fields["notes"] = str(notes)[:2000]   # лимит чтобы не разрастаться
    if auto_update is not None:
        if auto_update not in ("never", "weekly", "monthly"):
            return False, "auto_update должно быть never/weekly/monthly"
        fields["auto_update"] = auto_update
    if not fields:
        return False, "нечего обновить"
    _db_upsert_installed(service_id, **fields)
    return True, "OK"


def get_installed(service_id):
    """Один установленный сервис или None."""
    try:
        with _db() as c:
            r = c.execute(
                "SELECT * FROM installed_services WHERE id = ?", (service_id,)
            ).fetchone()
        return dict(r) if r else None
    except sqlite3.Error:
        return None


def counts():
    """Для шапки «Сервисы (running/installed/catalog)»."""
    catalog = load_catalog()
    catalog_count = sum(
        1 for s in catalog
        if s.get("client_facing", True) and is_compatible_with_platform(s)[0]
    )
    installed = list_installed()
    return {
        "catalog": catalog_count,                                  # сколько доступно для установки
        "catalog_total": len(catalog),                             # всего в каталоге (включая несовместимые)
        "installed": len(installed),
        "running": sum(1 for s in installed if s.get("status") == "running"),
        "stopped": sum(1 for s in installed if s.get("status") in ("stopped", "installed")),
        "error": sum(1 for s in installed if s.get("status") == "error"),
    }


# ============ Pre-install validation ============

def install_pre_check(service_id):
    """Проверки перед установкой: совместимость + порты + интернет.
    Возвращает dict с list'ами ok / warnings / errors.
    Если сервис УЖЕ установлен — порты-конфликты ignore'ятся (это его же порты)."""
    catalog = load_catalog()
    service = next((s for s in catalog if s.get("id") == service_id), None)
    if not service:
        return {"ok": False, "error": f"сервис '{service_id}' не найден в каталоге"}

    already_installed = get_installed(service_id) is not None

    result = {
        "service": service.get("name"),
        "platform_ok": True,
        "ram_ok": True,
        "disk_ok": True,
        "ports_ok": True,
        "checks": [],
        "blockers": [],
        "already_installed": already_installed,
    }

    reqs = service.get("requirements", {})

    # Архитектура
    arch = _platform_arch()
    platforms = reqs.get("platforms", [])
    if platforms and arch not in platforms:
        result["platform_ok"] = False
        result["blockers"].append(
            f"архитектура {arch} не поддерживается (нужно: {', '.join(platforms)})"
        )
    else:
        result["checks"].append(f"✓ архитектура {arch} поддерживается")

    # RAM
    ram_mb = _system_ram_mb()
    need_ram = reqs.get("min_ram_mb", 0)
    if ram_mb < need_ram:
        result["ram_ok"] = False
        result["blockers"].append(f"RAM: есть {ram_mb}MB, нужно {need_ram}MB")
    else:
        result["checks"].append(f"✓ RAM: есть {ram_mb}MB / нужно {need_ram}MB")

    # Диск
    free_disk = _system_free_disk_gb("/var")
    need_disk = reqs.get("min_disk_gb", 0)
    if free_disk < need_disk:
        result["disk_ok"] = False
        result["blockers"].append(f"Диск: свободно {free_disk}GB, нужно {need_disk}GB")
    else:
        result["checks"].append(f"✓ Диск: свободно {free_disk}GB / нужно {need_disk}GB")

    # Порты — если сервис уже установлен, его порты он сам занимает, не проблема
    needs_ports = reqs.get("needs_ports", [])
    if not already_installed:
        busy_ports = _busy_ports()
        busy_required = [p for p in needs_ports if p in busy_ports]
        if busy_required:
            result["ports_ok"] = False
            result["blockers"].append(
                f"Порты заняты: {', '.join(map(str, busy_required))} "
                f"(сервис требует: {', '.join(map(str, needs_ports))})"
            )
        elif needs_ports:
            result["checks"].append(f"✓ Порты свободны: {', '.join(map(str, needs_ports))}")
    elif needs_ports:
        result["checks"].append(
            f"✓ Порты {', '.join(map(str, needs_ports))} заняты самим сервисом (норма)"
        )

    # Docker установлен?
    docker_ok = bool(shutil.which("docker"))
    if not docker_ok:
        result["blockers"].append("Docker не установлен — нужен для запуска сервисов")
    else:
        result["checks"].append("✓ Docker установлен")

    result["ok"] = (
        result["platform_ok"] and result["ram_ok"]
        and result["disk_ok"] and result["ports_ok"] and docker_ok
    )
    return result


# ============ Service lifecycle (start/stop/restart) ============

# ============ Auto-update (v2.2.0) ============
#
# Каждый installed-сервис имеет setting `auto_update` (never/weekly/monthly).
# Background loop раз в час проверяет: пора ли обновить?
# Обновление = docker compose pull (тянет latest image-tag из манифеста) +
# docker compose up -d (recreate если image другой).
# Использует per-service install lock — не race'ит с install/uninstall.

_AUTO_UPDATE_INTERVALS = {
    "weekly":  7 * 86400,
    "monthly": 30 * 86400,
}


def _should_auto_update(inst):
    """True если сервис due для auto-update."""
    mode = inst.get("auto_update", "never")
    if mode not in _AUTO_UPDATE_INTERVALS:
        return False
    if inst.get("status") not in ("running", "installed", "stopped"):
        return False   # не трогаем сервисы в error/installing/updating/uninstalling
    last = inst.get("last_auto_update_at") or inst.get("installed_at") or 0
    return (time.time() - last) >= _AUTO_UPDATE_INTERVALS[mode]


def update_service(service_id, source="manual"):
    """docker compose pull + up -d для одного сервиса. source = manual | auto.
    Возвращает (ok, message). Запускается в фоновом потоке — отдаёт сразу."""
    inst = get_installed(service_id)
    if not inst:
        return False, "сервис не установлен"
    sdir = _service_dir(service_id)
    if not (sdir / "compose.yml").exists():
        return False, "compose.yml не найден"
    t = threading.Thread(
        target=_update_worker, args=(service_id, source), daemon=True,
        name=f"update-{service_id}"
    )
    t.start()
    return True, f"обновление запущено ({source})"


def _update_worker(sid, source):
    lock = _get_install_lock(sid)
    if not lock.acquire(blocking=False):
        _progress_set(sid, log_line=f"  ⚠ {sid}: другая операция уже идёт — skip update")
        return
    try:
        _do_update_worker(sid, source)
    finally:
        lock.release()


def _do_update_worker(sid, source):
    sdir = _service_dir(sid)
    compose_path = sdir / "compose.yml"
    _progress_set(sid, state="running", phase="pulling", started_at=time.time(),
                  error=None)
    _progress_reset_log(sid)
    _progress_set(sid, log_line=f"=== Обновление {sid} ({source}) ===")
    _db_upsert_installed(sid, status="updating")
    try:
        # 1. pull (тянет latest tag из compose.yml)
        _progress_set(sid, log_line="--- docker compose pull ---")
        rc, last = _stream_subprocess(
            ["sudo", "docker", "compose", "-f", str(compose_path), "pull"],
            cwd=sdir, timeout=900
        )
        if rc != 0:
            raise RuntimeError(f"pull rc={rc}: {last[-200:]}")
        # 2. up -d (recreate если image другой)
        _progress_set(sid, phase="starting", log_line="--- docker compose up -d ---")
        rc, last = _stream_subprocess(
            ["sudo", "docker", "compose", "-f", str(compose_path), "up", "-d"],
            cwd=sdir, timeout=180
        )
        if rc != 0:
            raise RuntimeError(f"up rc={rc}: {last[-200:]}")
        _progress_set(sid, state="done", phase="running",
                      log_line=f"=== ✓ Обновлён ({source}) ===")
        _db_upsert_installed(
            sid, status="running",
            last_started_at=int(time.time()),
            last_auto_update_at=int(time.time()),
            last_auto_update_ok=1,
            last_error=None,
        )
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _progress_set(sid, state="done", phase="error", error=err,
                      log_line=f"!!! FAIL: {err}")
        _db_upsert_installed(
            sid, status="error", last_error=err[:500],
            last_auto_update_at=int(time.time()), last_auto_update_ok=0,
        )


def _auto_update_loop():
    """Background loop: раз в час просыпается, выбирает due сервисы, обновляет
    последовательно (через update_service который сам берёт per-service lock)."""
    # Стартовая задержка 5 мин — чтобы дать дашборду полностью подняться
    time.sleep(300)
    while True:
        try:
            installed = list_installed()
            due = [s for s in installed if _should_auto_update(s)]
            if due:
                print(f"[auto-update] {len(due)} сервисов due: " +
                      ", ".join(s["id"] for s in due))
                for s in due:
                    sid = s["id"]
                    try:
                        ok, msg = update_service(sid, source="auto")
                        if not ok:
                            print(f"[auto-update] {sid}: {msg}")
                        # Ждём 30с между сервисами — чтобы Docker Hub не throttled
                        time.sleep(30)
                    except Exception as e:
                        print(f"[auto-update] {sid} crash: {e}")
        except Exception as e:
            print(f"[auto-update] loop error: {e}")
        # Проверяем раз в час
        time.sleep(3600)


_AUTO_UPDATER_STARTED = False


def ensure_auto_updater_started():
    """Запускает background auto-update loop один раз."""
    global _AUTO_UPDATER_STARTED
    if _AUTO_UPDATER_STARTED:
        return
    _AUTO_UPDATER_STARTED = True
    threading.Thread(target=_auto_update_loop, daemon=True,
                     name="services-auto-updater").start()


def service_action(service_id, action):
    """action: start | stop | restart | logs. Возвращает (ok, message_or_output)."""
    if action not in ("start", "stop", "restart", "logs"):
        return False, f"unknown action: {action}"

    sdir = _service_dir(service_id)
    compose_path = sdir / "compose.yml"
    if not compose_path.exists():
        return False, f"compose.yml не найден: {compose_path}"

    if action == "logs":
        try:
            r = subprocess.run(
                ["sudo", "docker", "compose", "-f", str(compose_path),
                 "logs", "--tail", "100", "--no-color"],
                capture_output=True, text=True, timeout=15
            )
            return True, r.stdout[-8000:] or r.stderr[-2000:]
        except subprocess.TimeoutExpired:
            return False, "timeout"

    cmd = ["sudo", "docker", "compose", "-f", str(compose_path)]
    if action == "start":
        cmd += ["up", "-d"]
    elif action == "stop":
        cmd += ["stop"]
    elif action == "restart":
        cmd += ["restart"]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, f"rc={r.returncode}: {(r.stderr or r.stdout)[-500:]}"
        new_status = {"start": "running", "restart": "running", "stop": "stopped"}.get(action, "running")
        _db_upsert_installed(service_id, status=new_status,
                             last_started_at=int(time.time()) if new_status == "running" else None,
                             last_error=None)
        return True, (r.stdout or "OK")[-500:]
    except subprocess.TimeoutExpired:
        return False, "timeout"


# ============ Docker status sync + discovery existing ============

def _docker_container_status(container_name):
    """docker inspect <name> --format '{{.State.Status}}'. None если нет."""
    try:
        r = subprocess.run(
            ["sudo", "docker", "inspect", "--format={{.State.Status}}", container_name],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _all_docker_containers():
    """Возвращает [{name, status, image, ports}, ...] всех контейнеров."""
    try:
        r = subprocess.run(
            ["sudo", "docker", "ps", "-a", "--format",
             "{{.Names}}|{{.State}}|{{.Image}}|{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        out = []
        for ln in r.stdout.splitlines():
            parts = ln.split("|", 3)
            if len(parts) == 4:
                out.append({"name": parts[0], "state": parts[1],
                            "image": parts[2], "ports": parts[3]})
        return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def sync_statuses():
    """Обновляет статусы installed_services из docker inspect. Раз в 30 сек."""
    installed = list_installed()
    for inst in installed:
        sid = inst["id"]
        compose_path = _service_dir(sid) / "compose.yml"
        if not compose_path.exists():
            continue
        # Парсим имена контейнеров из compose.yml (наивно — container_name: lines)
        try:
            text = compose_path.read_text(encoding="utf-8")
        except OSError:
            continue
        names = re.findall(r"container_name:\s*([^\s\n]+)", text)
        if not names:
            # без container_name docker compose использует <project>-<service>-1
            names = [f"{sid}-{sid}-1"]  # best-effort
        statuses = [_docker_container_status(n) for n in names]
        statuses = [s for s in statuses if s]
        # Aggregate:
        #   все running → running
        #   все exited → stopped (нормальный остановленный)
        #   dead или контейнер исчез → error
        #   mix → берём худший статус (error > stopped > running)
        if not statuses:
            new_status = "error"   # контейнеры исчезли
        elif any(s == "dead" for s in statuses):
            new_status = "error"
        elif all(s == "running" for s in statuses):
            new_status = "running"
        elif all(s in ("exited", "created", "paused") for s in statuses):
            new_status = "stopped"
        else:
            new_status = "stopped"   # mix running+exited — partial stop
        if new_status != inst.get("status"):
            _db_upsert_installed(sid, status=new_status)


def discover_existing():
    """При старте: ищет installed сервисы в /var/lib/smartcomm-services/*/compose.yml
    которые НЕ в БД (потеря после bug в v1.6.0) — регистрирует их.
    Безопасно: только тех чьи compose.yml существуют физически."""
    if not DATA_BASE.exists():
        return 0
    catalog_ids = {s.get("id") for s in load_catalog()}
    found = 0
    for entry in DATA_BASE.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if entry.name not in catalog_ids:
            continue
        if not (entry / "compose.yml").exists():
            continue
        if get_installed(entry.name):
            continue   # уже в БД
        # Регистрируем — статус определим через sync_statuses сразу после
        _db_upsert_installed(entry.name, status="installed",
                             catalog_version="1")
        found += 1
    if found:
        sync_statuses()
    return found


# ============ Background status sampler ============

_STATS_LOCK = threading.Lock()
_STATS = {}  # {container_name: {cpu_pct, mem_mb, mem_pct, net_rx_mb, net_tx_mb, ts}}


def _parse_docker_size(s):
    """'12.3MiB' / '1.2GiB' / '500kB' → MB float."""
    if not s:
        return 0.0
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)?", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    factors = {
        "B": 1/1024/1024, "KB": 1/1024, "MB": 1.0, "GB": 1024.0, "TB": 1024*1024,
        "KIB": 1/1024, "MIB": 1.0, "GIB": 1024.0, "TIB": 1024*1024,
    }
    return val * factors.get(unit, 1.0)


def _sample_docker_stats():
    """Один проход docker stats --no-stream — обновляет _STATS для всех контейнеров.
    Использует --format чтобы получить парсимый вывод."""
    try:
        r = subprocess.run(
            ["sudo", "docker", "stats", "--no-stream", "--format",
             "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return
        now = time.time()
        new_stats = {}
        for ln in r.stdout.splitlines():
            parts = ln.split("|", 4)
            if len(parts) != 5:
                continue
            name, cpu_s, mem_use_s, mem_pct_s, net_io = parts
            cpu_pct = 0.0
            try:
                cpu_pct = float(cpu_s.rstrip("%").strip())
            except ValueError:
                pass
            mem_mb = 0.0
            try:
                # "1.234MiB / 1.5GiB"
                mem_used = mem_use_s.split("/")[0].strip()
                mem_mb = _parse_docker_size(mem_used)
            except (ValueError, IndexError):
                pass
            mem_pct = 0.0
            try:
                mem_pct = float(mem_pct_s.rstrip("%").strip())
            except ValueError:
                pass
            net_rx_mb = net_tx_mb = 0.0
            try:
                # "1.5kB / 2.3MB"
                rx_s, tx_s = [p.strip() for p in net_io.split("/", 1)]
                net_rx_mb = _parse_docker_size(rx_s)
                net_tx_mb = _parse_docker_size(tx_s)
            except (ValueError, IndexError):
                pass
            new_stats[name] = {
                "cpu_pct": round(cpu_pct, 2),
                "mem_mb": round(mem_mb, 1),
                "mem_pct": round(mem_pct, 2),
                "net_rx_mb": round(net_rx_mb, 2),
                "net_tx_mb": round(net_tx_mb, 2),
                "ts": int(now),
            }
        with _STATS_LOCK:
            _STATS.update(new_stats)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def get_service_stats(service_id):
    """Aggregate stats всех контейнеров сервиса. Возвращает dict или None."""
    compose_path = _service_dir(service_id) / "compose.yml"
    if not compose_path.exists():
        return None
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError:
        return None
    names = re.findall(r"container_name:\s*([^\s\n]+)", text)
    if not names:
        return None
    with _STATS_LOCK:
        items = [_STATS[n] for n in names if n in _STATS]
    if not items:
        return None
    return {
        "cpu_pct": round(sum(i["cpu_pct"] for i in items), 2),
        "mem_mb": round(sum(i["mem_mb"] for i in items), 1),
        "mem_pct": round(sum(i["mem_pct"] for i in items), 2),
        "net_rx_mb": round(sum(i["net_rx_mb"] for i in items), 2),
        "net_tx_mb": round(sum(i["net_tx_mb"] for i in items), 2),
        "containers": len(items),
        "ts": max(i["ts"] for i in items),
    }


def _status_sampler_loop():
    """Раз в 30 сек обновляем статусы + ресурсы installed-сервисов."""
    while True:
        try:
            sync_statuses()
            _sample_docker_stats()
        except Exception as e:
            print(f"[services-sampler] error: {e}")
        time.sleep(30)


_SAMPLER_STARTED = False


def ensure_sampler_started():
    """Запускает background sampler один раз. Безопасно дёргать многократно."""
    global _SAMPLER_STARTED
    if _SAMPLER_STARTED:
        return
    _SAMPLER_STARTED = True
    threading.Thread(target=_status_sampler_loop, daemon=True,
                     name="services-status-sampler").start()


def _busy_ports():
    """Список TCP+UDP портов которые слушаются на любом интерфейсе.
    Раньше проверяли только TCP (-t) — UDP-сервисы (DNS, WireGuard) выпадали."""
    busy = set()
    for flag in ("-tlnH", "-ulnH"):
        try:
            r = subprocess.run(
                ["ss", flag], capture_output=True, text=True, timeout=5
            )
            if r.returncode != 0:
                continue
            for ln in r.stdout.splitlines():
                # формат: LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* ...
                parts = ln.split()
                if len(parts) >= 4:
                    addr = parts[3]
                    port_s = addr.rsplit(":", 1)[-1]
                    try:
                        busy.add(int(port_s))
                    except ValueError:
                        continue
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return busy


# ============ Install / Uninstall (v1.6.0) ============
#
# Установка асинхронна — крутится в фоне thread'е, чтобы HTTP запрос не блокировался
# (docker compose pull для immich/nextcloud занимает 5+ минут). UI опрашивает прогресс
# через /api/services/<id>/install-progress.
#
# Состояние трекается через `_PROGRESS` (in-memory) + БД (installed_services).

_PROGRESS_LOCK = threading.Lock()
_PROGRESS = {}  # {service_id: {state, phase, started_at, log: deque(maxlen=500), error}}
_INSTALL_LOCKS = {}      # per-service install/uninstall mutex
_INSTALL_LOCKS_GUARD = threading.Lock()


def _get_install_lock(service_id):
    """Per-service mutex чтобы install/uninstall одного сервиса не race'ил с собой
    (например при двойном клике или install+profile-batch одновременно)."""
    with _INSTALL_LOCKS_GUARD:
        if service_id not in _INSTALL_LOCKS:
            _INSTALL_LOCKS[service_id] = threading.Lock()
        return _INSTALL_LOCKS[service_id]

_PHASES = {
    "queued":      "В очереди",
    "preparing":   "Подготовка папок и compose.yml",
    "pulling":     "Скачивание образов с Docker Hub",
    "starting":    "Запуск контейнеров",
    "running":     "Запущен",
    "stopping":    "Остановка контейнеров",
    "removing":    "Удаление контейнеров и образов",
    "backing_up":  "Создание бэкапа",
    "done":        "Готово",
    "error":       "Ошибка",
}


def get_progress(service_id):
    """Текущий прогресс install/uninstall. Read-only snapshot."""
    with _PROGRESS_LOCK:
        p = _PROGRESS.get(service_id)
        if not p:
            return None
        return {
            "service_id": service_id,
            "state": p["state"],
            "phase": p["phase"],
            "phase_label": _PHASES.get(p["phase"], p["phase"]),
            "started_at": p["started_at"],
            "elapsed_sec": int(time.time() - p["started_at"]),
            "log": list(p["log"])[-50:],   # последние 50 строк (для UI)
            "error": p.get("error"),
        }


def _progress_set(service_id, **fields):
    """Безопасное обновление progress'а. log_line добавляет в deque.
    НЕ принимает 'log' как ключ — для очистки лога используйте
    _progress_reset_log() (нельзя заменять deque пока другой поток её читает)."""
    with _PROGRESS_LOCK:
        if service_id not in _PROGRESS:
            _PROGRESS[service_id] = {
                "state": "running", "phase": "queued",
                "started_at": time.time(), "log": deque(maxlen=500), "error": None,
            }
        for k, v in fields.items():
            if k == "log_line" and v:
                _PROGRESS[service_id]["log"].append(v)
            elif k == "log":
                continue   # игнорируем — используйте _progress_reset_log
            else:
                _PROGRESS[service_id][k] = v


def _progress_reset_log(service_id):
    """Очистить log внутри лока — безопасно для concurrent readers."""
    with _PROGRESS_LOCK:
        if service_id in _PROGRESS:
            _PROGRESS[service_id]["log"].clear()
            _PROGRESS[service_id]["error"] = None


def _service_dir(service_id):
    return DATA_BASE / service_id


def _render_compose(manifest):
    """Возвращает текст compose.yml с заменёнными placeholder'ами."""
    compose_raw = manifest.get("compose", "")
    if not compose_raw:
        raise ValueError("manifest без compose section")
    defaults = manifest.get("defaults", {})
    sid = manifest["id"]
    substitutions = {
        "{DATA}": str(_service_dir(sid)),
        "{MEDIA}": defaults.get("MEDIA", str(DATA_BASE / "_media")),
        "{TZ}": defaults.get("TZ", "Europe/Moscow"),
        "{CONTROLLER}": defaults.get("CONTROLLER", os.uname().nodename),
    }
    text = compose_raw
    for k, v in substitutions.items():
        text = text.replace(k, v)
    return text


def _db_upsert_installed(service_id, **fields):
    """Insert или update строку в installed_services."""
    try:
        with _db() as c:
            existing = c.execute(
                "SELECT id FROM installed_services WHERE id = ?", (service_id,)
            ).fetchone()
            now = int(time.time())
            if existing:
                set_parts = []
                params = []
                for k, v in fields.items():
                    set_parts.append(f"{k} = ?")
                    params.append(v)
                set_parts.append("last_action_at = ?")
                params.append(now)
                params.append(service_id)
                c.execute(
                    f"UPDATE installed_services SET {', '.join(set_parts)} WHERE id = ?",
                    params
                )
            else:
                fields.setdefault("installed_at", now)
                fields.setdefault("last_action_at", now)
                fields.setdefault("status", "installed")
                cols = list(fields.keys())
                vals = list(fields.values())
                cols.insert(0, "id")
                vals.insert(0, service_id)
                placeholders = ", ".join(["?"] * len(cols))
                c.execute(
                    f"INSERT INTO installed_services ({', '.join(cols)}) "
                    f"VALUES ({placeholders})",
                    vals
                )
    except sqlite3.Error as e:
        # log only, don't raise — установка важнее DB tracking
        print(f"[services] DB upsert failed for {service_id}: {e}")


def _db_delete_installed(service_id):
    try:
        with _db() as c:
            c.execute("DELETE FROM installed_services WHERE id = ?", (service_id,))
    except sqlite3.Error as e:
        print(f"[services] DB delete failed for {service_id}: {e}")


def _stream_subprocess(cmd, cwd=None, timeout=600):
    """Запускает docker compose с stream output → строки в _PROGRESS log.
    Возвращает (returncode, last_lines_for_error).

    Timeout enforced через threading.Timer (раньше: проверяли через time.time()
    после readline — но readline мог блокировать вечно если stdout не закрылся)."""
    sid = cwd.name if cwd else "?"
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"

    killed = {"flag": False}

    def _kill_on_timeout():
        if proc.poll() is None:
            killed["flag"] = True
            try:
                proc.kill()
            except OSError:
                pass

    timer = threading.Timer(timeout, _kill_on_timeout)
    timer.start()
    last_lines = deque(maxlen=20)
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            line = line.rstrip()
            if line:
                _progress_set(sid, log_line=line)
                last_lines.append(line)
        rc = proc.wait()
    finally:
        timer.cancel()

    if killed["flag"]:
        _progress_set(sid, log_line=f"[timeout {timeout}s — process killed]")
        return -1, "timeout"
    return rc, "\n".join(last_lines)


def install_service(service_id):
    """Запускает установку в фоновом потоке. Возвращает (ok, message)."""
    manifest = next((s for s in load_catalog() if s.get("id") == service_id), None)
    if not manifest:
        return False, f"сервис '{service_id}' не найден в каталоге"

    # Pre-check ещё раз
    check = install_pre_check(service_id)
    if not check["ok"]:
        return False, "pre-check failed: " + "; ".join(check.get("blockers", []))

    existing = get_installed(service_id)
    if existing and existing.get("status") in ("running", "installed", "installing"):
        # Уже стоит — переустановка через uninstall+install, чтобы не сюрпризить
        return False, f"сервис уже установлен (status={existing['status']}). Сначала удали."

    # Старт фонового потока
    t = threading.Thread(
        target=_install_worker, args=(manifest,), daemon=True,
        name=f"install-{service_id}"
    )
    t.start()
    return True, "установка запущена в фоне"


def _install_worker(manifest):
    sid = manifest["id"]
    lock = _get_install_lock(sid)
    if not lock.acquire(blocking=False):
        _progress_set(sid, log_line=f"  ⚠ install для {sid} уже идёт — пропускаю")
        return
    try:
        _do_install_worker(manifest)
    finally:
        lock.release()


def _do_install_worker(manifest):
    sid = manifest["id"]
    _progress_set(sid, state="running", phase="preparing", started_at=time.time())
    _progress_reset_log(sid)
    _progress_set(sid, log_line=f"=== Установка {manifest.get('name', sid)} ===")
    _db_upsert_installed(sid, status="installing",
                         catalog_version=str(manifest.get("schema_version", 1)))

    try:
        # 1. Создать папку — через sudo т.к. /var/lib/smartcomm-services/ обычно
        # owned root после install.sh. Сразу chown на текущего юзера чтобы
        # дальнейшая работа не требовала sudo для записи в эту папку.
        sdir = _service_dir(sid)
        uid_gid = f"{os.getuid()}:{os.getgid()}"
        r = subprocess.run(
            ["sudo", "mkdir", "-p", str(sdir)],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            raise RuntimeError(f"mkdir failed: {r.stderr.strip()[:200]}")
        r = subprocess.run(
            ["sudo", "chown", "-R", uid_gid, str(sdir)],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            raise RuntimeError(f"chown failed: {r.stderr.strip()[:200]}")
        _progress_set(sid, log_line=f"  создана папка: {sdir} (owner {uid_gid})")

        # 2. Сгенерировать compose.yml — теперь можно writeText без sudo
        compose_text = _render_compose(manifest)
        compose_path = sdir / "compose.yml"
        compose_path.write_text(compose_text, encoding="utf-8")
        _progress_set(sid, log_line=f"  compose.yml: {len(compose_text)} bytes")

        # 3. docker compose pull
        _progress_set(sid, phase="pulling",
                      log_line="=== docker compose pull ===")
        rc, last = _stream_subprocess(
            ["sudo", "docker", "compose", "-f", str(compose_path), "pull"],
            cwd=sdir, timeout=900   # 15 min для тяжёлых immich/nextcloud
        )
        if rc != 0:
            raise RuntimeError(f"pull rc={rc}: {last[-300:]}")

        # 4. docker compose up -d
        _progress_set(sid, phase="starting",
                      log_line="=== docker compose up -d ===")
        rc, last = _stream_subprocess(
            ["sudo", "docker", "compose", "-f", str(compose_path), "up", "-d"],
            cwd=sdir, timeout=180
        )
        if rc != 0:
            raise RuntimeError(f"up rc={rc}: {last[-300:]}")

        # 5. Готово
        _progress_set(sid, state="done", phase="running",
                      log_line=f"=== УСПЕХ — сервис запущен ===")
        _db_upsert_installed(sid, status="running", last_started_at=int(time.time()),
                             last_error=None)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _progress_set(sid, state="done", phase="error", error=err,
                      log_line=f"!!! FAIL: {err}")
        _db_upsert_installed(sid, status="error", last_error=err[:500])


def uninstall_service(service_id, delete_data=False):
    """Удаление в фоновом потоке. Возвращает (ok, message)."""
    installed = get_installed(service_id)
    if not installed:
        return False, f"сервис '{service_id}' не установлен"
    t = threading.Thread(
        target=_uninstall_worker, args=(service_id, delete_data), daemon=True,
        name=f"uninstall-{service_id}"
    )
    t.start()
    return True, "удаление запущено"


def _uninstall_worker(sid, delete_data):
    lock = _get_install_lock(sid)
    if not lock.acquire(blocking=False):
        _progress_set(sid, log_line=f"  ⚠ операция для {sid} уже идёт — пропускаю uninstall")
        return
    try:
        _do_uninstall_worker(sid, delete_data)
    finally:
        lock.release()


def _do_uninstall_worker(sid, delete_data):
    _progress_set(sid, state="running", phase="stopping", started_at=time.time(),
                  error=None)
    _progress_reset_log(sid)
    _progress_set(sid, log_line=f"=== Удаление {sid} ===")
    _db_upsert_installed(sid, status="uninstalling")

    try:
        sdir = _service_dir(sid)
        compose_path = sdir / "compose.yml"

        if compose_path.exists():
            # 1. docker compose down (volumes если delete_data)
            _progress_set(sid, phase="removing",
                          log_line=f"=== docker compose down{' -v' if delete_data else ''} ===")
            cmd = ["sudo", "docker", "compose", "-f", str(compose_path), "down"]
            if delete_data:
                cmd.append("-v")
            rc, last = _stream_subprocess(cmd, cwd=sdir, timeout=180)
            if rc != 0:
                _progress_set(sid, log_line=f"  warn: down rc={rc} — продолжаю удаление папки")
        else:
            _progress_set(sid, log_line="  compose.yml не найден — пропуск docker stop")

        # 2. Бэкап data если не удаляем
        if not delete_data and sdir.exists():
            _progress_set(sid, phase="backing_up",
                          log_line=f"=== Бэкап {sdir} в _backups/ ===")
            backup_dir = DATA_BASE / "_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{sid}-{int(time.time())}.tar.gz"
            try:
                subprocess.run(
                    ["sudo", "tar", "czf", str(backup_path), "-C", str(DATA_BASE), sid],
                    check=True, capture_output=True, text=True, timeout=300
                )
                _progress_set(sid, log_line=f"  бэкап: {backup_path}")
            except subprocess.CalledProcessError as e:
                _progress_set(sid, log_line=f"  warn: бэкап не удался — {e.stderr[:100] if e.stderr else e}")

        # 3. Удаление папки данных
        if delete_data and sdir.exists():
            _progress_set(sid, log_line=f"=== Удаление папки {sdir} ===")
            subprocess.run(["sudo", "rm", "-rf", str(sdir)], check=False, timeout=60)
        elif sdir.exists():
            # Сохраняем data, удаляем только compose файл
            try:
                compose_path.unlink(missing_ok=True)
            except (OSError, AttributeError):
                pass

        # 4. БД
        _db_delete_installed(sid)
        _progress_set(sid, state="done", phase="done",
                      log_line=f"=== Сервис удалён ===")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _progress_set(sid, state="done", phase="error", error=err,
                      log_line=f"!!! FAIL: {err}")
        _db_upsert_installed(sid, status="error", last_error=err[:500])
