#!/usr/bin/env python3
"""
bridgeservice.py (improved)
Bridge service for Termux:
- Poll incoming SMS (termux-sms-list) and forward to websocket server
- Send SMS on request
- WebSocket client with simple JSON protocol
- ADB wrapper & UI automation helper (uiautomator2 if available, otherwise fallback to uiautomator dump parser)
- Collects device profile: brand, model, android version, local IP, IMEI (SIM1 & SIM2), SIM info, serial number
"""

import os
import sys
import subprocess
import json
import threading
import time
import uuid
import traceback
import re

# websocket-client
try:
    import websocket
except Exception:
    print("websocket-client is required. pip install websocket-client")
    raise

# Try importing uiautomator2 (optional, recommended)
try:
    import uiautomator2 as u2
    UIAUTOMATOR2_AVAILABLE = True
except Exception:
    UIAUTOMATOR2_AVAILABLE = False

# adbutils (optional wrapper) - else fallback to subprocess adb
try:
    import adbutils
    ADBUTILS_AVAILABLE = True
except Exception:
    ADBUTILS_AVAILABLE = False

# Configuration (edit as needed)
WS_SERVER = os.environ.get("BRIDGE_WS", "wss://s14223.blr1.piesocket.com/v3/1?api_key=WVXN94EfJrQO7fSpSwwKJZgxbavdLdKLZBPLLlQR&notify_self=1")
POLL_SMS_INTERVAL = 3
POLL_UI_INTERVAL = 2

# --- Utilities ---
def run_cmd(cmd, capture=True, check=False):
    if isinstance(cmd, (list, tuple)):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    else:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    out, err = proc.communicate()
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out=out, stderr=err)
    if capture:
        return out.decode('utf-8', errors='ignore'), err.decode('utf-8', errors='ignore')
    return None, None

# --- Device Info helpers ---
def get_local_ip():
    try:
        out = os.popen("ip -f inet addr show wlan0").read()
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "0.0.0.0"

def get_device_info(adb):
    try:
        brand = adb.shell("getprop ro.product.manufacturer").strip()
        model = adb.shell("getprop ro.product.model").strip()
        release = adb.shell("getprop ro.build.version.release").strip()
        return {"brand": brand, "model": model, "android": release}
    except Exception:
        return {}

def get_serial(adb):
    try:
        serial = adb.shell("getprop ro.serialno").strip()
        return serial if serial else None
    except Exception:
        return None

def get_imei(adb, slot=0):
    try:
        out = adb.shell(f"service call iphonesubinfo {1 + slot}").strip()
        matches = re.findall(r"'(.*?)'", out)
        imei = "".join(matches).replace(".", "").replace(" ", "")
        return imei if imei else None
    except Exception:
        return None

def get_sim_info(adb, slot=0):
    info = {}
    try:
        out = adb.shell(f"dumpsys telephony.registry | grep mSimSlotIndex={slot} -A 20")
        m_op = re.search(r"operatorAlphaLong=([^\s]+)", out)
        m_num = re.search(r"mLine1Number=([^\s]+)", out)
        if m_op:
            info["operator"] = m_op.group(1)
        if m_num:
            info["number"] = m_num.group(1)
    except Exception:
        pass
    return info

# --- SMS Handler ---
class SMSHandler:
    def __init__(self, ws):
        self.ws = ws
        self.last_seen_ids = set()
        self.lock = threading.Lock()

    def list_sms(self):
        try:
            out, err = run_cmd(["termux-sms-list"], capture=True)
            items = json.loads(out) if out else []
            return items
        except Exception:
            try:
                out, _ = run_cmd(["termux-sms-list", "--limit", "50"])
                return json.loads(out)
            except Exception:
                return []

    def send_sms(self, number, text):
        try:
            run_cmd(["termux-sms-send", "-n", number, text], capture=False)
            return True
        except Exception as ex:
            print("send_sms error:", ex)
            return False

    def poll_loop(self):
        while True:
            try:
                msgs = self.list_sms()
                new = []
                for m in msgs:
                    mid = m.get('id') or m.get('date') or m.get('timestamp') or json.dumps(m)
                    if mid not in self.last_seen_ids:
                        new.append(m)
                        self.last_seen_ids.add(mid)
                if new:
                    for m in reversed(new):
                        payload = {"type": "sms_received", "data": m}
                        try:
                            self.ws.send(json.dumps(payload))
                        except Exception:
                            pass
                if len(self.last_seen_ids) > 1000:
                    self.last_seen_ids = set(list(self.last_seen_ids)[-500:])
            except Exception as ex:
                print("SMSHandler.poll error:", ex)
            time.sleep(POLL_SMS_INTERVAL)

# --- ADB / UI automation wrapper ---
class AdbWrapper:
    def __init__(self):
        self.use_adbutils = ADBUTILS_AVAILABLE
        self.adb_client = None
        if ADBUTILS_AVAILABLE:
            try:
                self.adb_client = adbutils.adb.device()
            except Exception:
                self.adb_client = None

    def shell(self, cmd):
        if self.adb_client:
            try:
                return self.adb_client.shell(cmd)
            except Exception:
                pass
        out, err = run_cmd(["adb", "shell", cmd])
        return out

    def push(self, local, remote):
        if self.adb_client:
            return self.adb_client.push(local, remote)
        out, err = run_cmd(["adb", "push", local, remote])
        return out

    def pull(self, remote, local):
        if self.adb_client:
            return self.adb_client.pull(remote, local)
        out, err = run_cmd(["adb", "pull", remote, local])
        return out

# --- UIAuto omitted for brevity (unchanged) ---
# (keep your existing UIAuto class)

# --- WebSocket Client ---
class WSClient:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self._connect_lock = threading.Lock()
        self.handlers = {}
        self.sms_handler = None
        self.adb = AdbWrapper()
        self.uia = UIAuto(self.adb)

    def start(self):
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def send(self, data):
        try:
            if self.ws:
                self.ws.send(json.dumps(data))
        except Exception:
            pass

    def _on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            print("Received non-json message:", message)
            return
        action = msg.get("action")
        data = msg.get("data", {})
        if action == "send_sms":
            number = data.get("number")
            text = data.get("text")
            ok = False
            if self.sms_handler:
                ok = self.sms_handler.send_sms(number, text)
            self.send({"type": "send_sms_result", "ok": ok, "id": msg.get("id")})
        elif action == "find_ui":
            by = data.get("by")
            value = data.get("value")
            res = None
            if by == "text":
                res = self.uia.find_by_text(value)
            elif by == "resource-id":
                res = self.uia.find_by_resource_id(value)
            self.send({"type": "find_ui_result", "result": res, "id": msg.get("id")})
        elif action == "click_ui":
            node = data.get("node")
            ok = self.uia.click_node(node or {})
            self.send({"type": "click_ui_result", "ok": ok, "id": msg.get("id")})
        elif action == "adb_shell":
            cmd = data.get("cmd")
            out = self.adb.shell(cmd)
            self.send({"type": "adb_shell_result", "out": out, "id": msg.get("id")})
        elif action == "open_app":
            package = data.get("package")
            ok = False
            if package:
                try:
                    self.adb.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
                    ok = True
                except Exception as ex:
                    self.send({"type": "error", "msg": str(ex), "id": msg.get("id")})
            self.send({"type": "open_app_result", "ok": ok, "id": msg.get("id")})
        else:
            h = self.handlers.get(action)
            if h:
                try:
                    h(data, msg)
                except Exception as ex:
                    self.send({"type": "error", "msg": str(ex), "id": msg.get("id")})

    def _on_open(self, ws):
        print("WS connected to", self.url)
        device_info = get_device_info(self.adb)
        ip_local = get_local_ip()
        serial = get_serial(self.adb)

        sim1 = {"imei": get_imei(self.adb, 0), **get_sim_info(self.adb, 0)}
        sim2 = {"imei": get_imei(self.adb, 1), **get_sim_info(self.adb, 1)}

        profile = {
            "platform": "termux",
            "device": device_info,
            "serial": serial,
            "ip_local": ip_local,
            "sims": [sim1, sim2]
        }

        self.send({
            "type": "bridge_hello",
            "id": str(uuid.uuid4()),
            "info": profile
        })

    def _on_close(self, ws, close_status_code, close_msg):
        print("WS closed", close_status_code, close_msg)

    def _on_error(self, ws, err):
        print("WS error:", err)

    def _run(self):
        while True:
            try:
                print("Connecting to WS:", self.url)
                self.ws = websocket.WebSocketApp(self.url,
                                                 on_message=self._on_message,
                                                 on_open=self._on_open,
                                                 on_close=self._on_close,
                                                 on_error=self._on_error)
                self.ws.run_forever()
            except Exception as ex:
                print("WS run_forever exception:", ex)
            print("WS disconnected, retry in 5s...")
            time.sleep(5)

# --- Main ---
def main():
    ws_client = WSClient(WS_SERVER)
    sms = SMSHandler(ws_client)
    ws_client.sms_handler = sms
    ws_client.start()
    t_sms = threading.Thread(target=sms.poll_loop, daemon=True)
    t_sms.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")

if __name__ == "__main__":
    main()
