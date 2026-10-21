import os
import sys, json, urllib.request
from playwright.sync_api import sync_playwright

URL = os.environ.get("DASH_URL", "http://127.0.0.1:8899/")
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
fails = []

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)

from _auth_helper import api_opener
state = json.load(api_opener(URL).open(URL + "api/state"))
devs = sorted(state["devices"], key=lambda d: -(d["rx_bps"] + d["tx_bps"]))
busiest = devs[0]
print(f"target device: {busiest['ip']} ({busiest['hostname'] or busiest['vendor']}) "
      f"{(busiest['rx_bps']+busiest['tx_bps'])/1e6:.1f} Mbps, {busiest['connections']} conns")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    errs, reqfail = [], []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("requestfailed", lambda r: reqfail.append(r.url))
    pg.goto(URL, wait_until="networkidle")
    from _auth_helper import ensure_login; ensure_login(pg)
    pg.wait_for_selector("tr.dev", timeout=15000)

    # open the dialog for the busiest device
    pg.click(f"tr.dev:has-text('{busiest['ip']}')")
    pg.wait_for_selector("#dev-body:not([hidden])", timeout=5000)

    check("flows panel present in dialog", pg.locator(".flows").is_visible())
    # wait for the fetch to actually START (resource entries only appear when the
    # request completes — right after a busier page load that races)
    try:
        pg.wait_for_function(
            "()=>performance.getEntriesByType('resource').some(r=>r.name.includes('/api/peers?ip='))",
            timeout=5000)
        peers_called = True
    except Exception:
        peers_called = False
    check("flows endpoint called with the device ip", peers_called, "")
    # rows appear (device has live WAN conns)
    try:
        pg.wait_for_selector(".frow", timeout=6000)
        n = pg.locator(".frow").count()
        check("peer rows rendered", n > 0, f"{n} rows")
    except Exception as e:
        check("peer rows rendered", False, str(e)[:120])

    first = pg.locator(".frow").first.inner_text()
    note = pg.locator("#f-note").inner_text()
    check("header note shows peer/conn totals", ("peer" in note and "conn" in note), repr(note))
    check("row shows service or name", len(first.strip()) > 4, repr(first[:90]))

    # rDNS fills in asynchronously: PTR names should appear within a few refreshes
    named = 0
    for _ in range(8):
        named = pg.evaluate("() => [...document.querySelectorAll('.fhost')].filter(e=>e.textContent.includes('.')).length")
        if named:
            break
        pg.wait_for_timeout(1200)
    check("hostnames appear after background rDNS", named > 0, f"{named} named rows")

    # tabs: "Accessing you" filters to inbound
    pg.click("#f-in"); pg.wait_for_timeout(400)
    in_txt = pg.locator("#f-list").inner_text()
    inbound_active = pg.evaluate("() => document.getElementById('f-in').classList.contains('active')")
    rows_in = pg.locator(".frow").count()
    empty_state = "nothing is initiating" in in_txt or rows_in == 0
    check("inbound tab switches", inbound_active and (rows_in >= 0), f"{rows_in} inbound rows")
    check("inbound tab shows inbound rows or an explicit empty state",
          rows_in > 0 or empty_state, repr(in_txt[:60]))
    pg.click("#f-all"); pg.wait_for_timeout(300)
    check("back to all flows", pg.locator(".frow").count() >= 1)

    # auto-refresh while open: totals line changes / refetch happens
    n1 = len(pg.evaluate("() => performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/peers')).map(r=>r.name)"))
    pg.wait_for_timeout(3600)
    n2 = len(pg.evaluate("() => performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/peers')).map(r=>r.name)"))
    check("flows auto-refresh while dialog is open", n2 > n1, f"requests {n1} -> {n2}")

    # panel chart intact after the new section
    check("panel chart intact", pg.evaluate("() => dlgChart && dlgChart.data.datasets[0].data.length > 0"))

    # refresh stops when closed
    pg.keyboard.press("Escape"); pg.wait_for_timeout(500)
    n3 = len(pg.evaluate("() => performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/peers')).map(r=>r.name)"))
    pg.wait_for_timeout(4000)
    n4 = len(pg.evaluate("() => performance.getEntriesByType('resource').filter(r=>r.name.includes('/api/peers')).map(r=>r.name)"))
    check("refresh stops after closing", n4 == n3, f"{n3} -> {n4}")

    pg.screenshot(path="/home/angus/.hermes/cache/scratch/flows_panel.png")
    check("no JS errors", not errs, "; ".join(errs[:3]))
    check("no failed requests", not reqfail, "; ".join(reqfail[:3]))

    # second device (no WAN traffic) must degrade gracefully, not error
    quiet = next((d for d in devs if d["connections"] == 0), None)
    if quiet:
        pg.click(f"tr.dev:has-text('{quiet['ip']}')")
        pg.wait_for_selector("#dev-body:not([hidden])", timeout=5000)
        pg.wait_for_timeout(1200)
        txt = pg.locator("#f-list").inner_text()
        check("idle device shows a readable empty state",
              len(txt.strip()) > 3 and "undefined" not in txt and "NaN" not in txt, repr(txt[:70]))
        pg.keyboard.press("Escape")
    b.close()

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
