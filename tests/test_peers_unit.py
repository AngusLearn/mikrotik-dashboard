"""Unit test of device_peers() semantics with synthetic conntrack rows."""
import sys, importlib.util, types

spec = importlib.util.spec_from_file_location("srv", "/home/angus/mikrotik-dashboard/server.py")
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)

WAN, LAN = "203.0.113.9", "192.168.1.50"
rows = [
    # device -> outside (reply carries the download)
    {"src-address": LAN, "src-port": 41000, "dst-address": "8.8.8.8", "dst-port": 443,
     "reply-src-address": "8.8.8.8", "reply-dst-address": LAN, "protocol": "tcp",
     "orig-rate": 1000, "repl-rate": 5_000_000, "orig-bytes": 1_000, "repl-bytes": 9_000_000,
     "seen-reply": True},
    # outside -> device via dstnat (local endpoint only on reply-dst): a real inbound peer
    {"src-address": "198.51.100.7", "src-port": 5555, "dst-address": WAN, "dst-port": 51414,
     "reply-src-address": LAN, "reply-dst-address": "198.51.100.7", "protocol": "tcp",
     "orig-rate": 2_000_000, "repl-rate": 0, "orig-bytes": 4_000_000, "repl-bytes": 0,
     "seen-reply": True},
    # unanswered inbound probe (scan-ish): no reply seen
    {"src-address": "198.51.100.66", "src-port": 40000, "dst-address": WAN, "dst-port": 23,
     "reply-src-address": LAN, "reply-dst-address": "198.51.100.66", "protocol": "tcp",
     "orig-rate": 0, "repl-rate": 0, "orig-bytes": 120, "repl-bytes": 0, "seen-reply": False},
    # hairpin: device talking to the router's own WAN address
    {"src-address": LAN, "src-port": 42000, "dst-address": WAN, "dst-port": 51414,
     "reply-src-address": WAN, "reply-dst-address": LAN, "protocol": "tcp",
     "orig-rate": 0, "repl-rate": 0, "orig-bytes": 500, "repl-bytes": 500, "seen-reply": True},
    # LAN <-> LAN: must not appear as a WAN peer
    {"src-address": LAN, "src-port": 43000, "dst-address": "192.168.1.1", "dst-port": 53,
     "reply-src-address": "192.168.1.1", "reply-dst-address": LAN, "protocol": "udp",
     "orig-rate": 0, "repl-rate": 0, "orig-bytes": 80, "repl-bytes": 200, "seen-reply": True},
    # another outbound peer, bittorrent port
    {"src-address": LAN, "src-port": 44000, "dst-address": "203.0.113.77", "dst-port": 6881,
     "reply-src-address": "203.0.113.77", "reply-dst-address": LAN, "protocol": "udp",
     "orig-rate": 300_000, "repl-rate": 100_000, "orig-bytes": 50_000, "repl-bytes": 20_000,
     "seen-reply": True},
]
lan = ["192.168.1.0/24"]
out = srv.device_peers(rows, lan, LAN, limit=25, self_ips=[WAN])
fails = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
    if not cond:
        fails.append(name)

by = {p["ip"]: p for p in out["peers"]}
ips = sorted(by)
check("LAN peer excluded", "192.168.1.1" not in ips, str(ips))
check("hairpin peer separated", WAN not in ips and out["hairpin"]["conns"] == 1,
      f"hairpin={out['hairpin']}")
check("WAN peers counted", out["peers_total"] == 4, str(ips))
check("inbound via dstnat classified inbound",
      by.get("198.51.100.7", {}).get("inbound") is True, str(by.get("198.51.100.7")))
check("unanswered inbound counted",
      by.get("198.51.100.66", {}).get("unanswered") == 1, str(by.get("198.51.100.66")))
check("outbound peer not inbound", by.get("8.8.8.8", {}).get("inbound") is False)
check("download attributed to the reply side",
      by["8.8.8.8"]["rx_bps"] == 5_000_000 and by["8.8.8.8"]["tx_bps"] == 1000,
      str(by["8.8.8.8"]))
check("inbound download attributed to the original side",
      by["198.51.100.7"]["rx_bps"] == 2_000_000, str(by["198.51.100.7"]))
check("cumulative bytes tracked",
      by["8.8.8.8"]["rx_total"] == 9_000_000 and by["8.8.8.8"]["tx_total"] == 1_000)
check("service labels", by["8.8.8.8"]["ports"] == ["HTTPS:443"]
      and by["203.0.113.77"]["ports"] == ["BitTorrent:6881"],
      f'{by["8.8.8.8"]["ports"]} {by["203.0.113.77"]["ports"]}')
rates = [p["rx_bps"] + p["tx_bps"] for p in out["peers"]]
check("sorted by live rate desc", rates == sorted(rates, reverse=True), str(rates))
check("totals add up", out["conns"] == 4 and abs(out["rx_bps"] - 7_100_000) < 1,
      f"conns={out['conns']} rx={out['rx_bps']}")
check("lan_conns reported", out["lan_conns"] == 1, str(out["lan_conns"]))
check("device with no flows returns empty, no error",
      srv.device_peers(rows, lan, "192.168.1.99")["peers_total"] == 0)
check("suffix grouping", srv.suffix_of("hkg07s57-in-f7.1e100.net") == "1e100.net"
      and srv.suffix_of("a.b.co.uk") == "b.co.uk", srv.suffix_of("x.y.example.com"))
check("service_of unknown port keeps proto", srv.service_of(51234, "udp") == "udp/51234",
      srv.service_of(51234, "udp"))
print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
