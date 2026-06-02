msmp-py
---
~~A nonebot plugin for communicating with a Minecraft Server through the new MSMP protocol~~

msmp-py was a plugin for the MSMP protocol, and now has evolved into a general purpose Minecraft server remote management tool for myself.

an actual project specifically for MSMP will be derived soon.

Example `.env`:

```env
MSMP_REMOTE_MODE=true
MSMP_URI=wss://address:port
MSMP_SECRET=YOUR_40_CHAR_SECRET
MSMP_USE_TLS=true
MSMP_SSL_PEM=server-cert.pem
MSMP_ORIGIN=msmp-py
MSMP_LOCAL_RELOAD_VIA_RCON=false
MSMP_WHITELIST_PATH=C:\path\to\server\whitelist.json
RCON_HOST=address.com
RCON_PORT=25575
RCON_PASSWORD=YOUR_RCON_PASSWORD
MSMP_ALLOWED_GROUPS=["123456789","987654321"]
```

Set `MSMP_USE_TLS=false` to connect without TLS. When TLS is disabled, `MSMP_SSL_PEM` is not required and `wss://` URIs are treated as `ws://`.

Set your Minecraft server's `management-server-allowed-origins` to the same value as `MSMP_ORIGIN`, for example `management-server-allowed-origins=msmp-py`.

`MSMP_REMOTE_MODE=false` makes `/whitelist` update a local server whitelist file instead of calling the MSMP allowlist command. If `MSMP_LOCAL_RELOAD_VIA_RCON=true`, the plugin then sends `whitelist reload` over RCON after the file write. That keeps RCON scoped to the one thing local mode needs from it.

In local whitelist mode, `MSMP_WHITELIST_PATH` must point to the server's whitelist JSON file; this is separate from `plugins/whitelist.json`, which tracks each user's whitelist quota.

If you enable local reloads, set `RCON_HOST`, `RCON_PORT`, and `RCON_PASSWORD` to the server's RCON settings.

`plugins/admins.json` controls who can use `/command`. Put a JSON object with QQ user IDs as keys in it, using the same shape as `plugins/whitelist.json`, for example:

```json
{
  "123456789": [],
  "987654321": []
}
```

Admins can run arbitrary Minecraft commands through RCON with:

```text
/command say hello world
```

Admins can also remove a player from the whitelist with:

```text
/removewhitelist PlayerName
```

In local whitelist mode, this removes the entry from `MSMP_WHITELIST_PATH` and, if `MSMP_LOCAL_RELOAD_VIA_RCON=true`, reloads the whitelist over RCON afterward.

Local whitelist mode writes entries in the Minecraft `whitelist.json` format:

```json
[
  {
    "uuid": "offline-player-uuid",
    "name": "PlayerName"
  }
]
```

The UUID is generated as Minecraft's offline player UUID from `OfflinePlayer:<name>`. If the server is already running, reload the whitelist after changing the file.
