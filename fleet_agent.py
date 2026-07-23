"""SmartComm Fleet-агент — heartbeat-клиент контроллера.

Фоновый поток: раз в HEARTBEAT_SEC собирает полный status-снапшот и шлёт его
POST'ом на fleet-портал (только исходящее соединение → проходит любой NAT).
В ответ портал отдаёт очередь команд — агент их выполняет и репортит результат.

Конфиг хранится в network settings (fleet_* ключи), редактируется из дашборда:
  fleet_enabled     "1"/"0"
  fleet_portal_url  https://portal.example:PORT  (без хвостового /)
  fleet_node_id     стабильный ID узла
  fleet_token       секрет аутентификации (портал хранит sha256)

Агент НИКОГДА не роняет дашборд: все ошибки (портал недоступен, таймаут)
глушатся, поток продолжает работать.
"""

import json
import threading
import time
import urllib.request
import urllib.error

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

HEARTBEAT_SEC = 60
HTTP_TIMEOUT = 15

# #72: защита от replay — виденные nonce с их expires_at (чистим протухшие).
_SEEN_NONCES = {}


def _canonical_cmd(cmd):
    """Та же детерминированная сериализация подписываемых полей, что и на портале."""
    fields = {k: cmd.get(k) for k in
              ("id", "node_id", "command", "params", "nonce", "issued_at", "expires_at")}
    if fields["params"] is None:
        fields["params"] = {}
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def _verify_command(cmd, pubkey_hex):
    """Проверка подписи Ed25519 + expiry + replay. Возвращает (ok, reason)."""
    now = int(time.time())
    for n in [n for n, exp in _SEEN_NONCES.items() if exp < now]:
        _SEEN_NONCES.pop(n, None)
    sig = cmd.get("sig"); nonce = cmd.get("nonce"); exp = cmd.get("expires_at")
    if not sig or not nonce or exp is None:
        return False, "нет подписи/nonce/expiry"
    try:
        if int(exp) < now:
            return False, "устарела (expired)"
    except (TypeError, ValueError):
        return False, "битый expires_at"
    if nonce in _SEEN_NONCES:
        return False, "replay (nonce уже виден)"
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(sig), _canonical_cmd(cmd))
    except (InvalidSignature, ValueError):
        return False, "неверная подпись"
    _SEEN_NONCES[nonce] = int(exp)
    return True, "ok"

_STARTED = False
_STATE = {"last_ok": 0, "last_error": None, "last_attempt": 0}


def _post_json(url, obj, headers, timeout=HTTP_TIMEOUT):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _heartbeat_loop(get_snapshot, get_setting, run_command):
    """get_snapshot() -> dict (полный /api/status снапшот)
    get_setting(key, default) -> str (чтение fleet_* настроек)
    run_command(command, params) -> (ok: bool, result: str) — исполнитель команд
    """
    time.sleep(20)   # даём дашборду подняться
    while True:
        try:
            if get_setting("fleet_enabled", "0") != "1":
                time.sleep(HEARTBEAT_SEC)
                continue
            portal = (get_setting("fleet_portal_url", "") or "").rstrip("/")
            node_id = get_setting("fleet_node_id", "")
            token = get_setting("fleet_token", "")
            if not portal or not node_id or not token:
                time.sleep(HEARTBEAT_SEC)
                continue
            # SECURITY (C2): только HTTPS. По http токен узла (X-Node-Token) ушёл бы
            # открытым текстом, а on-path атакующий мог бы подменить ответ портала и
            # навязать команды агенту (командный канал доверяет транспорту). Localhost
            # разрешаем для отладки. Иначе — цикл простаивает, ничего не шлём.
            _low = portal.lower()
            if not (_low.startswith("https://")
                    or _low.startswith("http://127.0.0.1")
                    or _low.startswith("http://localhost")):
                _STATE["last_error"] = "portal_url must be https:// — heartbeat skipped"
                time.sleep(HEARTBEAT_SEC)
                continue

            _STATE["last_attempt"] = int(time.time())
            headers = {"X-Node-Id": node_id, "X-Node-Token": token}

            # 1. Собрать снапшот и отправить, получить команды
            snapshot = get_snapshot()
            resp = _post_json(portal + "/fleet/api/heartbeat", snapshot, headers)
            _STATE["last_ok"] = int(time.time())
            _STATE["last_error"] = None

            # 2. Выполнить команды (если есть) и отрепортить.
            # #72: проверяем подпись Ed25519. Публичный ключ пинится в настройке
            # fleet_cmd_pubkey (ставится при онбординге). Ключ настроен → fail-CLOSED
            # (плохая/просроченная/replay-команда отвергается). НЕ настроен → fail-open
            # + видимое предупреждение (обратная совместимость при раскатке).
            pubkey_hex = (get_setting("fleet_cmd_pubkey", "") or "").strip()
            _STATE["cmd_signing"] = "ok" if pubkey_hex else "НЕ настроена (fleet_cmd_pubkey пуст)"
            for cmd in (resp.get("commands") or []):
                cid = cmd.get("id")
                name = cmd.get("command")
                params = cmd.get("params") or {}
                if pubkey_hex:
                    okv, reason = _verify_command(cmd, pubkey_hex)
                    if not okv:
                        _STATE["last_error"] = f"команда '{name}' ОТВЕРГНУТА: {reason}"
                        try:
                            _post_json(portal + "/fleet/api/command-result",
                                       {"id": cid, "status": "failed",
                                        "result": f"подпись отвергнута: {reason}"}, headers)
                        except Exception:
                            pass
                        continue
                try:
                    ok, result = run_command(name, params)
                except Exception as e:
                    ok, result = False, f"agent exception: {e}"
                try:
                    _post_json(portal + "/fleet/api/command-result",
                               {"id": cid,
                                "status": "done" if ok else "failed",
                                "result": str(result)[:2000]},
                               headers)
                except Exception:
                    pass   # результат дойдёт при следующем heartbeat-цикле если критично
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, OSError, ValueError) as e:
            _STATE["last_error"] = str(e)[:200]
        except Exception as e:
            _STATE["last_error"] = f"unexpected: {e}"[:200]
        time.sleep(HEARTBEAT_SEC)


def ensure_started(get_snapshot, get_setting, run_command):
    """Запустить heartbeat-поток один раз. Безопасно дёргать многократно."""
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    threading.Thread(
        target=_heartbeat_loop,
        args=(get_snapshot, get_setting, run_command),
        daemon=True, name="fleet-heartbeat",
    ).start()


def agent_state():
    """Для /api/fleet/status — показать оператору состояние агента."""
    return dict(_STATE)
