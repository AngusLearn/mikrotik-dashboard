import os
import sys
from playwright.sync_api import sync_playwright

URL = os.environ.get("DASH_URL", "http://127.0.0.1:8899/")
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
fails = []

def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)

SAMPLE = """() => [...document.querySelectorAll('.tdev')].map(g => {
    const m = /translate\\(([-\\d.]+),([-\\d.]+)\\)/.exec(g.getAttribute('transform'));
    return {mac: g.dataset.mac, x: +m[1], y: +m[2]};
})"""

FORCE_SWAP = """() => {
    const s = JSON.parse(JSON.stringify(window._lastState));
    const g = s.topo.nets.find(n => n.groups.some(x => x.devices.length > 1));
    const grp = g.groups.find(x => x.devices.length > 1);
    grp.devices.reverse();
    window.__before = [...document.querySelectorAll('.tdev')].map(e => { e.__tag = 1; return e.dataset.mac; });
    renderTopo(s);
    return grp.devices.map(d => d.mac);
}"""

def run(reduced):
    global fails
    label = "reduced-motion" if reduced else "normal motion"
    print(f"\n--- {label} ---")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1600, "height": 900},
                        reduced_motion="reduce" if reduced else "no-preference")
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(URL, wait_until="networkidle")
        from _auth_helper import ensure_login; ensure_login(pg)
        pg.wait_for_selector(".tdev", timeout=15000)
        pg.wait_for_timeout(800)
        n = pg.eval_on_selector_all(".tdev", "els => els.length")
        check(f"[{label}] map rendered", n >= 2, f"{n} device nodes")
        start = pg.evaluate(SAMPLE)
        order0 = [d["mac"] for d in sorted(start, key=lambda d: d["y"])]
        ys0 = sorted(round(d["y"]) for d in start)
        check(f"[{label}] start positions are distinct integers (layout grid)",
              len(set(ys0)) == len(ys0) and all(float(d["y"]).is_integer() for d in start), str(ys0[:6]) + "…")

        target = pg.evaluate(FORCE_SWAP)          # reversed order of one group, tweened
        samples = []
        for _ in range(26):                        # ~500 ms of frames
            samples.append(pg.evaluate(SAMPLE))
            pg.wait_for_timeout(20)
        pg.wait_for_timeout(600)
        end = pg.evaluate(SAMPLE)

        def subset(rows):     # visual top-to-bottom order of the reversed group only
            return [d["mac"] for d in sorted(rows, key=lambda d: d["y"]) if d["mac"] in target]

        sub0, sub1 = subset(start), subset(end)
        changed = sub0 != sub1
        if reduced:
            layouts = {tuple(round(d["y"]) for d in s) for s in samples}
            check("[reduced-motion] swap is instantaneous (no intermediate frames)",
                  len(layouts) <= 1, f"{len(layouts)} distinct layouts during swap")
        else:
            layouts = {tuple(round(d["y"]) for d in s) for s in samples}
            check("[normal] positions animate (many intermediate layouts)", len(layouts) >= 5,
                  f"{len(layouts)} distinct layouts sampled")
            offs = {round((d["y"] - 52) % 36) for s in samples for d in s} - {0}
            check("[normal] intermediate frames are off-grid (true tween, not a jump)",
                  len(offs) > 0, f"off-grid offsets seen: {sorted(offs)[:6]}")
            check("[normal] group order visually reversed", changed and sub1 == target,
                  f"{sub0} -> {sub1} (want {target})")
            check("[normal] settles exactly back on integer grid (tween completes, not stuck)",
                  all(float(d["y"]).is_integer() for d in end),
                  str([d["y"] for d in sorted(end, key=lambda d: d['y'])][:6]) + "…")
            check("[normal] no device left stacked on another",
                  len({round(d["y"]) for d in end}) == len(end), "")
            check("[normal] unchanged devices kept their row",
                  all(abs(next(e["y"] for e in end if e["mac"] == s["mac"]) - s["y"]) < 0.01
                      for s in start if s["mac"] not in target), "")

        # DOM nodes must be reused (no innerHTML rebuild) so the tween has something to animate
        reused = pg.evaluate("() => [...document.querySelectorAll('.tdev')].every(e => e.__tag)")
        check(f"[{label}] device nodes reused, not rebuilt", bool(reused))

        # edges follow their nodes during the tween
        edges_ok = pg.evaluate("""() => {
            const dev = document.querySelector('.tedge[data-edge^="dev:"]');
            const m = /C([-\\d.]+),([-\\d.]+) ([-\\d.]+),([-\\d.]+)/.exec(dev.getAttribute('d'));
            return !!m;}""")
        check(f"[{label}] edge paths still well-formed after swap", edges_ok)

        # live numbers keep updating (liveTopo unaffected): WAN counter + busiest device
        before_txt = pg.evaluate("() => [document.querySelector('[data-k=\"wdl\"]').textContent,"
                                 " [...document.querySelectorAll('.tdev [data-k=\"dl\"]')].map(e=>e.textContent)]")
        pg.wait_for_timeout(3500)
        after_txt = pg.evaluate("() => [document.querySelector('[data-k=\"wdl\"]').textContent,"
                                " [...document.querySelectorAll('.tdev [data-k=\"dl\"]')].map(e=>e.textContent)]")
        any_rate = any("bps" in t and "—" not in t for t in after_txt[1])
        check(f"[{label}] live rate text populated", any_rate, f"wan={after_txt[0]!r}")
        check(f"[{label}] still polling after the tween", before_txt != after_txt or after_txt[0] != "↓—",
              f"{before_txt[0]!r} -> {after_txt[0]!r}")

        # clicking a node still opens the dialog
        pg.click(".tdev"); pg.wait_for_selector("#dev-body:not([hidden])", timeout=5000)
        check(f"[{label}] click on map node still opens device panel", True)
        pg.keyboard.press("Escape")

        # structural change (device added) must rebuild, not tween
        rebuilt = pg.evaluate("""() => {
            const s = JSON.parse(JSON.stringify(window._lastState));
            const grp = s.topo.nets.find(n => n.groups.some(x => x.devices.length)).groups[0];
            grp.devices.push({mac:'AA:BB:CC:DD:EE:FF', ip:'10.9.9.9', name:'ghost'});
            renderTopo(s);
            return [...document.querySelectorAll('.tdev')].filter(e => e.dataset.mac === 'AA:BB:CC:DD:EE:FF').length
                 + [...document.querySelectorAll('.tdev')].filter(e => !e.__tag).length; }""")
        check(f"[{label}] structural change rebuilds the map", rebuilt >= 2, f"marker count {rebuilt}")

        check(f"[{label}] no JS errors", not errs, "; ".join(errs))
        diag = pg.eval_on_selector("#diag", "e => e.textContent")
        print(f"    diag: {diag}")
        b.close()
        if errs:
            fails.append(f"{label}: js errors")

run(False)
run(True)
print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
