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
import time
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
