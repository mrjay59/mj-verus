#!/usr/bin/env python3
"""
bridgeservice.py (improved)
- send_sms supports sim selection (sim=0 SIM1, sim=1 SIM2)
- heartbeat every 30 minutes sends device profile + screenshot (base64)
"""
import os
import subprocess
import json
import threading
import time
import uuid
import re
import base64
from datetime import datetime

# websocket-client
try:
    import websocket
except Exception:
    print("websocket-client is required. pip install websocket-client")
    raise

# optional libs
try:
    import uiautomator2 as u2
    UIAUTOMATOR2_AVAILABLE = True
except Exception:
    UIAUTOMATOR2_AVAILABLE = False

try:
    import adbutils
    ADBUTILS_AVAILABLE = True
except Exception:
    ADBUTILS_AVAILABLE = False

# config
WS_SERVER = os.environ.get("BRIDGE_WS", "wss://s14223.blr1.piesocket.com/v3/1?api_key=WVXN94EfJrQO7fSpSwwKJZgxbavdLdKLZBPLLlQR&notify_self=1")
POLL_SMS_INTERVAL = 3
HEARTBEAT_INTERVAL = 30 * 60  # seconds

def run_cmd(cmd, capture=True):
    if isinstance(cmd, (list, tuple)):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    else:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    out, err = proc.communicate()
    if capture:
        return out.decode('utf-8', errors='ignore'), err.decode('utf-8', errors='ignore')
    return None, None

# Device helpers
def get_local_ip():
    try:
        out = os.popen("ip -f inet addr show wlan0").read()
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        s = os.popen("ip route get 8.8.8.8 | awk '{print $7; exit}'").read().strip()
        if s:
            return s
    except Exception:
        pass
    return "0.0.0.0"

def get_device_info(adb):
    try:
        brand = adb.shell("getprop ro.product.manufacturer").strip()
        model = adb.shell("getprop ro.product.model").strip()
        android = adb.shell("getprop ro.build.version.release").strip()
        return {"brand": brand, "model": model, "android": android}
    except Exception:
        return {}

def get_serial(adb):
    try:
        s = adb.shell("getprop ro.serialno").strip()
        if not s:
            s = adb.shell("getprop ro.boot.serialno").strip()
        return s if s else None
    except Exception:
        return None

def get_imei(adb, slot=0):
    try:
        out = adb.shell(f"service call iphonesubinfo {1+slot}").strip()
        matches = re.findall(r"'(.*?)'", out)
        imei = "".join(matches).replace(".", "").replace(" ", "")
        return imei if imei else None
    except Exception:
        return None

def get_sim_info(adb, slot=0):
    info = {}
    try:
        # try dumpsys subscription
        out, _ = run_cmd(["adb", "shell", "dumpsys subscription"])
        if out:
            parts = out.split("\n\n")
            for p in parts:
                if f"slotIndex={slot}" in p or f"mSimSlotIndex={slot}" in p:
                    m_car = re.search(r"carrier=(\S+)", p)
                    m_num = re.search(r"number=(\S+)", p)
                    if m_car:
                        info['operator'] = m_car.group(1)
                    if m_num:
                        info['number'] = m_num.group(1)
                    break
        # fallback telephony registry
        if 'operator' not in info or 'number' not in info:
            out = adb.shell(f"dumpsys telephony.registry | grep mSimSlotIndex={slot} -A 20")
            m_op = re.search(r"operatorAlphaLong=([^\s]+)", out)
            m_num = re.search(r"mLine1Number=([^\s]+)", out)
            if m_op and 'operator' not in info:
                info['operator'] = m_op.group(1)
            if m_num and 'number' not in info:
                info['number'] = m_num.group(1)
    except Exception:
        pass
    return info

def capture_screenshot(adb):
    """Capture screenshot, return base64 PNG or None."""
    remote = "/sdcard/bridgeservice_screenshot.png"
    local = "/data/data/com.termux/files/home/bridgeservice/bridgeservice_screenshot.png"
    try:
        # prefer adb approach
        try:
            adb.shell(f"screencap -p {remote}")
            # try pull
            try:
                adb.pull(remote, local)
            except Exception:
                pass
            # if local exists read
            if os.path.exists(local):
                with open(local, "rb") as f:
                    return base64.b64encode(f.read()).decode("ascii")
            # try cat remote
            out = adb.shell(f"cat {remote}")
            if out:
                if isinstance(out, str):
                    b = out.encode("latin1")
                else:
                    b = out
                return base64.b64encode(b).decode("ascii")
        except Exception:
            pass
        # fallback: direct screencap command (on device) then read
        run_cmd(["screencap", "-p", remote])
        if os.path.exists(remote):
            with open(remote, "rb") as f:
                data = f.read()
            # ensure local copy
            try:
                open(local, "wb").write(data)
            except Exception:
                pass
            return base64.b64encode(data).decode("ascii")
    except Exception as e:
        print("capture_screenshot error:", e)
    return None

# SMS Handler
class SMSHandler:
    def __init__(self, ws, adb):
        self.ws = ws
        self.adb = adb
        self.last_seen_ids = set()

    def list_sms(self):
        try:
            out, _ = run_cmd(["termux-sms-list"])
            return json.loads(out) if out else []
        except Exception:
            try:
                out, _ = run_cmd(["termux-sms-list", "--limit", "50"])
                return json.loads(out)
            except Exception:
                return []

    def send_sms(self, number, text, sim=0):
        """Try send via adb intent with SIM extras; fallback to termux-sms-send"""
        try:
            sim_index = int(sim) if sim is not None else 0
        except Exception:
            sim_index = 0
        ok = False
        # Try ADB intent (may require default messaging app handles extras)
        intents = [
            f'am start -a android.intent.action.SENDTO -d sms:{number} --es sms_body "{text}" --ei android.telecom.extra.SIM_SLOT_INDEX {sim_index}',
            f'am start -a android.intent.action.SENDTO -d sms:{number} --es sms_body "{text}" --ei simSlot {sim_index} --ei subscription {sim_index} --ei com.android.phone.extra.slot {sim_index}'
        ]
        for it in intents:
            try:
                self.adb.shell(it)
                ok = True
                break
            except Exception:
                ok = False
        if not ok:
            # fallback to termux-sms-send (no sim selection)
            try:
                run_cmd(["termux-sms-send", "-n", number, text], capture=False)
                ok = True
            except Exception as e:
                print("send_sms fallback error:", e)
                ok = False
        return ok

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
            except Exception as e:
                print("SMSHandler.poll error:", e)
            time.sleep(POLL_SMS_INTERVAL)

# Adb wrapper
class AdbWrapper:
    def __init__(self):
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
        out, _ = run_cmd(["adb", "shell", cmd])
        return out

    def push(self, local, remote):
        if self.adb_client:
            return self.adb_client.push(local, remote)
        out, _ = run_cmd(["adb", "push", local, remote])
        return out

    def pull(self, remote, local):
        if self.adb_client:
            return self.adb_client.pull(remote, local)
        out, _ = run_cmd(["adb", "pull", remote, local])
        return out

# minimal UIAuto (kept basic; same approaches as before)
class UIAuto:
    def __init__(self, adb: AdbWrapper=None):
        self.adb = adb or AdbWrapper()
        self.u2 = None
        if UIAUTOMATOR2_AVAILABLE:
            try:
                self.u2 = u2.connect()
            except Exception:
                self.u2 = None

    # ... implement dump_ui_xml, find_by_text, find_by_resource_id, click_node as earlier ...
    def dump_ui_xml(self):
        remote = "/sdcard/window_dump.xml"
        try:
            run_cmd(["uiautomator", "dump", remote])
        except Exception:
            run_cmd(["adb", "shell", "uiautomator", "dump", remote])
        local_tmp = "/data/data/com.termux/files/home/bridgeservice/window_dump.xml"
        try:
            run_cmd(["adb", "pull", remote, local_tmp])
            with open(local_tmp, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            out, _ = run_cmd(["adb", "shell", "cat", remote])
            return out

    def parse_xml_search(self, xml_text, attr_key, attr_val):
        nodes = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for node in root.iter('node'):
                if node.attrib.get(attr_key) == attr_val or attr_val in node.attrib.get(attr_key, ""):
                    nodes.append(node.attrib)
        except Exception:
            parts = xml_text.split("<node ")
            for p in parts:
                if f'{attr_key}="{attr_val}"' in p:
                    d = {}
                    for token in p.split():
                        if '=' in token:
                            k, v = token.split('=', 1)
                            v = v.strip().strip('"').strip("'>")
                            d[k] = v
                    nodes.append(d)
        return nodes

    def find_by_text(self, text):
        if self.u2:
            try:
                e = self.u2(text=text)
                if e.exists:
                    return {"resource-id": getattr(e, "info").get("resourceName", None),
                            "text": e.info.get("text"), "bounds": e.info.get("bounds", None)}
            except Exception:
                pass
        xml = self.dump_ui_xml()
        nodes = self.parse_xml_search(xml, "text", text)
        return nodes[0] if nodes else None

    def find_by_resource_id(self, rid):
        if self.u2:
            try:
                e = self.u2(resourceId=rid)
                if e.exists:
                    return {"resource-id": rid, "text": e.info.get("text"), "bounds": e.info.get("bounds", None)}
            except Exception:
                pass
        xml = self.dump_ui_xml()
        nodes = self.parse_xml_search(xml, "resource-id", rid)
        return nodes[0] if nodes else None

    def click_node(self, node):
        bounds = node.get("bounds")
        if isinstance(bounds, dict):
            try:
                cx = int((bounds['left'] + bounds['right'])/2)
                cy = int((bounds['top'] + bounds['bottom'])/2)
                run_cmd(["adb", "shell", "input", "tap", str(cx), str(cy)])
                return True
            except Exception:
                pass
        if isinstance(bounds, str):
            m = re.findall(r'\[(-?\d+),(-?\d+)\]', bounds)
            if len(m) >= 2:
                l, t = map(int, m[0])
                r, b = map(int, m[1])
                cx = (l + r)//2
                cy = (t + b)//2
                run_cmd(["adb", "shell", "input", "tap", str(cx), str(cy)])
                return True
        if self.u2 and node.get("resource-id"):
            try:
                e = self.u2(resourceId=node.get("resource-id"))
                if e.exists:
                    e.click()
                    return True
            except Exception:
                pass
        return False

# WebSocket client
class WSClient:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.adb = AdbWrapper()
        self.uia = UIAuto(self.adb)
        self._stop = threading.Event()
        self.sms_handler = None
        self.handlers = {}

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        hb = threading.Thread(target=self.heartbeat_loop, daemon=True)
        hb.start()

    def stop(self):
        self._stop.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def send(self, data):
        try:
            if self.ws:
                self.ws.send(json.dumps(data))
        except Exception:
            pass

    def heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                device_info = get_device_info(self.adb)
                serial = get_serial(self.adb)
                ip_local = get_local_ip()
                sim1 = {"imei": get_imei(self.adb, 0), **get_sim_info(self.adb, 0)}
                sim2 = {"imei": get_imei(self.adb, 1), **get_sim_info(self.adb, 1)}
                screenshot = capture_screenshot(self.adb)
                payload = {
                    "type": "heartbeat",
                    "id": str(uuid.uuid4()),
                    "time": datetime.utcnow().isoformat() + "Z",
                    "info": {
                        "platform": "termux",
                        "device": device_info,
                        "serial": serial,
                        "ip_local": ip_local,
                        "sims": [sim1, sim2]
                    },
                    "screenshot": screenshot
                }
                self.send(payload)
            except Exception as e:
                print("heartbeat error:", e)
            # sleep HEARTBEAT_INTERVAL seconds, but responsive to stop
            for _ in range(HEARTBEAT_INTERVAL):
                if self._stop.is_set():
                    break
                time.sleep(1)

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
            sim = data.get("sim", 0)
            ok = False
            if self.sms_handler:
                ok = self.sms_handler.send_sms(number, text, sim)
            self.send({"type": "send_sms_result", "ok": ok, "id": msg.get("id")})
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
                except Exception as e:
                    self.send({"type":"error","msg": str(e),"id": msg.get("id")})
            self.send({"type":"open_app_result","ok": ok,"id": msg.get("id")})
        else:
            h = self.handlers.get(action)
            if h:
                try:
                    h(data, msg)
                except Exception as e:
                    self.send({"type":"error","msg": str(e),"id": msg.get("id")})

    def _on_open(self, ws):
        print("WS connected to", self.url)
        device_info = get_device_info(self.adb)
        ip_local = get_local_ip()
        serial = get_serial(self.adb)
        sim1 = {"imei": get_imei(self.adb,0), **get_sim_info(self.adb,0)}
        sim2 = {"imei": get_imei(self.adb,1), **get_sim_info(self.adb,1)}
        profile = {"platform":"termux","device": device_info,"serial": serial,"ip_local": ip_local,"sims":[sim1,sim2]}
        self.send({"type":"bridge_hello","id": str(uuid.uuid4()),"info": profile})

    def _on_close(self, ws, code, msg):
        print("WS closed", code, msg)

    def _on_error(self, ws, err):
        print("WS error:", err)

    def _run(self):
        while not self._stop.is_set():
            try:
                print("Connecting to WS:", self.url)
                self.ws = websocket.WebSocketApp(self.url,
                    on_message=self._on_message,
                    on_open=self._on_open,
                    on_close=self._on_close,
                    on_error=self._on_error)
                self.ws.run_forever()
            except Exception as e:
                print("WS run_forever exception:", e)
            print("WS disconnected, retrying in 5s...")
            time.sleep(5)

# main
def main():
    client = WSClient(WS_SERVER)
    sms = SMSHandler(client, client.adb)
    client.sms_handler = sms
    client.start()
    t_sms = threading.Thread(target=sms.poll_loop, daemon=True)
    t_sms.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        client.stop()

if __name__ == "__main__":
    main()
