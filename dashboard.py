#!/usr/bin/env python3
"""iRidium Pi5 diagnostic dashboard backend."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, wraps
from pathlib import Path

from flask import Flask, jsonify, send_from_directory, make_response, request, redirect, Response

import network as network_bp
import mikrotik as mt
import services as services_mod


# ============ AUTH декораторы и helper'ы ============

def _get_session():
    """Прочитать сессию из cookie. Возвращает dict или None."""
    sid = request.cookies.get("sc_session")
    if not sid:
        return None
    return network_bp.auth_get_session(sid)


def _client_ip():
    """X-Forwarded-For-aware client IP."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


def requires_auth(f):
    """API-endpoint: 401 если нет сессии. Страница: redirect на /login."""
    @wraps(f)
    def wrapper(*a, **kw):
        sess = _get_session()
        if not sess:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect("/login?next=" + request.full_path.rstrip("?"))
        request.auth_user = sess
        return f(*a, **kw)
    return wrapper


def requires_admin(f):
    @wraps(f)
    def wrapper(*a, **kw):
        sess = _get_session()
        if not sess:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        user = network_bp.auth_get_user(sess["username"])
        if not user or not user.get("is_admin"):
            return jsonify({"ok": False, "error": "forbidden — admin only"}), 403
        request.auth_user = sess
        return f(*a, **kw)
    return wrapper


def audit_action(action_name, target_from_path=False, log_details=None):
    """Декоратор: пишет в auth_audit при вызове endpoint'а.
    target_from_path=True — берёт target из request.path."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            sess = _get_session()
            username = sess["username"] if sess else None
            ip = _client_ip()
            target = request.path if target_from_path else None
            result = "success"
            try:
                resp = f(*a, **kw)
                # Если у Flask response status >= 400 — пометим как fail
                try:
                    status = resp.status_code if hasattr(resp, "status_code") \
                             else (resp[1] if isinstance(resp, tuple) else 200)
                    if status >= 400:
                        result = "fail"
                except Exception:
                    pass
                return resp
            except Exception:
                result = "fail"
                raise
            finally:
                details = None
                if log_details:
                    try:
                        details = log_details()
                    except Exception:
                        pass
                network_bp.auth_log(username, ip, action_name, target,
                                    details, result)
        return wrapper
    return deco

VERSION = "3.0.6"
RELEASE_DATE = "2026-06-30"
GITHUB_REPO = "moshonkinaa/smartcomm-dashboard"
# Минимальная версия клиента (PWA/cache) с которой backend ещё совместим.
# Если HTML/JS клиента старше — попросим hard refresh. Обычно = текущая VERSION,
# но если изменения косметические/back-compat — можно занизить.
MIN_COMPATIBLE_CLIENT = "1.0.0"

app = Flask(__name__)

# gzip compression for API + HTML — ~10x reduction for /api/network/devices
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

app.register_blueprint(network_bp.bp)


# ============ Глобальная защита: все endpoints требуют auth ============
# Кроме whitelist'а (login page, auth API, статика, чарт, manifest, SW).
# Это catch-all — даже если я забыл @requires_auth на каком-то endpoint'е.
_AUTH_PUBLIC_PATHS = {
    "/login", "/api/auth/login", "/api/auth/logout", "/api/auth/me",
    "/sw.js", "/manifest.json", "/chart.min.js", "/favicon.ico",
    "/marked.min.js",
}

@app.before_request
def _global_auth_gate():
    p = request.path
    if p in _AUTH_PUBLIC_PATHS:
        return None
    if request.method == "OPTIONS":
        return None
    sess = _get_session()
    if sess:
        request.auth_user = sess
        return None
    # Не залогинен:
    if p.startswith("/api/"):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    # HTML страница — редирект на логин
    return redirect("/login?next=" + p)
BASE = Path(__file__).resolve().parent

# Pool reused across requests to avoid thread create/destroy churn.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dash")


def time_cache(ttl_sec):
    """Per-args time-based cache for expensive subprocess wrappers."""
    def decorator(fn):
        store = {}
        lock = threading.Lock()
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with lock:
                hit = store.get(key)
                if hit and (now - hit[0]) < ttl_sec:
                    return hit[1]
            result = fn(*args, **kwargs)
            with lock:
                store[key] = (now, result)
            return result
        return wrapper
    return decorator


@app.after_request
def cache_headers(resp):
    """Long cache for static JS/CSS, no-cache for HTML and API."""
    p = request.path or ""
    if p.endswith(".js") or p.endswith(".css") or p.endswith(".png") or p.endswith(".ico"):
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    else:
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

def _mkbuf(n):
    return {
        "ts":   deque(maxlen=n),
        "temp": deque(maxlen=n),
        "cpu":  deque(maxlen=n),
        "ram":  deque(maxlen=n),
        "disk": deque(maxlen=n),
        "net":  deque(maxlen=n),   # KB/s combined rx+tx
    }


HISTORY_1H  = _mkbuf(60)    # 60 × 60s = 1 hour
HISTORY_24H = _mkbuf(288)   # 288 × 5min = 24 hours
LOCK = threading.Lock()
PREV_CPU = None
PREV_CPU_CORES = {}
PREV_NET = {"rx": 0, "tx": 0, "ts": time.time()}

# ============ Persisted metrics history (survives restarts) ============
METRICS_DB = "/var/lib/smartcomm-dashboard/metrics.db"

def _metrics_db():
    import sqlite3
    con = sqlite3.connect(METRICS_DB, timeout=5, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS samples (
        ts INTEGER PRIMARY KEY,
        temp REAL, cpu REAL, ram REAL, disk REAL, net REAL
    )""")
    return con

def _metrics_persist(ts, temp, cpu, ram, disk, net):
    try:
        con = _metrics_db()
        con.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?)",
                    (int(ts), temp, cpu, ram, disk, net))
        con.commit()
        con.close()
    except Exception:
        pass

def _metrics_cleanup(days=30):
    try:
        con = _metrics_db()
        cutoff = int(time.time()) - days*86400
        con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()
    except Exception:
        pass

def _metrics_hydrate():
    """Pull recent samples from DB into the in-memory deques on startup."""
    try:
        con = _metrics_db()
        now = int(time.time())
        # 1h buf: last 3600 sec, max 60 points
        rows1h = con.execute(
            "SELECT ts,temp,cpu,ram,disk,net FROM samples WHERE ts >= ? ORDER BY ts ASC",
            (now - 3600,)).fetchall()
        for r in rows1h[-60:]:
            HISTORY_1H["ts"].append(r[0]); HISTORY_1H["temp"].append(r[1])
            HISTORY_1H["cpu"].append(r[2]); HISTORY_1H["ram"].append(r[3])
            HISTORY_1H["disk"].append(r[4]); HISTORY_1H["net"].append(r[5])
        # 24h buf: last 86400 sec, downsample to ~288 points (every 5 min)
        rows24 = con.execute(
            "SELECT ts,temp,cpu,ram,disk,net FROM samples WHERE ts >= ? ORDER BY ts ASC",
            (now - 86400,)).fetchall()
        last_bucket = -1
        for r in rows24:
            bucket = r[0] // 300   # 5-min buckets
            if bucket == last_bucket:
                continue
            last_bucket = bucket
            HISTORY_24H["ts"].append(r[0]); HISTORY_24H["temp"].append(r[1])
            HISTORY_24H["cpu"].append(r[2]); HISTORY_24H["ram"].append(r[3])
            HISTORY_24H["disk"].append(r[4]); HISTORY_24H["net"].append(r[5])
        con.close()
        print(f"[metrics] hydrated {len(HISTORY_1H['ts'])} (1h) + {len(HISTORY_24H['ts'])} (24h) points from {METRICS_DB}")
    except Exception as e:
        print(f"[metrics] hydrate skipped: {e}")


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


_HAS_VCGENCMD = shutil.which("vcgencmd") is not None


def vcgen(arg):
    return sh(f"vcgencmd {arg}")


def _cpu_temp_x86():
    """x86 fallback: читает /sys/class/hwmon/hwmon*/ где name=coretemp,
    возвращает Package id 0 (или первый Core если Package недоступен)."""
    try:
        for h in sorted(os.listdir("/sys/class/hwmon")):
            base = f"/sys/class/hwmon/{h}"
            try:
                with open(f"{base}/name") as f:
                    if f.read().strip() != "coretemp":
                        continue
            except OSError:
                continue
            package_temp = first_temp = None
            for fn in sorted(os.listdir(base)):
                if not (fn.startswith("temp") and fn.endswith("_input")):
                    continue
                idx = fn[4:-6]
                try:
                    with open(f"{base}/temp{idx}_label") as f:
                        label = f.read().strip()
                    with open(f"{base}/temp{idx}_input") as f:
                        val_c = int(f.read().strip()) / 1000.0
                except (OSError, ValueError):
                    continue
                if label.startswith("Package") and package_temp is None:
                    package_temp = val_c
                elif first_temp is None:
                    first_temp = val_c
            if package_temp is not None:
                return round(package_temp, 1)
            if first_temp is not None:
                return round(first_temp, 1)
    except OSError:
        pass
    return None


def cpu_temp_c():
    """Pi: vcgencmd; x86: coretemp via hwmon (Package id 0)."""
    if _HAS_VCGENCMD:
        o = vcgen("measure_temp")
        m = re.search(r"=([\d.]+)", o)
        return float(m.group(1)) if m else None
    return _cpu_temp_x86()


def cpu_freq_mhz():
    """Pi: vcgencmd; x86: cpufreq scaling_cur_freq (kHz → MHz)."""
    if _HAS_VCGENCMD:
        o = vcgen("measure_clock arm")
        m = re.search(r"=(\d+)", o)
        return int(m.group(1)) // 1_000_000 if m else None
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            return int(f.read().strip()) // 1000
    except (OSError, ValueError):
        return None


def core_volt_v():
    """Pi: vcgencmd; x86: нет аналога (тут не реализуем — фронт скроет плитку)."""
    if not _HAS_VCGENCMD:
        return None
    o = vcgen("measure_volts core")
    m = re.search(r"=([\d.]+)", o)
    return float(m.group(1)) if m else None


def throttled():
    """Pi: get_throttled bitmask; x86: no-op (всё False, raw=0x0).
    Возвращает тот же контракт чтобы фронт не падал."""
    empty = {
        "undervolt_now": False, "freq_capped_now": False,
        "throttled_now": False, "soft_temp_now": False,
        "undervolt_ever": False, "freq_capped_ever": False,
        "throttled_ever": False, "soft_temp_ever": False,
    }
    if not _HAS_VCGENCMD:
        return "0x0", empty
    o = vcgen("get_throttled")
    m = re.search(r"=(0x[\da-fA-F]+)", o)
    raw = m.group(1) if m else "0x0"
    val = int(raw, 16)
    return raw, {
        "undervolt_now": bool(val & 0x1),
        "freq_capped_now": bool(val & 0x2),
        "throttled_now": bool(val & 0x4),
        "soft_temp_now": bool(val & 0x8),
        "undervolt_ever": bool(val & 0x10000),
        "freq_capped_ever": bool(val & 0x20000),
        "throttled_ever": bool(val & 0x40000),
        "soft_temp_ever": bool(val & 0x80000),
    }


def loadavg():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[:3]
    except Exception:
        return ["0", "0", "0"]


def uptime_sec():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0


def fmt_uptime(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d > 0:
        return f"{d} д {h} ч {m} мин"
    if h > 0:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def mem_info():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) * 1024
    except Exception:
        pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    used = total - avail
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total,
        "used": used,
        "percent": round(100 * used / total, 1) if total else 0,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
    }


def cpu_usage_pct():
    global PREV_CPU
    try:
        with open("/proc/stat") as f:
            parts = list(map(int, f.readline().split()[1:8]))
        total = sum(parts)
        idle = parts[3] + parts[4]
        if PREV_CPU:
            dt = total - PREV_CPU[0]
            di = idle - PREV_CPU[1]
            pct = round(100 * (dt - di) / dt, 1) if dt else 0
        else:
            pct = 0
        PREV_CPU = (total, idle)
        return pct
    except Exception:
        return 0


def cpu_usage_per_core():
    """Returns list of % usage per core, e.g. [12.3, 8.1, 0.5, 1.2]."""
    result = []
    try:
        with open("/proc/stat") as f:
            for ln in f:
                if not ln.startswith("cpu") or ln.startswith("cpu "):
                    continue
                parts = ln.split()
                name = parts[0]
                vals = list(map(int, parts[1:8]))
                total = sum(vals)
                idle = vals[3] + vals[4]
                prev = PREV_CPU_CORES.get(name)
                if prev:
                    dt = total - prev[0]
                    di = idle - prev[1]
                    pct = round(100 * (dt - di) / dt, 1) if dt else 0
                else:
                    pct = 0
                PREV_CPU_CORES[name] = (total, idle)
                result.append(pct)
    except Exception:
        pass
    return result


def disk_info(path="/"):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {
            "total": total,
            "used": used,
            "percent": round(100 * used / total, 1) if total else 0,
        }
    except Exception:
        return {"total": 0, "used": 0, "percent": 0}


def _primary_iface():
    """Имя интерфейса с default route. Кешируется на 60 сек.
    Pi обычно eth0, Cubi — enp45s0 и т.п."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            for ln in f:
                parts = ln.split()
                if len(parts) >= 4 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        pass
    return None


_IFACE_CACHE = {"iface": None, "ts": 0}
_IFACE_CACHE_LOCK = threading.Lock()


def primary_iface():
    """Имя интерфейса с default route. Кеш 60с ТОЛЬКО для удачного результата —
    если default route ещё не настроен (ранний boot), повторяем каждый вызов
    пока не найдём, чтобы избежать застревания на fallback'е."""
    now = time.time()
    with _IFACE_CACHE_LOCK:
        if _IFACE_CACHE["iface"] and now - _IFACE_CACHE["ts"] < 60:
            return _IFACE_CACHE["iface"]
    found = _primary_iface()
    if found:
        with _IFACE_CACHE_LOCK:
            _IFACE_CACHE["iface"] = found
            _IFACE_CACHE["ts"] = now
        return found
    # Fallback: первый UP не-loopback интерфейс
    try:
        for n in sorted(os.listdir("/sys/class/net")):
            if n == "lo":
                continue
            try:
                with open(f"/sys/class/net/{n}/operstate") as f:
                    if f.read().strip() == "up":
                        return n
            except OSError:
                continue
    except OSError:
        pass
    return "eth0"


def net_rate(iface=None):
    global PREV_NET
    if iface is None:
        iface = primary_iface()
    try:
        with open("/proc/net/dev") as f:
            for ln in f:
                lhs = ln.split(":", 1)
                if len(lhs) == 2 and lhs[0].strip() == iface:
                    fields = lhs[1].split()
                    rx, tx = int(fields[0]), int(fields[8])
                    now = time.time()
                    dt = max(0.5, now - PREV_NET["ts"])
                    drx = max(0, rx - PREV_NET["rx"])
                    dtx = max(0, tx - PREV_NET["tx"])
                    rx_rate = int(drx / dt)
                    tx_rate = int(dtx / dt)
                    PREV_NET = {"rx": rx, "tx": tx, "ts": now}
                    return rx_rate, tx_rate, rx, tx
    except Exception:
        pass
    return 0, 0, 0, 0


def process_uptime_sec(pid):
    """Real process uptime via ps -o etimes (more accurate than systemd
    ActiveEnterTimestamp when service has watchdog/auto-restart).
    Validate pid — defence-in-depth для shell injection."""
    if not pid or pid == "0":
        return None
    pid_s = str(pid).strip()
    if not pid_s.isdigit():
        return None
    out = sh(f"ps -o etimes= -p {pid_s} 2>/dev/null").strip()
    try:
        return int(out)
    except (ValueError, TypeError):
        return None


@time_cache(5)
def service_status(name):
    """One systemctl call (4 properties) + 1 ps for uptime — instead of 5 calls."""
    out = sh(
        f"systemctl show -p ActiveState -p UnitFileState -p MainPID "
        f"-p ActiveEnterTimestamp {name}"
    )
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    pid = props.get("MainPID", "0") or "0"
    if not pid.isdigit() or int(pid) <= 0:
        pid = "0"
    up = process_uptime_sec(pid) if pid != "0" else None
    return {
        "active":     props.get("ActiveState", "unknown") or "unknown",
        "enabled":    props.get("UnitFileState", "unknown") or "unknown",
        "pid":        pid,
        "started":    props.get("ActiveEnterTimestamp", ""),
        "uptime_sec": up,
        "uptime_fmt": fmt_uptime(up) if up is not None else None,
    }


@time_cache(5)
def top_processes(n=5, by="cpu"):
    key = "-%cpu" if by == "cpu" else "-%mem"
    out = sh(f"ps -eo pid,user,%cpu,%mem,comm --sort={key} --no-headers | head -{n}")
    result = []
    for ln in out.splitlines():
        parts = ln.split(None, 4)
        if len(parts) >= 5:
            try:
                result.append({
                    "pid": parts[0],
                    "user": parts[1],
                    "cpu": float(parts[2]),
                    "mem": float(parts[3]),
                    "cmd": parts[4][:40],
                })
            except ValueError:
                pass
    return result


@time_cache(5)
def iridium_connections():
    """Established TCP conns to/from iridium. Needs sudo because iridium runs
    as root; without sudo, ss-as-pi-user shows no process info for them."""
    out = sh("sudo ss -tn state established 'process_name like \"%iridium%\"' 2>/dev/null")
    if not out or "Local Address" not in out:
        # fallback: get all established + grep iridium-tagged ones
        out = sh("sudo ss -tnp state established 2>/dev/null")
    result = []
    for ln in out.splitlines():
        if "iridium" not in ln and "Local Address" not in ln:
            continue
        if "Local Address" in ln:
            continue
        parts = ln.split()
        if len(parts) >= 4:
            # ss -tn state X (no header): Recv-Q Send-Q Local Peer  (+ optional users:)
            # ss -tnp           : columns shifted by 0 or 1 depending on header presence
            # robust extraction: take first thing that looks like "IP:port"
            ip_cols = [c for c in parts if ":" in c and not c.startswith("users:")]
            if len(ip_cols) >= 2:
                result.append({"state": "ESTAB", "local": ip_cols[0], "peer": ip_cols[1]})
    return result[:20]


def fan_percent():
    """Best-effort: read fan PWM from Argon I2C register."""
    out = sh("i2cget -y 1 0x1a 2>/dev/null")
    m = re.search(r"0x([\da-fA-F]+)", out)
    if m:
        try:
            v = int(m.group(1), 16)
            if 0 <= v <= 100:
                return v
        except ValueError:
            pass
    return None


@time_cache(5)
def iridium_recent_log(n=5):
    # -r = reverse (newest first) — matches the user's "по убыванию даты"
    out = sh(f"journalctl -u irserver -n {n} -r --no-pager 2>/dev/null")
    return out.splitlines()[:n] if out else []


@time_cache(60)
def argon_installed():
    """Argon ONE V3 case is present iff its handler script + service exist."""
    if not os.path.exists("/etc/argon/argonpowerbutton.py"):
        return False
    active = sh("systemctl is-active argononed 2>/dev/null").strip()
    return active == "active"


@lru_cache(maxsize=1)
def platform_name():
    """Hardware platform string: Raspberry Pi model, DMI product or hostname."""
    # Raspberry Pi exposes model via device-tree
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip().rstrip("\x00")
        if model:
            return model
    except Exception:
        pass
    # x86 mini-PC: DMI product name (e.g. "Cubi 5 12M")
    try:
        with open("/sys/devices/virtual/dmi/id/product_name") as f:
            name = f.read().strip()
        if name and name.lower() not in (
            "to be filled by o.e.m.", "default string", "system product name", "none"
        ):
            try:
                with open("/sys/devices/virtual/dmi/id/sys_vendor") as f:
                    vendor = f.read().strip()
                if vendor and vendor.lower() not in (
                    "to be filled by o.e.m.", "default string", "system manufacturer"
                ):
                    return f"{vendor} {name}"
            except Exception:
                pass
            return name
    except Exception:
        pass
    return os.uname().nodename


@time_cache(10)
def iridium_log_meta():
    """Age of latest entry + per-minute / per-5min rates. ONE journalctl call:
    fetch last 5 min in unix-timestamp format, count + find max in Python."""
    out = sh(
        "journalctl -u irserver --since '5 minutes ago' -o short-unix "
        "--no-pager 2>/dev/null",
        timeout=8,
    )
    timestamps = []
    for ln in out.splitlines():
        head = ln.split(None, 1)[0] if ln else ""
        try:
            timestamps.append(float(head))
        except ValueError:
            pass
    now = time.time()
    if not timestamps:
        return {"latest_age_sec": None, "per_min": 0, "per_5min": 0}
    return {
        "latest_age_sec": int(now - max(timestamps)),
        "per_min":  sum(1 for t in timestamps if t > now - 60),
        "per_5min": len(timestamps),
    }


def button_recent_log(n=5):
    return sh(f"tail -n {n} /var/log/argon-button.log 2>/dev/null").splitlines()


# ============ iRidium-specific info from OS ============
#
# iRidium 1.x на Raspbian запускается от обычного юзера → данные в /var/lib/iRidium Server/.
# iRidium 2.x .deb на Debian запускается от root → данные в /root/iRidium Server/.
# Auto-detect через /proc/<irserver_pid>/fd где какой .db файл реально открыт.

IR_BIN  = "/iridiumserver/iridium"
_IR_BASE_FALLBACKS = (
    "/root/iRidium Server",          # iRidium 2.x от root
    "/var/lib/iRidium Server",       # iRidium 1.x от user/pi
    "/home/pi/iRidium Server",       # старый вариант если HOME=/home/pi
)


def _iridium_base_dir():
    """Найти базовую директорию iRidium Server. Один раз за процесс.
    Стратегия: lsof открытых файлов irserver-процесса → ищем 'iRidium Server'."""
    try:
        pid = sh("systemctl show -p MainPID --value irserver 2>/dev/null").strip()
        if pid and pid != "0":
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f"{fd_dir}/{fd}")
                    except OSError:
                        continue
                    idx = link.find("/iRidium Server/")
                    if idx >= 0:
                        return link[:idx + len("/iRidium Server")]
            except (PermissionError, OSError):
                pass
    except Exception:
        pass
    # Fallback: проверить известные пути на существование
    for cand in _IR_BASE_FALLBACKS:
        if os.path.isdir(cand):
            return cand
    return _IR_BASE_FALLBACKS[1]  # дефолт legacy путь


_IR_BASE = None


def iridium_paths():
    """Возвращает (base, docs, db). Кеш — определяем один раз после старта,
    т.к. пути не меняются пока irserver работает. Refresh при отсутствии."""
    global _IR_BASE
    if _IR_BASE is None or not os.path.isdir(_IR_BASE):
        _IR_BASE = _iridium_base_dir()
    return (
        _IR_BASE,
        f"{_IR_BASE}/Documents",
        f"{_IR_BASE}/DataBase/IridiumStorageV4.db",
    )


# Legacy aliases — теперь динамика
def _ir_docs(): return iridium_paths()[1]
def _ir_db():   return iridium_paths()[2]


@lru_cache(maxsize=1)
def iridium_version():
    """Parse 'Server Version: Pro or Lite:42647 (Jun  1 2026, 10:32:17)'.
    Cached — binary version doesn't change while service runs."""
    out = sh(f"sudo {IR_BIN} --version 2>&1 | head -1", timeout=10)
    m = re.search(r"Server Version:\s*(.+?):(\d+)\s*\(([^)]+)\)", out)
    if m:
        return {"edition": m.group(1).strip(), "build": m.group(2), "date": m.group(3)}
    return None


@time_cache(60)
def iridium_project_info():
    """Find the .irpz project file in Documents/. Returns name + size.
    Also tries to read the human-friendly project name from inside the
    .irpz (which is a zip archive with Project.xml or similar)."""
    import zipfile
    try:
        docs = _ir_docs()
        for fn in sorted(os.listdir(docs)):
            if fn.startswith("Project_") and fn.endswith(".irpz"):
                path = os.path.join(docs, fn)
                info = {
                    "name": fn[:-5],
                    "filename": fn,
                    "size": os.path.getsize(path),
                    "mtime": int(os.path.getmtime(path)),
                    "friendly_name": None,
                }
                # Try to extract human-readable project name from inside the irpz.
                # Search order: known files → all xml files (limit 8KB head).
                priority = ("project.xml", "project.json", "manifest.xml",
                            "info.xml", "config.xml")
                try:
                    with zipfile.ZipFile(path) as z:
                        names = z.namelist()
                        ordered = [n for n in names
                                   if os.path.basename(n).lower() in priority]
                        ordered += [n for n in names
                                    if n.lower().endswith(".xml")
                                    and n not in ordered]
                        for inner in ordered[:6]:
                            try:
                                with z.open(inner) as f:
                                    head = f.read(8192).decode("utf-8", "ignore")
                            except Exception:
                                continue
                            for pat in (
                                r'(?:project[_-]?name|projectName|appName|app[_-]?name|projectTitle|title)\s*=\s*["\']([^"\']{3,80})["\']',
                                r'<(?:project[_-]?name|projectName|appName|projectTitle|title)[^>]*>([^<]{3,80})<',
                                r'\bname\s*=\s*["\']([^"\']{3,80})["\']',
                            ):
                                m = re.search(pat, head, re.IGNORECASE)
                                if m:
                                    val = m.group(1).strip()
                                    if val and val.lower() not in ("xml", "value", "object", "page"):
                                        info["friendly_name"] = val
                                        break
                            if info["friendly_name"]:
                                break
                except Exception:
                    pass
                return info
    except (FileNotFoundError, PermissionError):
        pass
    return None


# iRidium listens on these — used to count "real client connections"
IRIDIUM_PORTS = ("8888", "8443", "30464", "30465", "30583")


@time_cache(5)
def iridium_clients_count():
    """How many established TCP sessions hit iRidium client/web ports."""
    out = sh("sudo ss -tn state established 2>/dev/null")
    n = 0
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) < 4:
            continue
        local = parts[2] if len(parts) >= 3 else parts[-2]
        # Local addr ends with :PORT — last colon-separated chunk
        port = local.rsplit(":", 1)[-1] if ":" in local else ""
        if port in IRIDIUM_PORTS:
            n += 1
    return n


@time_cache(60)
def iridium_db_size():
    try:
        return os.path.getsize(_ir_db())
    except (FileNotFoundError, PermissionError):
        return None


@time_cache(5)
def iridium_proc_info(pid):
    if not pid or pid == "0":
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            data = f.read()
        m_rss = re.search(r"VmRSS:\s+(\d+)\s+kB", data)
        m_th  = re.search(r"Threads:\s+(\d+)", data)
        m_vm  = re.search(r"VmSize:\s+(\d+)\s+kB", data)
        return {
            "rss_kb":   int(m_rss.group(1)) if m_rss else None,
            "vm_kb":    int(m_vm.group(1))  if m_vm  else None,
            "threads":  int(m_th.group(1))  if m_th  else None,
        }
    except (FileNotFoundError, PermissionError):
        return None


# Состояние портала :8888 — обновляется фоновым потоком, читается мгновенно
# из памяти. Не блокируем waitress-worker'ов синхронным HTTP-вызовом к iRidium
# (который может тупить из-за GC / скрипт-багов в проекте клиента).
_IR_HTTP_STATE = {"data": {"alive": None, "error": "ещё не проверен"},
                  "ts": 0.0, "lock": threading.Lock()}

def _iridium_http_check_now():
    """Один TCP+HTTP probe к :8888 с timeout 2 сек."""
    try:
        import urllib.request, socket
        t0 = time.time()
        req = urllib.request.Request("http://127.0.0.1:8888/")
        with urllib.request.urlopen(req, timeout=2) as r:
            ms = int((time.time() - t0) * 1000)
            return {"alive": True, "code": r.getcode(), "ms": ms}
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        return {"alive": True, "code": e.code, "ms": ms}
    except Exception as e:
        return {"alive": False, "error": str(e)[:80]}

def iridium_http_check():
    """Мгновенное чтение из RAM — не блокирует HTTP-worker."""
    with _IR_HTTP_STATE["lock"]:
        return dict(_IR_HTTP_STATE["data"])

def _iridium_http_sampler():
    """Фоновый поток: каждые 10 сек дёргает iRidium :8888 и обновляет _IR_HTTP_STATE."""
    while True:
        try:
            d = _iridium_http_check_now()
            with _IR_HTTP_STATE["lock"]:
                _IR_HTTP_STATE["data"] = d
                _IR_HTTP_STATE["ts"] = time.time()
        except Exception:
            pass
        time.sleep(10)


def iridium_info(pid):
    return {
        "version": iridium_version(),
        "project": iridium_project_info(),
        "db_size": iridium_db_size(),
        "proc":    iridium_proc_info(pid),
        "clients": iridium_clients_count(),
        "http":    iridium_http_check(),
        "api":     iridium_api_snapshot(),
    }


# ============ iRidium HTTP API integration ============
# Reverse-engineered: POST /html/login.html with form data sets 'ir-session-id' cookie,
# then GET /json/{module}/.../get returns JSON (text/plain content-type).

_IR_SESSION = {"cookie": None, "obtained_at": 0, "lock": threading.Lock()}
IR_LOGIN_URL = "http://127.0.0.1:8888/html/login.html"
IR_BASE      = "http://127.0.0.1:8888"


def _ir_login():
    """POST credentials, return new session cookie or None.
    Uses CookieJar so 'ir-session-id' is captured even before the 301 redirect
    to /html/main is followed."""
    pw = network_bp.setting_get("iridium_password", "")
    if not pw:
        return None
    user = network_bp.setting_get("iridium_username", "admin") or "admin"
    try:
        import urllib.request, urllib.parse, http.cookiejar
        data = urllib.parse.urlencode({
            "name": "authform", "Login": user, "Password": pw
        }).encode()
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        req = urllib.request.Request(IR_LOGIN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            opener.open(req, timeout=6)
        except Exception:
            pass    # the 301 → /html/main may 404 with this client; cookie is already in jar
        for c in jar:
            if c.name == "ir-session-id":
                return c.value
    except Exception:
        return None
    return None


def _ir_get_cookie():
    """Get current session cookie; relogin if missing or older than 30 min.
    Логин выполняется ВНУТРИ lock'а — иначе 6 параллельных _ir_get запросов
    делают 6 параллельных POST на /html/login.html, iRidium выдаёт 6 разных
    session-id, и каждый следующий инвалидирует предыдущий → часть запросов
    падает с auth failed."""
    with _IR_SESSION["lock"]:
        now = time.time()
        if _IR_SESSION["cookie"] and (now - _IR_SESSION["obtained_at"]) < 1800:
            return _IR_SESSION["cookie"]
        new = _ir_login()
        _IR_SESSION["cookie"] = new
        _IR_SESSION["obtained_at"] = now
        return new


def _ir_get(path):
    """GET /json/... with cookie. Returns parsed JSON or None on auth/error."""
    cookie = _ir_get_cookie()
    if not cookie:
        return None
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(IR_BASE + path)
        req.add_header("Cookie", f"ir-session-id={cookie}")
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
        # iRidium server возвращает Cyrillic в CP1251 (Win-1251), не UTF-8 —
        # fallback при ошибке декодирования.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", "replace")
        return _json.loads(text)
    except Exception:
        with _IR_SESSION["lock"]:
            _IR_SESSION["cookie"] = None
        return None


_IR_API_CACHE = {"data": None, "ts": 0.0,
                 "last_good": None, "last_good_ts": 0.0,
                 "lock": threading.Lock()}
# Stale-while-revalidate: показываем last_good этот период, даже если новый
# вызов упал. Скрывает кратковременные хиккапы iRidium (GC pauses, скрипт-баги).
IR_STALE_GRACE_SEC = 300   # 5 минут

def iridium_api_snapshot():
    """Мгновенное чтение последнего snapshot'а из RAM — НЕ блокирует
    waitress-worker'ов синхронным HTTP-вызовом к iRidium API.
    Snapshot обновляется фоновым потоком _iridium_api_sampler каждые 30 сек."""
    with _IR_API_CACHE["lock"]:
        if _IR_API_CACHE["data"] is None:
            return {"configured": bool(network_bp.setting_get("iridium_password", "")),
                    "ok": False, "error": "ещё не опрошен (запустился только что)"}
        return _IR_API_CACHE["data"]


def _iridium_api_refresh():
    """Один тик: вызвать impl, обновить кеш и last_good. Может занять до 11 сек
    в худшем случае (login 6с + GET 5с с retry), но это в background-потоке."""
    now = time.time()
    result = _iridium_api_snapshot_impl()
    with _IR_API_CACHE["lock"]:
        if result.get("ok"):
            _IR_API_CACHE["data"] = result
            _IR_API_CACHE["ts"] = now
            _IR_API_CACHE["last_good"] = result
            _IR_API_CACHE["last_good_ts"] = now
        else:
            lg = _IR_API_CACHE["last_good"]
            lg_age = now - _IR_API_CACHE["last_good_ts"]
            if lg is not None and lg_age < IR_STALE_GRACE_SEC:
                stale_copy = dict(lg)
                stale_copy["stale"] = True
                stale_copy["stale_age_sec"] = int(lg_age)
                stale_copy["last_error"] = result.get("error")
                _IR_API_CACHE["data"] = stale_copy
            else:
                _IR_API_CACHE["data"] = result
            _IR_API_CACHE["ts"] = now


def _iridium_api_sampler():
    """Фоновый поток: 30-сек шаг. Снимает данные iRidium API, обновляет кеш."""
    while True:
        try:
            _iridium_api_refresh()
        except Exception:
            pass
        time.sleep(30)


def _iridium_api_snapshot_impl():
    pw_configured = bool(network_bp.setting_get("iridium_password", ""))
    if not pw_configured:
        return {"configured": False}
    eps = [
        "/json/main/systemmenu/get",
        "/json/info/systemmenu/get",
        "/json/licence/licence/get",
        "/json/current_project/project/get",
        "/json/devices/devices/get",
        "/json/tags/server/tags/get",
    ]
    def _try_all():
        futures = [_EXECUTOR.submit(_ir_get, ep) for ep in eps]
        return [f.result() or {} for f in futures]
    results = _try_all()
    # Retry один раз через 200 мс если ВСЕ упали — защита от GC pause iRidium
    if all(not r for r in results):
        time.sleep(0.2)
        # Сбрасываем cookie чтобы новый _ir_get сделал свежий login
        with _IR_SESSION["lock"]:
            _IR_SESSION["cookie"] = None
        results = _try_all()
    main, info, licence, project, devices, tags = results
    if not main and not licence:
        return {"configured": True, "ok": False, "error": "iRidium API не отвечает (GC pause? скрипт-баг?)"}
    return {
        "configured": True,
        "ok": True,
        "username": main.get("username"),
        "server_time": main.get("server_time"),
        "platform_iridium": main.get("platform"),
        "build_full": (main.get("buildversion", "") + ":"
                       + main.get("buildnumber", "")),
        "system": info.get("system"),   # [serial, hostname, os, arch, model, docs, bin, logs]
        "licence": {
            "type": licence.get("type"),
            "server_max_clients": licence.get("server_max_clients"),
            "datapoints": licence.get("datapoints"),
            "serial": licence.get("serial"),
            "expired": licence.get("expired"),
            "products": licence.get("products") or [],
            "qr_mode": licence.get("qr_mode"),
            "script_mode": licence.get("script_mode"),
        },
        "project": {
            "cloud_id": project.get("cloud_id"),
            "name": project.get("name"),
            "type": project.get("type"),
            "status": project.get("status"),
            "run_time": project.get("run_time"),
            "guid": project.get("GUID"),
        },
        "devices": {
            "count": devices.get("device_count"),
            "names": devices.get("device_names") or [],
        },
        "tags": {
            "count": tags.get("tags_count"),
        },
    }


# ============ HEALTH CHECKS ============

def ntp_status():
    out = sh("timedatectl show -p NTPSynchronized --value").strip().lower()
    return out == "yes"


def failed_units_list():
    out = sh("systemctl --failed --no-legend --plain")
    result = []
    for ln in out.splitlines():
        parts = ln.split()
        if parts and parts[0].endswith((".service", ".target", ".socket", ".timer", ".mount")):
            result.append(parts[0])
    return result


def reboot_required_info():
    if not os.path.exists("/var/run/reboot-required"):
        return {"required": False, "pkgs": []}
    pkgs = []
    try:
        with open("/var/run/reboot-required.pkgs") as f:
            pkgs = [p.strip() for p in f if p.strip()]
    except Exception:
        pass
    return {"required": True, "pkgs": pkgs}


# Шаблоны kernel-сообщений которые ТЕХНИЧЕСКИ err/crit, но являются
# известными low-priority firmware-багами (Linux мог бы их не log-ировать как err,
# но логирует, потому что строго следует ACPI спеке). Не actionable администратором.
# Если приходит новая платформа с новым «шумом» — добавлять сюда.
_DMESG_KNOWN_FIRMWARE_NOISE = (
    # MSI Cubi 5 1M (и подобные) BIOS bug: ACPI _TMP/_FST ссылаются на
    # несуществующие методы. Linux пытается их вызвать → "Could not resolve".
    "ACPI BIOS Error",
    "ACPI Error: Aborting method",
    # Производная — отключение thermal zone когда _TMP не работает
    "Unable to get temperature, disabling",
    "Disabled thermal zone with critical trip point",
    # Raspberry Pi 5: PCIe слот пуст (нет NVMe/расширения) — link down это норма
    "brcm-pcie 1000110000.pcie: link down",
    # Raspberry Pi: WiFi firmware отвергает country setting если используется
    # только Ethernet — безобидно, WiFi всё равно работает по дефолту
    "brcmf_cfg80211_reg_notifier: Firmware rejected country setting",
)


def _is_firmware_noise(line):
    """True если строка — известный firmware шум, не actionable."""
    return any(p in line for p in _DMESG_KNOWN_FIRMWARE_NOISE)


def dmesg_errors(n=5):
    """Последние n РЕАЛЬНЫХ ошибок dmesg (err/crit/alert/emerg).
    Известный firmware-шум (ACPI BIOS Errors на некоторых MSI/Intel платформах)
    исключается — это БАГ ПРОШИВКИ, не системы; пользователь ничего с этим
    сделать не может. Возвращаем только actionable ошибки."""
    # Берём БОЛЬШЕ строк чем n, чтобы после фильтрации осталось n реальных
    out = sh(
        "sudo dmesg -l err,crit,alert,emerg --color=never -T 2>/dev/null | tail -50",
        timeout=10,
    )
    real = [ln for ln in out.splitlines() if ln.strip() and not _is_firmware_noise(ln)]
    return real[-n:]


def dmesg_firmware_noise_count():
    """Сколько firmware-bug сообщений в dmesg. Информативно — не считаются ошибками."""
    out = sh(
        "sudo dmesg -l err,crit,alert,emerg --color=never 2>/dev/null",
        timeout=10,
    )
    return sum(1 for ln in out.splitlines() if _is_firmware_noise(ln))


def root_block_device():
    out = sh("findmnt -n -o SOURCE /").strip()
    if not out:
        return None
    if "mmcblk" in out or "nvme" in out:
        return re.sub(r"p\d+$", "", out)
    return re.sub(r"\d+$", "", out)


def _read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _decode_mmc_date(raw):
    """MMC/SD date register MMYYYY (e.g., '06/2023')."""
    if not raw:
        return None
    if "/" in raw:
        return raw
    return raw


def smart_disk_health():
    """SMART для SATA/NVMe дисков (x86 неттопы — Cubi и т.п.).
    Возвращает dict или None если smartctl не установлен / диск не SATA/NVMe.

    Используем `smartctl --json` чтобы получить структурированный output."""
    dev = root_block_device()
    if not dev:
        return None
    if "mmcblk" in dev:
        return None   # SD-карта → отдельная функция sdcard_health()
    if not shutil.which("smartctl"):
        return None

    try:
        out = sh(
            f"sudo smartctl --json --info --health --attributes {dev} 2>/dev/null",
            timeout=8
        )
        if not out:
            return None
        data = json.loads(out)
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return None

    info = {
        "device": dev,
        "card_type": "SSD" if data.get("rotation_rate", 1) == 0 else "HDD",
        "model": data.get("model_name") or data.get("model_family", "?"),
        "serial": data.get("serial_number"),
        "firmware": data.get("firmware_version"),
    }

    # Размер в GB
    cap_bytes = (data.get("user_capacity") or {}).get("bytes") or 0
    if cap_bytes:
        info["size_h"] = f"{cap_bytes // (1024**3)} GB"

    # SMART overall health (PASSED/FAILED)
    info["smart_passed"] = (data.get("smart_status") or {}).get("passed")

    # Температура
    temp = (data.get("temperature") or {}).get("current")
    if temp:
        info["temp_c"] = temp

    # Power-on hours
    poh = (data.get("power_on_time") or {}).get("hours")
    if poh is not None:
        info["power_on_hours"] = poh
        info["power_on_years"] = round(poh / 8760, 1)   # 24×365

    # Атрибуты SMART (для SATA) или NVMe-specific
    for attr in ((data.get("ata_smart_attributes") or {}).get("table") or []):
        name = attr.get("name", "")
        raw = (attr.get("raw") or {}).get("value")
        norm = attr.get("value")
        if name == "Reallocated_Sector_Ct":
            info["reallocated_sectors"] = raw
        elif name in ("Wear_Leveling_Count", "SSD_Life_Left", "Percent_Lifetime_Remain"):
            # Normalized value = remaining life % (для большинства SSD)
            if norm is not None:
                info["lifespan_pct"] = norm
        elif name == "Power_Cycle_Count":
            info["power_cycles"] = raw
        elif name in ("Total_LBAs_Written", "Total_Host_Writes"):
            # Конвертируем LBA → TB (1 LBA = 512 байт)
            if raw:
                info["written_tb"] = round(raw * 512 / (1024 ** 4), 2)

    # NVMe-specific
    nvme = data.get("nvme_smart_health_information_log") or {}
    if nvme:
        if "percentage_used" in nvme:
            info["lifespan_pct"] = max(0, 100 - nvme["percentage_used"])
        if "data_units_written" in nvme:
            # NVMe data units = 512 KB (1000 sectors of 512 bytes)
            info["written_tb"] = round(nvme["data_units_written"] * 512 * 1000 / (1024 ** 4), 2)

    return info


def sdcard_health():
    """eMMC has real SMART via extcsd; plain SD cards do not (hardware
    limitation). We detect type and return what's actually readable."""
    dev = root_block_device()
    if not dev or "mmcblk" not in dev:
        return None
    short = dev.replace("/dev/", "")
    base = f"/sys/block/{short}/device"
    card_type = _read_sysfs(f"{base}/type") or "?"
    info = {
        "device": dev,
        "card_type": card_type,
        "name": _read_sysfs(f"{base}/name"),
        "manfid": _read_sysfs(f"{base}/manfid"),
        "date": _decode_mmc_date(_read_sysfs(f"{base}/date")),
        "fwrev": _read_sysfs(f"{base}/fwrev"),
        "hwrev": _read_sysfs(f"{base}/hwrev"),
        "size_h": sh(f"lsblk -dno SIZE {dev} 2>/dev/null").strip(),
    }
    # Known manufacturer IDs
    manuf_map = {
        "0x000003": "SanDisk", "0x00001b": "Samsung",
        "0x000027": "Phison", "0x000028": "Lexar",
        "0x000041": "Kingston", "0x000074": "Transcend",
        "0x00009f": "GoodRam", "0x0000ad": "Longsys",
        "0x000074": "PNY", "0x0000fe": "Micron",
    }
    info["manufacturer"] = manuf_map.get(info["manfid"], info["manfid"] or "?")

    # Try eMMC SMART (only works on eMMC, not SD)
    if card_type.upper() == "MMC":
        out = sh(f"sudo mmc extcsd read {dev} 2>/dev/null", timeout=8)
        if out:
            eol_names = {0: "не использовалось", 1: "норма",
                         2: "предупреждение (≤80% жизни)",
                         3: "критично — менять срочно"}
            for ln in out.splitlines():
                if "PRE_EOL_INFO" in ln:
                    m = re.search(r"0x([\da-fA-F]+)", ln)
                    if m:
                        v = int(m.group(1), 16)
                        info["pre_eol_raw"] = v
                        info["pre_eol"] = eol_names.get(v, f"unknown ({v})")
                elif "DEVICE_LIFE_TIME_EST_TYP_A" in ln:
                    m = re.search(r"0x([\da-fA-F]+)", ln)
                    if m:
                        v = int(m.group(1), 16)
                        if 1 <= v <= 10:
                            info["life_a_pct_max"] = v * 10
                        elif v == 11:
                            info["life_a_pct_max"] = 110
    return info


_UPDATES = {"ts": 0, "count": None, "ok": False}
_UPDATES_LOCK = threading.Lock()


def _updates_refresher():
    while True:
        try:
            out = sh("apt list --upgradable 2>/dev/null", timeout=120)
            count = len([l for l in out.splitlines() if "/" in l and "upgradable" in l])
            with _UPDATES_LOCK:
                _UPDATES["count"] = count
                _UPDATES["ts"] = int(time.time())
                _UPDATES["ok"] = True
        except Exception:
            with _UPDATES_LOCK:
                _UPDATES["ok"] = False
        time.sleep(6 * 3600)


threading.Thread(target=_updates_refresher, daemon=True).start()


_HEALTH = {"ts": 0, "data": None}
_HEALTH_LOCK = threading.Lock()


def health_summary():
    now = time.time()
    with _HEALTH_LOCK:
        if _HEALTH["data"] and (now - _HEALTH["ts"]) < 30:
            return _HEALTH["data"]
    data = {
        "ntp_synced": ntp_status(),
        "failed_units": failed_units_list(),
        "reboot": reboot_required_info(),
        "updates": _updates_snapshot(),
        "dmesg_errors": dmesg_errors(5),
        "dmesg_firmware_noise": dmesg_firmware_noise_count(),
        # Pi → sdcard (mmc extcsd / SD info), x86 → smart (smartctl JSON).
        # Frontend показывает одну плитку «Накопитель», содержимое разное.
        "sdcard": sdcard_health(),
        "smart": smart_disk_health(),
    }
    with _HEALTH_LOCK:
        _HEALTH["ts"] = now
        _HEALTH["data"] = data
    return data


def _updates_snapshot():
    """Атомарный снимок _UPDATES под локом."""
    with _UPDATES_LOCK:
        return {
            "count": _UPDATES["count"],
            "ts": _UPDATES["ts"],
            "fresh": _UPDATES["ok"],
        }


def _push_sample(buf, ts, t, c, m_pct, d_pct, net_kbps):
    buf["ts"].append(ts);    buf["temp"].append(t)
    buf["cpu"].append(c);    buf["ram"].append(m_pct)
    buf["disk"].append(d_pct); buf["net"].append(net_kbps)


def sampler():
    """Единый sampler: 60-сек шаг. Пишет в RAM (1h-буфер) и SQLite.
    24h-буфер обновляется каждые 5 итераций (5 мин)."""
    tick = 0
    while True:
        try:
            t = cpu_temp_c()
            c = cpu_usage_pct()
            m = mem_info()
            d = disk_info("/")
            rx_rate, tx_rate, _, _ = net_rate()
            net_kbps = round((rx_rate + tx_rate) / 1024, 2)
            ts = int(time.time())
            with LOCK:
                _push_sample(HISTORY_1H, ts, t, c, m.get("percent"), d.get("percent"), net_kbps)
                if tick % 5 == 0:
                    _push_sample(HISTORY_24H, ts, t, c, m.get("percent"), d.get("percent"), net_kbps)
            _metrics_persist(ts, t, c, m.get("percent"), d.get("percent"), net_kbps)
            # раз в час чистим старше 30 дней
            if tick % 60 == 0 and tick > 0:
                _metrics_cleanup(30)
        except Exception:
            pass
        tick += 1
        time.sleep(60)


_metrics_hydrate()
threading.Thread(target=sampler, daemon=True).start()
# MikroTik: подгрузить историю из БД и запустить sampler (30-сек шаг)
mt.mt_hydrate()
threading.Thread(target=mt.mt_sampler, args=(network_bp,), daemon=True).start()
# MikroTik DHCP comments → имена устройств в карте сети, раз в час
threading.Thread(target=mt.mt_auto_sync_loop, args=(network_bp,), daemon=True).start()
# iRidium HTTP probe (порт :8888) — раз в 10 сек в фоне
threading.Thread(target=_iridium_http_sampler, daemon=True).start()
# iRidium API snapshot (лицензия, проект, устройства) — раз в 30 сек в фоне
threading.Thread(target=_iridium_api_sampler, daemon=True).start()


@app.route("/")
@requires_auth
def index():
    return send_from_directory(BASE, "index.html")


@app.route("/client")
@requires_auth
def client_view():
    """Read-only view — same HTML, frontend hides action buttons via ?ro=1."""
    return send_from_directory(BASE, "index.html")


@app.route("/services")
@requires_auth
def services_page():
    return send_from_directory(BASE, "services.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE, "manifest.json")


@app.route("/sw.js")
def sw():
    return send_from_directory(BASE, "sw.js")


@app.route("/marked.min.js")
def marked_js():
    return send_from_directory(BASE, "marked.min.js")


@app.route("/api/iridium/settings", methods=["GET"])
def get_iridium_settings():
    """Tell frontend whether iRidium creds are configured (don't leak password)."""
    pw = network_bp.setting_get("iridium_password", "")
    user = network_bp.setting_get("iridium_username", "admin")
    disabled = network_bp.setting_get("iridium_disabled", "0") == "1"
    return jsonify({
        "configured": bool(pw),
        "username": user or "admin",
        "password_set": bool(pw),
        "disabled": disabled,
    })


@app.route("/api/iridium/settings", methods=["PUT"])
@requires_auth
@audit_action("settings_iridium_changed", target_from_path=True)
def put_iridium_settings():
    d = request.get_json(force=True) or {}
    if "password" in d:
        network_bp.setting_set("iridium_password", d["password"] or "")
    if "username" in d:
        network_bp.setting_set("iridium_username", d["username"] or "admin")
    if "disabled" in d:
        # bool или "1"/"0" — сохраняем строкой для совместимости с settings
        val = "1" if (d["disabled"] is True or str(d["disabled"]) in ("1", "true")) else "0"
        network_bp.setting_set("iridium_disabled", val)
    # Drop cached cookie so next API call re-logs in with new creds
    with _IR_SESSION["lock"]:
        _IR_SESSION["cookie"] = None
    # Test it
    # Bypass cache при смене пароля — вызываем impl напрямую
    with _IR_API_CACHE["lock"]:
        _IR_API_CACHE["data"] = None
    snap = _iridium_api_snapshot_impl()
    return jsonify({"ok": True, "test": snap})


@app.route("/api/mikrotik/status")
def api_mikrotik_status():
    return jsonify(mt.mt_status_snapshot(network_bp))


@app.route("/api/mikrotik/settings", methods=["GET"])
def get_mikrotik_settings():
    default_ip = mt._default_mt_ip(network_bp)
    return jsonify({
        "ip": network_bp.setting_get("mikrotik_ip", default_ip) or default_ip,
        "user": network_bp.setting_get("mikrotik_user", "admin") or "admin",
        "configured": bool(network_bp.setting_get("mikrotik_password", "")),
        "detected_gateway": network_bp.detect_gateway(),  # для UI placeholder
    })


@app.route("/api/mikrotik/settings", methods=["PUT"])
@requires_auth
@audit_action("settings_mikrotik_changed", target_from_path=True)
def put_mikrotik_settings():
    d = request.get_json(force=True) or {}
    default_ip = mt._default_mt_ip(network_bp)
    if "ip" in d:
        network_bp.setting_set("mikrotik_ip", d["ip"] or default_ip)
    if "user" in d:
        network_bp.setting_set("mikrotik_user", d["user"] or "admin")
    if "password" in d:
        network_bp.setting_set("mikrotik_password", d["password"] or "")
    # Сброс кеша чтобы сразу подхватить новые креды
    with mt._CACHE_LOCK:
        mt._CACHE.clear()
    test = mt.mt_status_snapshot(network_bp)
    return jsonify({"ok": True, "test": test})


@app.route("/api/mikrotik/history")
def api_mikrotik_history():
    rng = request.args.get("range", "1h")
    return jsonify(mt.mt_history(rng))


@app.route("/api/mikrotik/sync", methods=["POST"])
@requires_auth
@audit_action("mikrotik_sync", target_from_path=True)
def api_mikrotik_sync():
    """Синхронизируем DHCP comments → имена устройств. Override always."""
    network_bp.DB_PATH = network_bp.DB_PATH  # noqa - убедиться что атрибут есть
    return jsonify(mt.sync_dhcp_to_inventory(network_bp))


@app.route("/chart.min.js")
def chart_js():
    return send_from_directory(BASE, "chart.min.js")


@app.route("/api/status")
def api_status():
    # Fan out independent slow subprocess calls in parallel.
    # Light/local calls (mem_info, disk_info, cpu_usage_pct, vcgencmd…)
    # stay sequential — they touch /proc and complete in <2 ms each.
    f_irsrv     = _EXECUTOR.submit(service_status, "irserver")
    f_argon_sv  = _EXECUTOR.submit(service_status, "argononed")
    f_top_cpu   = _EXECUTOR.submit(top_processes, 5, "cpu")
    f_top_mem   = _EXECUTOR.submit(top_processes, 5, "mem")
    f_iconn     = _EXECUTOR.submit(iridium_connections)
    f_ilog      = _EXECUTOR.submit(iridium_recent_log, 50)
    f_ilog_meta = _EXECUTOR.submit(iridium_log_meta)
    f_argon_in  = _EXECUTOR.submit(argon_installed)
    f_health    = _EXECUTOR.submit(health_summary)

    rx_rate, tx_rate, rx_total, tx_total = net_rate()
    throt_raw, throt = throttled()
    irsrv = f_irsrv.result()
    return jsonify({
        "ts": int(time.time()),
        "host": os.uname().nodename,
        "platform_name": platform_name(),
        "system": {
            "uptime": uptime_sec(),
            "uptime_fmt": fmt_uptime(uptime_sec()),
            "loadavg": loadavg(),
            "kernel": os.uname().release,
        },
        "cpu": {
            "usage": cpu_usage_pct(),
            "freq_mhz": cpu_freq_mhz(),
            "temp_c": cpu_temp_c(),
            "per_core": cpu_usage_per_core(),
        },
        "mem": mem_info(),
        "disk_root": disk_info("/"),
        "voltage_v": core_volt_v(),
        "throttle": {"raw": throt_raw, "flags": throt},
        "net": {
            "iface": primary_iface(),
            "rx_rate": rx_rate, "tx_rate": tx_rate,
            "rx_total": rx_total, "tx_total": tx_total,
        },
        "irserver": irsrv,
        "argononed": f_argon_sv.result(),
        "fan_pct": fan_percent(),
        "top_cpu": f_top_cpu.result(),
        "top_mem": f_top_mem.result(),
        "iridium_conn": f_iconn.result(),
        "iridium_log": f_ilog.result(),
        "iridium_log_meta": f_ilog_meta.result(),
        "iridium_info": iridium_info(irsrv["pid"]),
        "iridium_disabled": network_bp.setting_get("iridium_disabled", "0") == "1",
        "health": f_health.result(),
        "argon_installed": f_argon_in.result(),
    })


@app.route("/api/history")
def api_history():
    from flask import request
    rng = request.args.get("range", "1h")
    buf = HISTORY_24H if rng == "24h" else HISTORY_1H
    with LOCK:
        ts   = list(buf["ts"])
        temp = list(buf["temp"])
        cpu  = list(buf["cpu"])
        ram  = list(buf["ram"])
        disk = list(buf["disk"])
        net  = list(buf["net"])
    # If buffer is empty/short on fresh restart, seed with current snapshot
    # so sparklines render immediately instead of waiting 60s.
    if len(ts) < 2:
        now = int(time.time())
        m = mem_info()
        d = disk_info("/")
        cur_temp = cpu_temp_c()
        cur_cpu = cpu_usage_pct()
        rx, tx, _, _ = net_rate()
        cur_net = round((rx + tx) / 1024, 2)
        # add two close points so polyline draws something
        if not ts:
            ts.append(now - 1); temp.append(cur_temp); cpu.append(cur_cpu)
            ram.append(m.get("percent")); disk.append(d.get("percent"))
            net.append(cur_net)
        ts.append(now); temp.append(cur_temp); cpu.append(cur_cpu)
        ram.append(m.get("percent")); disk.append(d.get("percent"))
        net.append(cur_net)
    return jsonify({
        "range": rng,
        "ts":   ts,
        "temp": temp,
        "cpu":  cpu,
        "ram":  ram,
        "disk": disk,
        "net":  net,
    })


def run_cmd(cmd, message):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=15)
        return jsonify({
            "ok": r.returncode == 0,
            "message": message,
            "stdout": r.stdout,
            "stderr": r.stderr,
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/action/restart-iridium", methods=["POST"])
@requires_auth
@audit_action("action_restart_iridium", target_from_path=True)
def act_restart_iridium():
    return run_cmd("sudo systemctl restart irserver", "iRidium service перезапущен")


@app.route("/api/action/start-iridium", methods=["POST"])
@requires_auth
@audit_action("action_start_iridium", target_from_path=True)
def act_start_iridium():
    return run_cmd("sudo systemctl start irserver", "iRidium service запущен")


@app.route("/api/action/stop-iridium", methods=["POST"])
@requires_auth
@audit_action("action_stop_iridium", target_from_path=True)
def act_stop_iridium():
    return run_cmd("sudo systemctl stop irserver", "iRidium service остановлен")


@app.route("/api/action/restart-argononed", methods=["POST"])
@requires_auth
@audit_action("action_restart_argononed", target_from_path=True)
def act_restart_argononed():
    return run_cmd("sudo systemctl restart argononed", "argononed перезапущен")


@app.route("/api/action/reboot", methods=["POST"])
@requires_auth
@audit_action("action_reboot_pi", target_from_path=True)
def act_reboot():
    subprocess.Popen("sleep 2 && sudo reboot", shell=True)
    return jsonify({"ok": True, "message": "Контроллер перезагрузится через 2 сек"})


@app.route("/api/action/shutdown", methods=["POST"])
@requires_auth
@audit_action("action_shutdown_pi", target_from_path=True)
def act_shutdown():
    subprocess.Popen("sleep 2 && sudo shutdown -h now", shell=True)
    return jsonify({"ok": True, "message": "Контроллер выключится через 2 сек"})


# ============ AUTH endpoints ============

@app.route("/login")
def page_login():
    return send_from_directory(BASE, "login.html")


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    d = request.get_json(force=True, silent=True) or {}
    username = (d.get("username") or "").strip().lower()
    password = d.get("password") or ""
    ip = _client_ip()
    user = network_bp.auth_get_user(username)
    if not user or not network_bp.auth_verify_password(
            password, user["salt"], user["password_hash"]):
        network_bp.auth_log(username or None, ip, "login_failed",
                            details={"username_tried": username}, result="fail")
        return jsonify({"ok": False, "error": "Неверный логин или пароль"}), 401
    sid = network_bp.auth_create_session(
        username, ip, request.headers.get("User-Agent", "")[:200]
    )
    with network_bp.db() as c:
        c.execute("UPDATE auth_users SET last_login=? WHERE id=?",
                  (int(time.time()), user["id"]))
    network_bp.auth_log(username, ip, "login_success", result="success")
    resp = jsonify({
        "ok": True,
        "username": username,
        "is_admin": bool(user["is_admin"]),
        "must_change_password": bool(user["must_change_password"]),
    })
    resp.set_cookie("sc_session", sid, httponly=True, samesite="Lax",
                    max_age=network_bp.SESSION_TTL_SEC, path="/")
    return resp


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    sid = request.cookies.get("sc_session")
    sess = network_bp.auth_get_session(sid) if sid else None
    if sess:
        network_bp.auth_log(sess["username"], _client_ip(), "logout")
        network_bp.auth_delete_session(sid)
    resp = jsonify({"ok": True})
    resp.delete_cookie("sc_session", path="/")
    return resp


@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    sess = _get_session()
    if not sess:
        return jsonify({"ok": False, "authenticated": False}), 200
    user = network_bp.auth_get_user(sess["username"])
    return jsonify({
        "ok": True,
        "authenticated": True,
        "username": sess["username"],
        "is_admin": bool(user["is_admin"]) if user else False,
        "must_change_password": bool(user["must_change_password"]) if user else False,
        "session_created": sess["created_at"],
        "last_seen": sess["last_seen"],
    })


@app.route("/api/auth/change-password", methods=["POST"])
@requires_auth
def api_auth_change_password():
    d = request.get_json(force=True, silent=True) or {}
    cur_pw = d.get("current_password") or ""
    new_pw = d.get("new_password") or ""
    sess = _get_session()
    user = network_bp.auth_get_user(sess["username"])
    if not network_bp.auth_verify_password(cur_pw, user["salt"], user["password_hash"]):
        network_bp.auth_log(sess["username"], _client_ip(),
                            "change_password", result="fail",
                            details={"reason": "wrong current password"})
        return jsonify({"ok": False, "error": "Неверный текущий пароль"}), 400
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "Минимум 4 символа"}), 400
    network_bp.auth_set_password(sess["username"], new_pw, clear_must_change=True)
    network_bp.auth_log(sess["username"], _client_ip(),
                        "change_password", result="success")
    return jsonify({"ok": True})


@app.route("/api/auth/users", methods=["GET"])
@requires_admin
def api_auth_users_list():
    return jsonify({"users": network_bp.auth_list_users()})


@app.route("/api/auth/users", methods=["POST"])
@requires_admin
def api_auth_users_create():
    d = request.get_json(force=True, silent=True) or {}
    username = (d.get("username") or "").strip().lower()
    password = d.get("password") or ""
    is_admin = bool(d.get("is_admin"))
    if not username or not re.match(r"^[a-z0-9_-]{2,32}$", username):
        return jsonify({"ok": False, "error": "Имя: 2-32 символа a-z 0-9 _ -"}), 400
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Пароль минимум 4 символа"}), 400
    if network_bp.auth_get_user(username):
        return jsonify({"ok": False, "error": "Уже существует"}), 400
    network_bp.auth_create_user(username, password, is_admin=is_admin)
    network_bp.auth_log(request.auth_user["username"], _client_ip(),
                        "create_user", target=username,
                        details={"is_admin": is_admin})
    return jsonify({"ok": True})


@app.route("/api/auth/users/<int:uid>", methods=["DELETE"])
@requires_admin
def api_auth_users_delete(uid):
    ok, err = network_bp.auth_delete_user(uid)
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    network_bp.auth_log(request.auth_user["username"], _client_ip(),
                        "delete_user", target=str(uid))
    return jsonify({"ok": True})


@app.route("/api/auth/users/<int:uid>/password", methods=["POST"])
@requires_admin
def api_auth_users_reset_password(uid):
    """Админ сбрасывает пароль другому юзеру (без знания старого)."""
    d = request.get_json(force=True, silent=True) or {}
    new_pw = d.get("new_password") or ""
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "Минимум 4 символа"}), 400
    with network_bp.db() as c:
        row = c.execute("SELECT username FROM auth_users WHERE id=?",
                        (uid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Пользователь не найден"}), 404
    network_bp.auth_set_password(row["username"], new_pw, clear_must_change=False)
    # Помечаем must_change чтобы юзер сменил при следующем входе
    with network_bp.db() as c:
        c.execute("UPDATE auth_users SET must_change_password=1 WHERE id=?", (uid,))
    network_bp.auth_log(request.auth_user["username"], _client_ip(),
                        "reset_user_password", target=row["username"])
    return jsonify({"ok": True})


@app.route("/api/auth/audit", methods=["GET"])
@requires_admin
def api_auth_audit():
    limit = max(1, min(int(request.args.get("limit", "200")), 1000))
    offset = max(0, int(request.args.get("offset", "0")))
    since_ts = request.args.get("since")
    since_ts = int(since_ts) if since_ts else None
    to_ts = request.args.get("to")
    to_ts = int(to_ts) if to_ts else None
    username = request.args.get("username")
    action = request.args.get("action")        # partial match (LIKE %X%)
    result = request.args.get("result")        # "success" | "fail"
    return jsonify({
        "audit": network_bp.auth_get_audit(limit, offset, since_ts, username,
                                            action=action, to_ts=to_ts,
                                            result=result),
    })


# ============ VERSIONING + AUTO-UPDATE ============

_UPDATE_STATE = {
    "checking": False,
    "applying": False,
    "last_check": 0,
    "last_check_result": None,    # {ok, latest_version, current_version, has_update, error}
    "last_apply": None,           # {ts, from_version, to_version, ok, error}
    "lock": threading.Lock(),
}


@app.route("/api/version")
def api_version():
    return jsonify({
        "version": VERSION,
        "release_date": RELEASE_DATE,
        "repo": GITHUB_REPO,
        "channel": "stable",
        "min_compatible_client": MIN_COMPATIBLE_CLIENT,
        "schema": network_bp.get_schema_state(),
    })


# ============ Services / Магазин (v1.5.0) ============

@app.route("/api/services/counts")
@requires_auth
def api_services_counts():
    """Для счётчика «Сервисы (N/M)» в шапке. Лёгкий, кешируется на фронте."""
    return jsonify(services_mod.counts())


@app.route("/api/services/catalog")
@requires_auth
def api_services_catalog():
    """Весь каталог + флаги совместимости + что установлено.
    Один-запрос-всё для страницы /services."""
    catalog = services_mod.load_catalog()
    installed_map = {s["id"]: s for s in services_mod.list_installed()}
    items = []
    for svc in catalog:
        compatible, reason = services_mod.is_compatible_with_platform(svc)
        item = dict(svc)
        item["_compatible"] = compatible
        item["_incompat_reason"] = None if compatible else reason
        inst = installed_map.get(svc["id"])
        item["_installed"] = inst   # None или dict
        item["_stats"] = services_mod.get_service_stats(svc["id"]) if inst else None
        # v2.5: uptime % за 24h — вычисляется на лету
        if inst:
            item["_uptime_pct_24h"] = services_mod.compute_uptime_pct(inst, 86400)
        items.append(item)
    return jsonify({
        "ok": True,
        "platform_arch": services_mod._platform_arch(),
        "ram_mb": services_mod._system_ram_mb(),
        "disk_free_gb": services_mod._system_free_disk_gb("/var"),
        "catalog_status": services_mod.catalog_status(),
        "services": items,
    })


@app.route("/api/services/installed")
@requires_auth
def api_services_installed():
    """Только установленные — для страницы «Мои сервисы»."""
    return jsonify({"ok": True, "services": services_mod.list_installed()})


@app.route("/api/services/refresh", methods=["POST"])
@requires_admin
@audit_action("services_refresh_catalog")
def api_services_refresh():
    """git pull каталога. Только админу."""
    ok, msg = services_mod.refresh_catalog()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/services/<service_id>/pre-check")
@requires_auth
def api_services_pre_check(service_id):
    """Pre-install чек (RAM/disk/ports/docker)."""
    return jsonify(services_mod.install_pre_check(service_id))


@app.route("/api/services/<service_id>/install", methods=["POST"])
@requires_admin
@audit_action("service_install", target_from_path=True)
def api_services_install(service_id):
    """Запустить установку. Возвращает сразу — прогресс через /install-progress."""
    ok, msg = services_mod.install_service(service_id)
    return jsonify({"ok": ok, "message": msg, "service_id": service_id}), (200 if ok else 400)


@app.route("/api/services/<service_id>/uninstall", methods=["POST"])
@requires_admin
@audit_action("service_uninstall", target_from_path=True)
def api_services_uninstall(service_id):
    """Удалить сервис. Требует password admin'а в body для повторной проверки.
    Body: {"password": "...", "delete_data": false}
    delete_data=true → удалить volume'ы И папку данных (НЕ восстановить!)."""
    data = request.get_json(silent=True) or {}
    pw = data.get("password", "")
    delete_data = bool(data.get("delete_data", False))

    # Двойная проверка пароля даже для admin сессии
    sess = _get_session()
    if not sess:
        return jsonify({"ok": False, "error": "not authenticated"}), 401
    user = network_bp.auth_get_user(sess.get("username"))
    if not user or not network_bp.auth_verify_password(pw, user["salt"], user["password_hash"]):
        return jsonify({"ok": False, "error": "Неверный пароль"}), 403

    ok, msg = services_mod.uninstall_service(service_id, delete_data=delete_data)
    return jsonify({"ok": ok, "message": msg, "delete_data": delete_data}), (200 if ok else 400)


@app.route("/api/services/<service_id>/install-progress")
@requires_auth
def api_services_install_progress(service_id):
    """Текущий прогресс install/uninstall (polling каждые 2-3с)."""
    p = services_mod.get_progress(service_id)
    if not p:
        return jsonify({"ok": False, "state": "none", "message": "no active operation"})
    return jsonify({"ok": True, **p})


@app.route("/api/services/<service_id>/action", methods=["POST"])
@requires_admin
@audit_action("service_action", target_from_path=True)
def api_services_action(service_id):
    """Лайфцикл: start/stop/restart. Body: {action: start|stop|restart}."""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    ok, msg = services_mod.service_action(service_id, action)
    return jsonify({"ok": ok, "action": action, "output": msg}), (200 if ok else 400)


@app.route("/api/services/<service_id>/logs")
@requires_auth
def api_services_logs(service_id):
    """Последние 100 строк `docker compose logs`."""
    ok, msg = services_mod.service_action(service_id, "logs")
    return jsonify({"ok": ok, "logs": msg if ok else "", "error": msg if not ok else None})


@app.route("/api/services/<service_id>/logs/stream")
@requires_auth
def api_services_logs_stream(service_id):
    """SSE stream: live-tail логов сервиса. Query params:
      ?since=30m — относительное время (default 30m, max 6h)
      ?level=error|warn|info — фильтр по уровню (default: все строки)
    Закрывается когда клиент отключается."""
    since = request.args.get("since", "30m")
    level = request.args.get("level")
    if level not in (None, "", "error", "warn", "info"):
        level = None
    if level == "":
        level = None
    # Limit since на максимум 6h чтобы не было загрузки 100MB логов на старте
    if since not in ("5m", "15m", "30m", "1h", "3h", "6h"):
        since = "30m"
    gen = services_mod.stream_logs(service_id, since=since, level=level)
    # SSE response — text/event-stream + no-cache + no-buffer
    return Response(gen, mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # для nginx если будет reverse-proxy
    })


@app.route("/api/services/<service_id>/network")
@requires_auth
def api_services_network(service_id):
    """Network info из compose.yml: ports, internal hostnames, containers."""
    info = services_mod.get_compose_network_info(service_id)
    if not info:
        return jsonify({"ok": False, "error": "compose.yml не найден"}), 404
    # Добавляем external base url из CONTROLLER:host_port
    return jsonify({"ok": True, **info})


@app.route("/api/services/<service_id>/changelog")
@requires_auth
def api_services_changelog(service_id):
    """Latest releases from upstream GitHub repo (если catalog YAML имеет
    image_origin.github). Кешируется на 1ч."""
    data = services_mod.get_changelog(service_id, max_entries=5)
    if data is None:
        return jsonify({"ok": False,
                        "error": "changelog не настроен (нет image_origin.github в манифесте)"}), 404
    return jsonify({"ok": True, "releases": data})


@app.route("/api/services/<service_id>/tags", methods=["GET"])
@requires_auth
def api_services_tags_get(service_id):
    """Кастомные теги сервиса."""
    return jsonify({"ok": True,
                    "tags": services_mod.get_custom_tags(service_id),
                    "all_tags": services_mod.list_all_custom_tags()})


@app.route("/api/services/<service_id>/tags", methods=["PUT"])
@requires_admin
@audit_action("service_tags_update", target_from_path=True,
              log_details=lambda: {"tags": (request.get_json(force=True, silent=True) or {}).get("tags")})
def api_services_tags_set(service_id):
    """Установить полный список тегов (replace, не append)."""
    d = request.get_json(force=True, silent=True) or {}
    tags = d.get("tags") or []
    ok, result = services_mod.set_custom_tags(service_id, tags)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "tags": result})


@app.route("/api/services/export")
@requires_admin
@audit_action("services_export_config")
def api_services_export():
    """Скачать zip всех compose.yml + manifest + README с инструкцией восстановления."""
    data = services_mod.export_config_zip()
    fname = f"smartcomm-services-{int(time.time())}.zip"
    return Response(data, mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Content-Length": str(len(data)),
    })


@app.route("/api/services/bulk-action", methods=["POST"])
@requires_admin
@audit_action("services_bulk_action", target_from_path=True,
              log_details=lambda: request.get_json(force=True, silent=True) or {})
def api_services_bulk_action():
    """Bulk start/stop/restart нескольких сервисов параллельно.
    Body: {action: 'restart', service_ids: [...] | null (=все installed)}"""
    d = request.get_json(force=True, silent=True) or {}
    action = d.get("action")
    if action not in ("start", "stop", "restart"):
        return jsonify({"ok": False, "error": "action: start|stop|restart"}), 400
    service_ids = d.get("service_ids")  # None или список
    if service_ids is not None and not isinstance(service_ids, list):
        return jsonify({"ok": False, "error": "service_ids must be list or null"}), 400
    results = services_mod.bulk_action(action, service_ids)
    success_count = sum(1 for ok, _ in results.values() if ok)
    return jsonify({
        "ok": success_count > 0,
        "total": len(results),
        "success": success_count,
        "fail": len(results) - success_count,
        "results": {sid: {"ok": ok, "message": msg[:200]}
                    for sid, (ok, msg) in results.items()},
    })


@app.route("/api/services/<service_id>/update", methods=["POST"])
@requires_admin
@audit_action("service_update", target_from_path=True)
def api_services_update(service_id):
    """Manual «Обновить» — docker compose pull + up -d. Прогресс через install-progress."""
    ok, msg = services_mod.update_service(service_id, source="manual")
    return jsonify({"ok": ok, "message": msg, "service_id": service_id}), (200 if ok else 400)


@app.route("/api/services/<service_id>/settings", methods=["PATCH"])
@requires_admin
@audit_action("service_settings_update", target_from_path=True)
def api_services_settings(service_id):
    """Обновить notes / auto_update. Body: {notes?, auto_update?}"""
    data = request.get_json(silent=True) or {}
    ok, msg = services_mod.update_settings(
        service_id,
        notes=data.get("notes"),
        auto_update=data.get("auto_update")
    )
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/services/<service_id>/stats")
@requires_auth
def api_services_stats(service_id):
    """CPU/MEM/NET — обновляется sampler'ом раз в 30с."""
    s = services_mod.get_service_stats(service_id)
    if not s:
        return jsonify({"ok": False, "message": "stats not available yet (sampler runs every 30s)"})
    return jsonify({"ok": True, **s})


@app.route("/api/services/<service_id>/metrics")
@requires_auth
def api_services_metrics(service_id):
    """Time-series метрик за range секунд. Default 24h, downsample до max_points точек."""
    try:
        range_sec = int(request.args.get("range", "86400"))   # 24h по умолчанию
        max_points = max(50, min(int(request.args.get("max_points", "288")), 2000))
    except ValueError:
        return jsonify({"ok": False, "error": "bad range/max_points"}), 400
    # Защита от слишком больших запросов
    range_sec = max(300, min(range_sec, 7 * 86400))   # от 5 мин до 7 дней
    points = services_mod.get_metrics_history(service_id, range_sec, max_points)
    return jsonify({"ok": True, "points": points, "range_sec": range_sec,
                    "count": len(points)})


@app.route("/api/services/profiles")
@requires_auth
def api_services_profiles():
    """Список профилей-пакетов с флагами совместимости."""
    profiles = services_mod.load_profiles()
    installed_map = {s["id"]: s for s in services_mod.list_installed()}
    items = []
    for p in profiles:
        svc_ids = p.get("services", [])
        installed_in_profile = [sid for sid in svc_ids if sid in installed_map]
        item = dict(p)
        item["_installed_count"] = len(installed_in_profile)
        item["_total_services"] = len(svc_ids)
        item["_ready"] = len(installed_in_profile) == len(svc_ids) and svc_ids
        items.append(item)
    return jsonify({"ok": True, "profiles": items})


@app.route("/api/services/profiles/<profile_id>/install", methods=["POST"])
@requires_admin
@audit_action("profile_install", target_from_path=True)
def api_services_profile_install(profile_id):
    """Запускает batch-установку всех сервисов профиля."""
    ok, msg = services_mod.install_profile(profile_id)
    return jsonify({"ok": ok, "message": msg, "profile_id": profile_id}), (200 if ok else 400)


@app.route("/api/changelog")
def api_changelog():
    """Отдаёт CHANGELOG.md как текст (для модалки версий).
    Если файл отсутствует локально — fallback на raw.githubusercontent.com.
    Это страховка для случаев когда install.sh не подтянул файл (legacy installs)."""
    p = BASE / "CHANGELOG.md"
    if p.exists():
        try:
            return jsonify({
                "ok": True,
                "version": VERSION,
                "source": "local",
                "markdown": p.read_text(encoding="utf-8"),
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    # Fallback на GitHub
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/CHANGELOG.md"
        req = urllib.request.Request(url, headers={"User-Agent": f"smartcomm-dashboard/{VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            md = resp.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "version": VERSION,
            "source": "github",
            "markdown": md,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"CHANGELOG.md отсутствует локально и не удалось скачать с GitHub: {e}",
        }), 404


def _parse_semver(v):
    """'1.2.3' → (1,2,3) or None if invalid."""
    try:
        parts = v.lstrip("v").split(".")
        return tuple(int(x) for x in parts[:3])
    except Exception:
        return None


def _check_github_release():
    """Тянем последний release с GitHub. Возвращает {ok, latest_version, tag,
    tarball_url, body} или {ok:false, error}."""
    try:
        import urllib.request
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", "smartcomm-dashboard/" + VERSION)
        req.add_header("Accept", "application/vnd.github+json")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "error": "На GitHub ещё нет ни одного release"}
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"сеть: {str(e)[:120]}"}
    tag = data.get("tag_name", "")
    latest = _parse_semver(tag)
    if not latest:
        return {"ok": False, "error": f"некорректный tag {tag!r}"}
    current = _parse_semver(VERSION)
    return {
        "ok": True,
        "latest_version": ".".join(str(x) for x in latest),
        "current_version": VERSION,
        "has_update": latest > current,
        "tag": tag,
        "tarball_url": data.get("tarball_url"),
        "release_url": data.get("html_url"),
        "body": data.get("body", "")[:2000],
        "published_at": data.get("published_at"),
    }


@app.route("/api/update/check")
@requires_auth
def api_update_check():
    """Ручная проверка наличия обновлений."""
    with _UPDATE_STATE["lock"]:
        _UPDATE_STATE["checking"] = True
    try:
        result = _check_github_release()
        _UPDATE_STATE["last_check"] = int(time.time())
        _UPDATE_STATE["last_check_result"] = result
        return jsonify(result)
    finally:
        with _UPDATE_STATE["lock"]:
            _UPDATE_STATE["checking"] = False


@app.route("/api/update/state")
@requires_auth
def api_update_state():
    """Снимок состояния updater'а — для UI."""
    with _UPDATE_STATE["lock"]:
        return jsonify({
            "version": VERSION,
            "release_date": RELEASE_DATE,
            "repo": GITHUB_REPO,
            "checking": _UPDATE_STATE["checking"],
            "applying": _UPDATE_STATE["applying"],
            "last_check": _UPDATE_STATE["last_check"],
            "last_check_result": _UPDATE_STATE["last_check_result"],
            "last_apply": _UPDATE_STATE["last_apply"],
        })


def _apply_update(tarball_url, to_version):
    """Скачать tarball, бекапнуть текущую папку, применить, рестартануть.
    Возвращает (ok, message)."""
    import urllib.request, tarfile, shutil, tempfile
    APP = BASE
    BACKUP = BACKUP_DIR / f"app-pre-{VERSION}-to-{to_version}-{int(time.time())}"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, "release.tar.gz")
            req = urllib.request.Request(tarball_url)
            req.add_header("User-Agent", "smartcomm-dashboard/" + VERSION)
            with urllib.request.urlopen(req, timeout=60) as r:
                with open(tar_path, "wb") as f:
                    # Не использовать walrus (:=) — Python 3.7 (Buster) не поддерживает
                    while True:
                        chunk = r.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            # Распаковываем во временный каталог
            extract_dir = os.path.join(tmp, "extract")
            os.makedirs(extract_dir)
            with tarfile.open(tar_path, "r:gz") as tar:
                tar.extractall(extract_dir)
            # GitHub tarball корневой каталог = "<owner>-<repo>-<sha>/"
            roots = [d for d in os.listdir(extract_dir)
                     if os.path.isdir(os.path.join(extract_dir, d))]
            if not roots:
                return False, "пустой tarball"
            src_root = os.path.join(extract_dir, roots[0])
            # Бекап текущей папки приложения
            shutil.copytree(str(APP), str(BACKUP), dirs_exist_ok=True)
            # Копируем новые файлы поверх (только .py, .html, .js, .json, .css, .md)
            ext_ok = (".py", ".html", ".js", ".json", ".css", ".md", ".service",
                      ".min.js")
            copied = 0
            for fname in os.listdir(src_root):
                src_f = os.path.join(src_root, fname)
                if not os.path.isfile(src_f):
                    continue
                if not fname.endswith(ext_ok):
                    continue
                shutil.copy2(src_f, str(APP / fname))
                copied += 1
            return True, f"скопировано {copied} файлов, бекап в {BACKUP}"
    except Exception as e:
        return False, str(e)[:200]


@app.route("/api/update/apply", methods=["POST"])
@requires_admin
@audit_action("update_applied", target_from_path=True)
def api_update_apply():
    """Ручной запуск обновления. Скачивает tarball, заменяет файлы, рестарт."""
    with _UPDATE_STATE["lock"]:
        if _UPDATE_STATE["applying"]:
            return jsonify({"ok": False, "error": "уже идёт обновление"}), 409
        _UPDATE_STATE["applying"] = True
    try:
        info = _UPDATE_STATE.get("last_check_result")
        if not info or not info.get("ok") or not info.get("has_update"):
            info = _check_github_release()
            if not info.get("ok"):
                return jsonify({"ok": False, "error": info.get("error")}), 502
            if not info.get("has_update"):
                return jsonify({"ok": False, "error": "обновлений нет"}), 200
        ok, msg = _apply_update(info["tarball_url"], info["latest_version"])
        _UPDATE_STATE["last_apply"] = {
            "ts": int(time.time()),
            "from_version": VERSION,
            "to_version": info["latest_version"],
            "ok": ok,
            "message": msg,
        }
        if not ok:
            return jsonify({"ok": False, "error": msg}), 500
        # Рестарт сервиса (асинхронно, через sleep чтобы успеть отдать response)
        subprocess.Popen("sleep 2 && sudo systemctl restart smartcomm-dashboard",
                         shell=True)
        return jsonify({
            "ok": True,
            "message": f"обновлено до {info['latest_version']} — рестарт через 2 сек",
            "from": VERSION,
            "to": info["latest_version"],
            "details": msg,
        })
    finally:
        with _UPDATE_STATE["lock"]:
            _UPDATE_STATE["applying"] = False


def _update_check_loop():
    """Раз в час чекаем GitHub. Auto-apply пока выключен — только notify."""
    time.sleep(180)   # 3 минуты после старта
    while True:
        try:
            result = _check_github_release()
            _UPDATE_STATE["last_check"] = int(time.time())
            _UPDATE_STATE["last_check_result"] = result
            if result.get("ok") and result.get("has_update"):
                print(f"[update] доступна новая версия "
                      f"{result['latest_version']} (текущая {VERSION})")
        except Exception:
            pass
        time.sleep(3600)


# ============ AUTO-BACKUP + DB INTEGRITY ============

BACKUP_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/smartcomm-dashboard")) / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
INVENTORY_DB = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/smartcomm-dashboard")) / "inventory.db"
BACKUP_RETENTION_DAYS = 30


def db_integrity_check():
    """Quick integrity check at startup. If fails, log error but keep running."""
    if not INVENTORY_DB.exists():
        return True
    try:
        import sqlite3
        conn = sqlite3.connect(str(INVENTORY_DB))
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        if ok != "ok":
            app.logger.error(f"inventory.db integrity: {ok}")
            return False
    except Exception as e:
        app.logger.error(f"integrity check failed: {e}")
        return False
    return True


def backup_inventory():
    """Daily backup of inventory.db with retention."""
    if not INVENTORY_DB.exists():
        return
    now = time.strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"inventory_{now}.db"
    try:
        # Use SQLite's online backup API for consistency without lock contention
        import sqlite3
        src = sqlite3.connect(str(INVENTORY_DB))
        dst_conn = sqlite3.connect(str(dst))
        with dst_conn:
            src.backup(dst_conn)
        dst_conn.close()
        src.close()
    except Exception as e:
        app.logger.error(f"backup failed: {e}")
        return
    # cleanup old backups
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    for f in BACKUP_DIR.glob("inventory_*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def _backup_loop():
    time.sleep(300)   # wait 5 min after boot
    while True:
        try:
            backup_inventory()
        except Exception:
            pass
        time.sleep(24 * 3600)


db_integrity_check()
threading.Thread(target=_backup_loop, daemon=True).start()

# AUTH: при первом старте создаём admin/admin (must_change_password=1)
network_bp.auth_bootstrap()

def _auth_cleanup_loop():
    """Раз в сутки чистим expired sessions и старый audit-лог."""
    time.sleep(120)
    while True:
        try:
            network_bp.auth_cleanup_expired()
        except Exception:
            pass
        time.sleep(86400)

threading.Thread(target=_auth_cleanup_loop, daemon=True).start()
# Updater — раз в час чекает GitHub Releases, log про доступные обновления
threading.Thread(target=_update_check_loop, daemon=True).start()

# Services: discover existing installations (compensate v1.6.0 БД bug) + start sampler
# + auto-updater (v2.2.0)
try:
    found = services_mod.discover_existing()
    if found:
        print(f"[services] discovered {found} previously-installed services")
    services_mod.ensure_sampler_started()
    services_mod.ensure_auto_updater_started()
except Exception as e:
    print(f"[services] init warn: {e}")


if __name__ == "__main__":
    # Production WSGI: waitress — multi-threaded, без Flask dev-server bottleneck'а
    # и без warning'а «do not use in production». Fallback на Flask dev если waitress нет.
    try:
        from waitress import serve
        print(f"[server] waitress на :8080 (8 потоков)")
        serve(app, host="0.0.0.0", port=8080, threads=8, ident="smartcomm-dashboard")
    except ImportError:
        print(f"[server] waitress не найден — fallback на Flask dev-server")
        app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
