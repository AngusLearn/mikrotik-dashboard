#!/usr/bin/env python3
"""
MikroTik LAN Monitor — live per-device traffic dashboard for RouterOS.

Read-only by default. Talks to the RouterOS API (port 8728).
Optional device pause/resume (block) only when allow_control=true.
"""
import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# BASE can be relocated (tests, Docker volumes) without touching the code
BASE = Path(os.environ.get("DASH_BASE", "")).resolve() if os.environ.get("DASH_BASE") \
    else Path(__file__).resolve().parent
APP = Path(__file__).resolve().parent  # code assets ship with the code, unlike
                                       # BASE ($DASH_BASE) which is mutable state
CONFIG_PATH = BASE / "config.json"
DB_PATH = BASE / "history.db"
OUI_PATH = BASE / "oui.tsv"
AUTH_PATH = BASE / "auth.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mtmon")

DEFAULT_CONFIG = {
    "host": "192.168.1.1",
    "port": 8728,
    "user": "",
    "password": "",
    "interval_seconds": 3,
    "retention_hours": 48,
    "allow_control": False,
    "block_list_name": "DASH_BLOCKED",
    # credential profiles: {name: {host,user,password,port}}, plus the active one
    "profiles": {},
    "active": "",
}

# keys a profile (and the flat active config) carries for connecting
_PROFILE_KEYS = ("host", "user", "password", "port")


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy: profiles dict
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("bad config.json: %s", e)
    return cfg


def save_config(cfg: dict) -> None:
    # keep only known keys, never log secrets
    clean = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    # atomic + perm-first: router password must never exist in a world-
    # readable or half-written file (same discipline as auth.json)
    tmp = CONFIG_PATH.with_name("config.json.tmp")
    tmp.write_text(json.dumps(clean, indent=2))
    tmp.chmod(0o600)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------
# Wireless (AP mode) gather
# --------------------------------------------------------------------------
def wifi_info(api):
    """Wireless state of this RouterOS box (works on APs and AP-routers).
    Returns (radios, assoc): radios maps wlan interface -> {ssid, band,
    chipset}; assoc maps station MAC (upper) -> {radio, ssid, band, chipset,
    signal, tx_rate, rx_rate, uptime}. Both WiFi stacks are covered: the
    legacy package lives under /interface/wireless (RouterOS 7 keeps it
    there!), the newer one under /wifi. ({}, {}) when the box has no WiFi."""
    radios, assoc = {}, {}
    legacy = api_fetch_all(api, "/interface/wireless")
    if legacy:
        for r in legacy:
            radios[r.get("name", "")] = {
                "ssid": r.get("ssid", ""), "band": r.get("band", ""),
                "chipset": r.get("interface-type", "")}
        for c in api_fetch_all(api, "/interface/wireless/registration-table") or []:
            mac = (c.get("mac-address") or "").upper()
            if mac:
                assoc[mac] = {"radio": c.get("interface", ""),
                              "signal": (c.get("signal-strength") or "").split("@")[0],
                              "tx_rate": c.get("tx-rate", ""),
                              "rx_rate": c.get("rx-rate", ""),
                              "uptime": c.get("uptime", ""),
                              # WinBox Quick Set shows this: the AP hears the
                              # station's IP even on a pure bridge (not ARP!)
                              "ip": c.get("last-ip", ""),
                              "ccq": c.get("tx-ccq", ""),
                              "security": c.get("authentication-type", "")}
    else:
        try:  # new WiFi stack (>= v7.19): /wifi radios + /wifi/registration-table
            for r in api_fetch_all(api, "/wifi") or []:
                radios[r.get("name", "")] = {"ssid": r.get("ssid", r.get("configuration", {}).get("ssid", "") if isinstance(r.get("configuration"), dict) else ""),
                                             "band": r.get("freq", ""),
                                             "chipset": r.get("radio", "")}
            for c in api_fetch_all(api, "/wifi/registration-table") or []:
                mac = (c.get("client-mac-address") or c.get("mac-address") or "").upper()
                if mac:
                    assoc[mac] = {"radio": c.get("wifi", "") or c.get("interface", ""),
                                  "signal": str(c.get("signal", "")),
                                  "tx_rate": str(c.get("tx-rate", "")),
                                  "rx_rate": str(c.get("rx-rate", "")),
                                  "uptime": c.get("uptime", ""),
                                  "ip": c.get("address", "") or c.get("last-ip", "")}
        except Exception:
            pass
    for a in assoc.values():
        a.update({k: v for k, v in radios.get(a.get("radio", ""), {}).items() if v})
    return radios, assoc


# --------------------------------------------------------------------------
# OUI vendor lookup (best effort)
# --------------------------------------------------------------------------
_oui: dict = {}


def load_oui() -> None:
    global _oui
    if not OUI_PATH.exists():
        try:
            import urllib.request

            url = "https://code.wireshark.org/review/gitweb?p=wireshark.git;a=blob_plain;f=manuf"
            log.info("downloading OUI db from wireshark manuf ...")
            data = urllib.request.urlopen(url, timeout=25).read().decode("utf-8", "ignore")
            OUI_PATH.write_text(data)
        except Exception as e:
            log.warning("OUI download failed (offline is fine): %s", e)
            return
    try:
        for line in OUI_PATH.read_text(errors="ignore").splitlines():
            if line.startswith("#") or "\t" not in line:
                continue
            mac_part, vendor = line.split("\t", 1)
            oui = mac_part.strip().split("/")[0].replace(":", "").replace("-", "").upper()
            vendor = vendor.strip()
            if len(oui) == 6 and vendor:
                _oui[oui] = vendor
        log.info("OUI db loaded: %d entries", len(_oui))
    except Exception as e:
        log.warning("OUI parse failed: %s", e)


def vendor_of(mac: str) -> str:
    return _oui.get(mac.replace(":", "").replace(".", "")[:6].upper(), "")


# --------------------------------------------------------------------------
# SQLite history
# --------------------------------------------------------------------------
_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --- per-profile history -----------------------------------------------------
# Router telemetry (samples / devices / wan_bytes / flows_hist / anet) is
# bucketed per router profile so switching routers never mixes two networks'
# charts. Auth state (sessions, login_fail) deliberately stays global in
# history.db. An unnamed profile (pre-profiles config) keeps using history.db.
_hist_ready: set[str] = set()


def hist_path() -> Path:
    try:
        prof = str(load_config().get("active", "") or "")
    except Exception:
        prof = ""
    if not prof:
        return DB_PATH
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", prof)[:64]
    return BASE / f"history-{safe}.db"


def hist_db() -> sqlite3.Connection:
    path = hist_path()
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    if str(path) not in _hist_ready:  # caller holds _db_lock; DDL is idempotent
        for ddl in HIST_SCHEMA:
            conn.execute(ddl)
        _hist_ready.add(str(path))
    return conn


HIST_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS samples(
         ts REAL, key TEXT, rx REAL, tx REAL,
         PRIMARY KEY(ts, key))""",
    "CREATE INDEX IF NOT EXISTS ix_key_ts ON samples(key, ts)",
    """CREATE TABLE IF NOT EXISTS devices(
         mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT,
         first_seen REAL, last_seen REAL,
         rx_total REAL DEFAULT 0, tx_total REAL DEFAULT 0)""",
    # analytics tables (ntopng-style panels): exact WAN byte deltas per poll,
    # active flows / active local hosts per poll, and 60 s rollups of
    # per-protocol / per-port byte estimates (conntrack rate-derived)
    "CREATE TABLE IF NOT EXISTS wan_bytes(ts REAL PRIMARY KEY, rx REAL, tx REAL)",
    "CREATE TABLE IF NOT EXISTS flows_hist(ts REAL PRIMARY KEY, flows REAL, hosts REAL)",
    """CREATE TABLE IF NOT EXISTS anet(
         ts REAL, key TEXT, rx REAL, tx REAL, flows REAL DEFAULT 0,
         PRIMARY KEY(ts, key))""",
]


def db_init() -> None:
    with _db_lock, db() as c:
        for ddl in HIST_SCHEMA:
            c.execute(ddl)
        # auth state lives in the DB so restarts do not lock everyone out
        # of an active session or reset the brute-force counter
        c.execute("CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, exp REAL)")
        c.execute(
            "CREATE TABLE IF NOT EXISTS login_fail("
            "ip TEXT PRIMARY KEY, count INTEGER, locked_until REAL)")


def db_prune(retention_hours: float) -> None:
    cutoff = time.time() - retention_hours * 3600
    # Clock-step guard: a poll that runs before NTP sync (RTC far ahead at
    # boot) stamps one burst in the future. Read queries only floor on
    # ts >= cutoff, so future-dated rows would sit in every "recent" window
    # forever. 300 s margin is far beyond any legit write/query skew.
    future = time.time() + 300
    # prune every history bucket (shared default + each profile file), not
    # just the active one — idle profiles must not grow unbounded either
    for path in [DB_PATH] + sorted(BASE.glob("history-*.db")):
        with _db_lock, sqlite3.connect(path, timeout=15) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("DELETE FROM samples WHERE ts < ? OR ts > ?", (cutoff, future))
            for tbl in ("wan_bytes", "flows_hist", "anet"):
                c.execute(f"DELETE FROM {tbl} WHERE ts < ? OR ts > ?", (cutoff, future))
    with _db_lock, db() as c:
        c.execute("DELETE FROM sessions WHERE exp < ?", (time.time(),))


# --------------------------------------------------------------------------
# Shared state (written by sampler thread, read by API)
# --------------------------------------------------------------------------
STATE = {
    "connected": False,
    "last_error": None,
    "updated": 0.0,
    "system": {},
    "wan": {},
    "devices": [],
    "totals": {},
    "nets": {},
    "topo": {},
    "config": {},
    "wifi": [],
}
_state_lock = threading.Lock()


# --------------------------------------------------------------------------
# RouterOS helpers
# --------------------------------------------------------------------------
def api_fetch_all(api, path: str, retries: int = 3):
    """Robust print. RouterOS can trap mid-stream ('no such item (4)') when
    tables churn (esp. /ip/firewall/connection under load) — retry, and return
    None on total failure so callers can distinguish 'empty' from 'failed'."""
    last = None
    for i in range(retries):
        try:
            return list(api.path(path))
        except Exception as e:
            last = e
            time.sleep(0.15 * (i + 1))
    log.warning("fetch %s failed after retries: %s", path, last)
    return None


def as_bytes(v) -> float:
    try:
        m = re.match(r"(\d+(?:\.\d+)?)\s*([KMGT]?i?B)?", str(v).strip(), re.I)
        if not m:
            return 0.0
        n = float(m.group(1))
        mult = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}.get(
            (m.group(2) or "").lower()[0] if m.group(2) else "", 1
        )
        return n * mult
    except Exception:
        return 0.0


def as_num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def human_bytes(n: float) -> str:
    n = float(n)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024


class RouterMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="router-sampler")
        self.api = None
        self.prev_wan = None  # {iface: (rx_bytes, tx_bytes)}
        self.wan_ifaces = []
        self.last_conns = None
        self.lan_nets = []
        self._acc: dict = {}      # 60 s rollup: key -> [rx_bytes, tx_bytes, flows]
        self._acc_ts = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.err_count = 0

    # ----- connection -----------------------------------------------------
    def connect(self):
        from librouteros import connect

        cfg = load_config()
        if not cfg.get("user"):
            raise RuntimeError("router credentials not configured — open the dashboard and enter them")
        self.api = connect(
            host=cfg["host"],
            port=int(cfg["port"]),
            username=cfg["user"],
            password=cfg["password"],
            timeout=8,
        )
        # force a round trip so bad auth fails here
        if api_fetch_all(self.api, "/system/resource") is None:
            raise RuntimeError("router api round-trip failed")
        log.info("connected to RouterOS at %s", cfg["host"])

    def ensure(self):
        if self.api is None:
            self.connect()

    def drop(self):
        try:
            if self.api:
                self.api.close()
        except Exception:
            pass
        self.api = None

    def conns(self, api):
        """Conntrack fetch with history: RouterOS traps mid-print when the
        table churns; if all retries fail, reuse the last good snapshot so
        per-device rates hold instead of collapsing to zero under load."""
        conns = api_fetch_all(api, "/ip/firewall/connection")
        if conns is None:
            return self.last_conns or []
        self.last_conns = conns
        return conns

    # ----- main loop ------------------------------------------------------
    def run(self):
        while not self.stop_flag.is_set():
            t0 = time.time()
            cfg = load_config()
            try:
                self.ensure()
                self.poll(cfg)
                self.err_count = 0
                with _state_lock:
                    STATE["connected"] = True
                    STATE["last_error"] = None
                    STATE["updated"] = time.time()
            except Exception as e:
                self.err_count += 1
                msg = str(e) or e.__class__.__name__
                with _state_lock:
                    STATE["connected"] = False
                    STATE["last_error"] = msg
                log.warning("poll failed: %s", msg)
                self.drop()
                self.prev_wan = None
                time.sleep(min(5 * self.err_count, 30))
                continue
            sleep_left = cfg["interval_seconds"] - (time.time() - t0)
            if sleep_left > 0:
                self.stop_flag.wait(sleep_left)

    # ----- one polling cycle ---------------------------------------------
    def poll(self, cfg):
        api = self.api
        now = time.time()

        # ---- system info
        res = (api_fetch_all(api, "/system/resource") or [{}])[0]
        system = {
            "board": res.get("board-name", ""),
            "version": re.split(r"\s+\(", res.get("version", ""))[0],
            "uptime": res.get("uptime", ""),
            "cpu_load": as_num(res.get("cpu-load"), 0),
            "cpu": res.get("cpu-name", ""),
            "mem_total": as_num(res.get("total-memory"), 0),
            "mem_free": as_num(res.get("free-memory"), 0),
        }

        # ---- interfaces + byte counters
        ifaces = api_fetch_all(api, "/interface") or []
        routes = api_fetch_all(api, "/ip/route") or []
        wan_names = []
        wan_ip = ""
        for r in routes:
            if r.get("dst-address") in ("0.0.0.0/0", "::/0"):
                gi = r.get("gateway") or ""
                name = r.get("interface") or (gi.split(",")[1] if "," in gi else "")
                if name:
                    wan_names.append(name.split(",")[0])
                if not wan_ip:
                    wan_ip = r.get("immediate-gw", "").split(" ")[0]
        wan_names = wan_names or ["ether1"]

        running_rate = {}
        eth = api_fetch_all(api, "/interface/ethernet") or []
        for e in eth:
            rate = e.get("rate") or e.get("tx-rate") or ""
            if rate:
                running_rate[e.get("name", "")] = str(rate).split("/")[0]

        cur = {}
        wan_meta = []
        for i in ifaces:
            name = i.get("name", "")
            rx = as_bytes(i.get("rx-byte", "0"))
            tx = as_bytes(i.get("tx-byte", "0"))
            cur[name] = (rx, tx)
            if name in wan_names:
                wan_meta.append(
                    {
                        "name": name,
                        "mac": i.get("mac-address", ""),
                        "running": str(i.get("running", "")) == "true",
                        "rate": running_rate.get(name, ""),
                    }
                )
        wan_ip_local = ""
        own_ips = set()
        for a in (api_fetch_all(api, "/ip/address") or []):
            addr = a.get("address", "").split("/")[0]
            if addr:
                own_ips.add(addr)
            if a.get("interface") in wan_names:
                wan_ip_local = addr

        # ---- diff -> WAN rate (rates for the UI, exact byte deltas for volume)
        wan_rx_bps = wan_tx_bps = 0.0
        wan_rx_by = wan_tx_by = None
        if self.prev_wan is not None:
            dt = max(now - self.prev_wan[0], 0.5)
            prev = self.prev_wan[1]
            rx_by = tx_by = 0.0
            for name in wan_names:
                prx, ptx = prev.get(name, (0, 0))
                nrx, ntx = cur.get(name, (0, 0))
                drx = max(nrx - prx, 0)
                dtx = max(ntx - ptx, 0)
                wan_rx_bps += drx * 8 / dt
                wan_tx_bps += dtx * 8 / dt
                rx_by += drx
                tx_by += dtx
            wan_rx_by, wan_tx_by = rx_by, tx_by
        self.prev_wan = (now, cur)
        self.wan_ifaces = wan_names

        # ---- device inventory: ARP + DHCP leases
        macinfo: dict = {}  # mac -> {...}
        ip2mac: dict = {}
        for a in (api_fetch_all(api, "/ip/arp") or []):
            mac = a.get("mac-address", "")
            ip = a.get("address", "")
            if not mac or not ip:
                continue
            if "/" in ip:
                ip = ip.split("/")[0]
            try:
                if not ipaddress.ip_address(ip).is_private:
                    continue  # e.g. upstream gateway on WAN iface
            except ValueError:
                continue
            d = macinfo.setdefault(mac, {"mac": mac, "ips": set(), "hostname": "", "via": ""})
            d["ips"].add(ip)
            ip2mac[ip] = mac
            d["via"] = d["via"] or a.get("interface", "")
            if a.get("hostname"):
                d["hostname"] = a["hostname"]
        for l in (api_fetch_all(api, "/ip/dhcp-server/lease") or []):
            mac = l.get("mac-address", "")
            ip = l.get("address", "")
            if not mac:
                continue
            d = macinfo.setdefault(mac, {"mac": mac, "ips": set(), "hostname": "", "via": ""})
            if ip:
                d["ips"].add(ip)
                ip2mac[ip] = mac
            if l.get("host-name"):
                d["hostname"] = l["host-name"].replace("\\22", "").replace('"', "")
            d["via"] = d["via"] or l.get("interface", "")
        # neighbours (non-IP devices still show)
        for n in (api_fetch_all(api, "/ip/neigh") or []):
            mac = n.get("mac-address", "")
            if mac and n.get("interface"):
                d = macinfo.setdefault(mac, {"mac": mac, "ips": set(), "hostname": "", "via": ""})
                d["via"] = d["via"] or n["interface"]
        # MikroTik Neighbor Discovery (/ip neighbor): gives the ROUTER IDENTITY,
        # board platform and RouterOS VERSION of MikroTik devices (APs, switches)
        # even with no DHCP lease — identity is a fallback name only, so ARP/DHCP
        # names still win when present. A box may answer ND with a different MAC
        # (bridge vs radio) than the one holding the DHCP lease, so the
        # management IP decides which entry gets the name.
        for n in (api_fetch_all(api, "/ip/neighbor") or []):
            mac = n.get("mac-address", "")
            if not mac:
                continue
            ident = str(n.get("identity") or n.get("name") or "")
            via = str(n.get("interface", "") or "")
            fw = re.split(r"\s+\(", str(n.get("version", "") or ""))[0]
            plat = str(n.get("platform", "") or "")
            # BOARD column of the CLI print (= GUI "Board Name"): distinct from
            # `platform` (generic "MikroTik"); carried via CDP when present.
            board = str(n.get("board", "") or "")
            target, extra_ip = mac, ""
            for key in ("address4", "address"):
                ip = str(n.get(key, "") or "").split("/")[0]
                if not ip:
                    continue
                try:
                    ia = ipaddress.ip_address(ip)
                    private = ia.version == 4 and ia.is_private
                except ValueError:
                    continue
                if not private or ip in own_ips:
                    continue
                if ip in ip2mac and ip2mac[ip] != mac:
                    target = ip2mac[ip]     # lease lives under another MAC of this box
                else:
                    extra_ip = ip
                break
            d = macinfo.get(target)
            if d is None:
                target, d = mac, macinfo.setdefault(
                    mac, {"mac": mac, "ips": set(), "hostname": "", "via": ""})
            if ident and not d["hostname"]:
                d["hostname"] = ident
            if not d["via"]:
                d["via"] = via
            if fw and not d.get("fw"):
                d["fw"] = fw
            if plat and not d.get("platform"):
                d["platform"] = plat
            if board and not d.get("board"):
                d["board"] = board
            if extra_ip and target == mac:
                d["ips"].add(extra_ip)
                ip2mac.setdefault(extra_ip, mac)

        # ---- routed subnets (VLANs) from /ip/address
        nets = []
        for a in (api_fetch_all(api, "/ip/address") or []):
            try:
                net = ipaddress.ip_network(a.get("address", ""), strict=False)
                nets.append((net, a.get("interface", "") or str(net)))
            except ValueError:
                pass
        lan_nets = [net for net, iface in nets if iface not in wan_names]
        self.lan_nets = lan_nets

        def net_of(ip):
            try:
                ia = ipaddress.ip_address(ip)
            except ValueError:
                return ""
            for net, iface in nets:
                if ia in net:
                    return f"{iface} ({net})"
            return "other"

        # ---- connection tracking -> per-device live rate
        def in_lan(ip):
            try:
                ia = ipaddress.ip_address(ip)
            except ValueError:
                return False
            return any(ia in net for net in lan_nets)

        dev_rate: dict = {}  # ip -> [rx_bps, tx_bps, conns]
        proto_acc: dict = {}  # proto -> [rx_bps, tx_bps, flows] (WAN-bound only)
        port_acc: dict = {}   # "tcp/443" -> [rx_bps, tx_bps, flows]
        flows_n = 0
        # keep the address->names map fresh here (request threads must not use this API conn)
        if time.time() - _dns_rev["ts"] > DNS_TTL:
            try:
                build_dns_rev(api)
            except Exception as e:
                log.debug("dns cache invert failed: %s", e)
        for c in self.conns(api):
            src = c.get("src-address", "").split("%")[0]
            dst = c.get("dst-address", "").split("%")[0]
            rs = str(c.get("reply-src-address", "")).split("%")[0]
            rd = str(c.get("reply-dst-address", "")).split("%")[0]
            # the LAN endpoint may only appear on the reply side (port-forwards / dstnat)
            local_ip = next((ip for ip in (src, dst, rd, rs) if in_lan(ip)), "")
            if not local_ip:
                continue  # router's own WAN traffic, not a LAN device
            if in_lan(src) and in_lan(dst):
                continue  # LAN -> LAN never crosses the WAN, not a device WAN rate
            orig = as_num(c.get("orig-rate", 0))
            # RouterOS v6 names this field "reply-rate"; both are bps, so the fallback is safe
            repl = as_num(c.get("repl-rate", 0)) or as_num(c.get("reply-rate", 0))
            inbound = not in_lan(src)  # original dir came from outside (dstnat / inbound)
            d = dev_rate.setdefault(local_ip, [0.0, 0.0, 0])
            if inbound:
                d[0] += orig  # outside -> device
                d[1] += repl
            else:
                d[0] += repl  # device -> outside, reply carries the download
                d[1] += orig
            d[2] += 1
            # global protocol/port tallies (same LAN-view direction as dev_rate)
            flows_n += 1
            proto = str(c.get("protocol", "")).lower() or "?"
            rx_o, tx_o = (orig, repl) if inbound else (repl, orig)
            pa = proto_acc.setdefault(proto, [0.0, 0.0, 0])
            pa[0] += rx_o
            pa[1] += tx_o
            pa[2] += 1
            port = c.get("dst-port") or c.get("src-port")
            pk = "icmp" if proto == "icmp" else (f"{proto}/{port}" if port else proto)
            qa = port_acc.setdefault(pk, [0.0, 0.0, 0])
            qa[0] += rx_o
            qa[1] += tx_o
            qa[2] += 1

        # one physical box often appears under several MACs (bridge vs radio,
        # MAC randomisation): when two inventory entries claim the same IP,
        # keep the lease/ARP owner, adopt the other's name/port if missing,
        # and drop duplicates left without any IP
        ip_macs: dict = {}
        for mac, d in macinfo.items():
            for ip in d["ips"]:
                ip_macs.setdefault(ip, []).append(mac)
        merged_out = set()
        dup_macs = set()
        for ip, macs in ip_macs.items():
            if len(macs) < 2:
                continue
            owner = ip2mac.get(ip) if ip2mac.get(ip) in macs else macs[0]
            od = macinfo[owner]
            for m in macs:
                if m == owner:
                    continue
                od2 = macinfo[m]
                if not od["hostname"] and od2["hostname"]:
                    od["hostname"] = od2["hostname"]
                if not od["via"] and od2["via"]:
                    od["via"] = od2["via"]
                if not od.get("fw") and od2.get("fw"):
                    od["fw"] = od2["fw"]
                if not od.get("platform") and od2.get("platform"):
                    od["platform"] = od2["platform"]
                if not od.get("board") and od2.get("board"):
                    od["board"] = od2["board"]
                od2["ips"].discard(ip)
                dup_macs.add(m)
        for m in dup_macs:
            if not macinfo[m]["ips"]:   # duplicate had nothing else to show
                merged_out.add(m)

        # per-IP totals accumulated locally (survives conntrack expiry)
        dt = cfg["interval_seconds"]
        per_dev_rows = []
        devices_out = []
        for mac, d in macinfo.items():
            if mac in merged_out:
                continue
            ips = sorted(d["ips"]) or [""]
            ip = ips[0]
            rx_bps = sum(dev_rate.get(x, [0, 0, 0, 0, 0])[0] for x in ips)
            tx_bps = sum(dev_rate.get(x, [0, 0, 0, 0, 0])[1] for x in ips)
            conns = sum(dev_rate.get(x, [0, 0, 0, 0, 0])[2] for x in ips)
            active = (rx_bps + tx_bps) > 1000 or len(ips) > 0 and any(
                x in ip2mac for x in ips
            )
            devices_out.append(
                {
                    "mac": mac,
                    "ip": ip,
                    "ips": ips,
                    "hostname": d["hostname"] or "",
                    "vendor": vendor_of(mac) or d.get("platform", ""),
                    "board": d.get("board", ""),
                    "fw": d.get("fw", ""),
                    "interface": d["via"],
                    "rx_bps": round(rx_bps, 1),
                    "tx_bps": round(tx_bps, 1),
                    "connections": conns,
                    "online": bool(ips),
                    "net": net_of(ip),
                }
            )
            per_dev_rows.append(
                (mac, ip, d["hostname"], rx_bps * dt / 8, tx_bps * dt / 8, now)
            )
        # devices seen only in conntrack (static IPs etc.)
        for ip, v in dev_rate.items():
            if ip not in ip2mac and ip:
                devices_out.append(
                    {
                        "mac": ip,  # no MAC known; key by IP so frontend maps stay unique
                        "ip": ip,
                        "ips": [ip],
                        "hostname": "",
                        "vendor": "",
                        "fw": "",
                        "interface": "",
                        "rx_bps": round(v[0], 1),
                        "tx_bps": round(v[1], 1),
                        "connections": v[2],
                        "online": True,
                        "net": net_of(ip),
                    }
                )
                per_dev_rows.append((ip, ip, "", v[0] * dt / 8, v[1] * dt / 8, now))

        # ---- wireless associations (attach SSID/chipset/signal to devices)
        try:
            wifi_radios, wifi_assoc = wifi_info(api)
        except Exception:
            wifi_radios, wifi_assoc = {}, {}
        for dev in devices_out:
            a = wifi_assoc.get((dev.get("mac") or "").upper())
            if a:
                dev["wlan"] = a
                if not dev["ip"] and a.get("ip"):     # bridge AP: ARP is empty,
                    dev["ip"] = a["ip"]              # but last-ip tells us who
                    dev["ips"] = [a["ip"]]           # this station is
                    dev["net"] = net_of(a["ip"])
        # stations associated on this box's radios that no ARP/lease knows
        # (pure bridge AP: clients have no ARP entry here) — still devices
        # worth showing; last-ip from the registration table gives identity
        known_macs = {(d.get("mac") or "").upper() for d in devices_out}
        for mac, a in wifi_assoc.items():
            if mac in known_macs:
                continue
            lip = (a.get("ip") or "").strip()
            devices_out.append({
                "mac": mac, "ip": lip, "ips": [lip] if lip else [],
                "hostname": "", "vendor": vendor_of(mac) or "", "board": "",
                "fw": "", "interface": a.get("radio", ""), "rx_bps": 0.0,
                "tx_bps": 0.0, "connections": 0, "online": True,
                "net": net_of(lip) if lip else "", "wlan": a,
            })
        wifi_out = []
        for name, info in sorted(wifi_radios.items()):
            info = dict(info)
            info["radio"] = name
            info["stations"] = sum(1 for a in wifi_assoc.values() if a.get("radio") == name)
            wifi_out.append(info)

        # ---- blocked devices
        blocked_ips = set()
        for e in (api_fetch_all(api, "/ip/firewall/address-list") or []):
            if e.get("list") == cfg.get("block_list_name", "DASH_BLOCKED"):
                blocked_ips.add(e.get("address", ""))
        for dev in devices_out:
            dev["blocked"] = dev["ip"] in blocked_ips or any(i in blocked_ips for i in dev["ips"])

        # attach cumulative totals from db
        with _db_lock, hist_db() as c:
            tots = {r[0]: r for r in c.execute(
                "SELECT mac, rx_total, tx_total, first_seen FROM devices")}
        for dev in devices_out:
            t = tots.get(dev["mac"])
            if t:
                dev["rx_total"] = round(t[1], 0)
                dev["tx_total"] = round(t[2], 0)
                dev["first_seen"] = t[3]

        devices_out.sort(key=lambda d: (-d["online"], -(d["rx_bps"] + d["tx_bps"])))

        # fold this poll's protocol/port rate estimates into the 60 s analytics
        # window; idle conns contribute 0 (rates read 0), which is why these are
        # labelled estimates and volume comes from the exact WAN byte counters
        if self._acc_ts is None:
            self._acc_ts = now
        for prefix, acc in (("proto:", proto_acc), ("port:", port_acc)):
            for k, v in acc.items():
                a = self._acc.setdefault(prefix + k, [0.0, 0.0, 0])
                a[0] += v[0] * dt / 8
                a[1] += v[1] * dt / 8
                a[2] += v[2]
        acc_flush = None
        if self._acc and now - self._acc_ts >= 60:
            acc_flush = [(now, k, v[0], v[1], v[2]) for k, v in self._acc.items()]
            self._acc = {}
            self._acc_ts = now

        # ---- persist
        with _db_lock, hist_db() as c:
            c.executemany(
                "INSERT OR REPLACE INTO samples VALUES (?,?,?,?)",
                [(now, "", wan_rx_bps, wan_tx_bps)]
                + [(now, mac, rx, tx) for mac, ip, hn, rx, tx, _ in per_dev_rows if (rx or tx)],
            )
            if wan_rx_by is not None:
                c.execute("INSERT OR REPLACE INTO wan_bytes VALUES(?,?,?)",
                          (now, wan_rx_by, wan_tx_by))
            c.execute("INSERT OR REPLACE INTO flows_hist VALUES(?,?,?)",
                      (now, flows_n, len(dev_rate)))
            if acc_flush:
                c.executemany("INSERT OR REPLACE INTO anet VALUES(?,?,?,?,?)", acc_flush)
            for mac, ip, hn, rx, tx, ts in per_dev_rows:
                c.execute(
                    """INSERT INTO devices(mac, ip, hostname, first_seen, last_seen, rx_total, tx_total)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(mac) DO UPDATE SET
                         ip=excluded.ip,
                         hostname=CASE WHEN excluded.hostname!='' THEN excluded.hostname ELSE devices.hostname END,
                         last_seen=excluded.last_seen,
                         rx_total=devices.rx_total+excluded.rx_total,
                         tx_total=devices.tx_total+excluded.tx_total""",
                    (mac, ip, hn, ts, ts, rx, tx),
                )
        if int(now) % 600 < dt:
            db_prune(cfg["retention_hours"])

        totals = {
            "devices_online": sum(1 for d in devices_out if d["online"]),
            "devices_total": len(devices_out),
            "wan_rx_bps": round(wan_rx_bps, 1),
            "wan_tx_bps": round(wan_tx_bps, 1),
            "flows": flows_n,
            "active_hosts": len(dev_rate),  # LAN IPs with a live WAN connection
        }
        netsum = {}
        for d in devices_out:
            n = netsum.setdefault(d.get("net", ""), {"online": 0, "total": 0, "rx_bps": 0.0, "tx_bps": 0.0})
            n["total"] += 1
            n["online"] += 1 if d["online"] else 0
            n["rx_bps"] += d["rx_bps"]
            n["tx_bps"] += d["tx_bps"]

        # ---- topology graph for the network map
        topo_nets = []
        by_net = {}
        for dev in devices_out:
            lbl = dev.get("net") or "other"
            if lbl == "other" or not dev["ips"]:
                continue
            iface = (dev.get("interface") or "").split(",")[0]
            net_iface = lbl.split(" (")[0]
            group = iface if iface and iface != net_iface else ""
            by_net.setdefault(lbl, {}).setdefault(group, []).append(
                {"mac": dev["mac"], "ip": dev["ip"],
                 "name": dev["hostname"] or dev["vendor"] or dev["ip"],
                 "fw": dev.get("fw") or "",
                 "board": dev.get("board") or dev.get("vendor") or "",
                 "wlan": (dev.get("wlan") or {}).get("ssid") and {
                     k: (dev["wlan"].get(k) or "").replace("Atheros ", "")
                     for k in ("ssid", "signal", "chipset", "band")}})
        for lbl, groups in sorted(by_net.items()):
            topo_nets.append({
                "label": lbl,
                "groups": [{"port": p, "devices": ds}
                           for p, ds in sorted(groups.items(), key=lambda kv: -len(kv[1]))]})
        topo = {
            "wan": {"name": (wan_meta[0]["name"] if wan_meta else "wan"),
                    "rate": (wan_meta[0].get("rate", "") if wan_meta else ""),
                    "ip": wan_ip_local or wan_ip},
            "router": {"board": system.get("board", ""), "version": system.get("version", "")},
            "nets": topo_nets,
        }
        with _state_lock:
            STATE["system"] = system
            STATE["wan"] = {
                "ifaces": wan_meta,
                "ip": wan_ip_local or wan_ip,
                "gw": wan_ip,
                "rx_bps": round(wan_rx_bps, 1),
                "tx_bps": round(wan_tx_bps, 1),
            }
            STATE["devices"] = devices_out
            STATE["totals"] = totals
            STATE["nets"] = netsum
            STATE["topo"] = topo
            STATE["wifi"] = wifi_out
        STATE["config"] = {
            "host": cfg["host"],
            "user": cfg.get("user", ""),
            "profile": cfg.get("active", ""),
            "allow_control": bool(cfg.get("allow_control")),
            "interval": cfg["interval_seconds"],
        }


# --------------------------------------------------------------------------
# Local web auth (Pi-hole style)
#   first boot: no auth.json -> a setup page forces creating the login password
#   through the browser (like pihole's setup wizard). Whoever reaches it first
#   on a shared LAN claims the dashboard; recovery is CLI `--passwd` ON THE BOX.
#   auth.json then holds the PBKDF2-SHA256 (240k iter, random salt) hash,
#   chmod 600; `--passwd` afterwards means RESET (forgot password).
#   `--no-passwd` removes the account -> back to setup-required (never open).
# --------------------------------------------------------------------------
PBKDF2_ITERS = 240_000
SESSION_TTL = 12 * 3600          # sliding; refreshed on each request
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_COOLDOWN = 300        # seconds of lockout per IP
MIN_PW_LEN = 8

_auth_lock = threading.Lock()    # serializes password rotation


def hash_password(pw: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF2_ITERS)
    return salt, dk.hex()


def load_auth() -> dict | None:
    try:
        return json.loads(AUTH_PATH.read_text())
    except Exception:
        return None


def auth_enabled() -> bool:
    # fail CLOSED: a present-but-corrupt auth.json must NOT open the gate;
    # verify_password() then simply refuses every login until the file is fixed
    # or removed via the CLI (--no-passwd). Absent file now means "setup mode"
    # (first boot), never "open dashboard".
    return AUTH_PATH.exists()


def setup_mode() -> bool:
    return not AUTH_PATH.exists()


def set_password_file(pw: str, only_if_absent: bool = False) -> bool:
    """Write the auth file (PBKDF2 outside the lock). With only_if_absent the
    check-and-create is atomic under _auth_lock — this is what makes the setup
    wizard safe against a concurrent second creator (no TOCTOU)."""
    if len(pw) < MIN_PW_LEN:
        raise ValueError(f"password too short (min {MIN_PW_LEN} chars)")
    # the CLI may run on a fresh box before the server ever started — tables
    # must exist before the revoke-DELETEs below
    db_init()
    salt, digest = hash_password(pw)
    payload = json.dumps(
        {"salt": salt, "hash": digest, "algo": f"pbkdf2-sha256:{PBKDF2_ITERS}",
         "set": time.time()}, indent=2)
    with _auth_lock:
        if only_if_absent and AUTH_PATH.exists():
            return False
        # atomic replace: a crash mid-write cannot leave a half-written auth file
        tmp = AUTH_PATH.with_name("auth.json.tmp")
        tmp.write_text(payload)
        tmp.chmod(0o600)
        os.replace(tmp, AUTH_PATH)
    with _db_lock, db() as c:
        c.execute("DELETE FROM sessions")      # old sessions die with the password
        c.execute("DELETE FROM login_fail")
    return True


def verify_password(pw: str) -> bool:
    a = load_auth()
    if not a:
        return False
    _, digest = hash_password(pw, a["salt"])
    return hmac.compare_digest(digest, a["hash"])


def check_login_lock(ip: str) -> int:
    """seconds left to wait, 0 when allowed. Stored in SQLite so a service
    restart cannot be used to dodge the cooldown."""
    with _db_lock, db() as c:
        row = c.execute("SELECT locked_until FROM login_fail WHERE ip=?", (ip,)).fetchone()
    if row and row[0] > time.time():
        return int(row[0] - time.time())
    return 0


def note_login_fail(ip: str) -> None:
    with _db_lock, db() as c:
        row = c.execute("SELECT count FROM login_fail WHERE ip=?", (ip,)).fetchone()
        cnt = (row[0] if row else 0) + 1
        until = 0.0
        if cnt >= LOGIN_FAIL_LIMIT:
            until = time.time() + LOGIN_FAIL_COOLDOWN
            cnt = 0
        c.execute("INSERT OR REPLACE INTO login_fail VALUES(?,?,?)", (ip, cnt, until))


def note_login_ok(ip: str) -> None:
    with _db_lock, db() as c:
        c.execute("DELETE FROM login_fail WHERE ip=?", (ip,))


def session_issue() -> str:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    with _db_lock, db() as c:
        c.execute("DELETE FROM sessions WHERE exp < ?", (now,))
        c.execute("INSERT INTO sessions VALUES(?,?)", (sid, now + SESSION_TTL))
    return sid


def session_valid(sid: str) -> bool:
    now = time.time()
    with _db_lock, db() as c:
        row = c.execute("SELECT exp FROM sessions WHERE sid=?", (sid,)).fetchone()
        if not row or row[0] <= now:
            c.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            return False
        c.execute("UPDATE sessions SET exp=? WHERE sid=?", (now + SESSION_TTL, sid))
        return True


def session_revoke(sid: str) -> None:
    with _db_lock, db() as c:
        c.execute("DELETE FROM sessions WHERE sid=?", (sid,))


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------
app = FastAPI(title="MikroTik LAN Monitor")
monitor = RouterMonitor()


@app.middleware("http")
async def _no_cache(request, call_next):
    # The dashboard is ALWAYS gated: no account yet -> only the setup wizard is
    # reachable; account exists -> only the login page is reachable without a
    # valid session. Static assets stay public (login/setup pages use them).
    path = request.url.path
    deny_to = "/setup"
    ok = path.startswith("/static")
    if not ok:
        if setup_mode():
            ok = path.startswith(("/setup", "/api/setup"))
        else:
            deny_to = "/login"
            ok = path.startswith(("/login", "/api/login"))
            if not ok:
                ok = bool(request.cookies.get("sid", "")) and \
                    session_valid(request.cookies.get("sid", ""))
    if not ok:
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, 401)
        return RedirectResponse(deny_to, status_code=302)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------------
# Live flows (per-device peer list) — built from the cached conntrack snapshot
# --------------------------------------------------------------------------
SERVICES = {
    20: "FTP-d", 21: "FTP", 22: "SSH", 23: "telnet", 25: "SMTP", 53: "DNS",
    67: "DHCP", 68: "DHCP", 80: "HTTP", 110: "POP3", 123: "NTP", 143: "IMAP",
    179: "BGP", 389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 515: "printer",
    587: "SMTP", 636: "LDAPS", 853: "DoT", 993: "IMAPS", 995: "POP3S",
    1080: "SOCKS", 1194: "OpenVPN", 1433: "MSSQL", 1701: "L2TP", 1900: "SSDP",
    1935: "RTMP", 3240: "Plex", 3389: "RDP", 3478: "STUN", 3479: "STUN",
    5060: "SIP", 51820: "WireGuard", 5222: "XMPP", 5223: "APNs", 5353: "mDNS",
    5355: "LLMNR", 5683: "CoAP", 6379: "Redis", 6881: "BitTorrent", 8080: "HTTP-alt",
    8443: "HTTPS-alt", 9091: "Transmission", 9418: "git", 11371: "HKP",
    27017: "MongoDB", 31337: "Backrooms?",
}


def service_of(port, proto):
    """Human label for a flow: well-known port, protocol families, or the raw port."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        return proto or "?"
    if proto == "udp":
        if p == 443:
            return "QUIC"
        if p in (3478, 3479):
            return "STUN/TURN"
        if p in (53, 123, 5353, 500, 4500, 1701):
            return SERVICES.get(p, "udp")
        if 6881 <= p <= 6889:
            return "BitTorrent"
        return f"udp/{p}"
    if 6881 <= p <= 6889:
        return "BitTorrent"
    return SERVICES.get(p, f"tcp/{p}")


_ptr_cache: dict = {}          # ip -> (name, expires)   name "" = known-negative
_ptr_busy: set = set()
_ptr_lock = threading.Lock()
_ptr_pool = None               # lazily created ThreadPoolExecutor
DNS_TTL = 30.0
_dns_rev = {"ts": 0.0, "map": {}, "lock": threading.Lock()}


def resolve_ptr(ip: str):
    """Cached reverse DNS. Unknown names resolve in the background, so the API
    never blocks on slow or black-holed PTR lookups."""
    global _ptr_pool
    now = time.time()
    with _ptr_lock:
        hit = _ptr_cache.get(ip)
        if hit and hit[1] > now:
            return (hit[0] or None), True
        fresh = hit is None
        if fresh and ip not in _ptr_busy and len(_ptr_busy) < 16:
            if _ptr_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                _ptr_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ptr")
            _ptr_busy.add(ip)
            _ptr_pool.submit(_do_ptr, ip)
    return (hit[0] if hit else None), fresh


def _do_ptr(ip: str) -> None:
    import socket
    name = ""
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(2.0)      # librouteros sets its own timeout
        try:
            name = socket.gethostbyaddr(ip)[0]
        finally:
            socket.setdefaulttimeout(old)
    except Exception:
        name = ""
    with _ptr_lock:
        # positive answers are stable; negatives expire fast so transient failures retry
        _ptr_cache[ip] = (name, time.time() + (7200 if name else 300))
        _ptr_busy.discard(ip)


def dns_names_for(ip: str) -> list:
    """P1 hint: names from the router's own DNS cache, inverted (address -> names).

    The map is rebuilt by the sampler thread only — librouteros connections are
    not thread-safe, so request handlers must never touch the API directly.
    """
    with _dns_rev["lock"]:
        m = _dns_rev["map"]
        return list(m.get(ip, []))[:3]


def build_dns_rev(api) -> dict:
    """Invert /ip/dns/cache into address -> [names]; called from the poll thread."""
    m = {}
    for r in api_fetch_all(api, "/ip/dns/cache") or []:
        nm = (r.get("name") or "").rstrip(".")
        for a in str(r.get("address", "")).split(","):
            a = a.split("%")[0].strip()
            if a and nm:
                m.setdefault(a, [])
                if nm not in m[a]:
                    m[a].append(nm)
    with _dns_rev["lock"]:
        _dns_rev["map"] = m
        _dns_rev["ts"] = time.time()
    return m


def dns_rev_age() -> float:
    return round(time.time() - _dns_rev["ts"], 1)


def suffix_of(host: str) -> str:
    """Rough registered-domain suffix of a PTR name (good enough to group peers)."""
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in (
        "co", "com", "net", "org", "gov", "edu", "ac", "go", "or", "ne", "in", "com.cn"
    ):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def device_peers(conns, lan_nets, ip: str, limit: int = 25, self_ips=()) -> dict:
    """Aggregate conntrack rows for one LAN IP into a peer list.

    `self_ips` = the router's own WAN address(es): flows there are hairpin /
    port-test traffic, not a real remote peer, so they are reported separately.
    """
    nets = [ipaddress.ip_network(n) for n in lan_nets] or [
        ipaddress.ip_network(x) for x in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ]
    self_ips = {str(x) for x in self_ips if x}

    def in_lan(a):
        try:
            ia = ipaddress.ip_address(a)
        except ValueError:
            return False
        return any(ia in n for n in nets)

    def side(c):
        """-> (direction, peer_ip) from this device's point of view, NAT-aware."""
        src = c.get("src-address", "").split("%")[0]
        dst = c.get("dst-address", "").split("%")[0]
        rs = str(c.get("reply-src-address", "")).split("%")[0]
        rd = str(c.get("reply-dst-address", "")).split("%")[0]
        # original tuple: src->dst ; reply tuple: reply-src is the original *destination*,
        # reply-dst is the original *source* (both after NAT where applicable)
        if src == ip:
            return ("lan", dst) if in_lan(dst) else ("out", dst)
        if dst == ip:
            return ("lan", src) if in_lan(src) else ("in", src)
        # dstnat/port-forward: header shows the router's WAN address as dst, but the
        # reply side names the real LAN target -> the peer is the remote initiator
        if rs == ip:
            return ("lan", src) if in_lan(src) else ("in", src)
        if rd == ip:
            return ("lan", dst) if in_lan(dst) else ("out", dst)
        return None, None

    peers: dict = {}
    totals = {"conns": 0, "rx_bps": 0.0, "tx_bps": 0.0}
    hairpin = {"conns": 0, "rx_bps": 0.0, "tx_bps": 0.0, "rx_total": 0.0, "tx_total": 0.0, "ips": {}}
    lan_conns = 0
    for c in conns:
        d, peer = side(c)
        if not d:
            continue
        if in_lan(peer):                 # LAN <-> LAN / device <-> router: not a WAN peer
            lan_conns += 1
            continue
        if peer in self_ips:             # hairpin / port-test to the router's own address
            o = as_num(c.get("orig-rate", 0))
            r = as_num(c.get("repl-rate", 0)) or as_num(c.get("reply-rate", 0))
            hairpin["conns"] += 1
            hairpin["rx_bps"] += (o if d == "in" else r)
            hairpin["tx_bps"] += (r if d == "in" else o)
            hairpin["rx_total"] += as_num(c.get("orig-bytes", 0) if d == "in" else c.get("repl-bytes", 0))
            hairpin["tx_total"] += as_num(c.get("repl-bytes", 0) if d == "in" else c.get("orig-bytes", 0))
            hairpin["ips"][peer] = hairpin["ips"].get(peer, 0) + 1
            continue
        orig = as_num(c.get("orig-rate", 0))
        repl = as_num(c.get("repl-rate", 0)) or as_num(c.get("reply-rate", 0))
        ob = as_num(c.get("orig-bytes", 0))
        rb = as_num(c.get("repl-bytes", 0))
        p = peers.setdefault(peer, {
            "ip": peer, "conns": 0, "rx_bps": 0.0, "tx_bps": 0.0, "rx_total": 0.0,
            "tx_total": 0.0, "ports": {}, "proto": {}, "inbound_conns": 0,
            "unanswered": 0, "first_dir": d,
        })
        p["conns"] += 1
        if d == "in":
            p["rx_bps"] += orig
            p["tx_bps"] += repl
            p["rx_total"] += ob
            p["tx_total"] += rb
            p["inbound_conns"] += 1
            if not c.get("seen-reply"):
                p["unanswered"] += 1
        else:
            p["rx_bps"] += repl
            p["tx_bps"] += orig
            p["rx_total"] += rb
            p["tx_total"] += ob
        port = c.get("dst-port") or c.get("src-port")
        proto = str(c.get("protocol", ""))
        if port is not None:
            k = service_of(port, proto)
            label = k if "/" in k else f"{k}:{port}"   # "HTTPS:443", not "udp/443:443"
            p["ports"][label] = p["ports"].get(label, 0) + 1
        p["proto"][proto] = p["proto"].get(proto, 0) + 1
        totals["conns"] += 1
        totals["rx_bps"] += (orig if d == "in" else repl)
        totals["tx_bps"] += (repl if d == "in" else orig)

    rows = sorted(peers.values(),
                  key=lambda p: (-((p["rx_bps"] + p["tx_bps"])), -(p["rx_total"] + p["tx_total"])))
    top = rows[:limit]
    for p in top:
        p["rx_bps"] = round(p["rx_bps"], 1)
        p["tx_bps"] = round(p["tx_bps"], 1)
        p["rx_total"] = round(p["rx_total"])
        p["tx_total"] = round(p["tx_total"])
        p["ports"] = [k for k, _ in sorted(p["ports"].items(), key=lambda kv: -kv[1])[:3]]
        p["proto"] = [k for k, _ in sorted(p["proto"].items(), key=lambda kv: -kv[1])]
        p["inbound"] = p["first_dir"] == "in"
    return {
        "ip": ip,
        "peers_total": len(peers),
        "conns": totals["conns"],
        "rx_bps": round(totals["rx_bps"], 1),
        "tx_bps": round(totals["tx_bps"], 1),
        "peers": top,
        "truncated": len(rows) > limit,
        "lan_conns": lan_conns,
        "hairpin": {"conns": hairpin["conns"], "ips": sorted(hairpin["ips"]),
                    "rx_total": round(hairpin["rx_total"]),
                    "tx_total": round(hairpin["tx_total"])},
    }


@app.get("/api/peers")
def api_peers(ip: str, limit: int = 25):
    """Who this LAN device is talking to right now (conntrack), with name hints."""
    conns = list(monitor.last_conns or [])
    lan = [str(n) for n in (monitor.lan_nets or [])]
    with _state_lock:
        wan = STATE.get("wan") or {}
        self_ips = [wan.get("ip", ""), wan.get("gw", "")]
    data = device_peers(conns, lan, ip, max(1, min(limit, 60)), self_ips=self_ips)
    # name enrichment: router DNS cache first (free, already on the router), then lazy rDNS
    pending = 0
    for p in data["peers"]:
        p["names"] = dns_names_for(p["ip"])          # cached, never touches the router
        ptr, fresh = resolve_ptr(p["ip"])
        p["ptr"] = ptr
        p["suffix"] = suffix_of(ptr) if ptr else ""
        if fresh:
            pending += 1
    # group peers by PTR suffix so "37 peers -> *.s3.amazonaws.com" is visible
    groups = {}
    for p in data["peers"]:
        s = p.get("suffix")
        if not s:
            continue
        g = groups.setdefault(s, {"suffix": s, "peers": 0, "rx_bps": 0.0, "tx_bps": 0.0})
        g["peers"] += 1
        g["rx_bps"] += p["rx_bps"]
        g["tx_bps"] += p["tx_bps"]
    data["groups"] = sorted(groups.values(),
                            key=lambda g: -(g["rx_bps"] + g["tx_bps"]))[:6]
    data["names_pending"] = pending
    data["updated"] = STATE.get("updated")
    data["dns_cache_age"] = dns_rev_age()
    return data


@app.on_event("startup")
def _startup():
    try:
        db_init()
    except sqlite3.OperationalError as e:  # the most common container death:
        raise RuntimeError(               # $DASH_BASE (/data) not writable ->
            f"cannot open {DB_PATH}: {e}. Fix: the data dir must be writable "
            "by uid 1000 (host: sudo chown -R 1000:1000 <data dir>, or pass "
            "--user 0 when running the container)") from e
    threading.Thread(target=load_oui, daemon=True).start()
    monitor.start()


@app.get("/api/state")
def api_state():
    with _state_lock:
        return {
            "connected": STATE["connected"],
            "last_error": STATE["last_error"],
            "updated": STATE["updated"],
            "system": STATE["system"],
            "wan": STATE["wan"],
            "devices": STATE["devices"],
            "totals": STATE["totals"],
            "nets": STATE["nets"],
            "wifi": STATE.get("wifi", []),
            "topo": STATE["topo"],
            "config": STATE["config"] or {"host": load_config()["host"]},
        }


@app.get("/api/history")
def api_history(range_min: float = 15, bucket: float = 0, ip: str = "", mac: str = ""):
    # per-device series are keyed by MAC (IP only as fallback for keyless devices)
    key = mac or ip or ""
    cutoff = time.time() - range_min * 60
    with _db_lock, hist_db() as c:
        rows = c.execute(
            "SELECT ts, rx, tx FROM samples WHERE key=? AND ts>=? ORDER BY ts", (key, cutoff)
        ).fetchall()
    if bucket > 0 and rows:
        b, out, acc, t0 = bucket, [], [], rows[0][0]
        for ts, rx, tx in rows:
            if ts - t0 >= b:
                out.append(
                    [t0, sum(a[0] for a in acc) / len(acc), sum(a[1] for a in acc) / len(acc)]
                )
                acc, t0 = [], ts
            acc.append((rx, tx))
        if acc:
            out.append([t0, sum(a[0] for a in acc) / len(acc), sum(a[1] for a in acc) / len(acc)])
        rows = out
    return {"key": key, "series": [[r[0], r[1], r[2]] for r in rows]}


@app.get("/api/history/stack")
def api_history_stack(range_min: float = 15, bucket: float = 0):
    """Per-device bucketed series for the stacked (netflow-style) chart."""
    cutoff = time.time() - range_min * 60
    with _db_lock, hist_db() as c:
        rows = c.execute(
            "SELECT key, ts, rx, tx FROM samples WHERE key!='' AND ts>=? ORDER BY key, ts",
            (cutoff,),
        ).fetchall()
    per_key: dict = {}
    for key, ts, rx, tx in rows:
        per_key.setdefault(key, []).append((ts, rx, tx))
    series = []
    for key, pts in per_key.items():
        if bucket > 0 and pts:
            b, out, acc, t0 = bucket, [], [], pts[0][0]
            for ts, rx, tx in pts:
                if ts - t0 >= b:
                    out.append([t0, sum(a[0] for a in acc) / len(acc), sum(a[1] for a in acc) / len(acc)])
                    acc, t0 = [], ts
                acc.append((rx, tx))
            if acc:
                out.append([t0, sum(a[0] for a in acc) / len(acc), sum(a[1] for a in acc) / len(acc)])
            pts = out
        series.append({"key": key, "points": pts})
    return {"series": series}


@app.get("/api/analytics/history")
def api_analytics_history(range_min: float = 360):
    """Range data for the analytics panels: exact WAN traffic volume, active
    flows / active local hosts over time, and protocol/port byte distribution
    (conntrack rate-derived estimates rolled up every 60 s in `anet`)."""
    cutoff = time.time() - range_min * 60
    with _db_lock, hist_db() as c:
        vol = c.execute(
            "SELECT COALESCE(SUM(rx),0), COALESCE(SUM(tx),0) FROM wan_bytes WHERE ts>=?",
            (cutoff,)).fetchone()
        flows = c.execute(
            "SELECT ts, flows, hosts FROM flows_hist WHERE ts>=? ORDER BY ts",
            (cutoff,)).fetchall()
        protos = c.execute(
            "SELECT key, SUM(rx), SUM(tx), SUM(flows) FROM anet "
            "WHERE ts>=? AND key LIKE 'proto:%' GROUP BY key "
            "ORDER BY SUM(rx)+SUM(tx) DESC", (cutoff,)).fetchall()
        ports = c.execute(
            "SELECT key, SUM(rx), SUM(tx), SUM(flows) FROM anet "
            "WHERE ts>=? AND key LIKE 'port:%' GROUP BY key "
            "ORDER BY SUM(rx)+SUM(tx) DESC LIMIT 10", (cutoff,)).fetchall()
    if len(flows) > 400:  # block-mean downsample keeps the chart light
        step = -(-len(flows) // 400)
        blocks = [flows[i:i + step] for i in range(0, len(flows), step)]
        flows = [[b[0][0], sum(r[1] for r in b) / len(b), sum(r[2] for r in b) / len(b)]
                 for b in blocks]
    else:
        flows = [[r[0], r[1], r[2]] for r in flows]

    def port_svc(key: str):
        proto, _, port = key.partition("/")
        return (service_of(port, proto) if port else proto), proto

    return {
        "volume": {"rx": round(vol[0]), "tx": round(vol[1])},
        "flows": flows,
        "protocols": [{"key": k.split(":", 1)[1], "rx": round(rx), "tx": round(tx),
                       "flows": int(n)} for k, rx, tx, n in protos],
        "ports": [dict({"key": k.split(":", 1)[1], "rx": round(rx), "tx": round(tx),
                        "flows": int(n)}, **{"svc": port_svc(k.split(":", 1)[1])[0]})
                  for k, rx, tx, n in ports],
    }


@app.get("/api/analytics/conns")
def api_analytics_conns(limit: int = 15):
    """Global 'top conversations' from the live conntrack snapshot: local device
    <-> remote peer, aggregated per 5-tuple-ish key. Byte totals are per live
    session (entries vanish when a connection expires), not historical."""
    conns = list(monitor.last_conns or [])
    nets = [ipaddress.ip_network(n) for n in (monitor.lan_nets or [])]
    with _state_lock:
        wan_ip = (STATE.get("wan") or {}).get("ip", "")
        gw_ip = (STATE.get("wan") or {}).get("gw", "")
        hosts = {d["ip"]: (d.get("hostname") or d.get("vendor") or "")
                 for d in STATE.get("devices", [])}
    skip_ips = {x for x in (wan_ip, gw_ip) if x}

    def in_lan(a):
        try:
            ia = ipaddress.ip_address(a)
        except ValueError:
            return False
        return any(ia in n for n in nets)

    conv: dict = {}
    total_flows = 0
    for c in conns:
        src = c.get("src-address", "").split("%")[0]
        dst = c.get("dst-address", "").split("%")[0]
        rs = str(c.get("reply-src-address", "")).split("%")[0]
        rd = str(c.get("reply-dst-address", "")).split("%")[0]
        # NAT-aware local/remote attribution (reply tuple is inverted:
        # reply-src is the original destination, reply-dst the original source)
        if in_lan(src):
            local, remote, outbound = src, dst, True
        elif in_lan(dst):
            local, remote, outbound = dst, src, False
        elif in_lan(rd):
            local, remote, outbound = rd, rs, True
        elif in_lan(rs):
            local, remote, outbound = rs, rd, False
        else:
            continue
        if in_lan(remote) or not remote or remote in skip_ips:
            continue  # LAN<->LAN and hairpin-to-own-WAN are not conversations
        total_flows += 1
        proto = str(c.get("protocol", "")).lower() or "?"
        port = c.get("dst-port") or c.get("src-port")
        svc = service_of(port, proto) if port else proto
        key = (local, remote, proto, str(port or ""), svc)
        e = conv.setdefault(key, {
            "lan_ip": local, "remote": remote, "proto": proto, "port": str(port or ""),
            "svc": svc, "conns": 0, "rx_bps": 0.0, "tx_bps": 0.0,
            "rx_total": 0.0, "tx_total": 0.0})
        e["conns"] += 1
        ob = as_num(c.get("orig-bytes", 0))
        rb = as_num(c.get("repl-bytes", 0))
        o = as_num(c.get("orig-rate", 0))
        r = as_num(c.get("repl-rate", 0)) or as_num(c.get("reply-rate", 0))
        if outbound:
            e["tx_bps"] += o
            e["rx_bps"] += r
            e["tx_total"] += ob
            e["rx_total"] += rb
        else:
            e["rx_bps"] += o
            e["tx_bps"] += r
            e["rx_total"] += ob
            e["tx_total"] += rb
    rows = sorted(conv.values(), key=lambda e: -(e["rx_total"] + e["tx_total"]))[:limit]
    for e in rows:
        e["rx_bps"] = round(e["rx_bps"], 1)
        e["tx_bps"] = round(e["tx_bps"], 1)
        e["rx_total"] = round(e["rx_total"])
        e["tx_total"] = round(e["tx_total"])
        e["lan_name"] = hosts.get(e["lan_ip"], "")
        names = dns_names_for(e["remote"])
        ptr, _fresh = resolve_ptr(e["remote"])
        e["remote_name"] = (names[0] if names else "") or ptr or ""
    return {"updated": STATE.get("updated"), "flows": total_flows,
            "conversations": rows}


# --- router profiles (switch routers/accounts from the browser) ------------

def _clear_display(msg: str) -> None:
    """Drop every cached view so a different router never shows the previous
    router's board/devices/map while connecting or after a failed connect."""
    with _state_lock:
        STATE.update({"connected": False, "last_error": msg, "updated": 0.0,
                      "system": {}, "wan": {}, "devices": [], "totals": {},
                      "nets": {}, "topo": {}, "wifi": []})


def _sync_profiles(cfg: dict) -> dict:
    """Migrate a flat-only config (pre-profiles) into one profile named by
    its host, so switching back to it is always possible."""
    pro = cfg.setdefault("profiles", {})
    if cfg.get("user") and cfg.get("active") not in pro:
        name = cfg.get("host") or "router"
        pro[name] = {k: cfg.get(k) for k in _PROFILE_KEYS}
        cfg["active"] = name
    return cfg


@app.get("/api/profiles")
def api_profiles_list():
    """Saved router profiles — host/user only, passwords never leave here."""
    cfg = _sync_profiles(load_config())
    save_config(cfg)  # persist any migration
    return {"profiles": [{"name": n, "host": p.get("host", ""),
                          "user": p.get("user", "")}
                         for n, p in cfg.get("profiles", {}).items()],
            "active": cfg.get("active", "")}


@app.post("/api/profiles/use")
async def api_profiles_use(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "")
    cfg = _sync_profiles(load_config())
    prof = cfg.get("profiles", {}).get(name)
    if not prof or not prof.get("user"):
        return JSONResponse(status_code=404, content={"error": "no such profile"})
    for k in _PROFILE_KEYS:
        if k in prof:
            cfg[k] = prof[k]
    cfg["active"] = name
    save_config(cfg)  # save_config chmods 600; never log secrets
    monitor.drop()  # next cycle reconnects to the selected router
    _clear_display(f"connecting to {prof.get('host', '')}…")
    return {"ok": True, "active": name}


@app.post("/api/profiles/delete")
async def api_profiles_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name") or "")
    cfg = _sync_profiles(load_config())
    if cfg.get("profiles", {}).pop(name, None) is None:
        return JSONResponse(status_code=404, content={"error": "no such profile"})
    was_active = cfg.get("active") == name
    cfg["active"] = ""
    if was_active:  # active creds go with it: dashboard returns to setup state
        cfg["host"], cfg["user"], cfg["password"] = DEFAULT_CONFIG["host"], "", ""
        monitor.drop()
        with _state_lock:
            STATE["config"] = {}  # else the dialog keeps pre-filling the old router
        _clear_display("router credentials not configured")
    save_config(cfg)  # save_config chmods 600; never log secrets
    return {"ok": True}


@app.post("/api/config")
async def api_config(request: Request):
    body = await request.json()
    cfg = load_config()
    changed_conn = ("host" in body and body["host"] != cfg.get("host")) or \
                   ("user" in body and body["user"] != cfg.get("user"))
    for k in ("host", "user", "password", "allow_control", "interval_seconds"):
        if k in body:
            cfg[k] = body[k]
    # every saved credential set becomes a named profile (host is the name),
    # so an existing router can be switched back to without retyping anything
    name = str(body.get("name") or cfg.get("host") or "router")
    cfg.setdefault("profiles", {})[name] = {k: cfg.get(k) for k in _PROFILE_KEYS}
    cfg["active"] = name
    save_config(cfg)  # save_config chmods 600; never log secrets
    monitor.drop()  # reconnect with the new credentials on next cycle
    if changed_conn:
        _clear_display("connecting to the new router…")
    return {"ok": True}


@app.post("/api/config/forget")
async def api_config_forget():
    """Un-configure the router (IP + username + password) so the dashboard can
    be pointed at a different router or a different account. Only the router
    credentials are forgotten: the dashboard login and the history DB stay;
    the UI falls back to the Enter-credentials form on the next poll."""
    cfg = _sync_profiles(load_config())
    if cfg.get("active"):
        cfg.get("profiles", {}).pop(cfg["active"], None)
    cfg["active"] = ""
    cfg["host"], cfg["user"], cfg["password"] = DEFAULT_CONFIG["host"], "", ""
    save_config(cfg)  # drops the active profile; save_config chmods 600
    monitor.drop()
    with _state_lock:
        STATE["config"] = {}
    _clear_display("router credentials not configured — "
                   "open the dashboard and enter them")
    log.info("router credentials forgotten via web UI")
    return {"ok": True}


@app.post("/api/control")
async def api_control(req: Request):
    cfg = load_config()
    if not cfg.get("allow_control"):
        return JSONResponse({"ok": False, "error": "control disabled (set allow_control=true in config.json)"}, 403)
    body = await req.json()
    action = body.get("action")
    ip = body.get("ip", "")
    if not ip:
        return JSONResponse({"ok": False, "error": "ip required"}, 400)
    try:
        monitor.ensure()
        api = monitor.api
        lst = cfg.get("block_list_name", "DASH_BLOCKED")
        if action == "block":
            # ensure the drop rule exists once, at the very top
            existing = [
                f for f in (api_fetch_all(api, "/ip/firewall/filter") or [])
                if f.get("comment") == "mikrotik-dash"
            ]
            if not existing:
                api.path("/ip/firewall/filter").add(
                    **{"chain": "forward", "action": "drop",
                       "src-address-list": lst, "comment": "mikrotik-dash",
                       "placement": {"before": "0"}}
                )
            api.path("/ip/firewall/address-list").add(
                **{"list": lst, "address": ip, "comment": body.get("comment", "dashboard")}
            )
        elif action == "unblock":
            for e in (api_fetch_all(api, "/ip/firewall/address-list") or []):
                if e.get("list") == lst and e.get("address") == ip:
                    api.path("/ip/firewall/address-list").remove(**{".id": e[".id"]})
        else:
            return JSONResponse({"ok": False, "error": "unknown action"}, 400)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, 500)


@app.get("/setup")
def setup_page():
    if not setup_mode():
        return RedirectResponse("/login", status_code=302)
    return FileResponse(APP / "static" / "setup.html")


@app.post("/api/setup")
async def api_setup(req: Request):
    """One-shot first-boot account creation. Works ONLY while no account
    exists; once set, the sole recovery path is the CLI on this box."""
    if not setup_mode():
        return JSONResponse({"ok": False, "error":
            "an account already exists (someone else completed setup first). "
            "To take over, run: python server.py --passwd on the dashboard machine"}, 409)
    try:
        body = await req.json()
    except Exception:
        body = {}
    pw = str(body.get("password", ""))
    if pw != str(body.get("confirm", "")):
        return JSONResponse({"ok": False, "error": "passwords do not match"}, 400)
    if len(pw) < MIN_PW_LEN:
        return JSONResponse({"ok": False, "error": f"password too short (min {MIN_PW_LEN})"}, 400)
    # PBKDF2 off the event loop; the exists-check + write are one atomic
    # operation inside set_password_file, so two simultaneous creators cannot
    # both "win" — the loser gets 409
    from starlette.concurrency import run_in_threadpool
    created = await run_in_threadpool(set_password_file, pw, True)
    if not created:
        return JSONResponse({"ok": False, "error":
            "an account already exists (someone else completed setup first). "
            "To take over, run: python server.py --passwd on the dashboard machine"}, 409)
    log.info("dashboard login account created via setup wizard from %s",
             req.client.host if req.client else "?")
    sid = session_issue()
    secure = (req.headers.get("x-forwarded-proto") or req.url.scheme) == "https"
    resp = JSONResponse({"ok": True})
    resp.set_cookie("sid", sid, max_age=SESSION_TTL, httponly=True,
                    samesite="lax", secure=secure, path="/")
    return resp


@app.get("/login")
def login_page():
    if setup_mode():
        return RedirectResponse("/setup", status_code=302)
    return FileResponse(APP / "static" / "login.html")


@app.get("/api/session")
def api_session(req: Request):
    sid = req.cookies.get("sid", "")
    return {"auth_required": True, "setup_required": setup_mode(),
            "authed": bool(sid) and session_valid(sid)}


@app.post("/api/login")
async def api_login(req: Request):
    ip = req.client.host if req.client else "?"
    wait = check_login_lock(ip)
    if wait:
        return JSONResponse({"ok": False, "error": f"too many attempts — wait {wait}s"}, 429)
    try:
        body = await req.json()
    except Exception:
        body = {}
    if setup_mode():
        return JSONResponse({"ok": False, "error": "no account yet — use setup"}, 409)
    # PBKDF2 (~0.2 s by design) must not block the event loop while the whole
    # LAN's dashboard polls are in flight — run it in the threadpool
    from starlette.concurrency import run_in_threadpool
    pw_ok = await run_in_threadpool(verify_password, str(body.get("password", "")))
    if pw_ok:
        note_login_ok(ip)
        sid = session_issue()
        resp = JSONResponse({"ok": True})
        # Secure flag whenever the browser reached us over HTTPS (direct or via
        # an TLS-terminating reverse proxy); plain LAN http cannot set it.
        secure = (req.headers.get("x-forwarded-proto") or req.url.scheme) == "https"
        resp.set_cookie("sid", sid, max_age=SESSION_TTL, httponly=True,
                        samesite="lax", secure=secure, path="/")
        return resp
    note_login_fail(ip)
    left = check_login_lock(ip)
    if left:
        return JSONResponse({"ok": False, "error": f"locked — wait {left}s"}, 429)
    return JSONResponse({"ok": False, "error": "wrong password"}, 401)


@app.post("/api/logout")
def api_logout(req: Request):
    session_revoke(req.cookies.get("sid", ""))
    resp = JSONResponse({"ok": True})
    # deletion cookie must repeat path/samesite or the browser may keep the
    # real cookie alive (RFC 6265 matches attributes, not just the name)
    resp.delete_cookie("sid", path="/", samesite="lax")
    return resp


@app.get("/")
def index():
    return FileResponse(APP / "static" / "index.html")


app.mount("/static", StaticFiles(directory=APP / "static"), name="static")


if __name__ == "__main__":
    import sys
    import uvicorn

    ap = argparse.ArgumentParser(
        description="MikroTik LAN Monitor (web dashboard on :8899 by default)")
    ap.add_argument("--passwd", action="store_true",
                    help="RESET a forgotten web login password (run on this box; "
                         "works while the server is running — like pihole -a -p). "
                         "First-time setup happens in the browser instead.")
    ap.add_argument("--passwd-stdin", action="store_true",
                    help="read the new password from stdin (non-interactive)")
    ap.add_argument("--no-passwd", action="store_true",
                    help="remove the login account (next visit runs the setup wizard)")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()

    if args.passwd_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 8:
            sys.exit(f"refusing: password too short (min {8} chars)")
        set_password_file(pw)
        print(f"password set — {AUTH_PATH} written; login page is now active")
        raise SystemExit(0)
    if args.passwd:
        pw = getpass.getpass("new dashboard password: ")
        if len(pw) < 8:
            sys.exit("too short (min 8 chars)")
        if pw != getpass.getpass("repeat: "):
            sys.exit("mismatch, nothing changed")
        set_password_file(pw)
        print("password set — login page is now active (all existing sessions revoked)")
        raise SystemExit(0)
    if args.no_passwd:
        try:
            AUTH_PATH.unlink()
            print("login account removed — next visit runs the first-time setup wizard")
        except FileNotFoundError:
            print("no account was set (setup wizard is already pending)")
        raise SystemExit(0)

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
