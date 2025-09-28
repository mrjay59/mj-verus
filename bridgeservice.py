#!/usr/bin/env python3
"""
bridgeservice.py (improved)
Bridge service for Termux:
- Poll incoming SMS (termux-sms-list) and forward to websocket server
- Send SMS on request with SIM selection
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
import re

# websocket-client
try:
    import websocket
except Exception:
    print("websocket-client is required. pip install websocket-client")
    raise

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

WS_SERVER = os.environ.get("BRIDGE_WS", "wss://s14223.blr1.piesocket.com/v3/1?api_key=WVXN94EfJrQO7fSpSwwKJZgxbavdLdKLZBPLLlQR&notify_self=1")
POLL_SMS_INTERVAL = 3

# --- Utilities ---
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

def get_local_ip():
    try:
        # more robust: parse ip route get
        s = os.popen("ip route get 8.8.8.8 | awk '{print $7; exit}'").read().strip()
        if s:
            return s
        out = os.popen("ip -f inet addr show wlan0").read()
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
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
        if not serial:
            serial = adb.shell("getprop ro.boot.serialno").strip()
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
        out = adb.shell("dumpsys subscription").strip()
        if out:
            blocks = out.split("\n\n")
            for b in blocks:
                if f"slotIndex={slot}" in b or f"mSimSlotIndex={slot}" in b:
                    m_car = re.search(r"carrier=(\S+)", b)
                    m_num = re.search(r"number=(\S+)", b)
                    if m_car:
                        info['operator'] = m_car.group(1)
                    if m_num:
                        info['number'] = m_num.group(1)
                    break
        if not info:
            out = adb.shell(f"dumpsys telephony.registry | grep mSimSlotIndex={slot} -A 20")
            m_op = re.search(r"operatorAlphaLong=([^\s]+)", out)
            m_num = re.search(r"mLine1Number=([^\s]+)", out)
            if m_op:
                info['operator'] = m_op.group(1)
            if m_num:
                info['number'] = m_num.group(1)
    except Exception:
        pass
    return info

# --- SMS Handler ---
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
            return []

    def send_sms(self, number, text, sim=0):
        ok = False
        sim_index = int(sim) if sim is not None else 0
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
                pass
        if not ok:
            try:
                run_cmd(["termux-sms-send", "-n", number, text], capture=False)
                ok = True
            except Exception:
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
                for m in new:
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
        run_cmd(["adb", "push", local, remote])

    def pull(self, remote, local):
        if self.adb_client:
            return self.adb_client.pull(remote, local)
        run_cmd(["adb", "pull", remote, local])

class UIAuto:
    def __init__(self, adb: AdbWrapper=None):
        self.adb = adb or AdbWrapper()

class WSClient:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.adb = AdbWrapper()
        self.uia = UIAuto(self.adb)
        self.sms_handler = None

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

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
            return
        action = msg.get("action")
        data = msg.get("data", {})
        if action == "send_sms":
            number = data.get("number")
            text = data.get("text")
            sim = data.get("sim", 0)
            ok = self.sms_handler.send_sms(number, text, sim) if self.sms_handler else False
            self.send({"type":"send_sms_result","ok": ok,"id": msg.get("id")})
        elif action == "open_app":
            pkg = data.get("package")
            ok = False
            if pkg:
                try:
                    self.adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
                    ok = True
                except Exception as e:
                    self.send({"type":"error","msg": str(e),"id": msg.get("id")})
            self.send({"type":"open_app_result","ok": ok,"id": msg.get("id")})

    def _on_open(self, ws):
        device_info = get_device_info(self.adb)
        ip_local = get_local_ip()
        serial = get_serial(self.adb)
        sim1 = {"imei": get_imei(self.adb,0), **get_sim_info(self.adb,0)}
        sim2 = {"imei": get_imei(self.adb,1), **get_sim_info(self.adb,1)}
        profile = {"platform":"termux","device":device_info,"serial":serial,"ip_local":ip_local,"sims":[sim1,sim2]}
        self.send({"type":"bridge_hello","id":str(uuid.uuid4()),"info":profile})

    def _run(self):
        while True:
            try:
                self.ws = websocket.WebSocketApp(self.url,on_message=self._on_message,on_open=self._on_open)
                self.ws.run_forever()
            except Exception:
                time.sleep(5)

def main():
    ws_client = WSClient(WS_SERVER)
    sms = SMSHandler(ws_client, ws_client.adb)
    ws_client.sms_handler = sms
    ws_client.start()
    t_sms = threading.Thread(target=sms.poll_loop, daemon=True)
    t_sms.start()
    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
