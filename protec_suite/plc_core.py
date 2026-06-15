#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Moniteur PLC Siemens S7-1215C - PROTEC AMCON Volute DUO"""

import json
import os
import queue
import struct
import threading
import time
from datetime import datetime

try:
    import snap7
    SNAP7_AVAILABLE = True
except ImportError:
    SNAP7_AVAILABLE = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config_plc.json")

DEFAULT_CONFIG = {
    "ip": "10.10.0.10",
    "rack": 0,
    "slot": 1,
    "db_num": 24,
    "db_size": 1404,
    "interval": 5,
}

VARIATEURS = [
    {"nom": "Vis", "short": "VIS", "offset_hz": 346, "offset_a": 350,
     "consigne_h": 362, "consigne_b": 366, "i_nom": 378, "analog": 394,
     "analog_nom": "Param Vis", "color": "#2ECC71"},
    {"nom": "Agitateur", "short": "AGIT", "offset_hz": 252, "offset_a": 256,
     "consigne_h": 264, "consigne_b": 268, "i_nom": 280, "analog": 300,
     "analog_nom": "Niv. Floculation", "color": "#3498DB"},
    {"nom": "Pompe a boues", "short": "P.BOUE", "offset_hz": 440, "offset_a": 444,
     "consigne_h": 452, "consigne_b": 456, "i_nom": 468, "analog": 488,
     "analog_nom": "Niv. Cuve Boues", "color": "#F39C12"},
    {"nom": "Pompe de filtrat", "short": "P.FILT", "offset_hz": 534, "offset_a": 538,
     "consigne_h": 548, "consigne_b": 552, "i_nom": 564, "analog": 582,
     "analog_nom": "Niv. Bac Filtrat", "color": "#1ABC9C"},
    {"nom": "Arbre trans. ext.", "short": "ARBRE", "offset_hz": 628, "offset_a": 632,
     "consigne_h": 644, "consigne_b": 648, "i_nom": 660, "analog": 676,
     "analog_nom": "Param Arbre", "color": "#9B59B6"},
    {"nom": "Agit. fosse boues", "short": "MIXER", "offset_hz": 722, "offset_a": 726,
     "consigne_h": 738, "consigne_b": 742, "i_nom": 752, "analog": 770,
     "analog_nom": "Param Mixer", "color": "#E67E22"},
]


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _sf(data, offset):
    try:
        return struct.unpack(">f", data[offset:offset + 4])[0]
    except Exception:
        return 0.0


def _parse_db(data):
    result = {"variateurs": {}, "niveaux": {}}
    for v in VARIATEURS:
        s = v["short"]
        result["variateurs"][s] = {
            "nom": v["nom"],
            "hz": round(_sf(data, v["offset_hz"]), 1),
            "a": round(_sf(data, v["offset_a"]), 2),
            "i_nom": round(_sf(data, v["i_nom"]), 2),
            "consigne_h": round(_sf(data, v["consigne_h"]), 1),
            "consigne_b": round(_sf(data, v["consigne_b"]), 1),
            "analog": round(_sf(data, v["analog"]), 1),
            "analog_nom": v["analog_nom"],
            "color": v["color"],
            "running": abs(_sf(data, v["offset_hz"])) > 0.5,
        }
    result["niveaux"] = {
        "niv_flocul": round(_sf(data, 300), 1),
        "niv_boues": round(_sf(data, 488), 1),
        "niv_bac_filt": round(_sf(data, 582), 1),
    }
    return result


class PLCMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._connected = False
        self._loop_running = False
        self._stop_flag = False
        self._cfg = load_config()
        self._history = []
        self._subscribers = []
        self._read_count = 0
        self._last_error = ""

    def connect(self, ip=None):
        if not SNAP7_AVAILABLE:
            return False, "python-snap7 non installé (pip install python-snap7)"
        if ip:
            self._cfg["ip"] = ip
        try:
            c = snap7.client.Client()
            c.connect(self._cfg["ip"], self._cfg["rack"], self._cfg["slot"])
            if not c.get_connected():
                return False, "Connexion échouée (get_connected = False)"
            c.db_read(self._cfg["db_num"], 0, 4)
            with self._lock:
                self._client = c
                self._connected = True
                self._last_error = ""
            return True, f"Connecté à {self._cfg['ip']}"
        except Exception as e:
            self._last_error = str(e)
            return False, str(e)

    def disconnect(self):
        self.stop_loop()
        with self._lock:
            try:
                if self._client:
                    self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._connected = False

    def read_once(self):
        with self._lock:
            if not self._connected or not self._client:
                return None
            client = self._client
            cfg = dict(self._cfg)

        try:
            raw = client.db_read(cfg["db_num"], 0, cfg["db_size"])
            data = bytes(raw)
            ts = datetime.now()
            self._read_count += 1

            reading = _parse_db(data)
            reading["ts"] = ts.isoformat()
            reading["read_count"] = self._read_count

            with self._lock:
                self._history.append(reading)
                if len(self._history) > 300:
                    self._history.pop(0)
                for q in list(self._subscribers):
                    try:
                        q.put_nowait(reading)
                    except queue.Full:
                        pass

            return reading
        except Exception as e:
            self._last_error = str(e)
            try:
                with self._lock:
                    c = self._client
                if c:
                    c.disconnect()
                    c.connect(cfg["ip"], cfg["rack"], cfg["slot"])
            except Exception:
                pass
            return None

    def start_loop(self, interval=None):
        if self._loop_running:
            return
        if interval is not None:
            self._cfg["interval"] = interval
        self._stop_flag = False
        self._loop_running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop_loop(self):
        self._stop_flag = True
        self._loop_running = False

    def _loop(self):
        while not self._stop_flag and self._connected:
            self.read_once()
            interval = self._cfg.get("interval", 5)
            t0 = time.time()
            while not self._stop_flag and (time.time() - t0) < interval:
                time.sleep(0.1)
        self._loop_running = False

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def get_history(self, n=100):
        with self._lock:
            return list(self._history[-n:])

    def status(self):
        with self._lock:
            last = self._history[-1]["ts"] if self._history else None
        return {
            "connected": self._connected,
            "monitoring": self._loop_running,
            "ip": self._cfg.get("ip", ""),
            "interval": self._cfg.get("interval", 5),
            "read_count": self._read_count,
            "snap7_available": SNAP7_AVAILABLE,
            "last_read": last,
            "last_error": self._last_error,
            "variateurs": VARIATEURS,
        }

    def get_config(self):
        return dict(self._cfg)

    def set_config(self, cfg):
        with self._lock:
            self._cfg.update(cfg)
        save_config(self._cfg)


_monitor = PLCMonitor()


def get_monitor():
    return _monitor
