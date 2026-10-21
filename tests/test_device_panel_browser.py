import os, sys, json, urllib.request
from playwright.sync_api import sync_playwright

URL = os.environ.get("DASH_URL", "http://127.0.0.1:8899/")
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
fails = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))
    if not ok: fails.append(name)

from _auth_helper import api_opener
state = json.load(api_opener(URL).open(URL + "api/state"))
devs = sorted(state["devices"], key=lambda d: -(d["rx_bps"] + d["tx_bps"]))
busiest = devs[0]
print(f"target: {busiest['ip']} ({busiest['hostname'] or busiest['vendor']})")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1600, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(URL, wait_until="networkidle")
    from _auth_helper import ensure_login; ensure_login(pg)

    # 1. script survived load: rows + map rendered
    pg.wait_for_selector("tr.dev", timeout=10000)
    n_rows = pg.locator("tr.dev").count()
    n_map = pg.locator(".tdev").count()
    check("rows rendered", n_rows > 0, f"rows={n_rows}")
    check("map rendered", n_map > 0, f"map nodes={n_map}")
    check("wan chart has points", pg.evaluate("window._diagPts") > 0)
    diag = pg.text_content("#diag")
    check("diag ok (chart+scale)", "MISSING" not in diag, diag.strip())

    # 2. empty state visible before selection
    check("panel empty-state shown", pg.is_visible("#dev-empty") and pg.locator("#dev-body").is_hidden())

    # 3. click a table row -> panel opens with title/meta/chart
    pg.click(f"tr.dev:has-text('{busiest['ip']}')")
    pg.wait_for_selector("#dev-body:not([hidden])", timeout=5000)
    check("dev-body shown", pg.is_visible("#dev-body"))
    check("empty-state hidden", pg.locator("#dev-empty").is_hidden())
    title = pg.text_content("#dev-title").strip()
    check("title set", title not in ("", "—"), repr(title))
    meta = pg.text_content("#dev-meta")
    check("meta has ip", busiest["ip"] in meta, meta.strip())
    # wait for the async history fetch + chart build (the panel opens before
    # openDev()'s await resolves — asserting pixels instantly is a race)
    try:
        pg.wait_for_function("() => dlgChart && dlgChart.data.datasets[0].data.length > 0",
                             timeout=8000)
    except Exception:
        pass
    # chart canvas drawn (non-blank pixels)
    drawn = pg.eval_on_selector("#devChart", """c => {const g=c.getContext('2d');
      const d=g.getImageData(0,0,c.width,c.height).data;
      for(let i=3;i<d.length;i+=4) if(d[i]>0) return true; return false;}""")
    check("devChart rendered", drawn)

    # 4. flows list resolves to real content (not 'loading…')
    try:
        pg.wait_for_function("!document.querySelector('#f-list').textContent.includes('loading')", timeout=8000)
    except Exception: pass
    fl = pg.text_content("#f-list")
    check("flows populated", fl not in ("", "loading…"), repr(fl[:60]))
    check("flows note set", pg.text_content("#f-note").strip() != "")
    # tab toggle
    pg.click("#f-in")
    check("f-in tab active", "active" in (pg.get_attribute("#f-in", "class") or ""))
    pg.click("#f-all")
    check("f-all tab active", "active" in (pg.get_attribute("#f-all", "class") or ""))

    # 5. selection highlight on row + map node
    check("row selected", pg.locator(f"tr.dev.sel:has-text('{busiest['ip']}')").count() == 1)
    check("map node selected", pg.locator(f".tdev.sel[data-mac='{busiest['mac']}']").count() == 1)

    # 6. selection survives a 3s poll re-render
    pg.wait_for_timeout(3600)
    check("selection survives poll",
          pg.locator(f"tr.dev.sel:has-text('{busiest['ip']}')").count() == 1
          and pg.locator(".tdev.sel").count() == 1)

    # 7. map click opens panel for another device
    other = next((d for d in devs if d["ip"] != busiest["ip"] and d.get("ip")), None)
    if other:
        el = pg.locator(f".tdev[data-mac='{other['mac']}']")
        if el.count():
            el.click()
            ok = pg.text_content("#dev-meta").find(other["ip"]) >= 0
            check("map click switches device", ok)

    # 8. close button, then Esc on a fresh open
    pg.click("#dev-close")
    check("close hides panel", pg.locator("#dev-body").is_hidden() and pg.is_visible("#dev-empty"))
    pg.click(f"tr.dev:has-text('{busiest['ip']}')")
    pg.wait_for_selector("#dev-body:not([hidden])")
    pg.keyboard.press("Escape")
    check("Esc closes panel", pg.locator("#dev-body").is_hidden())

    # 9. narrow viewport stacks the panel below the map
    pg.set_viewport_size({"width": 1100, "height": 900})
    pg.wait_for_timeout(400)
    rmap = pg.eval_on_selector("#topo", "e => e.getBoundingClientRect().bottom")
    rpan = pg.eval_on_selector("#devpanel", "e => e.getBoundingClientRect().top")
    check("panel stacks below map @1100px", rpan >= rmap - 1, f"map.bottom={rmap:.0f} panel.top={rpan:.0f}")
    pg.set_viewport_size({"width": 1600, "height": 1000})

    check("no JS errors", not errs, "; ".join(errs)[:300])
    pg.screenshot(path=os.path.expanduser("~/.hermes/cache/scratch/dash_panel.png"), full_page=False)
    b.close()

print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
