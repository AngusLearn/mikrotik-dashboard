"""Unit tests for the Ports map mode helpers: bridge_port_map (FDB -> physical
port join, trunk/access classification) and topo_group_devices (grouping into
the map's nets[].groups[] shape). No router, no server needed:
`venv/bin/python tests/test_ports_unit.py`.
"""
import os, sys, importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("srv", os.path.join(_HERE, "..", "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)

# --- fixture shaped from the live RB3011 probe -------------------------------
BPORTS = [
    {"bridge": "bridge1", "interface": "ether5", "pvid": 1},
    {"bridge": "bridge1", "interface": "ether10", "pvid": 1},
    {"bridge": "bridge1", "interface": "bonding-PVE", "pvid": 1},
    {"bridge": "bridge-vlan2", "interface": "ether8", "pvid": 2},
]
BVLAN = [
    {"bridge": "bridge-vlan2", "vlan-ids": 2, "tagged": "bonding-PVE", "untagged": "ether8"},
]
HOSTS = [
    {"mac-address": "AA:01", "interface": "ether5"},
    {"mac-address": "aa:02", "interface": "ether10"},          # lower-case FDB row
    {"mac-address": "AA:03", "interface": "bonding-PVE"},
    {"mac-address": "6C:3B", "interface": "bridge1"},          # router's own MAC -> skip
    {"mac-address": "", "interface": "ether5"},                # junk -> skip
    {"mac-address": "AA:04", "interface": "ghost9"},           # not a member port -> skip
]


def test_mac2port_basic():
    m2p, meta = srv.bridge_port_map(HOSTS, BPORTS, BVLAN)
    assert m2p == {"AA:01": "ether5", "AA:02": "ether10", "AA:03": "bonding-PVE"}
    assert "6C:3B" not in m2p and "AA:04" not in m2p


def test_access_vs_trunk():
    _, meta = srv.bridge_port_map(HOSTS, BPORTS, BVLAN)
    assert meta["ether5"]["trunk"] is False and meta["ether5"]["pvids"] == [1]
    assert meta["ether8"]["trunk"] is False and meta["ether8"]["pvids"] == [2]
    assert meta["bonding-PVE"]["trunk"] is True           # tagged member of vlan 2
    assert meta["bonding-PVE"]["tagged"] == [2]


def test_empty_inputs():
    m2p, meta = srv.bridge_port_map(None, None, None)
    assert m2p == {} and meta == {}


def _dev(mac, ip, net="LAN (192.0.2.0/24)", port=None, wlan=None):
    d = {"mac": mac, "ip": ip, "ips": [ip], "hostname": mac, "net": net}
    if port:
        d["port"] = port
    if wlan:
        d["wlan"] = wlan
    return d


def test_port_grouping_order_and_idle():
    devs = [
        _dev("A", "1.1.1.1", port="ether10"), _dev("B", "1.1.1.2", port="ether10"),
        _dev("C", "1.1.1.3", port="ether5"),
        _dev("D", "1.1.1.4"),                          # no port -> idle
        _dev("E", "1.1.1.5", port="WLAN", wlan={"ssid": "home", "signal": "-50"}),
        _dev("F", "2.2.2.2", net=""),                  # net 'other' -> skipped
        _dev("G", "", net="LAN", ),                    # no ips -> skipped
    ]
    nets = srv.topo_group_devices(devs, lambda d: d.get("port") or "idle")
    assert len(nets) == 1 and nets[0]["label"] == "LAN (192.0.2.0/24)"
    ports = [g["port"] for g in nets[0]["groups"]]
    # biggest first; WLAN and idle always last (WLAN before idle: a real
    # location ahead of "port unknown")
    assert ports == ["ether10", "ether5", "WLAN", "idle"]
    assert [d["mac"] for d in nets[0]["groups"][0]["devices"]] == ["A", "B"]


def test_default_mode_empty_group_preserved():
    # historical default map: devices without a sub-interface get group ""
    # (attached straight to the net hub), NOT an "idle" node
    devs = [_dev("A", "1.1.1.1"), _dev("B", "1.1.1.2")]
    nets = srv.topo_group_devices(devs, lambda d: "")
    assert nets[0]["groups"][0]["port"] == ""


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, "->", e)
    sys.exit(1 if fails else 0)
