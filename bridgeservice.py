#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridgeservice.py
- WebSocket client bridge for Termux / rooted Android
- Uses adb to query device info, SIM info, IP (via `adb shell ip route`), send SMS with SIM selection,
  dial USSD and auto-select SIM, dump UI and parse USSD response.
- Handlers (via WS messages):
    - send_sms { number, text, sim }         -> send SMS (tries adb intent then termux-sms-send)
    - open_app  { package }                   -> open app via monkey
    - send_ussd { code, sim }                 -> dial USSD, auto-select SIM and read response
    - adb_shell { cmd }                       -> run arbitrary adb shell and return output
"""
import os
import re
import json
import time
import uuid
import base64
import threading
import subprocess
from datetime import datetime

# websocket-client
try:
    import websocket
except Exception:
    raise SystemExit("websocket-client required: pip install websocket-client")

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

# CONFIG: ganti ke URL WebSocket server kamu
WS_SERVER = os.environ.get("BRIDGE_WS", "wss://yourserver.example/ws")
POLL_SMS_INTERVAL = 3
HEARTBEAT_INTERVAL = 30 * 60  # (tidak wajib dipakai di versi ini)

# ----------------- utilities -----------------
def run_local(cmd, capture=True):
    """Run local shell command (on Termux host)."""
    if isinstance(cmd, (list, tuple)):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    else:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE if capture else None)
    out, err = proc.communicate()
    if capture:
        return out.decode('utf-8', errors='ignore')
    return None

# ----------------- ADB wrapper -----------------
class AdbWrapper:
    def __init__(self):
        self.adb_client = None
        if ADBUTILS_AVAILABLE:
            try:
                self.adb_client = adbutils.adb.device()
            except Exception:
                self.adb_client = None

    def shell(self, cmd):
        """Return stdout as str. cmd should be plain shell fragment (no 'adb shell' prefix)."""
        if self.adb_client:
            try:
                return self.adb_client.shell(cmd)
            except Exception:
                pass
        out = run_local(f"adb shell {cmd}")
        return out or ""

    def pull(self, remote, local):
        if self.adb_client:
            try:
                return self.adb_client.pull(remote, local)
            except Exception:
                pass
        run_local(f"adb pull {remote} {local}")
        return None

    def push(self, local, remote):
        if self.adb_client:
            try:
                return self.adb_client.push(local, remote)
            except Exception:
                pass
        run_local(f"adb push {local} {remote}")
        return None

# ----------------- device info helpers -----------------
def get_local_ip(adb: AdbWrapper):
    """
    Use `adb shell ip route` and parse the line containing wlan0 and src <ip>.
    """
    try:
        out = adb.shell("ip route")
        if not out:
            # fallback: try ip route get
            out2 = adb.shell("ip route get 8.8.8.8")
            out = out2 or ""
        for line in out.splitlines():
            if "wlan0" in line and "src" in line:
                parts = line.split()
                if "src" in parts:
                    idx = parts.index("src")
                    if idx + 1 < len(parts):
                        return parts[idx+1]
            # sometimes 'dev wlan0' present and 'src' later
            if "dev wlan0" in line and "src" in line:
                m = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    return m.group(1)
    except Exception as e:
        print("get_local_ip error:", e)
    return "0.0.0.0"

def get_device_info(adb: AdbWrapper):
    try:
        brand = adb.shell("getprop ro.product.manufacturer").strip()
        model = adb.shell("getprop ro.product.model").strip()
        android = adb.shell("getprop ro.build.version.release").strip()
        return {"brand": brand, "model": model, "android": android}
    except Exception:
        return {}

def get_serial(adb: AdbWrapper):
    try:
        s = adb.shell("getprop ro.serialno").strip()
        if not s:
            s = adb.shell("getprop ro.boot.serialno").strip()
        return s or None
    except Exception:
        return None

def get_imei(adb: AdbWrapper, slot=0):
    """Try service call iphonesubinfo variants — returns None if unavailable."""
    try:
        # attempt common indices (some Android versions differ)
        # we call the index (1+slot) as earlier approach
        out = adb.shell(f"service call iphonesubinfo {1 + slot}").strip()
        if not out:
            return None
        matches = re.findall(r"'(.*?)'", out)
        imei = "".join(matches).replace(".", "").replace(" ", "")
        return imei or None
    except Exception:
        return None

def get_sim_info(adb: AdbWrapper, slot=0):
    """
    Try multiple sources: content query -> dumpsys subscription -> dumpsys telephony.registry
    Returns dict possibly containing 'operator' and 'number'.
    """
    info = {}
    try:
        # 1) content query for siminfo (may require root/permissions)
        try:
            raw = adb.shell("content query --uri content://telephony/siminfo")
            if raw and "Row:" in raw:
                # naive parse: find rows mentioning slotIndex / sim_id
                for line in raw.splitlines():
                    if "slot=" in line or "slotIndex=" in line or "slotIndex=" in line:
                        if f"slot={slot}" in line or f"slotIndex={slot}" in line:
                            # try extract number / carrier
                            mnum = re.search(r"number=(\S+)", line)
                            mcar = re.search(r"carrier=(\S+)", line)
                            if mnum:
                                info['number'] = mnum.group(1)
                            if mcar:
                                info['operator'] = mcar.group(1)
                            break
        except Exception:
            pass

        # 2) dumpsys subscription
        try:
            out = adb.shell("dumpsys subscription") or ""
            if out:
                # split into blocks
                blocks = out.split("\n\n")
                for b in blocks:
                    if f"slotIndex={slot}" in b or f"mSimSlotIndex={slot}" in b or f"SlotIndex: {slot}" in b:
                        m_car = re.search(r"carrier=(\S+)", b)
                        m_num = re.search(r"number=(\S+)", b)
                        if m_car and 'operator' not in info:
                            info['operator'] = m_car.group(1)
                        if m_num and 'number' not in info:
                            info['number'] = m_num.group(1)
                        # sometimes number present as 'mCc' or 'mNumber' — attempt more keys
                        if 'number' not in info:
                            m_num2 = re.search(r"mNumber=(\S+)", b)
                            if m_num2:
                                info['number'] = m_num2.group(1)
                        break
        except Exception:
            pass

        # 3) dumpsys telephony.registry fallback
        if 'operator' not in info or 'number' not in info:
            try:
                out2 = adb.shell("dumpsys telephony.registry") or ""
                for line in out2.splitlines():
                    if f"mSimSlotIndex={slot}" in line or f"mSlotIndex={slot}" in line or f"slotIndex={slot}" in line:
                        # operatorAlphaLong=...
                        m_op = re.search(r"operatorAlphaLong=([^\s]+)", line)
                        m_num = re.search(r"mLine1Number=([^\s]+)", line)
                        if m_op and 'operator' not in info:
                            info['operator'] = m_op.group(1)
                        if m_num and 'number' not in info:
                            info['number'] = m_num.group(1)
            except Exception:
                pass

    except Exception as e:
        print("get_sim_info error:", e)
    return info

# ----------------- screenshot helper -----------------
def capture_screenshot_base64(adb: AdbWrapper):
    """Return base64 PNG string or None"""
    remote = "/sdcard/bridgeservice_screenshot.png"
    local = "/data/data/com.termux/files/home/bridgeservice/bridgeservice_screenshot.png"
    try:
        adb.shell(f"screencap -p {remote}")
        # pull to local
        try:
            adb.pull(remote, local)
        except Exception:
            pass
        if os.path.exists(local):
            with open(local, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
        # fallback read remote via adb shell cat
        out = adb.shell(f"cat {remote}")
        if out:
            b = out.encode('latin1') if isinstance(out, str) else out
            return base64.b64encode(b).decode("ascii")
    except Exception as e:
        print("capture_screenshot error:", e)
    return None

# ----------------- USSD helper -----------------
import urllib.parse
def _encode_ussd(code: str) -> str:
    # encode '#' etc
    return urllib.parse.quote(code, safe='')

def send_ussd_and_read(adb: AdbWrapper, code: str, sim: int = 0, wait_response_sec: int = 6):
    """
    Dial USSD code and attempt to force-select SIM (sim=0 SIM1, sim=1 SIM2).
    Returns dict with keys: ok, error, raw_ui, ussd_text
    """
    result = {"ok": False, "error": None, "raw_ui": None, "ussd_text": None}
    try:
        if not code:
            result['error'] = "empty code"
            return result
        enc = _encode_ussd(code)  # *999%23
        sim_index = int(sim) if sim is not None else 0

        # try dialing with several intent variants
        intents = [
            f"am start -a android.intent.action.CALL -d tel:{enc}",
            f"am start -a android.intent.action.CALL -d tel:{enc} --ei android.telecom.extra.SIM_SLOT_INDEX {sim_index}",
            f"am start -a android.intent.action.CALL -d tel:{enc} --ei simSlot {sim_index} --ei subscription {sim_index} --ei com.android.phone.extra.slot {sim_index}",
        ]
        dialed = False
        for it in intents:
            try:
                adb.shell(it)
                dialed = True
                time.sleep(0.3)
            except Exception:
                pass
        if not dialed:
            result['error'] = "failed to send dial intent"
            return result

        # Wait shortly for chooser/ussd popup
        time.sleep(0.8)

        # If uiautomator2 available, try to click SIM choice then capture hierarchy
        if UIAUTOMATOR2_AVAILABLE:
            try:
                d = u2.connect()  # may throw if cannot connect
                # try to click via common labels
                sim_labels = ["SIM 1","SIM 2","SIM1","SIM2","Use SIM 1","Use SIM 2","Pilih SIM","Pilih kartu","Kartu 1","Kartu 2","Call with SIM 1","Call with SIM 2","Panggil dengan SIM 1","Panggil dengan SIM 2"]
                chosen = False
                for lbl in sim_labels:
                    try:
                        e = d(text=lbl)
                        if e.exists:
                            e.click()
                            chosen = True
                            break
                    except Exception:
                        pass
                # try to click first/second button if still not chosen
                if not chosen:
                    try:
                        buttons = d(className="android.widget.Button")
                        if buttons.exists:
                            idx = 0 if sim_index==0 else (1 if buttons.count>1 else 0)
                            buttons[idx].click()
                            chosen = True
                    except Exception:
                        pass
                # wait for USSD response to appear
                time.sleep(wait_response_sec)
                ui_dump = d.dump_hierarchy()
                result['raw_ui'] = ui_dump
                # parse XML and extract large text blocks
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(ui_dump)
                    texts = []
                    for node in root.iter('node'):
                        txt = node.attrib.get('text') or node.attrib.get('content-desc') or ''
                        if txt and len(txt.strip())>3:
                            texts.append(txt.strip())
                    if texts:
                        result['ussd_text'] = max(texts, key=lambda s: len(s))
                except Exception:
                    pass
                result['ok'] = True
                return result
            except Exception as e:
                # u2 connect/click failed -> fallback to dump method
                #print("u2 error:", e)
                pass

        # Fallback: uiautomator dump + parse + click coordinates if chooser present
        chosen = False
        for attempt in range(4):
            try:
                adb.shell('uiautomator dump /sdcard/ussd_dump.xml')
                local_tmp = '/data/data/com.termux/files/home/bridgeservice/ussd_dump.xml'
                # pull or read
                try:
                    adb.pull('/sdcard/ussd_dump.xml', local_tmp)
                    xml_text = open(local_tmp, 'r', encoding='utf-8', errors='ignore').read()
                except Exception:
                    xml_text = adb.shell('cat /sdcard/ussd_dump.xml') or ""
                result['raw_ui'] = xml_text
                # parse xml for candidate buttons / text nodes
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(xml_text)
                    candidates = []
                    for node in root.iter('node'):
                        cls = node.attrib.get('class','')
                        text = (node.attrib.get('text') or node.attrib.get('content-desc') or "").strip()
                        bounds = node.attrib.get('bounds','')
                        if bounds and ( 'Button' in cls or 'TextView' in cls or text):
                            candidates.append((text, bounds))
                    # find SIM-labeled node
                    target_bounds = None
                    sim_targets = ['SIM 1','SIM 2','SIM1','SIM2','SIM 1','SIM 2','Pilih SIM','Pilih kartu','kartu SIM 1','kartu SIM 2','Kartu 1','Kartu 2']
                    for text,bounds in candidates:
                        if not text:
                            continue
                        tlow = text.lower()
                        for st in sim_targets:
                            if st.lower() in tlow:
                                # choose matching sim index if present
                                if str(sim+1) in tlow or ('sim 1' in st.lower() and sim==0) or ('sim 2' in st.lower() and sim==1):
                                    target_bounds = bounds
                                    break
                                else:
                                    target_bounds = bounds
                                    break
                        if target_bounds:
                            break
                    # fallback pick by order
                    if not target_bounds and candidates:
                        nonempty = [c for c in candidates if c[0]]
                        if nonempty:
                            idx = sim if sim < len(nonempty) else 0
                            target_bounds = nonempty[idx][1]
                    if target_bounds:
                        m = re.findall(r'\[(-?\d+),(-?\d+)\]', target_bounds)
                        if len(m)>=2:
                            l,t = map(int, m[0]); r,b = map(int, m[1])
                            cx = (l+r)//2; cy = (t+b)//2
                            adb.shell(f"input tap {cx} {cy}")
                            chosen = True
                            time.sleep(1.2)
                            break
                except Exception:
                    pass
            except Exception:
                pass
            time.sleep(0.8)

        # After selecting (or if chooser didn't appear), wait for USSD response and dump xml
        time.sleep(wait_response_sec)
        try:
            adb.shell('uiautomator dump /sdcard/ussd_dump.xml')
            local_tmp = '/data/data/com.termux/files/home/bridgeservice/ussd_dump.xml'
            try:
                adb.pull('/sdcard/ussd_dump.xml', local_tmp)
                xml_text = open(local_tmp, 'r', encoding='utf-8', errors='ignore').read()
            except Exception:
                xml_text = adb.shell('cat /sdcard/ussd_dump.xml') or ""
            result['raw_ui'] = xml_text
            # parse text blocks
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(xml_text)
                texts = []
                for node in root.iter('node'):
                    txt = node.attrib.get('text') or node.attrib.get('content-desc') or ''
                    if txt and len(txt.strip())>3:
                        texts.append(txt.strip())
                if texts:
                    result['ussd_text'] = max(texts, key=lambda s: len(s))
            except Exception:
                pass
        except Exception:
            pass

        result['ok'] = True
        return result

    except Exception as e:
        result['error'] = str(e)
        return result

# ----------------- SMS handler -----------------
class SMSHandler:
    def __init__(self, wsclient, adb: AdbWrapper):
        self.ws = wsclient
        self.adb = adb
        self.last_seen_ids = set()

    def list_sms(self):
        try:
            out = run_local("termux-sms-list")
            return json.loads(out) if out else []
        except Exception:
            return []

    def send_sms(self, number, text, sim=0):
        """Try ADB intent options first (to choose SIM), fallback to termux-sms-send."""
        sim_idx = int(sim) if sim is not None else 0
        intents = [
            f'am start -a android.intent.action.SENDTO -d sms:{number} --es sms_body "{text}" --ei android.telecom.extra.SIM_SLOT_INDEX {sim_idx}',
            f'am start -a android.intent.action.SENDTO -d sms:{number} --es sms_body "{text}" --ei simSlot {sim_idx} --ei subscription {sim_idx} --ei com.android.phone.extra.slot {sim_idx}'
        ]
        for it in intents:
            try:
                self.adb.shell(it)
                return True
            except Exception:
                pass
        # fallback
        try:
            run_local(f'termux-sms-send -n {number} "{text}"', capture=False)
            return True
        except Exception:
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
                for m in reversed(new):
                    try:
                        self.ws.send(json.dumps({"type":"sms_received","data":m}))
                    except Exception:
                        pass
                # keep set from growing too much
                if len(self.last_seen_ids) > 2000:
                    self.last_seen_ids = set(list(self.last_seen_ids)[-1000:])
            except Exception as e:
                print("SMSHandler poll error:", e)
            time.sleep(POLL_SMS_INTERVAL)

# ----------------- WebSocket client -----------------
class WSClient:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.adb = AdbWrapper()
        self.sms = SMSHandler(self, self.adb)
        self._stop = threading.Event()

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        # start sms poll thread
        t2 = threading.Thread(target=self.sms.poll_loop, daemon=True)
        t2.start()

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
            # ignore send failures
            pass

    def _on_open(self, ws):
        # gather profile
        device_info = get_device_info(self.adb)
        ip_local = get_local_ip(self.adb)
        serial = get_serial(self.adb)
        sim1 = {"imei": get_imei(self.adb, 0), **get_sim_info(self.adb, 0)}
        sim2 = {"imei": get_imei(self.adb, 1), **get_sim_info(self.adb, 1)}
        profile = {"platform":"termux","device":device_info,"serial":serial,"ip_local":ip_local,"sims":[sim1,sim2]}
        self.send({"type":"bridge_hello","id":str(uuid.uuid4()),"info":profile})

    def _on_message(self, ws, message):
        # expect JSON with {action, data, id}
        try:
            msg = json.loads(message)
        except Exception:
            return
        action = msg.get("action")
        data = msg.get("data", {})
        req_id = msg.get("id")
        # handlers
        if action == "send_sms":
            n = data.get("number"); t = data.get("text"); s = data.get("sim", 0)
            ok = self.sms.send_sms(n, t, s)
            self.send({"type":"send_sms_result","ok":ok,"id":req_id})
        elif action == "open_app":
            pkg = data.get("package"); ok = False
            if pkg:
                try:
                    self.adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
                    ok = True
                except Exception as e:
                    self.send({"type":"error","msg":str(e),"id":req_id})
            self.send({"type":"open_app_result","ok":ok,"id":req_id})
        elif action == "adb_shell":
            cmd = data.get("cmd","")
            out = ""
            try:
                out = self.adb.shell(cmd)
            except Exception as e:
                out = str(e)
            self.send({"type":"adb_shell_result","out":out,"id":req_id})
        elif action == "send_ussd":
            code = data.get("code"); sim = data.get("sim",0)
            res = send_ussd_and_read(self.adb, code, sim)
            self.send({"type":"send_ussd_result","result":res,"id":req_id})
        else:
            # unknown action -> echo
            self.send({"type":"unknown_action","action":action,"id":req_id})

    def _on_close(self, ws, code, reason):
        print("WS closed", code, reason)

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
            time.sleep(5)

# ----------------- main -----------------
def main():
    client = WSClient(WS_SERVER)
    client.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        client.stop()

if __name__ == "__main__":
    main()
