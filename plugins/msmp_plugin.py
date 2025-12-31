# plugins/msmp.py
import asyncio, json, ssl
from typing import Any, Awaitable, Callable, Dict, List
from nonebot import on_command, get_driver
from nonebot.adapters.onebot.v11 import Bot, Event
from websockets.client import connect
from websockets.exceptions import ConnectionClosed

import json
from pathlib import Path

# config
cfg = get_driver().config
MSMP_URI: str = getattr(cfg, "msmp_uri")
MSMP_SECRET: str = getattr(cfg, "msmp_secret")
MSMP_SSL_PEM: str = getattr(cfg, "msmp_ssl_pem")
# Example .env:
# MSMP_URI=wss://example.com:25567
# MSMP_SECRET=YOUR_40_CHAR_SECRET
# MSMP_SSL_PEM=server-cert.pem
# MSMP_ALLOWED_GROUPS=["123456789","987654321"]
_allowed = getattr(cfg, "msmp_allowed_groups", [])
ALLOWED_GROUPS = {int(x) for x in _allowed} if _allowed else set()

WHITELIST_LIMIT = 2
USER_WHITELISTS: dict[str, set[str]] = {}

DATA_FILE = Path(__file__).parent / "whitelist.json"

sslctx = ssl.create_default_context(cafile=MSMP_SSL_PEM)
sslctx.check_hostname = False  # self-signed / host mismatch

# msmp client
Json = Dict[str, Any]
NotifHandler = Callable[[Json], Awaitable[None]]

class MSMPClient:
    def __init__(self, uri: str, secret: str, sslctx: ssl.SSLContext):
        self.uri, self.secret, self.sslctx = uri, secret, sslctx
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

    async def call(self, method: str, params: Json | None = None, timeout: float = 15.0):
        await self._wait_ready()
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

    async def _wait_ready(self):
        while not (self.ws and not self.ws.closed):
            await asyncio.sleep(0.05)

    async def _run(self):
        while self._running:
            try:
                async with connect(
                    self.uri,
                    ssl=self.sslctx,
                    extra_headers={"Authorization": f"Bearer {self.secret}"},
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    self.ws = ws
                    await self._recv_loop(ws)
            except Exception:
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

msmp = MSMPClient(MSMP_URI, MSMP_SECRET, sslctx)
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
    if not DATA_FILE.is_file():
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
        await msmp.call("minecraft:allowlist/add", {"add": [{"name": name}]})
    except Exception as e:
        await whitelist_cmd.finish(f"添加白名单失败：{e}")

# add local record
    used.add(name)
    save_whitelist_data()

    await whitelist_cmd.finish(f"已为你添加白名单：{name}")
