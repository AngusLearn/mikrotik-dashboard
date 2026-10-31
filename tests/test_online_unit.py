"""Unit tests for device_present() — the presence test behind the 'online'
counter. A DHCP lease alone must NOT count as presence: it survives for
hours after the device left. No router, no server needed:
`venv/bin/python tests/test_online_unit.py`.
"""
import os, sys, importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("srv", os.path.join(_HERE, "..", "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)

dp = srv.device_present
M = "aa:bb:cc:00:00:01"


def test_lease_only_not_present():
    # device has an IP (from a DHCP lease) but no ARP entry and no traffic
    assert dp(M, ["192.0.2.50"], 0, arp_ips=set(), mndp_macs=set()) is False


def test_live_arp_present():
    assert dp(M, ["192.0.2.50"], 0, arp_ips={"192.0.2.50"}, mndp_macs=set()) is True


def test_live_traffic_present():
    assert dp(M, ["192.0.2.50"], srv.ONLINE_MIN_BPS + 1, arp_ips=set(), mndp_macs=set()) is True
    assert dp(M, ["192.0.2.50"], srv.ONLINE_MIN_BPS - 1, arp_ips=set(), mndp_macs=set()) is False


def test_mndp_beacon_present_case_insensitive():
    # RouterOS prints MNDP MACs lowercase, sometimes upper — both must match
    assert dp(M, [], 0, set(), {"AA:BB:CC:00:00:01"}) is True
    assert dp("AA:BB:CC:00:00:01", [], 0, set(), {"AA:BB:CC:00:00:01"}) is True


def test_wifi_association_present():
    assert dp(M, [], 0, set(), set(), associated=True) is True


def test_no_ip_no_proof_not_present():
    assert dp(M, [""], 0, arp_ips={"192.0.2.9"}, mndp_macs=set()) is False


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
