"""Network inventory: SQLite-backed device map + nmap discovery."""
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import Blueprint, jsonify, request, send_from_directory

bp = Blueprint("network", __name__)
BASE = Path(__file__).resolve().parent

# DB lives in pi-writable state dir (systemd StateDirectory).
DB_PATH = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/smartcomm-dashboard")) / "inventory.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============ DB ============

def db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


DEFAULT_TYPES = [
    ("controller", "Контроллер"),
    ("camera",     "Камера"),
    ("network",    "Сетевое"),
    ("panel",      "Панель"),
    ("iot",        "IoT"),
    ("printer",    "Принтер"),
    ("sensor",     "Сенсор"),
    ("other",      "Без типа"),
]


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mac         TEXT UNIQUE,
            ip          TEXT,
            hostname    TEXT,
            vendor      TEXT,
            device_type TEXT DEFAULT 'other',
            name        TEXT,
            room        TEXT,
            description TEXT,
            login       TEXT,
            password    TEXT,
            url         TEXT,
            tags        TEXT DEFAULT '[]',
            notes       TEXT,
            open_ports  TEXT DEFAULT '[]',
            manual      INTEGER DEFAULT 0,
            first_seen  INTEGER,
            last_seen   INTEGER,
            created_at  INTEGER,
            updated_at  INTEGER
        );
        CREATE TABLE IF NOT EXISTS scans (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    INTEGER,
            finished_at   INTEGER,
            devices_found INTEGER,
            subnet        TEXT,
            status        TEXT,
            error         TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS device_types (
            key        TEXT PRIMARY KEY,
            label      TEXT NOT NULL,
            is_builtin INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 100,
            created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS device_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            ts         INTEGER NOT NULL,
            details    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_device_ts ON device_events(device_id, ts);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON device_events(ts);
        CREATE TABLE IF NOT EXISTS device_credentials (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  INTEGER NOT NULL,
            label      TEXT,
            username   TEXT,
            password   TEXT,
            notes      TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_creds_device ON device_credentials(device_id);
        CREATE TABLE IF NOT EXISTS device_audit (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  INTEGER,
            ts         INTEGER NOT NULL,
            actor      TEXT,
            action     TEXT,      -- 'update' | 'create' | 'delete' | 'bulk_update' | 'photo' | 'fingerprint'
            details    TEXT       -- JSON of changed fields
        );
        CREATE INDEX IF NOT EXISTS idx_audit_device_ts ON device_audit(device_id, ts);
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON device_audit(ts);
        """)
        # migrate: add scan_type column to scans if missing
        cols = [r["name"] for r in c.execute("PRAGMA table_info(scans)")]
        if "scan_type" not in cols:
            c.execute("ALTER TABLE scans ADD COLUMN scan_type TEXT DEFAULT 'manual'")
        # migrate: add is_online + snmp_community + monitor_on_dashboard to devices
        dcols = [r["name"] for r in c.execute("PRAGMA table_info(devices)")]
        if "is_online" not in dcols:
            c.execute("ALTER TABLE devices ADD COLUMN is_online INTEGER DEFAULT 0")
        if "snmp_community" not in dcols:
            c.execute("ALTER TABLE devices ADD COLUMN snmp_community TEXT")
        if "monitor_on_dashboard" not in dcols:
            c.execute("ALTER TABLE devices ADD COLUMN monitor_on_dashboard INTEGER DEFAULT 0")
        if "dhcp_static" not in dcols:
            c.execute("ALTER TABLE devices ADD COLUMN dhcp_static INTEGER")
        # Несколько фото на одно устройство (раньше было одно — `<id>.<ext>` на диске)
        c.execute("""
            CREATE TABLE IF NOT EXISTS device_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at INTEGER NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_photos_did ON device_photos(device_id)")
        # Миграция legacy: если в /photos есть <id>.<ext> и нет записи в device_photos — заносим
        photos_dir_path = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/smartcomm-dashboard")) / "photos"
        if photos_dir_path.exists():
            existing_files = {r["filename"] for r in c.execute("SELECT filename FROM device_photos")}
            for legacy in photos_dir_path.iterdir():
                if not legacy.is_file() or "." not in legacy.name:
                    continue
                name = legacy.name
                if name in existing_files:
                    continue
                base = name.rsplit(".", 1)[0]
                # legacy формат: «<digit>.ext». Новый формат содержит «_» (например 42_1718.jpg)
                if "_" in base:
                    continue
                try:
                    did = int(base)
                except ValueError:
                    continue
                # device существует?
                if not c.execute("SELECT 1 FROM devices WHERE id=?", (did,)).fetchone():
                    continue
                c.execute(
                    "INSERT INTO device_photos(device_id, filename, uploaded_at) VALUES (?, ?, ?)",
                    (did, name, int(legacy.stat().st_mtime))
                )
        # ===== AUTH: пользователи, сессии, аудит-лог =====
        c.execute("""
            CREATE TABLE IF NOT EXISTS auth_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                must_change_password INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_login INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS auth_sessions (
                sid TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                ip TEXT,
                user_agent TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS auth_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                username TEXT,
                ip TEXT,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                result TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON auth_audit(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_seen ON auth_sessions(last_seen)")
        # migrate: move legacy login/password into device_credentials as
        # "основной" credential (only for devices that don't already have one).
        legacy = c.execute(
            "SELECT id, login, password FROM devices "
            "WHERE (login IS NOT NULL AND login != '') "
            "   OR (password IS NOT NULL AND password != '')"
        ).fetchall()
        now_mig = int(time.time())
        for row in legacy:
            has = c.execute(
                "SELECT 1 FROM device_credentials WHERE device_id = ? LIMIT 1",
                (row["id"],)
            ).fetchone()
            if not has:
                c.execute("""INSERT INTO device_credentials
                             (device_id, label, username, password, created_at, updated_at)
                             VALUES (?, 'основной', ?, ?, ?, ?)""",
                          (row["id"], row["login"], row["password"], now_mig, now_mig))
        # seed default device types
        now = int(time.time())
        for i, (key, label) in enumerate(DEFAULT_TYPES):
            c.execute("""INSERT OR IGNORE INTO device_types
                         (key, label, is_builtin, sort_order, created_at)
                         VALUES (?, ?, 1, ?, ?)""",
                      (key, label, i, now))
        # seed default settings
        c.execute("INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                  ("autoscan_hours", "4", now))


init_db()


def setting_get(key, default=None):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def setting_set(key, value):
    with db() as c:
        c.execute("""INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                     ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                     updated_at = excluded.updated_at""",
                  (key, value, int(time.time())))


def audit_log(device_id, action, details, actor="pi"):
    """Record an audit entry. details should be JSON-serialisable."""
    try:
        with db() as c:
            c.execute("""INSERT INTO device_audit
                         (device_id, ts, actor, action, details)
                         VALUES (?, ?, ?, ?, ?)""",
                      (device_id, int(time.time()), actor, action,
                       json.dumps(details) if details else None))
    except Exception:
        pass


# Audit retention — keep 90 days
AUDIT_RETENTION_DAYS = 90


# ============ HELPERS ============

def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


def detect_subnet():
    """Return CIDR of the main interface, e.g. '192.168.95.0/24'."""
    out = sh("ip -4 -o addr show eth0 2>/dev/null || ip -4 -o addr show 2>/dev/null")
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", out)
    if not m:
        return None
    try:
        net = ipaddress.ip_interface(m.group(1)).network
        return str(net)
    except ValueError:
        return None


def detect_gateway():
    """Default-route gateway IP (например '192.168.95.1'). Читаем из /proc/net/route.
    Fallback: первый хост детектнутой подсети (обычно .1).
    Возвращает None если совсем ничего не нашли."""
    try:
        with open("/proc/net/route") as f:
            next(f)   # пропускаем header
            for line in f:
                parts = line.strip().split()
                # колонки: Iface Destination Gateway Flags RefCnt Use Metric Mask ...
                if len(parts) < 3:
                    continue
                # default route: Destination == 00000000
                if parts[1] != "00000000":
                    continue
                gw_hex = parts[2]
                if gw_hex == "00000000":
                    continue   # 0.0.0.0 не gateway
                # Hex в little-endian: '0157A8C0' = 192.168.87.1
                gw = ".".join(str(int(gw_hex[i:i+2], 16)) for i in (6, 4, 2, 0))
                return gw
    except (FileNotFoundError, PermissionError, OSError):
        pass
    # Fallback: .1 подсети (пользовательское правило «шлюз всегда .1»)
    subnet = detect_subnet()
    if subnet:
        try:
            net = ipaddress.ip_network(subnet, strict=False)
            return str(next(net.hosts()))
        except (ValueError, StopIteration):
            pass
    return None


def row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    for k in ("tags", "open_ports"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                d[k] = []
        else:
            d[k] = []
    return d


# ============ AUTH: пароли, сессии, аудит ============

SESSION_TTL_SEC = 30 * 86400   # 30 дней
AUDIT_RETENTION_AUTH_DAYS = 90

def auth_hash_password(password, salt=None):
    """PBKDF2-HMAC-SHA256 со 100k итераций — без внешних зависимостей."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            salt.encode("ascii"), 100_000)
    return salt, h.hex()


def auth_verify_password(password, salt, expected_hash):
    if not password or not salt or not expected_hash:
        return False
    _, h = auth_hash_password(password, salt)
    return secrets.compare_digest(h, expected_hash)


def auth_get_user(username):
    if not username:
        return None
    with db() as c:
        row = c.execute(
            "SELECT id, username, password_hash, salt, is_admin, "
            "must_change_password, created_at, last_login "
            "FROM auth_users WHERE username = ?",
            (username,)
        ).fetchone()
    return dict(row) if row else None


def auth_list_users():
    with db() as c:
        rows = c.execute(
            "SELECT id, username, is_admin, must_change_password, "
            "created_at, last_login FROM auth_users ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def auth_create_user(username, password, is_admin=False, must_change=False):
    salt, h = auth_hash_password(password)
    with db() as c:
        c.execute(
            "INSERT INTO auth_users (username, password_hash, salt, is_admin, "
            "must_change_password, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (username, h, salt, 1 if is_admin else 0,
             1 if must_change else 0, int(time.time()))
        )


def auth_set_password(username, new_password, clear_must_change=True):
    salt, h = auth_hash_password(new_password)
    with db() as c:
        c.execute(
            "UPDATE auth_users SET password_hash = ?, salt = ?, "
            "must_change_password = ? WHERE username = ?",
            (h, salt, 0 if clear_must_change else 1, username)
        )


def auth_delete_user(uid):
    with db() as c:
        # Не дать удалить последнего админа — иначе тюрьма
        admins = c.execute(
            "SELECT COUNT(*) FROM auth_users WHERE is_admin = 1"
        ).fetchone()[0]
        u = c.execute(
            "SELECT username, is_admin FROM auth_users WHERE id = ?", (uid,)
        ).fetchone()
        if not u:
            return False, "user not found"
        if u["is_admin"] and admins <= 1:
            return False, "нельзя удалить единственного админа"
        c.execute("DELETE FROM auth_users WHERE id = ?", (uid,))
        # И все его активные сессии
        c.execute("DELETE FROM auth_sessions WHERE username = ?", (u["username"],))
    return True, None


def auth_create_session(username, ip, user_agent):
    sid = secrets.token_hex(32)
    now = int(time.time())
    with db() as c:
        c.execute(
            "INSERT INTO auth_sessions (sid, username, created_at, last_seen, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, username, now, now, ip, (user_agent or "")[:200])
        )
    return sid


def auth_get_session(sid):
    """Returns dict or None. Touches last_seen."""
    if not sid or len(sid) != 64:
        return None
    now = int(time.time())
    with db() as c:
        row = c.execute(
            "SELECT sid, username, created_at, last_seen, ip, user_agent "
            "FROM auth_sessions WHERE sid = ?", (sid,)
        ).fetchone()
        if not row:
            return None
        if (now - row["created_at"]) > SESSION_TTL_SEC:
            c.execute("DELETE FROM auth_sessions WHERE sid = ?", (sid,))
            return None
        c.execute(
            "UPDATE auth_sessions SET last_seen = ? WHERE sid = ?",
            (now, sid)
        )
    return dict(row)


def auth_delete_session(sid):
    if not sid:
        return
    with db() as c:
        c.execute("DELETE FROM auth_sessions WHERE sid = ?", (sid,))


def auth_log(username, ip, action, target=None, details=None, result="success"):
    """Записать событие в аудит-лог."""
    try:
        det_json = json.dumps(details, ensure_ascii=False) if details else None
        with db() as c:
            c.execute(
                "INSERT INTO auth_audit (ts, username, ip, action, target, details, result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), username, ip, action, target, det_json, result)
            )
    except Exception:
        pass


def auth_get_audit(limit=200, offset=0, since_ts=None, username=None):
    sql = "SELECT id, ts, username, ip, action, target, details, result FROM auth_audit WHERE 1=1"
    params = []
    if since_ts is not None:
        sql += " AND ts >= ?"
        params.append(since_ts)
    if username:
        sql += " AND username = ?"
        params.append(username)
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db() as c:
        rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("details"):
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


def auth_bootstrap():
    """Создать дефолтного admin/admin если в БД нет ни одного пользователя.
    Помечает must_change_password=1 — UI заставит сменить."""
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
    if n == 0:
        auth_create_user("admin", "admin", is_admin=True, must_change=True)
        print("[auth] bootstrap: admin/admin создан (must_change_password=1)")


def auth_cleanup_expired():
    """Удалить старые сессии + старый аудит-лог."""
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM auth_sessions WHERE created_at < ?",
                  (now - SESSION_TTL_SEC,))
        c.execute("DELETE FROM auth_audit WHERE ts < ?",
                  (now - AUDIT_RETENTION_AUTH_DAYS * 86400,))


# ============ AUTO-CLASSIFICATION ============

# Order matters — first match wins. Network listed before "iot" so
# TP-Link / Asustek go to network, not smart-plug bucket.
VENDOR_TYPE_PATTERNS = [
    (r"hikvision|dahua|reolink|axis communication|imou|hanwha|hangzhou", "camera"),
    (r"routerboard|mikrotik|tp[\-_ ]?link|ubiquiti|cisco|asustek|netgear|d[\-_ ]?link|aruba|zyxel|juniper|extreme networks", "network"),
    (r"raspberry pi|espressif", "controller"),
    (r"sonos|denon|marantz|d&m holdings|yamaha|onkyo|dune hd|kef|bluesound|wiim|imaqliq", "iot"),
    (r"^apple$|apple inc|samsung electronics|microsoft", "panel"),
    (r"ricoh|hewlett[\-_ ]?packard|brother industries|epson|canon|kyocera|xerox", "printer"),
    (r"yandex|xiaomi|imilab|aqara|sonoff|tuya|broadlink|shenzhen sunchip|ampak|gigabyte|giga[\-_ ]?byte|ge appliances", "iot"),
    (r"icpdas|microchip|zao npk rotek|istor", "iot"),
]


def classify_vendor(vendor):
    if not vendor:
        return None
    v = vendor.lower()
    for pattern, t in VENDOR_TYPE_PATTERNS:
        if re.search(pattern, v):
            return t
    return None


def backfill_classification():
    """One-time reclassification for existing devices with no type yet."""
    now = int(time.time())
    with db() as c:
        rows = c.execute(
            "SELECT id, vendor FROM devices WHERE device_type IS NULL OR device_type = 'other'"
        ).fetchall()
        count = 0
        for r in rows:
            t = classify_vendor(r["vendor"])
            if t:
                c.execute(
                    "UPDATE devices SET device_type = ?, updated_at = ? WHERE id = ?",
                    (t, now, r["id"])
                )
                count += 1
    return count


# Run backfill at module load (after init_db)
backfill_classification()


# ============ NMAP ============

_SCAN_LOCK = threading.Lock()
_SCAN_STATE = {"running": False, "scan_id": None, "started_at": 0,
               "last_finished": 0, "scan_type": None}

# ============ FINGERPRINT QUEUE ============
from collections import deque
FP_QUEUE = deque()
FP_LOCK = threading.Lock()
FP_STATE = {
    "running":     False,
    "current_id":  None,
    "current_ip":  None,
    "started_at":  0,
    "done":        0,
    "total":       0,
    "last_error":  None,
}


def fp_enqueue(device_ids):
    """Add devices to the fingerprint queue (skips already-queued)."""
    with FP_LOCK:
        existing = set(FP_QUEUE)
        added = 0
        for did in device_ids:
            if did not in existing:
                FP_QUEUE.append(did)
                added += 1
        if added > 0:
            FP_STATE["total"] = FP_STATE["done"] + len(FP_QUEUE)
        return added


def _fp_one(did):
    """Fingerprint a single device by id. Returns (ports, url) or raises."""
    with db() as c:
        dev = c.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
    if not dev or not dev["ip"]:
        return None, None
    ports = nmap_service_scan(dev["ip"])
    url = guess_url(dev["ip"], ports)
    with db() as c:
        if url and not dev["url"]:
            c.execute("UPDATE devices SET open_ports = ?, url = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(ports), url, int(time.time()), did))
        else:
            c.execute("UPDATE devices SET open_ports = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(ports), int(time.time()), did))
    return ports, url


def _fp_worker():
    while True:
        with FP_LOCK:
            FP_STATE["heartbeat"] = int(time.time())
        did = None
        with FP_LOCK:
            if FP_QUEUE:
                did = FP_QUEUE.popleft()
                FP_STATE["running"] = True
                FP_STATE["current_id"] = did
                FP_STATE["started_at"] = int(time.time())
        if did is None:
            with FP_LOCK:
                if FP_STATE["running"]:
                    FP_STATE["running"] = False
                    FP_STATE["current_id"] = None
                    FP_STATE["current_ip"] = None
            time.sleep(3)
            continue
        try:
            with db() as c:
                ip_row = c.execute("SELECT ip FROM devices WHERE id = ?", (did,)).fetchone()
            with FP_LOCK:
                FP_STATE["current_ip"] = ip_row["ip"] if ip_row else None
            _fp_one(did)
        except Exception as e:
            with FP_LOCK:
                FP_STATE["last_error"] = str(e)[:120]
        finally:
            with FP_LOCK:
                FP_STATE["done"] += 1
        # small breather between probes — avoid hammering the network
        time.sleep(1)


threading.Thread(target=_fp_worker, daemon=True).start()


def nmap_scan(subnet):
    """Return list of {ip, mac, vendor, hostname} for live hosts in subnet."""
    out = subprocess.run(
        ["sudo", "nmap", "-sn", "-PR", "-oX", "-", subnet],
        capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(f"nmap exit {out.returncode}: {out.stderr[:200]}")
    hosts = []
    try:
        root = ET.fromstring(out.stdout)
    except ET.ParseError as e:
        raise RuntimeError(f"nmap XML parse error: {e}")
    for h in root.findall("host"):
        status = h.find("status")
        if status is None or status.get("state") != "up":
            continue
        ip = mac = vendor = hostname = None
        for a in h.findall("address"):
            t = a.get("addrtype")
            if t == "ipv4":
                ip = a.get("addr")
            elif t == "mac":
                mac = a.get("addr", "").lower()
                vendor = a.get("vendor") or vendor
        hn = h.find("hostnames/hostname")
        if hn is not None:
            hostname = hn.get("name")
        if ip:
            hosts.append({"ip": ip, "mac": mac, "vendor": vendor, "hostname": hostname})
    return hosts


def merge_scan(hosts):
    """Upsert nmap results into DB. Returns (new_count, updated_count, new_ids).
    Auto-classifies new devices and devices still on default 'other'."""
    now = int(time.time())
    new = updated = 0
    new_ids = []
    with db() as c:
        for h in hosts:
            auto_type = classify_vendor(h.get("vendor"))
            existing = None
            if h["mac"]:
                existing = c.execute(
                    "SELECT * FROM devices WHERE mac = ?", (h["mac"],)
                ).fetchone()
            if not existing and h["ip"]:
                existing = c.execute(
                    "SELECT * FROM devices WHERE ip = ? AND manual = 0 AND mac IS NULL",
                    (h["ip"],)
                ).fetchone()
            if existing:
                # Only upgrade device_type if user hasn't overridden ('other' is default).
                upgrade_type = (
                    auto_type
                    and (existing["device_type"] is None or existing["device_type"] == "other")
                )
                if upgrade_type:
                    c.execute("""
                        UPDATE devices SET ip = ?, hostname = COALESCE(?, hostname),
                            vendor = COALESCE(?, vendor), mac = COALESCE(?, mac),
                            device_type = ?, last_seen = ?, updated_at = ?
                        WHERE id = ?
                    """, (h["ip"], h["hostname"], h["vendor"], h["mac"],
                          auto_type, now, now, existing["id"]))
                else:
                    c.execute("""
                        UPDATE devices SET ip = ?, hostname = COALESCE(?, hostname),
                            vendor = COALESCE(?, vendor), mac = COALESCE(?, mac),
                            last_seen = ?, updated_at = ?
                        WHERE id = ?
                    """, (h["ip"], h["hostname"], h["vendor"], h["mac"],
                          now, now, existing["id"]))
                updated += 1
            else:
                cur = c.execute("""
                    INSERT INTO devices (mac, ip, hostname, vendor, device_type,
                        first_seen, last_seen, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (h["mac"], h["ip"], h["hostname"], h["vendor"],
                      auto_type or "other", now, now, now, now))
                new += 1
                new_ids.append(cur.lastrowid)
    return new, updated, new_ids


# ============ SERVICE FINGERPRINTING ============

def nmap_service_scan(ip):
    """Probe common service ports on a single host. Returns list of
    {port, proto, name, product, version}."""
    out = subprocess.run(
        ["sudo", "nmap", "-sV", "-T4", "--version-intensity", "3",
         "-p", "22,80,443,554,1883,5000,8000,8080,8443,8888,9000,9100,1900",
         "-oX", "-", ip],
        capture_output=True, text=True, timeout=90,
    )
    if out.returncode != 0:
        raise RuntimeError(f"nmap exit {out.returncode}: {out.stderr[:200]}")
    ports = []
    try:
        root = ET.fromstring(out.stdout)
    except ET.ParseError as e:
        raise RuntimeError(f"nmap XML parse error: {e}")
    for p in root.findall(".//port"):
        st = p.find("state")
        if st is None or st.get("state") != "open":
            continue
        svc = p.find("service")
        ports.append({
            "port":    int(p.get("portid")),
            "proto":   p.get("protocol"),
            "name":    svc.get("name") if svc is not None else None,
            "product": svc.get("product") if svc is not None else None,
            "version": svc.get("version") if svc is not None else None,
        })
    return ports


def guess_url(ip, ports):
    """Pick the most sensible web-UI URL from open ports."""
    # priority: 443/8443 (https) > 80 (http) > 8080/8000 (alt http) > 8888
    for port in ports:
        if port["port"] == 443 and port["name"] in ("https", "ssl/http", "ssl"):
            return f"https://{ip}/"
        if port["port"] == 8443:
            return f"https://{ip}:8443/"
    for port in ports:
        if port["port"] == 80 and port["name"] in ("http", "http-proxy"):
            return f"http://{ip}/"
    for port in ports:
        if port["port"] in (8080, 8000) and port["name"] and "http" in port["name"]:
            return f"http://{ip}:{port['port']}/"
    for port in ports:
        if port["port"] == 8888:
            return f"http://{ip}:8888/"
    return None


def _scan_worker(subnet, scan_type="manual"):
    error = None
    found = 0
    new_ids = []
    with db() as c:
        cur = c.execute(
            "INSERT INTO scans (started_at, subnet, status, scan_type) VALUES (?, ?, 'running', ?)",
            (int(time.time()), subnet, scan_type)
        )
        scan_id = cur.lastrowid
    with _SCAN_LOCK:
        _SCAN_STATE["running"] = True
        _SCAN_STATE["scan_id"] = scan_id
        _SCAN_STATE["started_at"] = int(time.time())
        _SCAN_STATE["scan_type"] = scan_type
    try:
        hosts = nmap_scan(subnet)
        _, _, new_ids = merge_scan(hosts)
        found = len(hosts)
        # mDNS enrichment — supplements nmap with friendly names from
        # Sonos/HomeKit/Apple/printers etc.
        try:
            merge_mdns(mdns_scan())
        except Exception:
            pass
    except Exception as e:
        error = str(e)
    finally:
        now = int(time.time())
        with db() as c:
            c.execute("""
                UPDATE scans SET finished_at = ?, devices_found = ?,
                                  status = ?, error = ?
                WHERE id = ?
            """, (now, found, "error" if error else "done", error, scan_id))
        with _SCAN_LOCK:
            _SCAN_STATE["running"] = False
            _SCAN_STATE["last_finished"] = now
        # auto-fingerprint newly discovered devices in background
        if new_ids:
            fp_enqueue(new_ids)


# ============ ROUTES ============

@bp.route("/network")
def page():
    return send_from_directory(BASE, "network.html")


@bp.route("/api/network/devices", methods=["GET"])
def list_devices():
    with db() as c:
        rows = c.execute("""
            SELECT * FROM devices ORDER BY
                CASE WHEN ip IS NULL THEN 1 ELSE 0 END,
                CAST(SUBSTR(ip, 1, INSTR(ip, '.')-1) AS INTEGER),
                ip
        """).fetchall()
        # Тянем все фото одним запросом — группируем по device_id
        photo_rows = c.execute(
            "SELECT id, device_id, uploaded_at FROM device_photos ORDER BY uploaded_at ASC"
        ).fetchall()
    photos_by_dev = {}
    for pr in photo_rows:
        photos_by_dev.setdefault(pr["device_id"], []).append({
            "id": pr["id"], "uploaded_at": pr["uploaded_at"]
        })
    timelines = get_all_timelines()
    devices = []
    for r in rows:
        d = row_to_dict(r)
        d["timeline_24h"] = timelines.get(d["id"], [])
        d["photos"] = photos_by_dev.get(d["id"], [])
        devices.append(d)
    return jsonify({
        "devices": devices,
        "subnet": detect_subnet(),
        "gateway": detect_gateway(),
        "scan": dict(_SCAN_STATE),
    })


@bp.route("/api/network/devices", methods=["POST"])
def create_device():
    d = request.get_json(force=True) or {}
    now = int(time.time())
    fields = ("mac", "ip", "hostname", "vendor", "device_type", "name",
              "room", "description", "login", "password", "url", "notes")
    vals = {f: d.get(f) for f in fields}
    if d.get("mac"):
        vals["mac"] = d["mac"].lower()
    tags = json.dumps(d.get("tags") or [])
    open_ports = json.dumps(d.get("open_ports") or [])
    try:
        with db() as c:
            cur = c.execute("""
                INSERT INTO devices (mac, ip, hostname, vendor, device_type, name,
                                     room, description, login, password, url, notes,
                                     tags, open_ports, manual,
                                     first_seen, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
            """, (vals["mac"], vals["ip"], vals["hostname"], vals["vendor"],
                  vals["device_type"] or "other", vals["name"], vals["room"],
                  vals["description"], vals["login"], vals["password"],
                  vals["url"], vals["notes"], tags, open_ports,
                  now, now, now))
        return jsonify({"ok": True, "id": cur.lastrowid})
    except sqlite3.IntegrityError as e:
        return jsonify({"ok": False, "error": str(e)}), 409


@bp.route("/api/network/devices/<int:did>", methods=["PUT"])
def update_device(did):
    d = request.get_json(force=True) or {}
    editable = ("name", "room", "description", "login", "password",
                "url", "device_type", "notes", "ip", "hostname",
                "vendor", "mac", "monitor_on_dashboard")
    sets, vals = [], []
    audited = {}
    for f in editable:
        if f in d:
            sets.append(f"{f} = ?")
            v = d[f]
            if f == "mac" and v:
                v = v.lower()
            vals.append(v)
            if f != "password":
                audited[f] = v
            else:
                audited[f] = "***"
    if "tags" in d:
        sets.append("tags = ?")
        vals.append(json.dumps(d["tags"] or []))
        audited["tags"] = d["tags"]
    if not sets:
        return jsonify({"ok": False, "error": "no fields to update"}), 400
    sets.append("updated_at = ?")
    vals.append(int(time.time()))
    vals.append(did)
    with db() as c:
        c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", vals)
    audit_log(did, "update", audited)
    return jsonify({"ok": True})


@bp.route("/api/network/devices/<int:did>", methods=["DELETE"])
def delete_device(did):
    with db() as c:
        c.execute("DELETE FROM devices WHERE id = ?", (did,))
    audit_log(did, "delete", None)
    return jsonify({"ok": True})


@bp.route("/api/network/devices/bulk", methods=["PUT"])
def bulk_update():
    d = request.get_json(force=True) or {}
    ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]
    fields = d.get("fields") or {}
    if not ids or not fields:
        return jsonify({"ok": False, "error": "ids and fields required"}), 400
    editable = {"room", "device_type", "monitor_on_dashboard"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in editable:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return jsonify({"ok": False, "error": "no editable fields"}), 400
    sets.append("updated_at = ?")
    vals.append(int(time.time()))
    placeholders = ",".join("?" * len(ids))
    with db() as c:
        c.execute(
            f"UPDATE devices SET {', '.join(sets)} WHERE id IN ({placeholders})",
            vals + ids
        )
    audit_log(None, "bulk_update",
              {"ids": ids, "fields": fields, "count": len(ids)})
    return jsonify({"ok": True, "count": len(ids)})


@bp.route("/api/network/devices/<int:did>/events", methods=["GET"])
def device_events(did):
    """List events + uptime summary for a device over the last N days."""
    days = int(request.args.get("days", "30"))
    days = max(1, min(days, RETENTION_EVENT_DAYS))
    since = int(time.time()) - days * 86400
    with db() as c:
        dev = c.execute(
            "SELECT id, is_online, last_seen FROM devices WHERE id = ?", (did,)
        ).fetchone()
        if not dev:
            return jsonify({"ok": False, "error": "not found"}), 404
        rows = c.execute("""
            SELECT event_type, ts FROM device_events
             WHERE device_id = ? AND ts >= ?
             ORDER BY ts DESC
        """, (did, since)).fetchall()
    events = [dict(r) for r in rows]

    # Compute uptime % over the window: sum of (online intervals).
    # Walk events chronologically; assume current is_online at window end.
    now = int(time.time())
    chrono = list(reversed(events))   # oldest → newest
    total_window = now - since
    online_seconds = 0
    # Determine initial state at window start: opposite of first event in window,
    # OR current state if no events in window.
    if not chrono:
        state = bool(dev["is_online"])
        cursor = since
    else:
        # state right before first event was opposite of that event
        state = (chrono[0]["event_type"] == "offline")
        cursor = since
    for e in chrono:
        if state:
            online_seconds += max(0, e["ts"] - cursor)
        cursor = e["ts"]
        state = (e["event_type"] == "online")
    # tail until now
    if state:
        online_seconds += max(0, now - cursor)

    offline_events = sum(1 for e in events if e["event_type"] == "offline")
    return jsonify({
        "ok": True,
        "is_online": bool(dev["is_online"]),
        "last_seen": dev["last_seen"],
        "days": days,
        "total_events": len(events),
        "offline_events": offline_events,
        "uptime_pct": round(100 * online_seconds / total_window, 2) if total_window else None,
        "online_seconds": online_seconds,
        "window_seconds": total_window,
        "events": events[:200],     # limit response size
    })


@bp.route("/api/network/devices/<int:did>/fingerprint", methods=["POST"])
def fingerprint_device(did):
    with db() as c:
        dev = c.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
    if not dev:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not dev["ip"]:
        return jsonify({"ok": False, "error": "no IP"}), 400
    try:
        ports = nmap_service_scan(dev["ip"])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    url = guess_url(dev["ip"], ports)
    with db() as c:
        if url and not dev["url"]:
            c.execute("""UPDATE devices SET open_ports = ?, url = ?, updated_at = ?
                         WHERE id = ?""",
                      (json.dumps(ports), url, int(time.time()), did))
        else:
            c.execute("UPDATE devices SET open_ports = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(ports), int(time.time()), did))
    return jsonify({"ok": True, "ports": ports, "url": url})


@bp.route("/api/network/reclassify", methods=["POST"])
def reclassify():
    count = backfill_classification()
    return jsonify({"ok": True, "updated": count})


# ============ MONITORED DEVICES (for dashboard tiles) ============

@bp.route("/api/network/monitored", methods=["GET"])
def list_monitored():
    """Devices flagged for dashboard tiles, with 24h timeline + status-since."""
    with db() as c:
        rows = c.execute("""
            SELECT id, name, hostname, ip, room, device_type, is_online,
                   last_seen, vendor, url
              FROM devices
             WHERE monitor_on_dashboard = 1
             ORDER BY room, name, ip
        """).fetchall()
    timelines = get_all_timelines()
    result = []
    with db() as c:
        for r in rows:
            d = dict(r)
            d["timeline_24h"] = timelines.get(d["id"], [])
            target = "online" if d["is_online"] else "offline"
            chg = c.execute("""
                SELECT ts FROM device_events
                 WHERE device_id = ? AND event_type = ?
                 ORDER BY ts DESC LIMIT 1
            """, (d["id"], target)).fetchone()
            d["status_since"] = chg["ts"] if chg else d["last_seen"]
            result.append(d)
    return jsonify({"devices": result})


# ============ CREDENTIALS (multi-user per device) ============

@bp.route("/api/network/devices/<int:did>/credentials", methods=["GET"])
def list_credentials(did):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM device_credentials WHERE device_id = ? ORDER BY id",
            (did,)
        ).fetchall()
    return jsonify({"credentials": [dict(r) for r in rows]})


@bp.route("/api/network/devices/<int:did>/credentials", methods=["POST"])
def add_credential(did):
    d = request.get_json(force=True) or {}
    now = int(time.time())
    with db() as c:
        cur = c.execute("""
            INSERT INTO device_credentials (device_id, label, username, password, notes,
                                            created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (did, d.get("label"), d.get("username"), d.get("password"),
              d.get("notes"), now, now))
    return jsonify({"ok": True, "id": cur.lastrowid})


@bp.route("/api/network/credentials/<int:cid>", methods=["PUT"])
def update_credential(cid):
    d = request.get_json(force=True) or {}
    fields = ("label", "username", "password", "notes")
    sets, vals = [], []
    for f in fields:
        if f in d:
            sets.append(f"{f} = ?")
            vals.append(d[f])
    if not sets:
        return jsonify({"ok": False, "error": "no fields"}), 400
    sets.append("updated_at = ?")
    vals.append(int(time.time()))
    vals.append(cid)
    with db() as c:
        c.execute(f"UPDATE device_credentials SET {', '.join(sets)} WHERE id = ?", vals)
    return jsonify({"ok": True})


@bp.route("/api/network/credentials/<int:cid>", methods=["DELETE"])
def delete_credential(cid):
    with db() as c:
        c.execute("DELETE FROM device_credentials WHERE id = ?", (cid,))
    return jsonify({"ok": True})


# ============ PHOTOS (multiple per device) ============

PHOTOS_DIR = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/smartcomm-dashboard")) / "photos"
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


@bp.route("/api/network/devices/<int:did>/photos", methods=["GET"])
def list_device_photos(did):
    with db() as c:
        rows = c.execute(
            "SELECT id, filename, uploaded_at FROM device_photos "
            "WHERE device_id=? ORDER BY uploaded_at ASC", (did,)
        ).fetchall()
    return jsonify({"photos": [dict(r) for r in rows]})


@bp.route("/api/network/devices/<int:did>/photos", methods=["POST"])
def upload_device_photo(did):
    if "photo" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f = request.files["photo"]
    if f.filename == "":
        return jsonify({"ok": False, "error": "empty filename"}), 400
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > 2 * 1024 * 1024:
        return jsonify({"ok": False, "error": "max 2 MB"}), 400
    ext = (f.filename.rsplit(".", 1)[-1] or "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    now = int(time.time())
    # Уникальное имя: <did>_<timestamp>.<ext>; если коллизия (>1 фото в секунду)
    # добавляем суффикс.
    fname = f"{did}_{now}.{ext}"
    n = 1
    while (PHOTOS_DIR / fname).exists():
        fname = f"{did}_{now}_{n}.{ext}"
        n += 1
    dst = PHOTOS_DIR / fname
    f.save(str(dst))
    with db() as c:
        cur = c.execute(
            "INSERT INTO device_photos(device_id, filename, uploaded_at) VALUES (?, ?, ?)",
            (did, fname, now)
        )
        photo_id = cur.lastrowid
    audit_log(did, "photo", {"added": fname, "size": size})
    return jsonify({"ok": True, "id": photo_id, "filename": fname,
                    "size": size, "uploaded_at": now})


@bp.route("/api/network/photos/<int:pid>", methods=["GET"])
def get_photo_file(pid):
    from flask import send_file
    with db() as c:
        row = c.execute(
            "SELECT filename FROM device_photos WHERE id=?", (pid,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    p = PHOTOS_DIR / row["filename"]
    if not p.exists():
        return jsonify({"ok": False, "error": "file missing on disk"}), 404
    return send_file(str(p), max_age=86400)


@bp.route("/api/network/photos/<int:pid>", methods=["DELETE"])
def delete_photo_file(pid):
    with db() as c:
        row = c.execute(
            "SELECT device_id, filename FROM device_photos WHERE id=?", (pid,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not found"}), 404
        did = row["device_id"]
        fname = row["filename"]
        c.execute("DELETE FROM device_photos WHERE id=?", (pid,))
    p = PHOTOS_DIR / fname
    if p.exists():
        try: p.unlink()
        except Exception: pass
    audit_log(did, "photo", {"deleted": fname})
    return jsonify({"ok": True})


# Backwards-compat: одиночное /photo возвращает первую фотку устройства
@bp.route("/api/network/devices/<int:did>/photo", methods=["GET"])
def get_first_photo(did):
    from flask import send_file
    with db() as c:
        row = c.execute(
            "SELECT filename FROM device_photos WHERE device_id=? "
            "ORDER BY uploaded_at ASC LIMIT 1", (did,)
        ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "no photo"}), 404
    p = PHOTOS_DIR / row["filename"]
    if not p.exists():
        return jsonify({"ok": False, "error": "file missing"}), 404
    return send_file(str(p), max_age=300)


# ============ AUDIT LOG ============

@bp.route("/api/network/devices/<int:did>/audit", methods=["GET"])
def device_audit(did):
    limit = max(1, min(int(request.args.get("limit", "50")), 200))
    with db() as c:
        rows = c.execute("""
            SELECT id, ts, actor, action, details FROM device_audit
             WHERE device_id = ?
             ORDER BY ts DESC LIMIT ?
        """, (did, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["details"]:
            try:
                d["details"] = json.loads(d["details"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return jsonify({"audit": out})


# ============ TAGS — bulk add / remove + photo-aware list ============

@bp.route("/api/network/devices/bulk/tags", methods=["POST"])
def bulk_tags():
    """{op: 'add'|'remove'|'set', ids: [...], tags: [...]}"""
    d = request.get_json(force=True) or {}
    op = d.get("op", "add")
    ids = [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]
    new_tags = d.get("tags") or []
    if not ids or (op != "set" and not new_tags):
        return jsonify({"ok": False, "error": "ids + tags required"}), 400
    now = int(time.time())
    with db() as c:
        for did in ids:
            row = c.execute("SELECT tags FROM devices WHERE id = ?", (did,)).fetchone()
            if not row:
                continue
            try:
                current = set(json.loads(row["tags"] or "[]"))
            except (ValueError, TypeError):
                current = set()
            if op == "add":
                result = sorted(current | set(new_tags))
            elif op == "remove":
                result = sorted(current - set(new_tags))
            else:  # set
                result = sorted(set(new_tags))
            c.execute("UPDATE devices SET tags = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(result), now, did))
    audit_log(None, "bulk_tags", {"op": op, "ids": ids, "tags": new_tags})
    return jsonify({"ok": True, "count": len(ids)})


@bp.route("/api/network/tags", methods=["GET"])
def list_tags():
    """All unique tags across devices, sorted by usage frequency."""
    counts = {}
    with db() as c:
        rows = c.execute("SELECT tags FROM devices WHERE tags IS NOT NULL AND tags != '[]'").fetchall()
    for r in rows:
        try:
            for t in json.loads(r["tags"] or "[]"):
                if t:
                    counts[t] = counts.get(t, 0) + 1
        except (ValueError, TypeError):
            pass
    sorted_tags = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return jsonify({"tags": [{"name": n, "count": c} for n, c in sorted_tags]})


@bp.route("/api/network/devices/<int:did>/ping", methods=["POST"])
def ping_device(did):
    with db() as c:
        dev = c.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
    if not dev or not dev["ip"]:
        return jsonify({"ok": False, "error": "no IP"}), 400
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", dev["ip"]],
            capture_output=True, text=True, timeout=5,
        )
        alive = r.returncode == 0
        rtt = None
        if alive:
            m = re.search(r"time=([\d.]+)\s*ms", r.stdout)
            rtt = float(m.group(1)) if m else None
            now = int(time.time())
            with db() as c:
                was_offline = not dev["is_online"]
                c.execute(
                    "UPDATE devices SET is_online = 1, last_seen = ? WHERE id = ?",
                    (now, did)
                )
                if was_offline:
                    c.execute(
                        "INSERT INTO device_events (device_id, event_type, ts) VALUES (?, 'online', ?)",
                        (did, now)
                    )
        return jsonify({"ok": True, "alive": alive, "rtt_ms": rtt})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/network/devices/<int:did>/snmp/probe", methods=["POST"])
def snmp_probe_device(did):
    d = request.get_json(force=True) or {}
    with db() as c:
        dev = c.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
    if not dev or not dev["ip"]:
        return jsonify({"ok": False, "error": "no IP"}), 400
    community = d.get("community") or dev["snmp_community"] or "public"
    save = d.get("save", True)
    try:
        info = snmp_probe(dev["ip"], community)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not info or (not info.get("sysName") and not info.get("sysDescr")):
        return jsonify({"ok": False,
                        "error": "нет ответа SNMP — проверь community и что SNMP включён на устройстве"}), 200
    if save:
        with db() as c:
            sets = ["updated_at = ?", "snmp_community = ?"]
            vals = [int(time.time()), community]
            if not dev["hostname"] and info.get("sysName"):
                sets.append("hostname = ?"); vals.append(info["sysName"][:120])
            if not dev["notes"] and info.get("sysDescr"):
                sets.append("notes = ?"); vals.append(info["sysDescr"][:500])
            vals.append(did)
            c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", vals)
    return jsonify({"ok": True, "info": info})


@bp.route("/api/network/fingerprint/all", methods=["POST"])
def fingerprint_all():
    """Enqueue all devices that have an IP for fingerprinting."""
    with db() as c:
        rows = c.execute(
            "SELECT id FROM devices WHERE ip IS NOT NULL AND ip != ''"
        ).fetchall()
    ids = [r["id"] for r in rows]
    added = fp_enqueue(ids)
    return jsonify({"ok": True, "scheduled": added, "total_queued": len(FP_QUEUE)})


@bp.route("/api/network/fingerprint/status", methods=["GET"])
def fingerprint_status():
    with FP_LOCK:
        return jsonify({
            "state":   dict(FP_STATE),
            "queue":   len(FP_QUEUE),
            "remaining": FP_STATE["total"] - FP_STATE["done"]
                          if FP_STATE["total"] > FP_STATE["done"] else 0,
        })


# ============ SETTINGS ============

@bp.route("/api/network/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "autoscan_hours": float(setting_get("autoscan_hours", "4") or "4"),
    })


@bp.route("/api/network/settings", methods=["PUT"])
def update_settings():
    d = request.get_json(force=True) or {}
    if "autoscan_hours" in d:
        try:
            h = float(d["autoscan_hours"])
            if h < 0 or h > 168:
                return jsonify({"ok": False, "error": "0–168 hours"}), 400
            setting_set("autoscan_hours", str(h))
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "bad number"}), 400
    return jsonify({"ok": True})


# ============ DEVICE TYPES ============

@bp.route("/api/network/types", methods=["GET"])
def list_types():
    with db() as c:
        rows = c.execute(
            "SELECT key, label, is_builtin, sort_order FROM device_types ORDER BY sort_order, key"
        ).fetchall()
    return jsonify({"types": [dict(r) for r in rows]})


@bp.route("/api/network/types", methods=["POST"])
def add_type():
    d = request.get_json(force=True) or {}
    key = (d.get("key") or "").strip().lower()
    label = (d.get("label") or "").strip()
    if not key or not label:
        return jsonify({"ok": False, "error": "key + label required"}), 400
    if not re.match(r"^[a-z][a-z0-9_]{0,20}$", key):
        return jsonify({"ok": False, "error": "key: a-z, 0-9, _ только"}), 400
    try:
        with db() as c:
            c.execute("""INSERT INTO device_types (key, label, is_builtin, sort_order, created_at)
                         VALUES (?, ?, 0, 500, ?)""",
                      (key, label, int(time.time())))
        return jsonify({"ok": True})
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "тип с таким ключом уже есть"}), 409


@bp.route("/api/network/types/<key>", methods=["PUT"])
def rename_type(key):
    d = request.get_json(force=True) or {}
    label = (d.get("label") or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "label required"}), 400
    with db() as c:
        r = c.execute("SELECT * FROM device_types WHERE key = ?", (key,)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "not found"}), 404
        c.execute("UPDATE device_types SET label = ? WHERE key = ?", (label, key))
    return jsonify({"ok": True})


@bp.route("/api/network/types/<key>", methods=["DELETE"])
def delete_type(key):
    with db() as c:
        r = c.execute("SELECT * FROM device_types WHERE key = ?", (key,)).fetchone()
        if not r:
            return jsonify({"ok": False, "error": "not found"}), 404
        if r["is_builtin"]:
            return jsonify({"ok": False, "error": "встроенный тип нельзя удалить"}), 400
        in_use = c.execute("SELECT COUNT(*) AS n FROM devices WHERE device_type = ?",
                           (key,)).fetchone()["n"]
        if in_use:
            return jsonify({"ok": False,
                            "error": f"тип используется {in_use} устройствами — сначала переназначь"}), 400
        c.execute("DELETE FROM device_types WHERE key = ?", (key,))
    return jsonify({"ok": True})


# ============ MDNS / SNMP / TIMELINE ============

def mdns_scan():
    """Parse avahi-browse output to enrich devices with friendly names."""
    out = sh("avahi-browse -arpt --no-fail 2>/dev/null", timeout=20)
    results = []
    seen = set()
    for ln in out.splitlines():
        if not ln.startswith("="):
            continue
        parts = ln.split(";")
        if len(parts) < 8 or parts[2] != "IPv4":
            continue
        ip = parts[7]
        if not ip:
            continue
        name = parts[3]
        service = parts[4]
        hostname = parts[6]
        key = (ip, name)
        if key in seen:
            continue
        seen.add(key)
        results.append({"ip": ip, "name": name, "service": service, "hostname": hostname})
    return results


def merge_mdns(results):
    """Enrich existing devices with mDNS name/hostname (never overwrites)."""
    if not results:
        return 0
    now = int(time.time())
    enriched = 0
    with db() as c:
        for r in results:
            row = c.execute(
                "SELECT id, name, hostname FROM devices WHERE ip = ?", (r["ip"],)
            ).fetchone()
            if not row:
                continue
            sets, vals = [], []
            if not row["name"] and r["name"]:
                sets.append("name = ?"); vals.append(r["name"][:120])
            if not row["hostname"] and r["hostname"]:
                sets.append("hostname = ?"); vals.append(r["hostname"][:120])
            if sets:
                sets.append("updated_at = ?"); vals.append(now)
                vals.append(row["id"])
                c.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", vals)
                enriched += 1
    return enriched


def snmp_probe(ip, community="public"):
    """Basic SNMP info: sysDescr, sysName, sysUpTime, ifNumber."""
    info = {}
    oids = [
        ("1.3.6.1.2.1.1.1.0", "sysDescr"),
        ("1.3.6.1.2.1.1.5.0", "sysName"),
        ("1.3.6.1.2.1.1.3.0", "sysUpTime"),
        ("1.3.6.1.2.1.2.1.0", "ifNumber"),
    ]
    for oid, key in oids:
        out = sh(
            f"snmpget -v2c -c {community} -t 2 -r 1 {ip} {oid} 2>/dev/null",
            timeout=6,
        )
        m = re.search(r"=\s*\w+:\s*(.+)$", out.strip())
        if m:
            v = m.group(1).strip().strip('"')
            # strip type prefix like "STRING:" if any (just in case)
            info[key] = v
    return info


# ============ 24H TIMELINE ============

def _compute_timeline(events, current_is_online, now, window_start, buckets=48):
    bucket_sec = (now - window_start) // buckets
    if bucket_sec <= 0:
        return [0] * buckets
    # initial state at window_start
    if not events:
        state = bool(current_is_online)
    else:
        state = (events[0]["event_type"] == "offline")  # opposite of first event
    result = []
    cursor = window_start
    ev_idx = 0
    for b in range(buckets):
        b_start = window_start + b * bucket_sec
        b_end = b_start + bucket_sec
        if cursor < b_start:
            cursor = b_start
        on_time = 0
        while ev_idx < len(events) and events[ev_idx]["ts"] < b_end:
            e = events[ev_idx]
            if state and cursor < e["ts"]:
                on_time += min(e["ts"], b_end) - max(cursor, b_start)
            cursor = max(cursor, e["ts"])
            state = (e["event_type"] == "online")
            ev_idx += 1
        if state and cursor < b_end:
            on_time += b_end - max(cursor, b_start)
        cursor = b_end
        result.append(round(100 * on_time / bucket_sec))
    return result


_TIMELINE_CACHE = {"ts": 0, "data": {}}


def get_all_timelines():
    now = time.time()
    if (now - _TIMELINE_CACHE["ts"]) < 60 and _TIMELINE_CACHE["data"]:
        return _TIMELINE_CACHE["data"]
    int_now = int(now)
    window_start = int_now - 24 * 3600
    with db() as c:
        all_events = c.execute("""
            SELECT device_id, event_type, ts FROM device_events
             WHERE ts >= ?
             ORDER BY device_id, ts ASC
        """, (window_start,)).fetchall()
        all_devs = c.execute("SELECT id, is_online FROM devices").fetchall()
    by_dev = {}
    for e in all_events:
        by_dev.setdefault(e["device_id"], []).append(dict(e))
    data = {}
    for d in all_devs:
        data[d["id"]] = _compute_timeline(
            by_dev.get(d["id"], []), d["is_online"], int_now, window_start
        )
    _TIMELINE_CACHE["ts"] = now
    _TIMELINE_CACHE["data"] = data
    return data


# ============ AUTO-SCAN ============

_AUTOSCAN_STATE = {"next_at": 0}


def _autoscan_loop():
    """Periodic background scan. Interval read from settings table dynamically."""
    time.sleep(120)   # let dashboard settle on boot before first scan
    while True:
        try:
            hours = float(setting_get("autoscan_hours", "4") or "4")
        except (ValueError, TypeError):
            hours = 4.0
        if hours <= 0:
            _AUTOSCAN_STATE["next_at"] = 0   # disabled
            time.sleep(300)
            continue
        with _SCAN_LOCK:
            running = _SCAN_STATE["running"]
        if not running:
            subnet = detect_subnet()
            if subnet:
                try:
                    _scan_worker(subnet, scan_type="auto")
                except Exception:
                    pass
        next_at = time.time() + hours * 3600
        _AUTOSCAN_STATE["next_at"] = next_at
        # Sleep in 60-sec chunks so we react to setting changes within a minute.
        while time.time() < next_at:
            time.sleep(60)
            try:
                new_hours = float(setting_get("autoscan_hours", "4") or "4")
            except (ValueError, TypeError):
                new_hours = hours
            if new_hours <= 0:
                _AUTOSCAN_STATE["next_at"] = 0
                next_at = 0
                break
            if abs(new_hours - hours) > 0.001:
                hours = new_hours
                next_at = time.time() + hours * 3600
                _AUTOSCAN_STATE["next_at"] = next_at


threading.Thread(target=_autoscan_loop, daemon=True).start()


# ============ PRESENCE CHECK ============
# Light ARP-scan every minute: updates last_seen for known devices,
# does NOT add new ones (that's the full discovery scan's job).

_PRESENCE_STATE = {"last_run": 0, "last_alive": 0, "running": False}
# Гистерезис: device_id → сколько подряд скан-проходов устройство НЕ ответило на ARP.
# Помечаем offline только после MISS_THRESHOLD пропусков и дополнительного ICMP-ping.
# Защищает WiFi-устройства от ложных «прерываний» из-за power-save.
_PRESENCE_MISSES = {}
MISS_THRESHOLD = 3   # 3 минуты тишины перед попыткой ping-fallback


def _icmp_ping(ip, timeout_sec=1):
    """Быстрый ICMP-ping. True если устройство ответило хотя бы раз из 2 пакетов."""
    try:
        r = subprocess.run(
            ["ping", "-c", "2", "-W", str(timeout_sec), "-q", ip],
            capture_output=True, text=True, timeout=timeout_sec * 2 + 1
        )
        return r.returncode == 0
    except Exception:
        return False


def _presence_check():
    """Light ARP-scan + ICMP-fallback + гистерезис.
    Логика: nmap -sn ловит большинство; те кто ARP пропустил —
    проверяются через ICMP после 3 пропусков подряд.
    Помечаются offline только если и ARP, и ICMP молчат."""
    if _SCAN_STATE.get("running"):
        return 0
    subnet = detect_subnet()
    if not subnet:
        return 0
    _PRESENCE_STATE["running"] = True
    try:
        hosts = nmap_scan(subnet)
    except Exception:
        _PRESENCE_STATE["running"] = False
        return 0
    now = int(time.time())
    ping_recovered = 0       # сколько спасли через ICMP
    truly_offline = set()

    with db() as c:
        prev_online = {r["id"] for r in c.execute(
            "SELECT id FROM devices WHERE is_online = 1"
        )}
        seen_ids = set()
        for h in hosts:
            if h.get("mac"):
                rows = c.execute("""
                    UPDATE devices
                       SET is_online = 1, last_seen = ?, ip = COALESCE(?, ip)
                     WHERE mac = ?
                     RETURNING id
                """, (now, h["ip"], h["mac"])).fetchall()
            elif h.get("ip"):
                rows = c.execute("""
                    UPDATE devices SET is_online = 1, last_seen = ?
                     WHERE ip = ? AND (mac IS NULL OR mac = '')
                     RETURNING id
                """, (now, h["ip"])).fetchall()
            else:
                rows = []
            seen_ids.update(r["id"] for r in rows)
        # Те кого видели — сбрасываем счётчик пропусков
        for did in seen_ids:
            _PRESENCE_MISSES.pop(did, None)

        # Кандидаты на offline = были online, в этом скане не ответили на ARP
        candidates = prev_online - seen_ids
        for did in candidates:
            _PRESENCE_MISSES[did] = _PRESENCE_MISSES.get(did, 0) + 1
            if _PRESENCE_MISSES[did] < MISS_THRESHOLD:
                continue   # ещё в пределах гистерезиса — оставляем online
            # Подряд MISS_THRESHOLD пропусков — пробуем ICMP перед маркировкой offline
            row = c.execute("SELECT ip FROM devices WHERE id=?", (did,)).fetchone()
            ip = row["ip"] if row else None
            if ip and _icmp_ping(ip):
                # Живое — обновляем как онлайн, сбрасываем счётчик
                _PRESENCE_MISSES.pop(did, None)
                c.execute(
                    "UPDATE devices SET is_online = 1, last_seen = ? WHERE id = ?",
                    (now, did)
                )
                seen_ids.add(did)
                ping_recovered += 1
            else:
                truly_offline.add(did)

        # Применяем offline (только подтверждённые ICMP-молчанием)
        if truly_offline:
            ph = ",".join("?" * len(truly_offline))
            c.execute(f"UPDATE devices SET is_online = 0 WHERE id IN ({ph})",
                      list(truly_offline))
            for did in truly_offline:
                _PRESENCE_MISSES.pop(did, None)

        # Логируем переходы (только реальные, после ICMP-проверки)
        went_online = seen_ids - prev_online
        for did in went_online:
            c.execute("""INSERT INTO device_events (device_id, event_type, ts)
                         VALUES (?, 'online', ?)""", (did, now))
        for did in truly_offline:
            c.execute("""INSERT INTO device_events (device_id, event_type, ts)
                         VALUES (?, 'offline', ?)""", (did, now))

    _PRESENCE_STATE.update(
        last_run=now, last_alive=len(seen_ids), running=False,
        went_online=len(went_online), went_offline=len(truly_offline),
        ping_recovered=ping_recovered,
        miss_pending=sum(1 for v in _PRESENCE_MISSES.values() if 0 < v < MISS_THRESHOLD),
    )
    return len(seen_ids)


def _presence_loop():
    time.sleep(45)   # let things settle on boot
    while True:
        try:
            _presence_check()
        except Exception:
            _PRESENCE_STATE["running"] = False
        time.sleep(60)


threading.Thread(target=_presence_loop, daemon=True).start()


# ============ RETENTION ============
# Keep DB compact: trim events older than 30 days, scans older than 90 days.

RETENTION_EVENT_DAYS = 30
RETENTION_SCAN_DAYS  = 90


def cleanup_old_data():
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM device_events WHERE ts < ?",
                  (now - RETENTION_EVENT_DAYS * 86400,))
        c.execute("DELETE FROM scans WHERE started_at < ?",
                  (now - RETENTION_SCAN_DAYS * 86400,))
        c.execute("DELETE FROM device_audit WHERE ts < ?",
                  (now - AUDIT_RETENTION_DAYS * 86400,))
    # Vacuum once a week-ish to actually reclaim space.
    try:
        with db() as c:
            c.execute("VACUUM")
    except Exception:
        pass


def _cleanup_loop():
    time.sleep(600)        # 10 min after boot
    while True:
        try:
            cleanup_old_data()
        except Exception:
            pass
        time.sleep(24 * 3600)   # once a day


threading.Thread(target=_cleanup_loop, daemon=True).start()


@bp.route("/api/network/scan", methods=["POST"])
def start_scan():
    with _SCAN_LOCK:
        if _SCAN_STATE["running"]:
            return jsonify({"ok": False, "error": "scan already running"}), 409
    subnet = (request.get_json(silent=True) or {}).get("subnet") or detect_subnet()
    if not subnet:
        return jsonify({"ok": False, "error": "cannot detect subnet"}), 400
    threading.Thread(target=_scan_worker, args=(subnet,), daemon=True).start()
    return jsonify({"ok": True, "subnet": subnet})


@bp.route("/api/network/scan/status", methods=["GET"])
def scan_status():
    with db() as c:
        last = c.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_auto = c.execute(
            "SELECT * FROM scans WHERE scan_type = 'auto' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return jsonify({
        "state": dict(_SCAN_STATE),
        "last":  dict(last) if last else None,
        "last_auto": dict(last_auto) if last_auto else None,
        "next_auto_at": _AUTOSCAN_STATE.get("next_at", 0),
        "autoscan_hours": float(setting_get("autoscan_hours", "4") or "4"),
        "presence": dict(_PRESENCE_STATE),
    })


@bp.route("/api/network/export.csv", methods=["GET"])
def export_csv():
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    cols = ("name", "ip", "mac", "vendor", "device_type", "room",
            "description", "login", "password", "url", "last_seen")
    w.writerow(cols)
    with db() as c:
        for r in c.execute("SELECT * FROM devices"):
            w.writerow([r[col] or "" for col in cols])
    from flask import Response
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=network.csv"})
