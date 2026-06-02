# plugins/msmp.py
import asyncio, hashlib, json, ssl
from typing import Any, Awaitable, Callable, Dict, List
from nonebot import on_command, get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, Event
from websockets.client import connect
from websockets.exceptions import ConnectionClosed

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# config
cfg = get_driver().config
MSMP_REMOTE_MODE: bool = getattr(cfg, "msmp_remote_mode", True)
MSMP_URI: str | None = getattr(cfg, "msmp_uri", None)
MSMP_SECRET: str | None = getattr(cfg, "msmp_secret", None)
MSMP_USE_TLS: bool = getattr(cfg, "msmp_use_tls", True)
MSMP_SSL_PEM: str | None = getattr(cfg, "msmp_ssl_pem", None)
MSMP_ORIGIN: str | None = getattr(cfg, "msmp_origin", "msmp-py")
MSMP_WHITELIST_PATH: str | None = getattr(cfg, "msmp_whitelist_path", None)
# Example .env:
# MSMP_REMOTE_MODE=true
# MSMP_URI=wss://frp-fog.com:25567
# MSMP_SECRET=YOUR_40_CHAR_SECRET
# MSMP_USE_TLS=true
# MSMP_SSL_PEM=server-cert.pem
# MSMP_ORIGIN=msmp-py
# MSMP_WHITELIST_PATH=C:\path\to\server\whitelist.json
# MSMP_ALLOWED_GROUPS=["123456789","987654321"]
_allowed = getattr(cfg, "msmp_allowed_groups", [])
ALLOWED_GROUPS = {int(x) for x in _allowed} if _allowed else set()

WHITELIST_LIMIT = 2
USER_WHITELISTS: dict[str, set[str]] = {}

DATA_FILE = Path(__file__).parent / "whitelist.json"

def _config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _build_ssl_context(use_tls: bool, cafile: str | None) -> ssl.SSLContext | None:
    if not use_tls:
        return None
    if not cafile:
        raise RuntimeError("MSMP_SSL_PEM is required when MSMP_USE_TLS is enabled")

    ctx = ssl.create_default_context(cafile=cafile)
    ctx.check_hostname = False  # self-signed / host mismatch
    return ctx


def _normalize_msmp_uri(uri: str, use_tls: bool) -> str:
    uri = uri.strip()
    scheme = "wss" if use_tls else "ws"
    if not uri.lower().startswith(("ws://", "wss://")):
        return f"{scheme}://{uri.lstrip('/')}"

    parsed = urlsplit(uri)
    if parsed.scheme not in {"ws", "wss"}:
        return uri

    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _resolve_whitelist_path(path: str | None) -> Path | None:
    if not path:
        return None
    return Path(path).expanduser()


def _require_remote_config(uri: str | None, secret: str | None) -> tuple[str, str]:
    if not uri:
        raise RuntimeError("MSMP_URI is required")
    if not secret:
        raise RuntimeError("MSMP_SECRET is required")
    return uri, secret


def _require_local_whitelist_path(path: Path | None) -> Path:
    if path is None:
        raise RuntimeError("MSMP_WHITELIST_PATH is required when MSMP_REMOTE_MODE is disabled")
    return path


def _construct_offline_player_uuid(username: str) -> str:
    digest = hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest()
    uuid_bytes = bytearray(digest)
    uuid_bytes[6] = digest[6] & 0x0F | 0x30
    uuid_bytes[8] = digest[8] & 0x3F | 0x80
    uuid_hex = bytes(uuid_bytes).hex()
    return (
        f"{uuid_hex[:8]}-{uuid_hex[8:12]}-{uuid_hex[12:16]}-"
        f"{uuid_hex[16:20]}-{uuid_hex[20:]}"
    )


def _add_whitelist_entry_locally(name: str) -> None:
    whitelist_path = _require_local_whitelist_path(MSMP_WHITELIST_FILE)
    if whitelist_path.is_file():
        with whitelist_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        raw = []

    if not isinstance(raw, list):
        raise RuntimeError("MSMP_WHITELIST_PATH must point to a JSON array file")

    offline_uuid = _construct_offline_player_uuid(name)
    entry_found = False
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        existing_name = str(entry.get("name", ""))
        existing_uuid = str(entry.get("uuid", ""))
        if existing_name.lower() == name.lower() or existing_uuid == offline_uuid:
            entry["uuid"] = offline_uuid
            entry["name"] = name
            entry_found = True
            break

    if not entry_found:
        raw.append({"uuid": offline_uuid, "name": name})

    whitelist_path.parent.mkdir(parents=True, exist_ok=True)
    with whitelist_path.open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


MSMP_REMOTE_MODE = _config_bool(MSMP_REMOTE_MODE)
MSMP_USE_TLS = _config_bool(MSMP_USE_TLS)
MSMP_WHITELIST_FILE = _resolve_whitelist_path(MSMP_WHITELIST_PATH)

MSMP_ORIGIN = MSMP_ORIGIN.strip() if MSMP_ORIGIN else None
MSMP_URI, MSMP_SECRET = _require_remote_config(MSMP_URI, MSMP_SECRET)
MSMP_URI = _normalize_msmp_uri(MSMP_URI, MSMP_USE_TLS)
sslctx = _build_ssl_context(MSMP_USE_TLS, MSMP_SSL_PEM)
if not MSMP_REMOTE_MODE:
    _require_local_whitelist_path(MSMP_WHITELIST_FILE)

# msmp client
Json = Dict[str, Any]
NotifHandler = Callable[[Json], Awaitable[None]]

class MSMPClient:
    def __init__(self, uri: str, secret: str, sslctx: ssl.SSLContext | None, origin: str | None):
        self.uri, self.secret, self.sslctx, self.origin = uri, secret, sslctx, origin
        self.ws = None
        self._rid = 0
        self._lock = asyncio.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._subs: Dict[str, List[NotifHandler]] = {}
        self._running = False
        self._reader_task: asyncio.Task | None = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._reader_task = asyncio.create_task(self._run())

    async def stop(self):
        self._running = False
        if self.ws:
            await self.ws.close()
        if self._reader_task:
            await self._reader_task

    def on(self, notif_method: str, handler: NotifHandler):
        self._subs.setdefault(notif_method, []).append(handler)

    def is_ready(self) -> bool:
        return bool(self.ws and not self.ws.closed)

    async def call(
        self,
        method: str,
        params: Json | None = None,
        timeout: float = 15.0,
        ready_timeout: float = 10.0,
    ):
        await self._wait_ready(timeout=ready_timeout)
        async with self._lock:
            self._rid += 1
            rid = self._rid
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            req: Json = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:  # include params only when provided
                req["params"] = params
            await self.ws.send(json.dumps(req))
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _wait_ready(self, timeout: float):
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.is_ready():
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("MSMP connection is not ready")
            await asyncio.sleep(0.05)

    async def _run(self):
        while self._running:
            try:
                logger.info(f"Connecting to MSMP at {self.uri}")
                async with connect(
                    self.uri,
                    ssl=self.sslctx,
                    origin=self.origin,
                    extra_headers={"Authorization": f"Bearer {self.secret}"},
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    self.ws = ws
                    logger.info("MSMP connection established")
                    await self._recv_loop(ws)
            except Exception as e:
                logger.warning(f"MSMP connection failed: {type(e).__name__}: {e}; retrying in 3s")
            finally:
                self.ws = None
                logger.info("MSMP connection closed")
            if self._running:
                await asyncio.sleep(3.0)

    async def _recv_loop(self, ws):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result"))
                else:
                    meth = msg.get("method", "")
                    params = msg.get("params", {})
                    for cb in self._subs.get(meth, []):
                        asyncio.create_task(cb(params))
        except ConnectionClosed:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionClosed(1006, "reconnecting"))
            self._pending.clear()

msmp = MSMPClient(MSMP_URI, MSMP_SECRET, sslctx, MSMP_ORIGIN)
driver = get_driver()

@driver.on_startup
async def _startup():
    load_whitelist_data()
    await msmp.start()
    # await msmp.call("minecraft:notification/players/joined")
    # await msmp.call("minecraft:notification/players/left")
    msmp.on("minecraft:notification/players/joined", _on_join)
    msmp.on("minecraft:notification/players/left", _on_left)
    

@driver.on_shutdown
async def _shutdown():
    save_whitelist_data()
    await msmp.stop()

def load_whitelist_data() -> None:
    global USER_WHITELISTS
    if not DATA_FILE.is_file() or DATA_FILE.stat().st_size == 0:
        USER_WHITELISTS = {}
        return
    with DATA_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    # convert list to set
    USER_WHITELISTS = {
        str(uid): set(names) for uid, names in raw.items() if isinstance(names, list)
    }


def save_whitelist_data() -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = {uid: sorted(list(names)) for uid, names in USER_WHITELISTS.items()}
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

async def _broadcast_to_allowed(message: str):
    if not ALLOWED_GROUPS:
        return
    for bot in driver.bots.values():
        for gid in ALLOWED_GROUPS:
            try:
                await bot.send_group_msg(group_id=gid, message=message)
            except Exception:
                pass
            
def _extract_names(params):
    names = []
    if isinstance(params, dict):
        if "player" in params and isinstance(params["player"], dict):
            n = params["player"].get("name")
            if n: names.append(n)
        if "players" in params and isinstance(params["players"], list):
            for p in params["players"]:
                if isinstance(p, dict) and "name" in p:
                    names.append(p["name"])
    elif isinstance(params, list):
        for p in params:
            if isinstance(p, dict):
                if "name" in p:
                    names.append(p["name"])
                elif "player" in p and isinstance(p["player"], dict) and "name" in p["player"]:
                    names.append(p["player"]["name"])
    elif isinstance(params, str):
        names.append(params)
    return names or ["Unknown"]

async def _on_join(params):
    for name in _extract_names(params):
        await _broadcast_to_allowed(f"【RPC】{name} 加入了游戏")

async def _on_left(params):
    for name in _extract_names(params):
        await _broadcast_to_allowed(f"【RPC】{name} 离开了游戏")
    
    

# /msmpstatus command
msmpstatus_cmd = on_command("msmpstatus")

@msmpstatus_cmd.handle()
async def _(bot: Bot, event: Event):
    status = "connected" if msmp.is_ready() else "disconnected"
    await msmpstatus_cmd.finish(f"MSMP is {status}: {MSMP_URI}")


# /players command
players_cmd = on_command("players", aliases={"在线玩家", "在线列表"})

@players_cmd.handle()
async def _(bot: Bot, event: Event):
    try:
        players = await msmp.call("minecraft:players")
        names = [p.get("name") for p in (players or []) if isinstance(p, dict)]
        msg = f"在线玩家 ({len(names)}): " + (", ".join(names) if names else "无")
    except Exception as e:
        msg = f"查询失败: {e}"
    await players_cmd.finish(msg)


# /whitelist command
whitelist_cmd = on_command("whitelist", aliases={"白名单"})

@whitelist_cmd.handle()
async def _(bot: Bot, event: Event):
    text = event.get_plaintext().strip()
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        await whitelist_cmd.finish("用法：/whitelist <玩家名>")

    name = parts[1].strip()
    if not name:
        await whitelist_cmd.finish("用法：/whitelist <玩家名>")

    user_id = str(event.user_id)
    used = USER_WHITELISTS.setdefault(user_id, set())

    if name in used:
        await whitelist_cmd.finish(f"你已经为 {name} 申请过白名单了。")

    if len(used) >= WHITELIST_LIMIT:
        used_list = ", ".join(sorted(used))
        await whitelist_cmd.finish(
            f"你已经用完白名单名额（{WHITELIST_LIMIT} 个）：{used_list}"
        )

# call server
    try:
        if MSMP_REMOTE_MODE:
            await msmp.call("minecraft:allowlist/add", {"add": [{"name": name}]})
        else:
            _add_whitelist_entry_locally(name)
    except Exception as e:
        await whitelist_cmd.finish(f"添加白名单失败：{e}")

# add local record
    used.add(name)
    save_whitelist_data()

    await whitelist_cmd.finish(f"已为你添加白名单：{name}")
