# Changelog

## 2026-10-22
20. **Batch #39 — device-panel chart now live** — the per-device traffic chart
    (`#devChart`) was a one-time snapshot fetched when the panel opened (with
    coarse 60 s buckets), so it visibly froze while flows below it kept moving.
    It now re-fetches every 5 s while open (bucket 30 s) and updates the chart
    in place (`update('none')`); the refresh timer is cleared on close/Esc.
    node --check on the extracted inline script; JS syntax OK.
19. **Batch #38 — traffic chart now live-pace** — the WAN/stack chart reloaded
    only every 15 s while the map refreshed every 3 s, so it read as stale next
    to the moving map dashes. `loadWan` now runs on the same 3 s tick (data was
    already sampled every `interval_seconds`; only the browser poll was slow).
    node --check on the extracted inline script; JS syntax OK.

## 2026-10-21
18. **Batch #37 — Docker/Pi packaging** — app verified container-shaped: measured
    footprint is ~50-60 MB RSS, <5 % steady CPU, all state behind `$DASH_BASE`.
    Added `requirements.txt` (pinned from live venv), `Dockerfile`
    (python:3.12-slim, non-root uid 1000, state on /data volume, HEALTHCHECK),
    `docker-compose.yml`, `.dockerignore` (config.json/auth.json/*.db never
    baked into image). server.py fix surfaced by the build: static files
    (index/login/setup, mount) resolved via `APP` = code dir instead of `BASE`
    — `BASE/$DASH_BASE` is *mutable state* and now cleanly separable
    (host behavior unchanged: APP == BASE there). Smoke-tested with podman:
    fresh volume -> setup wizard serves, chart vendor 200; after copying
    config+auth -> `connected to RouterOS at 192.168.1.1`, per-profile
    history db filling (12 rows), runs as uid 1000. Image 141 MB (mostly
    base). Rootless-podman on this box needed world-writable test volume
    (uidmap); rootful docker on Pi only needs `chown 1000:1000 data/`.
    Note: vendor-name OUI downloads need internet once (offline = graceful).

## 2026-10-18
17. **Batch #36 — station IPs on bridge APs (WinBox Quick Set parity)** —
    user noticed WinBox Quick Set shows wireless clients' IPs although the
    AP's ARP table holds almost nothing (pure bridge: routed traffic never
    hits the AP CPU). The source is the `last-ip` field of each
    `/interface/wireless/registration-table` row (new stack: `address`) —
    the first wifi_info probe had filtered it out. assoc now carries
    `ip` (+ `ccq`, `security`); stations appended for IP-less clients get
    their last-ip as real identity (ip/ips/net via net_of), so they appear
    on the map and in charts like normal devices, and ARP-matched devices
    with empty IP are backfilled. Device panel gains `CCQ %` (link quality,
    the WinBox column). test_wifi_browser.py extended ALL PASS 9 checks:
    all three live stations identified via last-ip (192.168.1.2/.5/.6,
    CCQ 83/98/26) and the map SSID check now fires on real nodes; live
    restarted.
16. **Batch #35 — WiFi stations view for APs (SSID / band / chipset / signal)** —
    monitoring a wireless AP (e.g. the hAP ac at 192.168.1.30) now surfaces
    its WiFi clients. New `wifi_info()` gathers radios + associations from
    both RouterOS WiFi stacks (legacy lives under `/interface/wireless` even
    on v7 — registration-table needs plain `print`, `reprint` traps; the new
    `/wifi` stack is probed as fallback only when legacy is absent). Stations
    are matched to devices by MAC and enriched with `{radio, ssid, band,
    chipset, signal, tx/rx rate, uptime}` (chipset from the radio's
    `interface-type`, e.g. "Atheros AR9888"); clients associated to a pure
    bridge AP have no IP on the AP (its ARP only holds 2 routed hosts) so
    unknown stations are appended as devices anyway (radio in the interface
    column, no map node — map groups by net, by design). UI: header gains
    `📶 N WiFi`; device-table rows and map nodes (auto-fit boxes, node pitch
    now `36+14*extra_lines`, wlan ssid added to the order-insensitive rebuild
    signature) show a `📶 SSID −55dBm` line; the docked device panel gets a
    cyan WiFi line `SSID · band · chipset · signal · PHY rate · online`.
    New suite `tests/test_wifi_browser.py` (:8894 `WIFI_TEST_PORT`, temp
    DASH_BASE pointed at the real AP) ALL PASS 8 checks, including station
    counts matching the AP's registration-table (3/3) and the exact panel
    line; regression `test_map_reorder_browser`, `test_device_panel_browser`,
    `test_flows_browser` re-run ALL PASS against a temp instance; unit suites
    green; live service restarted on the new code (WiFi lines appear when the
    AP profile is active).
15. **Batch #34 — per-profile history (charts no longer mix routers)** —
    supersedes Batch #32's "history stays shared" note. Router telemetry
    (`samples`, `devices`, `wan_bytes`, `flows_hist`, `anet`) now lands in
    **`history-<profile>.db`** (sanitized profile name); the legacy
    `history.db` keeps global auth state (`sessions`, `login_fail` — these
    deliberately do NOT split) and serves as the bucket for an unnamed/flat
    config. All telemetry reads and writes go through `hist_db()`, which
    resolves the active profile per operation and lazily creates the schema;
    `db_prune` now iterates every bucket so idle profiles cannot grow
    unbounded. New suite `tests/test_history_split.py` ALL PASS 10 checks
    against both real routers with live WAN traffic (400 KB/s curl during
    the window): profile A recorded 8.6 MB WAN rx in its own file, A's file
    frozen while B active and vice versa, switch-back resumes, legacy file
    untouched post-migration, `/api/history` serves only the active
    profile's points. Unit suites re-run green; live restarted, active
    profile file filling (legacy history.db stale since). Fresh per-profile
    history starts at upgrade time — old mixed history stays in history.db
    as read-only legacy and ages out with retention.
14. **Batch #33 — Router profiles dialog redesign (close button; switch vs add
    separated)** — per user feedback the dialog no longer mixes switching and
    adding in one dropdown. It now has a **✕ close button** (plus Esc) and two
    clearly separated zones: a **saved-routers list** (each row shows IP +
    username, the active one marked green "connected" and offers **Delete**;
    inactive rows offer **Switch**) and an **ADD / UPDATE ROUTER** form below
    a divider, which always starts blank. Deleting the *active* router closes
    the dialog so the banner's Enter-credentials flow is reachable again;
    deleting an inactive one refreshes the list in place. `#a-prof`/`#a-use`/
    `#a-forget` replaced by `#a-close`, `#prof-list` rows with `.p-use`/
    `.p-del` (`data-name`), server untouched. Both browser suites rewritten
    to the new structure and extended (✕ close/reopen, blank add-form,
    active-row-has-no-Switch assertions, flattened copied config so migration
    is deterministic even after profiles exist): forget ALL PASS 15,
    profiles ALL PASS 19 against both real routers; zero pageerror.
13. **Batch #32 — router profiles (one-click switch between routers/accounts)** —
    credentials are now stored as named **profiles** in config.json
    (`profiles: {name: {host,user,password,port}}` + `active`), migrated
    automatically from a flat config on first use (named by its host). The
    ⚙ Router dialog became a profile manager: a dropdown lists saved routers
    (active one starred); selecting another profile prefills host/user (never
    the password) and shows a **Switch** button — one click reconnects using
    that profile's stored password, no retyping. **Save & connect** upserts
    the selected profile (blank password keeps a saved profile's password);
    **Delete profile** removes one (deleting the active one returns the
    dashboard to the Enter-credentials state, login + history kept). New
    endpoints `GET /api/profiles` (host/user only, no passwords),
    `POST /api/profiles/use`, `POST /api/profiles/delete` — all login-gated.
    Also fixed the real staleness bug behind "it reused the last server's
    data": switching routers now immediately clears every cached view
    (devices, map, WAN, board, totals) via `_clear_display()` instead of
    showing the previous router's screen until the first successful poll;
    `/api/profiles/delete` of the active profile additionally clears the
    config snapshot so the dialog no longer pre-fills the forgotten router
    (found by the test, fixed for real — not by loosening the wait). New
    suite `tests/test_profiles_browser.py` ALL PASS 17 checks on both real
    routers (192.168.1.30 hAP ac ↔ 192.168.1.1 RB3011): migration, second
    profile add + board verification, one-click switch back with instant
    view clear + stored-password reconnect, dialog Switch flow, delete
    inactive/404 unknown/401 unauthenticated gates. Forget suite updated
    (credentials cleared from config, not file deletion) ALL PASS 13;
    unit suites re-run green; live restarted via systemctl --user.
    Note: traffic **history** (history.db, 48 h retention) stays shared
    across profiles — the live view is per-router, the charts are per-box.
12. **Batch #31 — web-UI "Forget router" (switch router/account without CLI)** —
    the dashboard can now be moved to another router or another RouterOS
    account entirely from the browser: a new header button **⚙ Router** opens
    the router dialog (the error banner's link shares the same opener now),
    which pre-fills the current host + username but NEVER the password, and
    offers a **Forget router** action behind a confirm dialog. New endpoint
    `POST /api/config/forget` (login-gated by the standing middleware)
    deletes `config.json`, drops the router connection and clears the state
    snapshot, so the poller degrades to its existing "credentials not
    configured" path and the UI shows the Enter-credentials form — re-entering
    new credentials immediately connects to the new router (`monitor.drop()` +
    next cycle), no service restart needed. `STATE.config` also carries
    `user` now for the prefill. New suite `tests/test_forget_browser.py`
    (temp DASH_BASE + temp login + own port, reads the real router) ALL PASS
    13 checks: forget refused 401 without login, prefill has host+user but
    empty password, confirm asked, config.json deleted from disk, `/api/state`
    stays 200 reporting not-configured, banner offers credentials, dialog
    empties and Forget hides, re-entered credentials reconnect and
    `config.json` comes back chmod 600, zero `pageerror`. Peers/auth-unit/
    prune-unit suites re-run green; live restarted via `systemctl --user`,
    reconnected and confirmed polling.
11. **Batch #30 — map node boxes auto-fit their text** — the Batch #27 board
   line (`RB962UiGS-5HacT2HnT · RouterOS 7.24.5`) overran the fixed 196 px
   node box (SVG text never wraps). New `fitDev()` measures each device
   node's rendered text (`getComputedTextLength`) after `buildTopo` and on
   every `liveTopo` rate update (a 0 bps row becoming 940 Mbps lengthens the
   row mid-life), widens the rect (grow-only, min 196), moves the online dot
   with the right edge, and extends the svg so nothing clips (`#topo`
   already scrolls horizontally). Verified on a throwaway :8898 instance:
   zero node texts over their box (measured, not eyeballed), board node grew
   196 → 239 px with the dot at w-10, a simulated 940 Mbps spike re-fit with
   no overflow, unrelated nodes keep 196 px (one long-rate node auto-grew to
   198), zero `pageerror`; map-reorder, device-panel and flows suites ALL
   PASS; live static bytes hash-equal to disk (no restart needed for UI).
10. **Batch #29 — service restored to its systemd user unit** — found that
    `mikrotik-dashboard.service` (user unit, enabled, `Restart=always`) exists
    and has been crash-looping on `address already in use` ever since restarts
    were done by hand (kill listener + `nohup server.py &`): the manual
    instance won the port and the unit's every restart died against it, while
    the noise in `server.log` looked routine. All earlier batches tonight were
    restarted that wrong way; the service ran unmanaged until now. Killed the
    stray nohup instance, `systemctl --user restart mikrotik-dashboard.service`
    — unit active, MainPID owns :8899, reconnected and confirmed polling,
    served bytes hash-equal to disk. From now on: restarts go through the
    unit, never nohup.
9. **Batch #28 — real board name (CLI `BOARD` column) instead of vendor** —
   Batch #25's claim that "the board name is what the neighbor sends as
   `platform`" was WRONG: `/ip/neighbor` carries TWO fields — `platform`
   (generic `MikroTik` on this AP) and a separate `board` field
   (`RB962UiGS-5HacT2HnT`, arrived via CDP: `discovered-by=cdp,mndp`), which
   is exactly the CLI `BOARD` column / GUI "Board Name". The poll now reads
   `board` too: `devices[].board` is exposed, the duplicate-MAC merge adopts
   it, and the map node, device-row meta and detail panel meta all show the
   board name where they previously fell back to the OUI vendor (vendor still
   used when no `board` is discovered, e.g. pure-MNDP neighbors or non-
   MikroTik gear). Verified on a throwaway :8898 instance reading the real
   router (direct librouteros dump confirmed `board: RB962UiGS-5HacT2HnT`):
   map node third line `RB962UiGS-5HacT2HnT · RouterOS 7.24.5`, row meta
   `192.168.1.30 · RB962UiGS-5HacT2HnT · RouterOS 7.24.5 · E4:8D:...`,
   panel meta carries it too, zero `pageerror`; device-panel, map-reorder and
   flows suites ALL PASS; live service restarted (pid 111775), reconnected
   and confirmed polling.
8. **Batch #27 — board + RouterOS firmware line on the network map** — the
   `fw`/`vendor` fields from Batch #25 now also reach the map: a device whose
   ND entry carries firmware grows its map node to a third line
   (`MikroTik · RouterOS 7.24.5`) with the box height 31 → 44 px and that
   row's pitch 36 → 50 px; devices without firmware render exactly as before
   (same box, same 36 px pitch), so hub/port centring, tween geometry and the
   reorder test are untouched (group height is preserved when its order
   changes). Backend change: `topo.nets[].groups[].devices[]` gained `fw` and
   `board`. Verified on a throwaway :8898 instance reading the real router —
   node text list `['💻 MikroTik-AP', '192.168.1.30 ↓0 bps ↑0 bps', 'MikroTik ·
   RouterOS 7.24.5']`, grown rect, all other nodes unchanged, min row gap
   still >= 36 px, click-to-open panel and Esc-close work on the grown node,
   zero `pageerror` — across 1600/1440/1100 px viewports; map-reorder,
   device-panel and flows suites ALL PASS afterwards; live service restarted
   and confirmed polling.
7. **Batch #26 — clock-step guard in the history pruner** — this host booted
   (20:02 HKT) with the RTC reading ~5 h ahead (`2026-10-18 04:02`), so the
   dashboard's pre-NTP first poll stamped one 26-row burst of `samples` into
   the future before `systemd-timesyncd` corrected the clock (journal: boot
   events at fake `04:02:15`, initial sync at `20:02:46`). Because every read
   query (`/api/history`, `/api/history/devices`) floors at `ts >= cutoff`
   and `db_prune` only deleted `ts < cutoff`, such rows would have poisoned
   every "recent" chart window permanently. `db_prune` now also deletes rows
   with `ts > now + 300` in `samples`, `wan_bytes`, `flows_hist` and `anet`
   (the 300 s margin is far beyond legitimate write/query skew). Verified:
   new `tests/test_prune_unit.py` ALL PASS (fresh/margin rows kept; old,
   future and RTC-skew-burst rows pruned; session expiry unchanged), live
   restart (uvicorn pid 88932) reconnected to RouterOS and the first prune
   tick removed all 26 poisoned rows from `history.db` (future-row count
   verified 0 afterwards, polling confirmed fresh via `wan_bytes`); all other
   suites re-run green afterwards (peers, auth unit, prune units;
   device-panel/flows/map-reorder/auth-browser had passed on the throwaway
   :8898 instance earlier the same night, page unchanged by this fix).
6. **Batch #25 — board platform + RouterOS firmware for MikroTik neighbors** —
   the ND poll (Batch #24) now also reads the `platform` and `version` fields
   every `/ip neighbor` entry carries: the firmware (e.g. `7.24.5`, stripped of
   the build suffix) shows as **`RouterOS 7.24.5`** in the device-row meta and
   in the detail panel line; the board `platform` is a vendor fallback, used
   only when the OUI database cannot name the MAC's manufacturer. The
   duplicate-MAC merge adopts `fw` and `platform` from the dropped duplicate
   (the ND entry often lives under the bridge MAC while the lease sits on the
   radio MAC), so the AP keeps both after dedupe. Note: what the board name
   shows is exactly what the neighbor sends as `platform` — this network's AP
   reports the generic `MikroTik`. Verified live on a throwaway `:8898`
   instance reading the real router (owner's live instance untouched):
   `devices[].fw == "7.24.5"` for the merged `MikroTik-AP` row, row meta and
   panel meta both render `RouterOS 7.24.5`, panel Esc/✕ close unaffected,
   zero `pageerror`; device-panel, flows, map-reorder, auth-browser (26/26),
   auth-unit and peers suites ALL PASS; live service restarted and reconnected
   with served bytes hash-equal to disk.
5. **Batch #24 — device identity from MikroTik Neighbor Discovery + duplicate-MAC
   merge** — `/ip neighbor` is now polled every cycle (read-only) and its
   `identity` becomes a fallback device name for MikroTik gear that has no
   DHCP lease (APs/switches show as e.g. `MikroTik-AP` instead of `unknown`);
   its `interface` also supplies the physical port when ARP had none. ARP/DHCP
   names still take precedence. Pitfall found live: a MikroTik box answers ND
   with its bridge MAC (`...:7E`) while its stale ARP entry used the radio MAC
   (`...:7F`) — the same IP under two MACs rendered as two devices. New merge:
   when several inventory entries claim one IP, the lease/ARP owner keeps it,
   adopts the duplicate's hostname/port if its own were empty, and duplicates
   left IP-less are dropped (device count 25 → 24 on this network). Verified on
   the live network: exactly one AP row (`MikroTik-AP`, .30, via ether10), and
   all suites (device panel, flows, map reorder, peers unit, auth unit 26 checks
   live, auth e2e 26 isolated) ALL PASS — auth suites ran against the gated live
   instance with the owner's real password via `DASH_TEST_PW`.
4. **Batch #23 — audit round 3 hardening + tests can never claim the wizard** —
   independent review of the setup flow (5/6 PASS) caught the wizard's TOCTOU:
   two simultaneous creators could both write `auth.json`. `set_password_file`
   gained `only_if_absent=True` which performs exists-check + atomic write under
   `_auth_lock` (PBKDF2 computed outside the lock and off the event loop via
   `run_in_threadpool`, same as login); the loser now gets the 409 takeover hint
   (wording also made explicit after the real-world incident where the test
   suite claimed the owner's fresh instance). Harness guard: `_auth_helper`
   now **refuses** to complete setup on any instance unless
   `DASH_TEST_ALLOW_SETUP=1` is exported (login-based test runs unchanged);
   verified live — helper against the setup-pending :8899 raises instead of
   claiming. Verified: `test_auth_unit.py` ALL PASS, `test_auth_browser.py`
   26/26 ALL PASS (wizard flow unchanged after rework).
3. **Batch #22 — first-boot setup wizard; dashboard now ALWAYS gated** — a fresh
   instance (no `auth.json`) no longer serves an open dashboard: every request
   redirects to a **setup wizard** (`/setup`) that forces creating the login
   password through the browser (password + confirm, min 8, one-shot — a second
   `POST /api/setup` returns 409 even before any account race window closes,
   because creation flips the mode atomically). `server.py --passwd` is now
   purely the **forgot-password reset** (Pi-hole `pihole -a -p` role), working
   while the service runs; `--no-passwd` deletes the account and drops back to
   setup-required — never to open mode. Middleware gate rewritten: only
   `/static` is public; in setup mode additionally `/setup|/api/setup`,
   otherwise `/login|/api/login` or a valid `sid` session; everything else is
   302 (pages) or 401 (APIs). Setup completion logs the claiming IP (the
   first-visitor-wins race on a shared LAN is documented in the README).
   Test infrastructure: `tests/_auth_helper.py` (`ensure_login()` for Playwright
   pages, `api_opener()` for pre-browser urllib calls) transparently passes the
   gate with `DASH_TEST_PW` (default `pytest-dash-12345`); all three live suites
   gained a login step. Verified: `test_auth_unit.py` 25/25 (setup-mode checks
   updated), `test_auth_browser.py` 26/26 incl. new wizard flow on temp :8898
   (wizard shown, mismatch rejected, create→auto-login, one-shot 409, wizard pw
   works on login, `--no-passwd`→setup fallback, `/api/login` refused in setup
   mode), device-panel/flows/map-reorder/peers suites ALL PASS against the now
   gated live instance; one pre-existing race fixed (`devChart` pixels asserted
   before openDev's history fetch resolved → now waits on `dlgChart` data).
   NOTE: live instance setup was completed by the test suite — its login is
   currently the test password; owner should run `--passwd` to choose their own.
2. **Batch #21 — optional local login (Pi-hole style) with CLI reset** — the whole
   dashboard can now be password-gated: `venv/bin/python server.py --passwd`
   (interactive; run it any time, even while the service runs and even when
   locked out — the Pi-hole `pihole -a -p` equivalent) writes PBKDF2-SHA256
   (240k iter, random salt) hash to `auth.json` chmod 600; `--passwd-stdin` for
   non-interactive, `--no-passwd` to remove the gate. While `auth.json` exists:
   every page redirects to a dark-theme `/login` page and every `/api/*` returns
   401 until the HttpOnly `sid` cookie (12 h sliding) is set; wrong-password
   counter locks an IP for 5 min after 10 tries; changing the password revokes
   all sessions. **Default unchanged (no file = open LAN mode)**, so existing
   deployments are unaffected until opted in. `BASE` honors `DASH_BASE` env and
   the server accepts `--port` — both to allow isolated test/Docker instances.
   Hardened after an independent opencode security review (round 1: 4 FIX-FIRST):
   sessions AND brute-force counters moved into SQLite (`sessions`, `login_fail`)
   so a service restart can neither log everyone out nor dodge a lockout;
   `/api/session` no longer public (was an unthrottled gate-status oracle);
   `auth.json` written atomically (tmp + `os.replace`); corrupt `auth.json` fails
   CLOSED (gate stays up); min password length 8; cookie gets `Secure` when
   reached over HTTPS or via a TLS-terminating proxy (`X-Forwarded-Proto`);
   round 2 of the review caught logout not clearing the cookie (deletion must
   repeat `path="/"` + `samesite` — RFC 6265 matches attributes, not names) and
   a fresh-box crash: `--passwd` on a DASH_BASE with no `history.db` yet died on
   `no such table: sessions` — `set_password_file` now calls `db_init()` itself
   (idempotent, outside the lock). Also from round 1 caveats: `save_config`
   (router password) is written atomically perm-first too, and login's PBKDF2
   moved to a threadpool so auth attempts cannot stall the event loop.
   Verified: `tests/test_auth_unit.py` 24/24 (hashing, file perms, DB-backed
   session sliding/expiry/revocation, lockout persisting across connections,
   fail-closed on corrupt file) and `tests/test_auth_browser.py` 19/19
   end-to-end on a temp `DASH_BASE` instance at :8898 (redirect/401 gates,
   login page flow in Chromium, CLI reset-while-running kills old password,
   `--no-passwd` reopens) — live instance suites still ALL PASS with auth off.
1. **Batch #20 — ntopng-style traffic analytics panels (network map kept)** — new
   sections *below* the map: **Flows & active hosts** (dual-axis history chart of
   active WAN connections + LAN hosts with live WAN conns, own 1h/6h/24h range),
   **Protocol distribution** doughnut + exact **traffic volume** for the range,
   **Top ports** (bytes ↓/↑ + flow count), and **Top conversations** — global live
   conntrack view of local device ⇄ remote peer per 5-tuple-ish key with service
   labels (`QUIC` added for udp/443). Backend: `poll()` now tallies protocol/port
   rates during the existing conntrack loop and keeps exact WAN byte deltas;
   three new SQLite tables (`wan_bytes` exact byte deltas, `flows_hist` per-poll
   flows/hosts, `anet` 60 s protocol/port rollups) pruned on the same retention;
   new endpoints `GET /api/analytics/history` (range queries, block-mean
   downsample >400 pts) and `GET /api/analytics/conns` (cached conntrack snapshot
   only — request threads still never touch the router API). Honest labels:
   protocol/port bytes are rate-derived estimates (idle conns read 0 bps),
   conversation "session" bytes reset when conntrack entries expire; only the
   volume card is exact (interface counter deltas). Verified headless Chromium
   @1440/1000/800: map still renders 25 devices, flowChart/protoChart/wanChart
   painted (pixel-alpha, not tooltips), conversations/ports tables populated,
   range switch, device panel open/Esc unchanged, zero `pageerror`; all four
   existing test suites ALL PASS (`test_flows_browser` race fixed: waits for the
   `/api/peers` resource entry instead of assuming it already completed).

## 2026-10-17
6. **Batch #19 — docked panel is now responsive in both axes** — width fixed at
   360px grew to `clamp(340px, 30vw, 560px)`; height is now content-driven
   **stretched to the map's height** (`.mapwrap` stretches, panel caps at
   `calc(100vh - 20px)`), with the Live flows list flexing to absorb all spare
   height instead of a fixed 184px box. Trap hit and fixed: author rules like
   `#dev-body{display:flex}` override the UA `[hidden]{display:none}`, so
   `hidden` elements kept participating in flex layout (closed panel still ate
   panel space, Esc appeared broken); page-wide `[hidden]{display:none !important}`
   restores it. Verified in headless Chromium at 1476x939 / 1920x1080 / 1476x700:
   panel height = viewport cap (919/1025/680px), flows list fills the remainder
   (582/653/357px), flows bottom never exceeds panel, Esc + ✕ close, empty-state
   centers, stacking below the map at 1100px intact, panel regression suite still
   ALL PASS, zero `pageerror`.
5. **Batch #18 — draggable dialog replaced by a docked detail panel (whiteout fix)** —
   the previous session was cut off mid-refactor: the markup had already become a
   side panel docked beside the network map (`#devpanel`/`#dev-body`/`#devChart`),
   but the script still targeted the removed dialog (`#devdlg`, `#dlg-title`,
   `#dlgChart`, `#dlg-head`). The drag-setup IIFE ran at load and dereferenced a
   `null` `#devdlg`, killing the whole inline script — the page rendered as dead
   chrome. Completed the refactor: `openDev()` now fills `#dev-title`/`#dev-meta`/
   `#dev-block` and draws into `#devChart`; the flows poll follows panel visibility
   instead of `dialog.open`; close via ✕ or `Esc`; the selected device is
   highlighted in both map and table and survives the 3 s re-renders; panel docks
   right of the map, stacks below at ≤1200 px; dead dialog/drag CSS and
   `makeDraggable()` removed. Verified: headless Chromium, 21 checks — rows/map/
   charts render with zero `pageerror`, open/switch/close/Esc, flows populate +
   tab toggle, selection survives a poll, stacking geometry at 1100 px; served
   bytes sha256-match the file. `test_dialog_drag_browser.py` deleted (feature
   gone), flows/map-reorder tests retargeted to `#dev-body`, new
   `tests/test_device_panel_browser.py` carries the regression checks.
4. **Batch #17 — P0+P1 live flows: who each device is talking to right now** —
   new `GET /api/peers?ip=…&limit=…` builds a peer list for one LAN device from
   the conntrack snapshot the sampler already keeps (no extra router round-trip
   per request), aggregated per remote IP: live ↓/↑ bps, cumulative bytes
   (conntrack rates read 0 for idle-but-established flows, so byte totals are
   kept and shown instead of "0 bps"), connection count, service:port labels
   (`HTTPS:443`, `QUIC:443`, `WireGuard:51820`, …), protocol mix, direction, and
   `unanswered` (inbound flows with no seen-reply = blocked/probed). New device
   dialog section **Live flows** with an **Accessing you** tab for inbound
   initiators, a rate-relative background bar, and auto-refresh every 3 s while
   the dialog is open (stops on close). P1 naming: names come from the router's
   own DNS cache inverted (address→names, refreshed by the sampler every 30 s)
   plus **lazy background reverse DNS** with positive 2 h / negative 5 min
   caching, so the first paint answers in ~20 ms and names fill in on later
   refreshes ("resolving N name(s)…" in the header); peers are grouped by PTR
   suffix (`1e100.net ×6`). Data-hygiene rules decided from real data: LAN↔LAN
   flows are counted as `lan_conns` and excluded from WAN peers, and flows to
   the router's own WAN address are separated as `hairpin` (474 such conns on
   the Transmission box were hairpin/port-tests, which would otherwise look
   like an attacker list). Two real bugs found and fixed while building it:
   (a) **librouteros connections are not thread-safe** — the first version read
   `/ip/dns/cache` from the request thread and poisoned the sampler's socket
   (`not enough values to unpack`, dashboard showed 0 devices); all router I/O
   is now sampler-thread only and handlers read caches; (b) **dstnat direction
   was inverted** — a port-forwarded flow's reply-*src** is the real LAN target,
   so inbound flows were being attributed to the router's WAN address as
   outbound; inbound now names the real remote initiator. Verified: 16-assertion
   unit test over synthetic conntrack (outbound/inbound-dstnat/unanswered probe/
   hairpin/LAN/service labels/sort/totals) all pass; 16-assertion Playwright run
   against live traffic (rows render, rDNS fills in within refreshes, tab
   switching, empty states for idle devices, refresh stops on close, dialog drag
   and chart intact, zero JS errors); live check on the Transmission box:
   1204 peers / 1462 WAN conns, 6 inbound callers on tcp/51414, WAN address
   absent from the peer list; regression suites for drag and map tween re-run
   green. Limits stated in UI terms: conntrack is *current* flows only — a
   connection that starts and ends between polls is invisible, and there is no
   ASN/geo on RouterOS 7.24.5 (`/ip/ipgeo` absent) and no domains in conntrack.
3. **Batch #16 — animated reordering of the network map** — the backend sorts
   devices by live rate every poll (`server.py`: `devices_out.sort(key=…
   -(rx_bps+tx_bps))`), so the top of the map is the busiest device. The client's
   map signature included that order, so every reorder threw away the whole SVG
   and rebuilt it — an instant, jarring jump. Now: the signature is
   **structure-only** (device set/names/ports, each group's device list sorted),
   positions are **derived** by `topoLayout()` instead of being baked into the
   markup, and a pure reorder goes through `swapTopo()`, which tweens each node's
   y with `requestAnimationFrame` (420 ms, ease-in-cubic — `TSWAP`) while
   `paintTopo()` rewrites device/hub/port `transform` **and** every edge `d`
   from the same animated values, so the connector lines track their nodes
   instead of snapping. Hub/port node y is recomputed from the animated device
   positions each frame, so the vertical layout stays consistent mid-flight.
   Devices being animated keep their DOM identity (no `innerHTML` rebuild), so
   hover, `data-ip` and click-to-open survive. `prefers-reduced-motion: reduce`
   snaps instantly instead; new/removed devices still rebuild (plus a 0.45 s
   fade-in, also disabled under reduced motion). Verified with headless
   Chromium against real traffic, 25 devices across ports/VLANs, in both motion
   modes (19 assertions): 18 distinct intermediate layouts sampled during one
   swap, off-grid fractional y values mid-tween (proof of a real tween, not a
   jump), exact settle back to integer grid, no stacked nodes, untouched devices
   byte-identical, nodes reused not rebuilt, edges well-formed, click-to-open and
   live rate text unaffected, structural change still rebuilds, zero JS errors.
   Reduced-motion run: exactly 1 layout during the same swap (instant).
2. **Batch #15 — drag anywhere on the dialog, not just the title bar** —
   feedback on #14: a slim title strip is a fiddly target. The drag surface is
   now the whole `#devdlg` (`pointerdown` on the dialog, pointer capture on the
   dialog), excluding real controls (`button,input,a,select,label,summary`) and
   excluding touch-on-canvas so Chart.js touch/scrolling survives; mouse-drag
   over the chart moves the panel. Two escape hatches: **hold Alt** to select
   text instead of dragging (needed because `preventDefault()` on pointerdown
   suppresses selection — `user-select:none` now applies only while dragging),
   and **double-press the title bar** to re-centre (own 350 ms detection, since
   the `preventDefault()` makes `dblclick` unreliable). Cursor affordance:
   `grab` over the dialog, `grabbing` while dragging, `pointer` on buttons.
   Verified with headless Chromium (suite of 17 assertions): drags from title,
   meta line, chart canvas and panel body all move it by the exact pointer
   delta; single click on the chart does not move it; pointer capture releases
   (canvas `mousemove` counter rises after the drag); Close button and
   Pause/Resume unaffected; Alt+drag selects the IP/MAC and does not move the
   panel; a plain drag leaves no text selection; double-click re-centres;
   clamping, Esc, main-page buttons, chart data and zero JS errors re-checked,
   plus the two earlier suites re-run green. Note: the hover tooltip on the
   per-device chart is dead by pre-existing config (`pointRadius:0` with
   Chart.js `intersect:true`), unrelated to this change — say the word to fix
   it via `interaction:{mode:'index',intersect:false}`.
1. **Batch #14 — draggable per-device dialog** — the detail popup was a
   native `<dialog>`, pinned to the viewport centre by CSS, so it always sat
   on top of the device you clicked. Its title/meta block is now a drag
   handle (`.dlg-head`, grip glyph, `cursor:grab`, `touch-action:none` so it
   also works on touch): first `pointerdown` detaches the dialog from the
   centring rules (`inset:auto;margin:0` + measured `left/top`), then pointer
   capture moves it and clamps it inside the viewport. Position persists
   across close/re-open; double-click the bar to re-centre; `Esc`, the Close
   button, and the pause/resume action are unchanged. First cut had a clamp
   bug (`+style.top` on a `"334px"` string → `NaN` → every move snapped to
   `top:4`, i.e. stuck to the top of the page, while left/right worked);
   fixed by parsing both axes with `parseFloat`. Verified with headless
   Chromium against the running server (14 assertions): opens centred, exact
   pointer delta down/right and up/left, clamped at all four viewport edges,
   clamped to fully-visible (max `top = innerHeight − height − 4`), position
   persists on re-open, dblclick re-centres, Esc closes, buttons inside the
   dialog still receive clicks after a drag, stays on screen when the window
   shrinks, dialog chart intact (59 points), zero JS errors.

## 2026-10-16 (evening)
0. **Batch #13 — ROOT CAUSE of "no download/upload graphic"** — RouterOS traps
   mid-print (`no such item (4)`) on `/ip/firewall/connection` when the
   conntrack table churns, i.e. exactly when real traffic flows. The old
   fallback call had a wrong signature and returned [], so per-device rates
   collapsed to zero precisely under load. Fix: `api_fetch_all` retries (3x with
   backoff) and returns `None` on total failure (all call sites guard); new
   `RouterMonitor.conns()` keeps the last good conntrack snapshot so rates hold
   instead of zeroing. Verified live: with a 400 MB WAN download running,
   the active device now shows 3–9.7 Mbps (was 0.0 before the fix).
1. **Batch #12 — observability for the "no graphics" report** — visible `diag:`
   line in dashboard header (backend / Chart version / time-scale registered /
   chart points / map nodes / table rows; turns red when broken) + uvicorn
   access logging (`log_level=info`) so server-side journal shows exactly which
   resources the client browser requests. Also fixed a line accidentally eaten
   while patching (mode-button handler restored, verified by node --check + harness).
   Follow-ups from OpenCode nit list: fetch-failure log raised debug→warning;
   `diag:` line now renders red "backend DOWN" when /api/state itself is unreachable.
1. **Durability: systemd user service** — dashboard now runs as
   `systemctl --user mikrotik-dashboard.service` (Restart=always, enabled,
   linger=yes, survives logout/reboot), using a project venv at
   `~/mikrotik-dashboard/venv` (systemd rejects the `+` in the Hermes tool
   path masked form; venv also isolates deps). Ad-hoc background servers were
   being killed by runtime lifecycle cleanup — root cause of intermittent
   "server unreachable" gaps.
1. **Font-defaults nit (found by OpenCode verify of #9)** — `Chart.defaults.font`
   was replaced with `{size:10}`, dropping family/style/weight; now sets
   `Chart.defaults.font.size` only so Chart.js keeps its proper font stack.
1. **v1** — FastAPI + librouteros backend, SQLite history, dark Chart.js dashboard
   (live WAN chart, device table with vendor/hostname/rates, 15m–24h history,
   search/sort, in-app credential dialog, optional block/unblock, Windows start.bat).
2. **VLAN support** — subnets discovered from `/ip/address`; VLAN filter bar,
   per-VLAN totals, subnet badges. Both `bridge1` + `bridge-vlan2` covered.
3. **NetFlow stacked chart** — `↓/↑ by device` modes, top-9 + `others`, stepped areas.
4. **Bug fixes found against live router** — v7 conntrack fields are `repl-rate`/`orig-bytes`
   (downloads were 0); dstnat sessions attributed via reply-side addresses; WAN IP /
   upstream gateway no longer listed as devices; seeded test data purged from history.db.
5. **Network map** — SVG topology (cloud→router→VLAN hubs→ports→devices),
   dash-speed-encodes-traffic edges, device icons, click-for-detail, port grouping
   from ARP iface (bridge FDB is HW-offloaded and reports no port).
6. **Rate visibility on map** — green ↓ / orange ↑ lines on every device card,
   VLAN hub ↓/↑ line, WAN ↓/↑ label above router; element-ref caching (_tm/_te/_tnl/_wl).
7. **OpenCode review round 1 + fixes** — net: edges now animated; conntrack-only
   devices keyed by IP (was `mac:"?"` collision); rebuild signature extended
   (names/IPs/WAN IP/router version); empty-map cleanup; nodeSVG y typo;
   HTML-escaping for device table / title / edge keys.
8. **OpenCode self-corrections (it applied its remaining findings)** —
   LAN↔LAN flows excluded from per-device WAN rates; v6 `reply-rate` fallback documented;
   `esc()` also escapes `'`.
9. **Blank-chart fix** — Chart.js `responsive:true` requires parent with
   `position:relative` + fixed height: all three canvases wrapped in
   `div{position:relative;height:270/230px}` and `maintainAspectRatio:false` set
   (root cause candidate for "no download/upload graphic" on client browsers).
   Server now sends `Cache-Control: no-store` on all responses (kills stale-page
   issues); page shows a visible red error strip on any JS/fetch failure
   (`window error` + `unhandledrejection`).

## Operating rules (user)
- Client machines: browser only — never install anything on the viewing machine.
- Every change must be recorded in this file and verified by an independent
  OpenCode run before being reported as done.
