"""Unit tests for the last-good fetch cache (RouterMonitor.fetch).

Under router overload a print either traps or times out; api_fetch_all then
returns None. A failed fetch must NEVER blank the dashboard: the previous
rows are served and the cycle is marked degraded so run() reconnects.
No router, no server needed: `venv/bin/python tests/test_stale_unit.py`.
"""
import os, sys, importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("srv", os.path.join(_HERE, "..", "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)


class FakeApi:
    """Stands in for a librouteros connection; paths in .fail raise like a
    timeout/mid-stream trap."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.fail = set()

    def path(self, p):
        if p in self.fail:
            raise RuntimeError("timeout (synthetic)")
        return [dict(r) for r in self.rows.get(p, [])]


ARP_ROWS = [{"mac-address": "aa:bb:cc:00:00:01", "address": "192.0.2.5"}]


def test_success_caches():
    m = srv.RouterMonitor()
    api = FakeApi({"/ip/arp": ARP_ROWS})
    rows = m.fetch(api, "/ip/arp")
    assert rows == ARP_ROWS
    assert not m.poll_degraded
    assert m._last_ok["/ip/arp"] == ARP_ROWS


def test_failed_fetch_serves_last_good():
    m = srv.RouterMonitor()
    api = FakeApi({"/ip/arp": ARP_ROWS})
    m.fetch(api, "/ip/arp")
    api.fail.add("/ip/arp")
    assert m.fetch(api, "/ip/arp") == ARP_ROWS, "failed fetch must serve last-good rows"
    assert m.poll_degraded is True, "degraded flag must be set for run() to reconnect"


def test_unknown_noncritical_returns_empty_without_raising():
    m = srv.RouterMonitor()
    api = FakeApi()
    api.fail.add("/ip/neigh")
    assert m.fetch(api, "/ip/neigh") == []
    assert m.poll_degraded is True


def test_critical_without_cache_raises():
    m = srv.RouterMonitor()
    api = FakeApi()
    api.fail.add("/system/resource")
    try:
        m.fetch(api, "/system/resource", critical=True)
        raise AssertionError("critical fetch failure must raise")
    except RuntimeError as e:
        assert "/system/resource" in str(e)


def test_reset_forgets_cache():
    """A profile switch must not serve the previous router's cached rows."""
    m = srv.RouterMonitor()
    api = FakeApi({"/ip/arp": ARP_ROWS})
    m.fetch(api, "/ip/arp")
    api.fail.add("/ip/arp")
    m.reset()
    assert m._last_ok == {}
    assert m.fetch(api, "/ip/arp") == []


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
