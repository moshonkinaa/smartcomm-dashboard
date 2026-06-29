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

# Где хранятся данные установленных сервисов
DATA_BASE = Path("/var/lib/smartcomm-services")

# DB — берём из network.py (общая)
DB_PATH = "/var/lib/smartcomm-dashboard/network.db"


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
            ["git", "-C", str(CATALOG_DIR), "log", "-1", "--format=%h|%s|%ct"],
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
    Возвращает (ok, message)."""
    try:
        if not CATALOG_DIR.exists():
            CATALOG_DIR.parent.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(
                ["git", "clone", "--depth=1", CATALOG_REPO, str(CATALOG_DIR)],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode != 0:
                return False, f"git clone failed: {r.stderr.strip()[:200]}"
        else:
            r = subprocess.run(
                ["git", "-C", str(CATALOG_DIR), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30
            )
            if r.returncode != 0:
                return False, f"git pull failed: {r.stderr.strip()[:200]}"
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
    Возвращает dict с list'ами ok / warnings / errors."""
    catalog = load_catalog()
    service = next((s for s in catalog if s.get("id") == service_id), None)
    if not service:
        return {"ok": False, "error": f"сервис '{service_id}' не найден в каталоге"}

    result = {
        "service": service.get("name"),
        "platform_ok": True,
        "ram_ok": True,
        "disk_ok": True,
        "ports_ok": True,
        "checks": [],
        "blockers": [],
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

    # Порты
    busy_ports = _busy_ports()
    needs_ports = reqs.get("needs_ports", [])
    busy_required = [p for p in needs_ports if p in busy_ports]
    if busy_required:
        result["ports_ok"] = False
        result["blockers"].append(
            f"Порты заняты: {', '.join(map(str, busy_required))} "
            f"(сервис требует: {', '.join(map(str, needs_ports))})"
        )
    elif needs_ports:
        result["checks"].append(f"✓ Порты свободны: {', '.join(map(str, needs_ports))}")

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


def _busy_ports():
    """Список TCP-портов которые слушаются на 0.0.0.0 / 127.0.0.1 / IPv6."""
    busy = set()
    try:
        r = subprocess.run(
            ["ss", "-tlnH"], capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
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
    with _PROGRESS_LOCK:
        if service_id not in _PROGRESS:
            _PROGRESS[service_id] = {
                "state": "running", "phase": "queued",
                "started_at": time.time(), "log": deque(maxlen=500), "error": None,
            }
        for k, v in fields.items():
            if k == "log_line" and v:
                _PROGRESS[service_id]["log"].append(v)
            else:
                _PROGRESS[service_id][k] = v


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
    Возвращает (returncode, last_lines_for_error)."""
    sid = cwd.name if cwd else "?"  # для prefix
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"
    last_lines = deque(maxlen=20)
    started = time.time()
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
        if time.time() - started > timeout:
            proc.kill()
            _progress_set(sid, log_line=f"[timeout {timeout}s — kill]")
            return -1, "timeout"
    rc = proc.wait()
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
    _progress_set(sid, state="running", phase="preparing", started_at=time.time(),
                  log=deque(maxlen=500))
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
    _progress_set(sid, state="running", phase="stopping", started_at=time.time(),
                  log=deque(maxlen=500), error=None)
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
