# VEIL Live Inputs

Live telemetry lands in the active twin data directory, not in the curated twin
store by default.

## API

`POST /api/live/events` accepts source-neutral events. The live ingest,
streaming, discovery, gateway/device control, history, and export routes are
**open by default** — VEIL is local-first, so live telemetry works with zero
setup on a machine you run yourself. The server logs a one-time warning when it
serves the live API without a token.

**If you publish a VEIL twin to the web, set a token.** Without one, anyone who
can reach the server can inject telemetry or control your gateways. Configure it
with either `VEIL_LIVE_TOKEN` or a gitignored token file at `data/.live_token`
for the active twin data directory (a repo-root `.live_token` is also supported
as a fallback for older local checkouts). Once a token is set, every live route
requires it — send it as `Authorization: Bearer <token>`,
`X-VEIL-Live-Token: <token>`, or `?token=<token>`. The viewer prompts for it
once and remembers it.

```bash
openssl rand -hex 16 > data/.live_token   # generate and require a token
```

```json
{
  "schema": "veil.live.v1",
  "kind": "position",
  "device_id": "!abcd1234",
  "label": "Dog",
  "observed_at": "2026-06-20T18:22:41Z",
  "position": {
    "lat": 40.1234567,
    "lon": -74.1234567,
    "alt_m": 103.4,
    "accuracy_m": null
  },
  "motion": {
    "speed_mps": 2.1,
    "heading_deg": 84
  },
  "link": {
    "gateway_id": "!11223344",
    "snr_db": 8.5,
    "rssi_dbm": -92,
    "hops": 0
  },
  "source": {
    "protocol": "meshtastic",
    "transport": "bluetooth"
  }
}
```

Supported `kind` values are `position`, `message`, `data`, `status`, `media`,
and `command`. General payloads can use `message`, `data`, `payload`,
`metadata`, or `media`.

Meshtastic `TELEMETRY_APP` packets are logged as `status` events with the full
decoded telemetry payload in `data.telemetry`. Common device battery/radio
metrics are also copied to `data.battery`, including `battery_level_pct`,
`voltage_v`, `uptime_seconds`, `channel_utilization_pct`, and
`air_util_tx_pct` when the tracker sends them.

Other routes:

- `GET /api/live/latest` returns registered gateways and latest device state.
  By default it returns only devices you have configured/named; pass
  `?discovery=1` to include all observed Meshtastic nodes from the gateway's
  node database.
- `GET /api/live/stream` streams Server-Sent Events.
- `POST /api/live/gateways` registers a gateway device.
- `POST /api/live/gateways/restart` starts/restarts a registered gateway bridge.
- `POST /api/live/gateways/stop` stops a gateway bridge but keeps the
  registration.
- `POST /api/live/gateways/remove` stops and removes a gateway registration.
- `POST /api/live/devices` changes a device display label or visibility.
- `POST /api/live/devices/command` queues a command through a registered
  gateway bridge. Supported commands are `request_position` and `traceroute`.
- `GET /api/live/days` lists recorded telemetry days.
- `GET /api/live/history?date=YYYY-MM-DD` returns a day's events.
- `POST /api/live/export` appends a day or snapshot into `twin.gpkg`.

## Storage

The server writes:

- `data/live/events.jsonl` for the recent raw stream.
- `data/live/daily/YYYY-MM-DD.jsonl` for recent day replay.
- `data/live/commands/GATEWAY.jsonl` for gateway command queues.
- `data/live/telemetry.sqlite` as the separate replay database.
- `data/live/registry.json` for gateway/device UI preferences.

The JSONL files rotate before unbounded growth. Tune `VEIL_LIVE_JSONL_MAX_BYTES`,
`VEIL_LIVE_JSONL_GENERATIONS`, `VEIL_LIVE_COMMAND_JSONL_MAX_BYTES`,
`VEIL_LIVE_COMMAND_JSONL_GENERATIONS`, `VEIL_LIVE_LATEST_MAX_LINES`,
`VEIL_LIVE_HISTORY_MAX_LINES`, and the corresponding `*_TAIL_MAX_BYTES` values
when a larger or smaller recent replay window is needed.

The live stream does not write every location into `twin.gpkg`. Use the
Telemetry replay bar's `Append to twin store` button when a day or snapshot is
worth materializing for later querying.

The MCP server also exposes live telemetry directly to models:

- `live_telemetry_snapshot` reads current gateways, bridge status, devices,
  latest positions/messages, and freshness.
- `live_telemetry_history` reads raw events from `data/live/telemetry.sqlite`.
- `live_telemetry_store_summary` summarizes days, counts, devices, and exports.
- `discover_live_connections`, `manage_live_gateway`, and `manage_live_device`
  expose the same gateway/device management path as the UI.
- `export_live_telemetry_to_twin` materializes selected live events as
  `live_device` entities in `twin.gpkg`.

## Meshtastic Bridge

The normal path is the viewer UI:

1. Start VEIL with `npm start`.
2. Open the `Telemetry` panel under Ask G.A.I.A.
3. In `Telemetry > Meshtastic / LoRa`, enter a gateway name.
4. Pick Bluetooth, Serial, or Internet.
5. For Bluetooth or Serial, click `Scan` / `Refresh` and choose the discovered
   device from the dropdown. If discovery finds exactly one device, the UI can
   use it automatically. You can still paste a Bluetooth address or serial port
   manually.
6. For Internet, enter the Meshtastic TCP host or URL.
7. Click `Register & Connect Gateway`.

That UI request writes the gateway registration and asks `server.js` to start a
managed `scripts/live/meshtastic_serial_bridge.py` process using
`.venv-live/bin/python`. Incoming tracker packets then appear under
`Registered Gateway Device: <name>` after the tracker has been named/added.
Use `Discovery` mode in the live panel to inspect other observed Meshtastic
nodes, then `Add` only the devices you want shown on the map/key in normal
`Configured` mode.

For Meshtastic packets, `source.transport` is the radio path (`lora`). The
computer-to-gateway connection is recorded separately as
`source.ingress_transport` (`bluetooth`, `serial`, or `internet`). The gateway
radio itself is filtered out of the tracked-device list when its Meshtastic node
id can be detected; for BLE, the bridge also derives that node id from the
Bluetooth address as a fallback.

Use `Remove Gateway` to stop that gateway bridge and remove its current child
devices from the live menu. Use a device row's `Remove` button to clear just
that tracked device from the configured live registry; it will not reappear on
the normal map/key unless you add it again. Raw event logs and discovery-mode
observations remain in the rotated JSONL window and the SQLite telemetry store.

Use a device row's `Fix` button to request a Meshtastic position response from
that tracker. VEIL sends the command to the already-connected gateway bridge
(Bluetooth, serial, or internet); the gateway relays a LoRa `POSITION_APP`
request to the tracker with `wantResponse=True`. This is not a direct Bluetooth
connection from the computer to the tracker. The request cannot guarantee a new
GPS lock: sleeping nodes, out-of-range nodes, device configuration, or missing
GPS signal can still produce no response or only a last-known position.

Install/update the live bridge dependencies:

```bash
python3 -m venv .venv-live
. .venv-live/bin/activate
pip install -r requirements-live.txt
```

The server uses `.venv-live/bin/python` automatically. Override it with
`VEIL_LIVE_PYTHON=/path/to/python` if needed.

If the live server uses an environment token, run manual bridge/replay commands
with the same `VEIL_LIVE_TOKEN` or pass `--token <token>`. Managed bridges
started by `server.js` receive the configured live token automatically.

The terminal commands below are optional debug/manual mode. They are not
required for normal UI registration.

Bluetooth gateway:

```bash
python3 scripts/live/meshtastic_serial_bridge.py \
  --transport bluetooth \
  --address AA:BB:CC:DD:EE:FF \
  --gateway-name "Field gateway" \
  --register
```

Serial gateway:

```bash
python3 scripts/live/meshtastic_serial_bridge.py \
  --transport serial \
  --port /dev/ttyUSB0 \
  --gateway-name "USB gateway" \
  --register
```

TCP/IP gateway:

```bash
python3 scripts/live/meshtastic_serial_bridge.py \
  --transport internet \
  --host 192.168.1.42 \
  --gateway-name "Network gateway" \
  --register
```

Run a demo stream:

```bash
python3 scripts/live/replay_demo.py --count 120 --interval 1
```
