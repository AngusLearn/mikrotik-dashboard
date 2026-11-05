"""Unit tests for the IPv6 support: EUI-64 IID derivation, NDP map parsing,
and v6 attribution in device_peers(). No router needed:
`venv/bin/python tests/test_ipv6_unit.py`.
"""
import os, sys, importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("srv", os.path.join(_HERE, "..", "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)


# ---- eui64_iid -------------------------------------------------------------
def test_eui64_iid_matches_live_slaac():
    # verified against a live router: C0:74:2B:FC:11:3F derives
    # fe80::c274:2bff:fefc:113f (U/L bit = bit 1 of first byte)
    assert srv.eui64_iid("C0:74:2B:FC:11:3F") == "c2742bfffefc113f"
    assert srv.v6_iid("fe80::c274:2bff:fefc:113f%bridge1") == srv.eui64_iid("c0:74:2b:fc:11:3f")


def test_eui64_iid_classic_example():
    # 00:11:22:33:44:55 -> 0211:22ff:fe33:4455 (dashed MAC form too)
    assert srv.eui64_iid("00:11:22:33:44:55") == "021122fffe334455"
    assert srv.eui64_iid("00-11-22-33-44-55") == "021122fffe334455"
    assert srv.eui64_iid("00:11:22:33:44:55") == srv.v6_iid(
        "2001:db8::0211:22ff:fe33:4455")


def test_eui64_iid_bad_mac():
    assert srv.eui64_iid("not-a-mac") == ""
    assert srv.eui64_iid("") == ""
    assert srv.eui64_iid("00:11:22:33:44") == ""


def test_v6_iid_invalid():
    assert srv.v6_iid("192.168.1.5") == ""
    assert srv.v6_iid("garbage") == ""


# ---- ndp_maps --------------------------------------------------------------
NDP_ROWS = [
    {"address": "fe80::c274:2bff:fefc:113f", "mac-address": "C0:74:2B:FC:11:3F",
     "interface": "bridge1", "status": "stale"},
    {"address": "2001:db8:1:2:aa:bb:cc:dd", "mac-address": "AA:BB:CC:00:00:01",
     "interface": "bridge1", "status": "reachable"},
    # a FAILED entry is not presence proof (device gone)
    {"address": "fe80::dead:beef:1", "mac-address": "DE:AD:BE:EF:00:01",
     "interface": "bridge1", "status": "failed"},
    # router's own link-local must not become a device
    {"address": "fe80::6e3b:6bff:feb9:8017", "mac-address": "6E:3B:6B:B9:80:17",
     "interface": "bridge1", "status": "reachable"},
    # malformed rows are skipped
    {"address": "fe80::1", "mac-address": "", "status": "reachable"},
    {"address": "", "mac-address": "AA:AA:AA:AA:AA:AA", "status": "reachable"},
]
OWN = ["fe80::6e3b:6bff:feb9:8017", "::1"]


def test_ndp_maps_presence_and_attribution():
    present, addr2mac = srv.ndp_maps(NDP_ROWS, OWN)
    assert "C0:74:2B:FC:11:3F" in present          # stale counts (like ARP)
    assert "AA:BB:CC:00:00:01" in present
    assert "DE:AD:BE:EF:00:01" not in present      # failed excluded
    assert "6E:3B:6B:B9:80:17" not in present      # router itself excluded
    assert addr2mac["2001:db8:1:2:aa:bb:cc:dd"] == "AA:BB:CC:00:00:01"
    assert "fe80::6e3b:6bff:feb9:8017" not in addr2mac  # own addr not mapped
    assert srv.ndp_maps([], OWN) == (set(), {})


# ---- device_peers with IPv6 ------------------------------------------------
LAN4 = "192.168.1.50"
LAN6 = "2001:db8:1:2:aa:bb:cc:dd"
WAN6 = "2001:db8:ffff::1"
CONNS = [
    # v6 outbound: device -> outside (reply carries download)
    {"src-address": LAN6, "src-port": 41000, "dst-address": "2001:4860:4860::8888",
     "dst-port": 443, "reply-src-address": "2001:4860:4860::8888",
     "reply-dst-address": LAN6, "protocol": "tcp", "orig-rate": 1000,
     "repl-rate": 4_000_000, "orig-bytes": 100, "repl-bytes": 8_000_000,
     "seen-reply": True},
    # v6 LAN<->LAN: never a WAN peer
    {"src-address": LAN6, "dst-address": "2001:db8:1:2::1", "protocol": "icmp6",
     "orig-rate": 0, "repl-rate": 0, "orig-bytes": 64, "repl-bytes": 64,
     "seen-reply": True},
    # v4 row must keep working untouched
    {"src-address": LAN4, "src-port": 42000, "dst-address": "93.184.216.34",
     "dst-port": 80, "reply-src-address": "93.184.216.34",
     "reply-dst-address": LAN4, "protocol": "tcp", "orig-rate": 500,
     "repl-rate": 50_000, "orig-bytes": 200, "repl-bytes": 20_000,
     "seen-reply": True},
]
NETS = ["192.168.1.0/24", "2001:db8:1::/48"]  # /64 would NOT contain the :2: subnet


def test_peers_v6_attributed_via_self_addrs():
    r = srv.device_peers(CONNS, NETS, LAN4, self_addrs=[LAN6])
    peers = {p["ip"]: p for p in r["peers"]}
    assert "2001:4860:4860::8888" in peers          # v6 peer appears
    assert peers["2001:4860:4860::8888"]["rx_bps"] == 4_000_000
    assert "93.184.216.34" in peers                 # v4 still there
    assert r["lan_conns"] == 1                      # the v6 LAN<->LAN row
    assert r["conns"] == 2


def test_peers_v6_invisible_without_self_addrs():
    # without the NDP-provided v6 addrs the v6 flows are NOT silently dropped
    # into another device: they simply don't belong to this ip
    r = srv.device_peers(CONNS, NETS, LAN4)
    peers = {p["ip"] for p in r["peers"]}
    assert "2001:4860:4860::8888" not in peers
    assert "93.184.216.34" in peers


def test_peers_v6_only_device():
    # a v6-only device (no v4): queried by its v6 address directly
    r = srv.device_peers(CONNS, NETS, LAN6)
    peers = {p["ip"]: p for p in r["peers"]}
    assert "2001:4860:4860::8888" in peers
    assert peers["2001:4860:4860::8888"]["tx_bps"] == 1000  # upload = orig


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
