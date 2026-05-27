#!/usr/bin/env python3
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion


MQTT_BROKER = os.environ.get("HA_MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.environ.get("HA_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("HA_MQTT_USER", "")
MQTT_PASS = os.environ.get("HA_MQTT_PASS", "")
TOPIC_PREFIX = os.environ.get("PANDA_MQTT_TOPIC_PREFIX", "panda_breath_mod")
WEB_PORT = int(os.environ.get("PANDA_WEB_INTERNAL_PORT", "8080"))

state_lock = threading.Lock()
state = {
    "mqtt_connected": False,
    "last_seen": None,
    "topics": {},
    "logs": [],
}


COMMAND_TOPICS = {
    "auto": ("auto/set", "PRESS"),
    "manual": ("manual/set", "PRESS"),
    "dry": ("drying/set", "PRESS"),
    "stop": ("heizung_stop/set", "PRESS"),
    "unlock": ("unlock/set", "PRESS"),
    "power_on": ("panda_power/set", "ON"),
    "power_off": ("panda_power/set", "OFF"),
    "slicer_on": ("slicer_priority_mode/set", "ON"),
    "slicer_off": ("slicer_priority_mode/set", "OFF"),
}

NUMBER_TOPICS = {
    "soll": "soll/set",
    "limit": "limit/set",
    "filtertemp": "filtertemp/set",
    "dry_temp": "dry_temp/set",
    "dry_time": "dry_time/set",
}


def topic_path(suffix):
    return f"{TOPIC_PREFIX}/{suffix}"


def update_topic(topic, payload):
    key = topic[len(TOPIC_PREFIX) + 1 :] if topic.startswith(f"{TOPIC_PREFIX}/") else topic
    with state_lock:
        state["topics"][key] = payload
        state["last_seen"] = time.time()
        if key == "log":
            state["logs"].append(payload)
            state["logs"] = state["logs"][-200:]


def on_connect(client, userdata, flags, reason_code, properties=None):
    connected = not reason_code.is_failure
    with state_lock:
        state["mqtt_connected"] = connected
    if connected:
        client.subscribe(topic_path("#"))


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
    with state_lock:
        state["mqtt_connected"] = False


def on_message(client, userdata, msg):
    update_topic(msg.topic, msg.payload.decode(errors="replace"))


mqtt_client = mqtt.Client(
    callback_api_version=CallbackAPIVersion.VERSION2,
    client_id=f"PandaWeb_{os.getpid()}",
)
if MQTT_USER or MQTT_PASS:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.on_connect = on_connect
mqtt_client.on_disconnect = on_disconnect
mqtt_client.on_message = on_message


def start_mqtt():
    while True:
        try:
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_forever(retry_first_connection=True)
        except Exception:
            with state_lock:
                state["mqtt_connected"] = False
            time.sleep(5)


def publish_command(name, value=None):
    if name in COMMAND_TOPICS:
        suffix, payload = COMMAND_TOPICS[name]
    elif name in NUMBER_TOPICS:
        suffix = NUMBER_TOPICS[name]
        payload = str(value)
    else:
        raise ValueError("unknown command")
    mqtt_client.publish(topic_path(suffix), payload, qos=1, retain=False)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Panda Breath Control</title>
  <style>
    :root { color-scheme: dark; font-family: Inter, system-ui, sans-serif; background: #151719; color: #f4f6f8; }
    .hidden { display: none !important; }
    body { margin: 0; background: #151719; }
    header { height: 48px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; background: #25272b; border-bottom: 1px solid #3a3d42; }
    h1 { font-size: 16px; margin: 0; font-weight: 700; }
    main { padding: 16px; display: grid; gap: 16px; }
    .status { display: flex; gap: 8px; align-items: center; font-weight: 700; }
    .dot { width: 12px; height: 12px; border-radius: 50%; background: #8f1d1d; }
    .dot.on { background: #42b84f; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 8px; }
    .tile { border: 1px solid #44484f; border-radius: 6px; padding: 14px; background: #1e2023; min-height: 58px; }
    .tile label { display: block; color: #aeb6c1; font-size: 12px; margin-bottom: 7px; }
    .tile strong { font-size: 22px; line-height: 1.1; }
    .controls { display: grid; grid-template-columns: repeat(4, minmax(130px, 1fr)); gap: 8px; }
    button { border: 1px solid #4a4f57; border-radius: 6px; background: #292c31; color: #fff; height: 42px; font-weight: 700; cursor: pointer; }
    button.primary { background: #1d7b3b; border-color: #249247; }
    button.danger { background: #8f2222; border-color: #aa2a2a; }
    .numbers { display: grid; grid-template-columns: 190px 1fr 150px; gap: 8px; align-items: center; }
    input { height: 38px; border-radius: 5px; border: 1px solid #4a4f57; background: #1e2023; color: #fff; padding: 0 10px; font-size: 15px; }
    pre { margin: 0; min-height: 130px; max-height: 260px; overflow: auto; padding: 12px; background: #08090a; border: 1px solid #33373d; border-radius: 6px; color: #d7e1ed; }
    @media (max-width: 820px) { .grid, .controls { grid-template-columns: repeat(2, minmax(0, 1fr)); } .numbers { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>Panda Breath Control</h1>
    <div style="display:flex;gap:12px;align-items:center">
      <button id="helpToggle" style="height:32px;padding:0 12px;font-size:13px" onclick="document.getElementById('helpPanel').classList.toggle('hidden')">? Help</button>
      <div class="status"><span id="mqttDot" class="dot"></span><span id="mqttText">MQTT</span></div>
    </div>
  </header>
  <div id="helpPanel" class="hidden" style="background:#1a1d21;border-bottom:1px solid #3a3d42;padding:16px 20px;font-size:13px;line-height:1.7;color:#c8d0db">
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px">
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Mode Buttons</strong>
        <b>Auto</b> — Device controls heating automatically. It reads the chamber temp and runs the heater until the <em>Chamber Target</em> is reached, then holds it. This is the normal operating mode during a print.<br><br>
        <b>Manual</b> — Heater runs continuously at the set target. No automatic shutoff based on temp. Use for preheat or when you want direct control.<br><br>
        <b>Dry</b> — Filament drying mode. Runs at <em>Dry Temp</em> for <em>Dry Time</em> minutes, then shuts off automatically.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Power &amp; Safety</strong>
        <b>Power On / Off</b> — Controls the physical relay powering the heating element. Turn off to cut power entirely regardless of mode.<br><br>
        <b>Stop Heat</b> — Emergency stop. Immediately halts heating and resets the target to 20°C. This also activates a safety lock.<br><br>
        <b>Unlock</b> — Clears the safety lock set by Stop Heat before normal operation can resume.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Number Controls</strong>
        <b>Chamber Target</b> — Target temperature (°C) for the enclosure in Auto or Manual mode. Edit and press Send.<br><br>
        <b>Bed Limit</b> — If the bed temperature exceeds this value the heater will not activate. Prevents overheating when printing high-temp materials.<br><br>
        <b>Filter Fan Start</b> — Temperature at which the exhaust/filter fan kicks on automatically.<br><br>
        <b>Dry Temp / Dry Time</b> — Temperature (°C) and duration (minutes) used when Dry mode is activated.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Status Tiles</strong>
        <b>Status</b> — Current activity: Done, Heating…, Ready, etc.<br><br>
        <b>Chamber</b> — Live chamber temperature from the Panda Breath sensor.<br><br>
        <b>Bed</b> — Bed temperature. With CC2: sourced from the CC2 printer live. With Klipper: sourced from Home Assistant via the HA REST API.<br><br>
        <b>Heat</b> — Whether the heating element relay is ON or OFF right now.<br><br>
        <b>Mode</b> — Current operating mode (Automatic / Manual / Dry).<br><br>
        <b>CC2 Nozzle / Print / Progress</b> — Live metrics from the Elegoo CC2. Only populated when the cc2_backend service is running.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Slicer Priority — CC2 (Automatic)</strong>
        <b>How it works</b> — Enable <b>Slicer Priority Mode</b> in Home Assistant and set the Panda to <b>Auto</b> mode. When a print starts, the cc2_backend reads the active AMS tray's filament type directly from the CC2 and the Panda backend automatically sets the Chamber Target. No OrcaSlicer changes needed.<br><br>
        <b>Default filament → chamber temp:</b><br>
        PLA / PLA+ / TPU / TPE → 0°C (off)<br>
        PETG → 35°C<br>
        ABS / ASA → 55°C<br>
        PA → 65°C &nbsp;|&nbsp; PA-CF / PA12-CF / PC → 70°C<br>
        PC-ABS → 65°C<br><br>
        <b>Custom overrides</b> — Set the <code>CC2_FILAMENT_MAP</code> env var as JSON to override any value, e.g. <code>&#123;"PETG":"40","ABS":"60"&#125;</code>. Unmapped filaments fall back to the defaults above.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Slicer Priority — Klipper</strong>
        <b>How it works</b> — When <b>Slicer Priority</b> is ON, the backend polls Moonraker for M191/M141 commands in the active G-code. When detected, it sets the Chamber Target automatically.<br><br>
        <b>OrcaSlicer setup</b> — In OrcaSlicer go to <em>Filament → Custom G-code → Start G-code</em> and add <code>M191 S[chamber_temperature]</code>. Set your desired chamber temp per filament profile under <em>Filament → Temperature → Chamber</em>. OrcaSlicer embeds the temp in the G-code and the backend picks it up automatically.<br><br>
        <b>Slicer On / Off buttons</b> — Toggle whether slicer-detected temps override the manual Chamber Target.
      </div>
      <div>
        <strong style="color:#fff;display:block;margin-bottom:6px">Setup: CC2 vs Klipper</strong>
        <b>Klipper/Moonraker</b> — Run <code>docker-compose.yml</code>. Bed temp from Home Assistant. Slicer Priority uses M191/M141 from G-code. Requires HA MQTT integration and Mosquitto broker.<br><br>
        <b>Elegoo CC2</b> — Run <code>docker-compose.cc2.yml</code> as your only compose file. The cc2_backend connects directly to the CC2 and pushes all sensor data to HA via MQTT autodiscovery. Bed temp is live from the CC2. Slicer Priority works automatically via AMS tray filament detection — no G-code changes needed.<br><br>
        <b>To automate heating</b> — Set Panda to <b>Auto</b> mode and enable <b>Slicer Priority Mode</b> in HA. The heater will track the correct target for each filament type automatically when a print starts.
      </div>
    </div>
  </div>
  <main>
    <section class="grid" id="tiles"></section>
    <section class="controls">
      <button data-cmd="auto">Auto</button>
      <button data-cmd="manual">Manual</button>
      <button data-cmd="dry">Dry</button>
      <button data-cmd="slicer_on">Slicer On</button>
      <button data-cmd="slicer_off">Slicer Off</button>
      <button data-cmd="power_on">Power On</button>
      <button data-cmd="power_off">Power Off</button>
      <button class="danger" data-cmd="stop">Stop Heat</button>
      <button data-cmd="unlock">Unlock</button>
    </section>
    <section class="numbers">
      <label for="soll">Chamber Target</label><input id="soll" inputmode="decimal"><button data-send="soll">Send</button>
      <label for="limit">Bed Limit</label><input id="limit" inputmode="decimal"><button data-send="limit">Send</button>
      <label for="filtertemp">Filter Fan Start</label><input id="filtertemp" inputmode="decimal"><button data-send="filtertemp">Send</button>
      <label for="dry_temp">Dry Temp</label><input id="dry_temp" inputmode="decimal"><button data-send="dry_temp">Send</button>
      <label for="dry_time">Dry Time</label><input id="dry_time" inputmode="numeric"><button data-send="dry_time">Send</button>
    </section>
    <pre id="logs"></pre>
  </main>
  <script>
    const tileDefs = [
      ['status', 'Status'], ['bed', 'Bed'], ['ist', 'Chamber'], ['heizung', 'Heat'],
      ['fan', 'Fan'], ['panda_modus', 'Mode'], ['panda_power', 'Panda Power'], ['lock_status', 'Lock'],
      ['version', 'Version'], ['slicer_target_temp', 'Slicer Target'],
      ['cc2_nozzle_temp', 'CC2 Nozzle'], ['cc2_print_status', 'CC2 Print'], ['cc2_print_progress', 'CC2 Progress'],
    ];
    const tiles = document.getElementById('tiles');
    tiles.innerHTML = tileDefs.map(([key, label]) => `<div class="tile"><label>${label}</label><strong id="t_${key}">--</strong></div>`).join('');
    async function post(data) {
      await fetch('/api/command', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(data) });
    }
    document.querySelectorAll('[data-cmd]').forEach(btn => btn.onclick = () => post({ command: btn.dataset.cmd }));
    document.querySelectorAll('[data-send]').forEach(btn => btn.onclick = () => post({ command: btn.dataset.send, value: document.getElementById(btn.dataset.send).value }));
    function setActive(cmd, active) {
      const btn = document.querySelector(`[data-cmd="${cmd}"]`);
      if (btn) btn.classList.toggle('primary', active);
    }
    async function refresh() {
      const res = await fetch('/api/state');
      const data = await res.json();
      document.getElementById('mqttDot').classList.toggle('on', data.mqtt_connected);
      document.getElementById('mqttText').textContent = data.mqtt_connected ? 'MQTT connected' : 'MQTT disconnected';
      const topics = data.topics || {};
      for (const [key] of tileDefs) document.getElementById(`t_${key}`).textContent = topics[key] ?? '--';
      for (const key of ['soll', 'limit', 'filtertemp', 'dry_temp', 'dry_time']) {
        const input = document.getElementById(key);
        if (document.activeElement !== input && topics[key] !== undefined) input.value = topics[key];
      }
      // Mode buttons — mutually exclusive
      const mode = topics.panda_modus || '';
      setActive('auto',   mode === 'Automatic');
      setActive('manual', mode === 'Manual');
      setActive('dry',    mode === 'Dry');
      // Power buttons — mutually exclusive
      const power = topics.panda_power || '';
      setActive('power_on',  power === 'ON');
      setActive('power_off', power === 'OFF');
      // Slicer buttons — mutually exclusive
      const slicer = topics.slicer_priority_mode || '';
      setActive('slicer_on',  slicer === 'ON');
      setActive('slicer_off', slicer === 'OFF');
      document.getElementById('logs').textContent = (data.logs || []).join('\\n');
    }
    setInterval(refresh, 1500);
    refresh();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def send_html(self, include_body=True):
        body = INDEX_HTML.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_html()
            return
        if self.path == "/api/state":
            with state_lock:
                payload = {
                    "mqtt_connected": state["mqtt_connected"],
                    "last_seen": state["last_seen"],
                    "topics": dict(state["topics"]),
                    "logs": list(state["logs"]),
                }
            self.send_json(200, payload)
            return
        self.send_json(404, {"error": "not found"})

    def do_HEAD(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_html(include_body=False)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/api/command":
            self.send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode()
        ctype = self.headers.get("content-type", "")
        try:
            if "application/json" in ctype:
                data = json.loads(body or "{}")
            else:
                parsed = parse_qs(body)
                data = {key: values[-1] for key, values in parsed.items()}
            publish_command(data.get("command", ""), data.get("value"))
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    threading.Thread(target=start_mqtt, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print(f"Panda web control listening on 0.0.0.0:{WEB_PORT}")
    server.serve_forever()
