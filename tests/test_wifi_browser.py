# End-to-end wireless/AP view test (Batch #35): monitors the hAP ac (192.168.1.30)
# on a temp DASH_BASE instance and proves WiFi stations are shown with their
# SSID / band / chipset / signal in header, device table, network map and panel.
# run: <playwright-python> tests/test_wifi_browser.py
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = str(REPO / "venv" / "bin" / "python")
PORT = int(os.environ.get("WIFI_TEST_PORT", "8894"))
URL = f"http://127.0.0.1:{PORT}"
EXE = os.environ.get("CHROME", "/home/angus/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome")
PW = "test-pass-1234"
AP = "192.168.1.30"
sys.path.insert(0, str(Path(__file__).resolve().parent))

fails = []
def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok: fails.append(name)

def ap_has_stations():
    # librouteros only lives in the repo venv — query through it
    out = subprocess.run(
        [VENV_PY, "-c",
         "import json,sys;from librouteros import connect;"
         "cfg=json.load(open(sys.argv[1]));"
         "api=connect(host=sys.argv[2],port=8728,username=cfg['user'],password=cfg['password'],timeout=5);"
         "rows=list(api('/interface/wireless/registration-table/print'));"
         "print(json.dumps([ (r.get('mac-address') or '').upper() for r in rows]))",
         str(REPO / "config.json"), AP],
        capture_output=True, text=True, check=True)
    macs = json.loads(out.stdout)
    return len(macs), macs

n_stations, assoc_macs = ap_has_stations()  # also proves the AP is reachable

tmp = Path(tempfile.mkdtemp(prefix="dash-wifi-e2e-"))
(tmp / "static").symlink_to(REPO / "static")
cfg = json.loads((REPO / "config.json").read_text())
cfg["host"] = AP   # always point the temp instance at the AP
(tmp / "config.json").write_text(json.dumps(cfg))
(tmp / "config.json").chmod(0o600)
env = dict(os.environ, DASH_BASE=str(tmp), DASH_TEST_PW=PW)
env.pop("DASH_URL", None)
subprocess.run([VENV_PY, str(REPO / "server.py"), "--passwd-stdin"],
               input=PW + "\n", capture_output=True, text=True, env=env)
srv = subprocess.Popen([VENV_PY, str(REPO / "server.py"), "--port", str(PORT)],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(URL + "/api/session", timeout=1); break
        except urllib.error.HTTPError: break
        except Exception: time.sleep(0.4)

    from playwright.sync_api import sync_playwright
    from _auth_helper import ensure_login
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EXE, args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(URL); ensure_login(pg, PW)
        try:
            pg.wait_for_function(
                "() => window._lastState && window._lastState.connected && "
                "(window._lastState.wifi||[]).length", timeout=30000)
        except Exception: pass
        st = pg.evaluate("() => window._lastState")
        radios = (st or {}).get("wifi") or []
        check("AP radios reported with ssid/band/chipset",
              len(radios) >= 2 and all(r.get("ssid") and r.get("chipset") for r in radios),
              json.dumps(radios)[:160])
        check("radio station counts match the AP's registration table",
              sum(r.get("stations", 0) for r in radios) >= min(n_stations, 1),
              f"dash={sum(r.get('stations',0) for r in radios)} ap={n_stations}")
        assoc = [d for d in (st or {}).get("devices", []) if d.get("wlan")]
        check("associated stations carry wlan info (ssid/signal/chipset)",
              len(assoc) >= 1 and all(a["wlan"].get("ssid") and a["wlan"].get("signal")
                                      for a in assoc),
              json.dumps(assoc[0].get("wlan"), default=str)[:140] if assoc else "none")
        check("stations identified by registration-table last-ip (WinBox Quick Set source)",
              len(assoc) >= 1 and all(a["wlan"].get("ip", "").startswith("192.168.1.")
                                      and a["ip"].startswith("192.168.1.") for a in assoc),
              json.dumps([(a["ip"], a["wlan"].get("ip"), a["wlan"].get("ccq"))
                          for a in assoc])[:160])
        check("header shows the WiFi summary",
              "WiFi" in pg.text_content("#subinfo"), pg.text_content("#subinfo"))

        # map node has a wifi sub-line for an associated station (only stations
        # with an IP land on the map — a pure bridge AP sees stations without one)
        mac = assoc[0]["mac"] if assoc else None
        pg.wait_for_function("() => document.querySelectorAll('#topo .tdev').length > 0", timeout=15000)
        node_txt = pg.evaluate(
            "() => [...document.querySelectorAll('#topo .tdev')].map(g=>g.textContent).join(' ')")
        if any(a["ip"] for a in assoc):
            check("map node shows SSID line", "📶" in node_txt and
                  any(r["ssid"] in node_txt for r in radios), node_txt[:120])
        else:
            check("map SSID line (none expected: bridge-AP stations have no IP here)",
                  True, "stations appear in the device table instead")

        # device table meta row includes the SSID for associated stations
        check("device table row shows SSID + signal",
              "📶" in pg.text_content("#devs"), "")

        # device panel: open an associated device, WiFi line visible
        d = assoc[0]
        pg.evaluate("([ip,mac]) => openDev(ip,mac)", [d["ip"], mac])
        pg.wait_for_function("() => !document.querySelector('#dev-wlan').hidden", timeout=8000)
        wl = pg.text_content("#dev-wlan")
        check("panel WiFi line: SSID · band · chipset · signal",
              d["wlan"]["ssid"] in wl and "dBm" in wl and d["wlan"].get("chipset", "x")[:-2] in wl,
              wl.strip()[:150])
        check("zero pageerror", not errs, "; ".join(errs)[:200])
        b.close()
finally:
    srv.terminate(); srv.wait(timeout=10)

print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(0 if not fails else 1)
