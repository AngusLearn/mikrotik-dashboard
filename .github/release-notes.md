First tagged release. Multi-arch images (`amd64` + `arm64`) are published on Docker Hub as `anguslearn/mikrotik-dashboard:v1.0.0`.

### What it does
A self-hosted live-traffic dashboard for MikroTik routers (RouterOS v6/v7) — no agent, no cloud.

- **Live per-device traffic**: real-time up/down charts, current speed, cumulative totals, 15m–24h history, sparklines, search/sort
- **Network map**: devices grouped by router port, busiest-first, with router identity, board name and RouterOS version
- **Live flows**: who each device talks to right now (rate, service, conns, reverse-DNS) + *Accessing you* tab for inbound callers
- **Traffic analytics**: active flows/hosts history, protocol distribution, top ports, top conversations (ntpng-style)
- **Router extras**: hardware sensors (temp/voltage), DHCP pool usage, opt-in per-device internet pause/resume
- **Router profiles**: switch routers in the browser; each keeps its own history
- Graceful degradation when the router is busy; auto-reconnect

### Run it
```bash
curl -O https://raw.githubusercontent.com/AngusLearn/mikrotik-dashboard/main/docker-compose.public.yml
mkdir -p data && docker compose -f docker-compose.public.yml up -d
# → http://localhost:8899
```

See [CHANGELOG.md](CHANGELOG.md) for the full dated history (pre-semver); tagged releases start here.
