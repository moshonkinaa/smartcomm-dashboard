"""MikroTik RouterOS REST API клиент.
Используется REST (доступен с RouterOS 7+) — простая HTTP-аутентификация,
никаких бинарных протоколов или SSH-ключей.
"""
import json
import time
import urllib.request
import urllib.error
import base64
import re
import sqlite3
import threading
from collections import deque

METRICS_DB = "/var/lib/smartcomm-dashboard/metrics.db"

# In-memory буферы для быстрого ответа /api/mikrotik/history
MT_HISTORY_1H  = {"ts": deque(maxlen=120), "cpu": deque(maxlen=120),
                  "rx_bps": deque(maxlen=120), "tx_bps": deque(maxlen=120)}    # 120 × 30s = 1 час
MT_HISTORY_24H = {"ts": deque(maxlen=288), "cpu": deque(maxlen=288),
                  "rx_bps": deque(maxlen=288), "tx_bps": deque(maxlen=288)}    # 288 × 5min = 24 часа
MT_HIST_LOCK = threading.Lock()


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _settings_get(network_bp, key, default=""):
    """Тянем mikrotik_ip / mikrotik_user / mikrotik_password из таблицы settings."""
    return network_bp.setting_get(key, default) or default


def _default_mt_ip(network_bp):
    """IP MikroTik по умолчанию: gateway сети контроллера (обычно .1)."""
    try:
        gw = network_bp.detect_gateway()
        if gw:
            return gw
    except Exception:
        pass
    return "192.168.1.1"   # last-resort, должно быть видно что неверно


def _basic_auth(user, password):
    s = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(s).decode()


def _safe_int(v, default=0):
    """int() из сырого значения RouterOS REST. Не падает на пустой строке /
    значении с суффиксом / None — возвращает default вместо ValueError.
    RouterOS иногда отдаёт '12.5' или '' или значения с единицами."""
    if v is None:
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        try:
            # На случай '12.5' или '53C' — берём ведущее число
            import re as _re
            m = _re.match(r"-?\d+", str(v).strip())
            return int(m.group(0)) if m else default
        except Exception:
            return default


def _req(network_bp, path, method="GET", body=None, timeout=4):
    """GET/POST на /rest/<path>. Возвращает распарсенный JSON или None."""
    ip = _settings_get(network_bp, "mikrotik_ip", _default_mt_ip(network_bp))
    user = _settings_get(network_bp, "mikrotik_user", "admin")
    pw = _settings_get(network_bp, "mikrotik_password", "")
    if not pw:
        return None
    url = f"http://{ip}/rest/{path.lstrip('/')}"
    try:
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", _basic_auth(user, pw))
        if body is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        # MikroTik (особенно WinBox на русской Windows) пишет comments в CP1251,
        # не в UTF-8. Сначала пробуем UTF-8 strict, fallback к CP1251.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1251", "replace")
        return json.loads(text)
    except Exception:
        return None


# ============ Public API ============

def cached(ttl):
    """Простой per-function кеш с TTL — снижаем нагрузку на MikroTik."""
    def deco(fn):
        def wrapper(network_bp, *a, **kw):
            now = time.time()
            key = fn.__name__
            with _CACHE_LOCK:
                v = _CACHE.get(key)
                if v and (now - v[0]) < ttl:
                    return v[1]
            r = fn(network_bp, *a, **kw)
            with _CACHE_LOCK:
                _CACHE[key] = (now, r)
            return r
        return wrapper
    return deco


@cached(30)
def mt_resource(network_bp):
    """CPU, RAM, диск, uptime, версия, board."""
    return _req(network_bp, "system/resource") or {}


@cached(30)
def mt_identity(network_bp):
    j = _req(network_bp, "system/identity") or {}
    return j.get("name", "")


@cached(30)
def mt_interfaces(network_bp):
    """Все интерфейсы со счётчиками rx/tx-byte."""
    return _req(network_bp, "interface") or []


@cached(30)
def mt_health(network_bp):
    """/system/health — температура/напряжение/вентилятор. Возвращает список dict'ов
    вида [{"name":"cpu-temperature","value":"53"}, ...]. Формат отличается между
    моделями:
      - RB5009: отдаёт единый объект {"name":"cpu-temperature","value":"53"}
      - RB4011: массив [{"name":"voltage",...}, {"name":"temperature","value":"54"}]
      - Старые/малые модели (hAP, hEX): могут не иметь датчиков вообще → []
    Нормализуем всё в список."""
    j = _req(network_bp, "system/health")
    if j is None:
        return []
    if isinstance(j, dict):
        return [j]
    if isinstance(j, list):
        return j
    return []


def mt_cpu_temp_c(network_bp):
    """Температура CPU MikroTik в °C или None если датчика нет / не публикуется.
    Ищем по разным именам метрик которые используют разные модели RouterOS."""
    health = mt_health(network_bp)
    if not health:
        return None
    # Приоритет: cpu-temperature > temperature > board-temperature
    # (cpu-temperature точнее, temperature на RB4011 = температура платы/CPU)
    by_name = {}
    for item in health:
        if isinstance(item, dict) and item.get("name"):
            by_name[item["name"]] = item.get("value")
    for key in ("cpu-temperature", "temperature", "board-temperature", "cpu-temp"):
        if key in by_name:
            return _safe_int(by_name[key], default=None)
    return None


@cached(30)
def mt_wan_iface(network_bp):
    """Имя WAN-интерфейса (тот, через который default route).
    REST возвращает массив маршрутов; ищем dst-address=0.0.0.0/0 и берём gateway-status."""
    routes = _req(network_bp, "ip/route", timeout=4) or []
    for r in routes:
        if r.get("dst-address") == "0.0.0.0/0" and r.get("active") == "true":
            # gateway-status выглядит как "192.0.2.1 reachable via ether1"
            gs = r.get("gateway-status", "")
            m = re.search(r"via\s+(\S+)", gs)
            if m:
                return m.group(1)
            # Иначе берём имя из immediate-gw
            ig = r.get("immediate-gw", "")
            m = re.search(r"%(\S+)$", ig)
            if m:
                return m.group(1)
    # fallback — первый ether
    for ifa in mt_interfaces(network_bp):
        n = ifa.get("name", "")
        if n.startswith("ether"):
            return n
    return None


# Для подсчёта скорости (байты/сек) нужно два снимка с разницей по времени
_TRAFFIC_PREV = {}
_TRAFFIC_LOCK = threading.Lock()

def mt_traffic_rates(network_bp):
    """Возвращает {iface_name: {rx_bps, tx_bps, rx_total, tx_total}}.
    Считаем дельту между двумя последовательными снимками.
    КРИТИЧНО: дёргаем _req напрямую без кеша — иначе sampler (30 сек)
    видит закешированные байты, delta=0, rate=0."""
    ifaces = _req(network_bp, "interface") or []
    now = time.time()
    rates = {}
    with _TRAFFIC_LOCK:
        for ifa in ifaces:
            name = ifa.get("name")
            if not name:
                continue
            rx = int(ifa.get("rx-byte", "0") or 0)
            tx = int(ifa.get("tx-byte", "0") or 0)
            prev = _TRAFFIC_PREV.get(name)
            if prev:
                dt = max(now - prev["ts"], 0.001)
                rx_bps = max((rx - prev["rx"]) / dt, 0)
                tx_bps = max((tx - prev["tx"]) / dt, 0)
            else:
                rx_bps = tx_bps = 0
            rates[name] = {
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                "rx_total": rx,
                "tx_total": tx,
            }
            _TRAFFIC_PREV[name] = {"ts": now, "rx": rx, "tx": tx}
    return rates


@cached(60)
def mt_dhcp_leases(network_bp):
    """Все DHCP leases. Поля: address, mac-address, comment, dynamic, status, server.
    static=true когда dynamic=false."""
    raw = _req(network_bp, "ip/dhcp-server/lease", timeout=6) or []
    out = []
    for r in raw:
        mac = (r.get("mac-address") or "").upper()
        if not mac:
            continue
        out.append({
            "mac": mac,
            "ip": r.get("address", ""),
            "comment": r.get("comment", "") or "",
            "static": r.get("dynamic", "true") == "false",
            "status": r.get("status", ""),
            "server": r.get("server", ""),
        })
    return out


def mt_status_snapshot(network_bp):
    """Всё для плиток на дашборде одним вызовом.
    ВАЖНО: WAN bps читаем из MT_HISTORY_1H (sampler — единственный кто их пишет),
    а не вызываем mt_traffic_rates здесь — иначе race condition (sampler и HTTP
    endpoint оба обновляют _TRAFFIC_PREV, второй из них видит почти нулевую дельту)."""
    pw = _settings_get(network_bp, "mikrotik_password", "")
    if not pw:
        return {"configured": False}
    res = mt_resource(network_bp)
    if not res:
        return {"configured": True, "ok": False, "error": "не отвечает или неверный пароль"}
    identity = mt_identity(network_bp)
    wan = mt_wan_iface(network_bp)

    # WAN bps — берём последнее значение из истории (sampler заполнил)
    with MT_HIST_LOCK:
        rx_bps = MT_HISTORY_1H["rx_bps"][-1] if MT_HISTORY_1H["rx_bps"] else 0
        tx_bps = MT_HISTORY_1H["tx_bps"][-1] if MT_HISTORY_1H["tx_bps"] else 0

    # WAN totals — кумулятивные счётчики, race не важен, берём из кеша
    wan_total_rx = wan_total_tx = 0
    if wan:
        for ifa in mt_interfaces(network_bp):
            if ifa.get("name") == wan:
                wan_total_rx = int(ifa.get("rx-byte", 0) or 0)
                wan_total_tx = int(ifa.get("tx-byte", 0) or 0)
                break

    # Конвертим в человеческие единицы. _safe_int — RouterOS иногда отдаёт
    # значения с суффиксами / пустые строки на нестандартных прошивках.
    total_mem = _safe_int(res.get("total-memory"))
    free_mem = _safe_int(res.get("free-memory"))
    used_mem = total_mem - free_mem
    total_hdd = _safe_int(res.get("total-hdd-space"))
    free_hdd = _safe_int(res.get("free-hdd-space"))
    used_hdd = total_hdd - free_hdd

    # Температура CPU — есть не на всех моделях (hAP lite/mini без датчика).
    cpu_temp = mt_cpu_temp_c(network_bp)

    return {
        "configured": True,
        "ok": True,
        "ip": _settings_get(network_bp, "mikrotik_ip", _default_mt_ip(network_bp)),
        "identity": identity,
        "board": res.get("board-name"),
        "version": res.get("version"),
        "cpu_load": _safe_int(res.get("cpu-load")),
        "cpu_count": _safe_int(res.get("cpu-count")),
        "cpu_freq_mhz": _safe_int(res.get("cpu-frequency")),
        "cpu_temp_c": cpu_temp,   # None если датчика нет
        "arch": res.get("architecture-name"),
        "uptime": res.get("uptime"),
        "mem": {
            "total_mb": round(total_mem / 1024 / 1024, 1),
            "used_mb":  round(used_mem  / 1024 / 1024, 1),
            "pct":      round(used_mem  / total_mem * 100, 1) if total_mem else 0,
        },
        "disk": {
            "total_mb": round(total_hdd / 1024 / 1024, 1),
            "used_mb":  round(used_hdd  / 1024 / 1024, 1),
            "pct":      round(used_hdd  / total_hdd * 100, 1) if total_hdd else 0,
        },
        "wan": {
            "iface": wan,
            "rx_kbps": round(rx_bps / 1024, 1),
            "tx_kbps": round(tx_bps / 1024, 1),
            "rx_total_gb": round(wan_total_rx / 1024**3, 2),
            "tx_total_gb": round(wan_total_tx / 1024**3, 2),
        },
    }


# ============ History persistence (MikroTik CPU + WAN bps) ============

def _mt_db():
    con = sqlite3.connect(METRICS_DB, timeout=5, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS mt_samples (
        ts INTEGER PRIMARY KEY,
        cpu_load REAL,
        wan_rx_bps REAL,
        wan_tx_bps REAL
    )""")
    return con


def _mt_persist(ts, cpu, rx_bps, tx_bps):
    try:
        con = _mt_db()
        con.execute("INSERT OR REPLACE INTO mt_samples VALUES (?,?,?,?)",
                    (int(ts), cpu, rx_bps, tx_bps))
        con.commit()
        con.close()
    except Exception:
        pass


def _mt_cleanup(days=30):
    try:
        con = _mt_db()
        cutoff = int(time.time()) - days * 86400
        con.execute("DELETE FROM mt_samples WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()
    except Exception:
        pass


def mt_hydrate():
    """Подгружает последние 1ч и 24ч из БД в RAM-буферы при старте."""
    try:
        con = _mt_db()
        now = int(time.time())
        rows1h = con.execute(
            "SELECT ts,cpu_load,wan_rx_bps,wan_tx_bps FROM mt_samples "
            "WHERE ts >= ? ORDER BY ts ASC", (now - 3600,)).fetchall()
        for r in rows1h[-120:]:
            MT_HISTORY_1H["ts"].append(r[0]); MT_HISTORY_1H["cpu"].append(r[1])
            MT_HISTORY_1H["rx_bps"].append(r[2]); MT_HISTORY_1H["tx_bps"].append(r[3])
        rows24 = con.execute(
            "SELECT ts,cpu_load,wan_rx_bps,wan_tx_bps FROM mt_samples "
            "WHERE ts >= ? ORDER BY ts ASC", (now - 86400,)).fetchall()
        last_bucket = -1
        for r in rows24:
            bucket = r[0] // 300
            if bucket == last_bucket:
                continue
            last_bucket = bucket
            MT_HISTORY_24H["ts"].append(r[0]); MT_HISTORY_24H["cpu"].append(r[1])
            MT_HISTORY_24H["rx_bps"].append(r[2]); MT_HISTORY_24H["tx_bps"].append(r[3])
        con.close()
        print(f"[mikrotik] hydrated {len(MT_HISTORY_1H['ts'])} (1h) + {len(MT_HISTORY_24H['ts'])} (24h) points")
    except Exception as e:
        print(f"[mikrotik] hydrate skipped: {e}")


def mt_sample_and_store(network_bp):
    """Один тик sampler'а — собираем данные напрямую (НЕ через mt_status_snapshot,
    чтобы избежать круговой зависимости: snapshot читает из истории, которую
    пишет sampler). Sampler — единственный кто обновляет _TRAFFIC_PREV."""
    pw = _settings_get(network_bp, "mikrotik_password", "")
    if not pw:
        return
    res = mt_resource(network_bp)
    if not res:
        return
    cpu = int(res.get("cpu-load", 0))
    wan = mt_wan_iface(network_bp)
    rates = mt_traffic_rates(network_bp)   # ← обновляет _TRAFFIC_PREV
    wan_rate = rates.get(wan, {}) if wan else {}
    rx = wan_rate.get("rx_bps", 0)
    tx = wan_rate.get("tx_bps", 0)
    ts = int(time.time())
    with MT_HIST_LOCK:
        MT_HISTORY_1H["ts"].append(ts);   MT_HISTORY_1H["cpu"].append(cpu)
        MT_HISTORY_1H["rx_bps"].append(rx); MT_HISTORY_1H["tx_bps"].append(tx)
        # 24h-буфер обновляем каждые 5 мин (sampler раз в 30 сек, каждый 10-й)
        if not MT_HISTORY_24H["ts"] or (ts - MT_HISTORY_24H["ts"][-1]) >= 300:
            MT_HISTORY_24H["ts"].append(ts); MT_HISTORY_24H["cpu"].append(cpu)
            MT_HISTORY_24H["rx_bps"].append(rx); MT_HISTORY_24H["tx_bps"].append(tx)
    _mt_persist(ts, cpu, rx, tx)


def mt_auto_sync_loop(network_bp):
    """Раз в час автоматически синхронизирует DHCP comments → имена устройств
    и обновляет флаг static/dynamic. Если MikroTik не настроен — тихо ждём.
    Первый запуск через 60 сек после старта (даём dashboard прогреться)."""
    time.sleep(60)
    while True:
        try:
            if _settings_get(network_bp, "mikrotik_password", ""):
                r = sync_dhcp_to_inventory(network_bp)
                if r.get("ok") and r.get("applied", 0) > 0:
                    print(f"[mikrotik] auto-sync: applied={r['applied']}, skipped={r['skipped']}")
        except Exception as e:
            print(f"[mikrotik] auto-sync error: {e}")
        time.sleep(3600)


def mt_sampler(network_bp):
    """Фоновый поток — 30-сек шаг, заполняет историю + чистит старое.
    Warm-up: первый снимок только заполняет _TRAFFIC_PREV (delta была бы 0),
    через 2 сек делаем второй — реальная дельта, идёт в историю."""
    try:
        mt_traffic_rates(network_bp)   # populate _TRAFFIC_PREV, отбрасываем нули
    except Exception:
        pass
    time.sleep(2)
    tick = 0
    while True:
        try:
            mt_sample_and_store(network_bp)
            if tick % 120 == 0 and tick > 0:   # раз в час
                _mt_cleanup(30)
        except Exception:
            pass
        tick += 1
        time.sleep(30)


def mt_history(rng="1h"):
    buf = MT_HISTORY_24H if rng == "24h" else MT_HISTORY_1H
    with MT_HIST_LOCK:
        return {
            "range": rng,
            "ts":     list(buf["ts"]),
            "cpu":    list(buf["cpu"]),
            "rx_bps": list(buf["rx_bps"]),
            "tx_bps": list(buf["tx_bps"]),
        }


def sync_dhcp_to_inventory(network_bp):
    """Применяем DHCP comments к именам устройств в карте сети.
    Override always (по решению пользователя).
    Возвращает {applied: N, skipped: N, details: [...]}.
    """
    import sqlite3
    leases = mt_dhcp_leases(network_bp)
    if not leases:
        return {"ok": False, "error": "не получили leases с MikroTik"}
    applied = 0
    skipped = 0
    details = []
    audit_entries = []
    con = sqlite3.connect(network_bp.DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        for lease in leases:
            mac = lease["mac"]
            comment = lease["comment"].strip()
            ip = lease["ip"]
            static = lease["static"]
            row = con.execute(
                "SELECT id, name FROM devices WHERE upper(mac)=? LIMIT 1", (mac,)
            ).fetchone()
            if not row:
                # устройство не найдено в нашей БД — пропускаем (может ещё не отсканено)
                skipped += 1
                details.append({"mac": mac, "ip": ip, "comment": comment,
                                "static": static, "action": "no-match"})
                continue
            old_name = row["name"] or ""
            if not comment:
                skipped += 1
                details.append({"id": row["id"], "mac": mac, "ip": ip, "comment": "",
                                "static": static, "action": "no-comment"})
                continue
            if comment == old_name:
                skipped += 1
                details.append({"id": row["id"], "mac": mac, "ip": ip, "comment": comment,
                                "static": static, "action": "unchanged"})
                continue
            # ОБНОВЛЯЕМ
            con.execute("UPDATE devices SET name=? WHERE id=?", (comment, row["id"]))
            applied += 1
            details.append({"id": row["id"], "mac": mac, "ip": ip, "comment": comment,
                            "static": static, "action": "renamed",
                            "from": old_name, "to": comment})
            audit_entries.append((row["id"], "sync_mikrotik",
                                  f"name: '{old_name}' → '{comment}' (DHCP comment)"))
        # Заодно сохраним static-флаг — добавим столбец если нет
        try:
            con.execute("ALTER TABLE devices ADD COLUMN dhcp_static INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        for lease in leases:
            mac = lease["mac"]
            con.execute("UPDATE devices SET dhcp_static=? WHERE upper(mac)=?",
                        (1 if lease["static"] else 0, mac))
        # audit
        for did, action, det in audit_entries:
            con.execute("INSERT INTO device_audit(device_id, ts, action, details) VALUES(?,?,?,?)",
                        (did, int(time.time()), action, det))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "applied": applied, "skipped": skipped,
            "total_leases": len(leases), "details": details}
