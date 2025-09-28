#!/usr/bin/env python3
"""
bridgeservice.py
Bridge service for Termux:
- Poll incoming SMS (termux-sms-list) and forward to websocket server
- Send SMS on request
- WebSocket client with simple JSON protocol
- ADB wrapper & UI automation helper (uiautomator2 if available, otherwise fallback to uiautomator dump parser)
"""

import os
import sys
import subprocess
import json
import threading
import time
import uuid
import traceback

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
WS_SERVER = os.environ.get("BRIDGE_WS", "ws://192.168.2.8:9000/ws")  # ganti ke server anda
POLL_SMS_INTERVAL = 3  # detik
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

# --- SMS Handler ---
class SMSHandler:
    def __init__(self, ws):
        self.ws = ws
        self.last_seen_ids = set()
        self.lock = threading.Lock()

    def list_sms(self):
        # termux-sms-list returns JSON array
        try:
            out, err = run_cmd(["termux-sms-list"], capture=True)
            items = json.loads(out) if out else []
            return items
        except Exception as ex:
            # fallback: try with --limit
            try:
                out, _ = run_cmd(["termux-sms-list", "--limit", "50"])
                return json.loads(out)
            except Exception:
                return []

    def send_sms(self, number, text):
        # termux-sms-send -n NUMBER TEXT
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
                # Each message often has 'date', 'address', 'body', 'type', 'id'
                new = []
                for m in msgs:
                    mid = m.get('id') or m.get('date') or m.get('timestamp') or json.dumps(m)
                    if mid not in self.last_seen_ids:
                        new.append(m)
                        self.last_seen_ids.add(mid)
                if new:
                    for m in reversed(new):  # oldest first
                        payload = {"type":"sms_received", "data": m}
                        try:
                            self.ws.send(json.dumps(payload))
                        except Exception:
                            pass
                # keep last_seen max-size to avoid memory grow
                if len(self.last_seen_ids) > 1000:
                    # drop oldest (no timestamp order guaranteed) — reset safetly
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
                self.adb_client = adbutils.adb.device()  # default device
            except Exception:
                self.adb_client = None

    def shell(self, cmd):
        if self.adb_client:
            try:
                return self.adb_client.shell(cmd)
            except Exception:
                pass
        # fallback to subprocess (assumes 'adb' binary available)
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

class UIAuto:
    """
    UI automation helper.
    Methods:
      - find_by_text(text) -> node dict or None
      - find_by_resource_id(rid) -> node dict or None
      - click_node(node) -> attempt click via adb input tap (if bounds available) or uiautomator2 click
    """
    def __init__(self, adb: AdbWrapper=None):
        self.adb = adb or AdbWrapper()
        self.u2 = None
        if UIAUTOMATOR2_AVAILABLE:
            try:
                # connect to local device
                self.u2 = u2.connect()  # may be '127.0.0.1:7912' if using atx-agent
            except Exception:
                self.u2 = None

    def dump_ui_xml(self):
        # use uiautomator dump
        remote = "/sdcard/window_dump.xml"
        try:
            out, err = run_cmd(["uiautomator", "dump", remote])
        except Exception:
            # may still work by running via adb
            run_cmd(["adb", "shell", "uiautomator", "dump", remote])
        # pull file
        local_tmp = "/data/data/com.termux/files/home/bridgeservice/window_dump.xml"
        try:
            run_cmd(["adb", "pull", remote, local_tmp])
            with open(local_tmp, "r", encoding="utf-8", errors="ignore") as f:
                data = f.read()
            return data
        except Exception:
            # try reading directly on device
            out, err = run_cmd(["adb", "shell", "cat", remote])
            return out

    def parse_xml_search(self, xml_text, attr_key, attr_val):
        # Simple heuristic parser: find nodes with attr_key="attr_val"
        # This is not full XML parsing but works for uiautomator dump output
        nodes = []
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)
            for node in root.iter('node'):
                if node.attrib.get(attr_key) == attr_val or attr_val in (node.attrib.get(attr_key,"")):
                    nodes.append(node.attrib)
        except Exception:
            # fallback crude text search
            parts = xml_text.split("<node ")
            for p in parts:
                if f'{attr_key}="{attr_val}"' in p:
                    # crude parse of bounds
                    d = {}
                    for token in p.split():
                        if '=' in token:
                            k,v = token.split('=',1)
                            v = v.strip().strip('"').strip("'>")
                            d[k] = v
                    nodes.append(d)
        return nodes

    def find_by_text(self, text):
        if self.u2:
            try:
                e = self.u2(text=text)
                if e.exists:
                    return {"resource-id": getattr(e,"info").get("resourceName", None),
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
        # node may contain 'bounds' like "[left,top][right,bottom]"
        bounds = node.get("bounds") or node.get("bounds")
        if isinstance(bounds, dict):  # from uiautomator2
            # some formats: {'left':..., 'top':..., 'right':..., 'bottom':...}
            try:
                cx = int((bounds['left'] + bounds['right'])/2)
                cy = int((bounds['top'] + bounds['bottom'])/2)
                run_cmd(["adb", "shell", "input", "tap", str(cx), str(cy)])
                return True
            except Exception:
                pass
        if isinstance(bounds, str):
            # parse "[l,t][r,b]"
            import re
            m = re.findall(r'\[(-?\d+),(-?\d+)\]', bounds)
            if len(m) >= 2:
                l,t = map(int, m[0])
                r,b = map(int, m[1])
                cx = (l + r)//2
                cy = (t + b)//2
                run_cmd(["adb", "shell", "input", "tap", str(cx), str(cy)])
                return True
        # fallback: attempt click by uiautomator2 if available
        if self.u2 and node.get("resource-id"):
            try:
                e = self.u2(resourceId=node.get("resource-id"))
                if e.exists:
                    e.click()
                    return True
            except Exception:
                pass
        return False

# --- WebSocket Client ---
class WSClient:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self._connect_lock = threading.Lock()
        self.handlers = {}
        # register default handlers
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
        # simple dispatch: expect {"action": "...", "data": {...}}
        action = msg.get("action")
        data = msg.get("data", {})
        if action == "send_sms":
            number = data.get("number")
            text = data.get("text")
            ok = False
            if self.sms_handler:
                ok = self.sms_handler.send_sms(number, text)
            self.send({"type":"send_sms_result", "ok": ok, "id": msg.get("id")})
        elif action == "find_ui":
            by = data.get("by")
            value = data.get("value")
            res = None
            if by == "text":
                res = self.uia.find_by_text(value)
            elif by == "resource-id":
                res = self.uia.find_by_resource_id(value)
            self.send({"type":"find_ui_result", "result": res, "id": msg.get("id")})
        elif action == "click_ui":
            node = data.get("node")
            ok = self.uia.click_node(node or {})
            self.send({"type":"click_ui_result", "ok": ok, "id": msg.get("id")})
        elif action == "adb_shell":
            cmd = data.get("cmd")
            out = self.adb.shell(cmd)
            self.send({"type":"adb_shell_result", "out": out, "id": msg.get("id")})
        else:
            # custom handlers
            h = self.handlers.get(action)
            if h:
                try:
                    h(data, msg)
                except Exception as ex:
                    self.send({"type":"error", "msg": str(ex), "id": msg.get("id")})

    def _on_open(self, ws):
        print("WS connected to", self.url)
        # register as bridge
        self.send({"type":"bridge_hello", "id": str(uuid.uuid4()), "info":{"platform":"termux"}})

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

    # start ws
    ws_client.start()

    # start sms polling thread
    t_sms = threading.Thread(target=sms.poll_loop, daemon=True)
    t_sms.start()

    # keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")

if __name__ == "__main__":
    main()
