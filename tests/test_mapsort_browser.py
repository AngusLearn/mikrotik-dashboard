# Browser suite for the network-map Speed/IP sort toggle (Batch #41).
# Deterministic: page timers are frozen after the first render so the 3 s
# poll cannot re-sort devices mid-test (live speed order churns every poll).
# Auth: _auth_helper via DASH_TEST_PW on a disposable instance, or point
# DASH_SID_DB at a history.db whose sessions table you may insert into
# (used against the dev instance where only the owner knows the password).
import os
import sys
from playwright.sync_api import sync_playwright

URL = os.environ.get("DASH_URL", "http://127.0.0.1:8899/")
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
SID_DB = os.environ.get("DASH_SID_DB", "")

fails = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)

def login(pg):
    if SID_DB:
        import sqlite3, time, secrets
        sid = secrets.token_urlsafe(32)
        with sqlite3.connect(SID_DB) as c:
            c.execute("CREATE TABLE IF NOT EXISTS sessions(sid TEXT PRIMARY KEY, exp REAL)")
            c.execute("INSERT OR REPLACE INTO sessions VALUES (?,?)", (sid, time.time() + 900))
        pg.context.add_cookies([{"name": "sid", "value": sid, "url": URL}])
        pg.goto(URL, wait_until="networkidle")   # re-goto root (reload would stay on /login)
    else:
        from _auth_helper import ensure_login
        ensure_login(pg)

NETDEVS = """() => {
  const t = (window._lastState || {}).topo; if (!t) return null;
  return t.nets.map(n => n.groups.map(g => g.devices.map(d => d.ip)).filter(x => x.length)).filter(x => x.length);
}"""
YPOS = r"""() => [...document.querySelectorAll('#topo g.tdev')].map(g => ({
  ip: (g.textContent.match(/(\d+\.){3}\d+/) || [null])[0],
  y: +(g.getAttribute('transform').match(/,([\d.]+)/) || [0, 0])[1]
})).filter(d => d.ip)"""

def ip_nums(ips):
    return [[int(x) for x in ip.split('.')] if ip else [999] * 4 for ip in ips]

def is_ip_sorted(ips):
    n = ip_nums(ips)
    return n == sorted(n)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1680, "height": 1050})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle")
    login(pg)
    pg.wait_for_selector(".tdev", timeout=15000)
    pg.wait_for_timeout(800)
    # freeze the poll/refresh timers: without this the server's per-poll speed
    # re-sort churns the order and the toggle-back comparison is a coin flip
    pg.evaluate("() => { for (let i = 1; i < 100000; i++) { try { clearInterval(i); clearTimeout(i); } catch (e) {} } }")
    pg.wait_for_timeout(400)

    speed = pg.evaluate(NETDEVS)
    check("topo state present with grouped devices",
          bool(speed) and any(len(g) >= 2 for grp in speed for g in grp),
          f"{len(speed or [])} net(s)")

    # default = speed order: label says Speed, and an IP-sorted group must not
    # yet exist unless the server order already happens to be IP-sorted
    check("button starts in Speed mode", "Speed" in pg.text_content("#mapsort"))

    pg.click("#mapsort")
    pg.wait_for_timeout(900)                      # tween settles
    ip_order = pg.evaluate(NETDEVS)
    groups = [g for grp in ip_order for g in grp]
    check("every group sorted by ascending IP after toggle",
          all(is_ip_sorted(g) for g in groups), f"{len(groups)} group(s)")
    check("button label flips to IP", "IP" in pg.text_content("#mapsort"))

    # node y order must follow the IP order inside each group
    rows = {r["ip"]: r["y"] for r in pg.evaluate(YPOS)}
    y_ok = all([rows[ip] for ip in g if ip in rows] ==
               sorted(rows[ip] for ip in g if ip in rows) for g in groups)
    check("node y order matches IP order (tweened layout, not just data)", y_ok)

    # toggle back: same (frozen) state must restore the pristine speed order
    pg.click("#mapsort")
    pg.wait_for_timeout(900)
    back = pg.evaluate(NETDEVS)
    check("toggle back restores the pristine speed order",
          back == speed, "order identical to pre-toggle state")
    check("button label flips back to Speed", "Speed" in pg.text_content("#mapsort"))

    # persistence: mode survives a full reload (localStorage). The page is
    # live again here, so only assert the sort rule, never a specific order.
    pg.click("#mapsort")            # -> IP again
    pg.wait_for_timeout(400)
    pg.reload(wait_until="networkidle")
    pg.wait_for_selector(".tdev", timeout=15000)
    pg.wait_for_timeout(1200)
    check("IP mode survives page reload (localStorage)",
          "IP" in pg.text_content("#mapsort") and
          all(is_ip_sorted(g) for grp in pg.evaluate(NETDEVS) for g in grp))

    check("no JS errors", not errs, "; ".join(errs[:3]))
    b.close()

print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
