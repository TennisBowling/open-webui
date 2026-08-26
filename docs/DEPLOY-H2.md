# Deploying behind HTTP/2 + HTTP/3 (for slow / metered iOS PWA links)

## Why

`start_modified.sh` launches the app with bare `uvicorn`, which speaks
**HTTP/1.1 only**. The cold load of this app fires ~122 requests (entry chunk,
route chunks, fonts, icons, CSS). Over HTTP/1.1 a browser opens at most ~6 TCP
connections per origin and pipelines nothing, so those 122 requests serialize in
~6 lanes. On a high-latency cellular link each lane pays a full round-trip per
request and head-of-line-blocks behind the slowest response — cold start drags.

Putting a thin reverse proxy in front fixes this without touching the app:

- **HTTP/2 multiplexing** — all 122 requests share **one** connection with
  independent streams, so a single slow asset no longer blocks the rest.
- **HTTP/3 / QUIC** — runs over UDP with **0-RTT** session resumption (a returning
  PWA can send its first request in the very first packet) and **connection
  migration**, which survives an iOS **Wi-Fi ↔ cellular handoff** without tearing
  down and re-establishing the connection. This is the single biggest win for a
  phone that keeps changing radios.
- **TLS + compression negotiation** and transparent **WebSocket upgrade** for the
  socket.io transport at `/ws/`.

The service worker already makes the *second* load cheap (durable Cache API
precache). H2/H3 is about making the *first* load — and every load after a cache
eviction or radio handoff — fast and resilient.

## How

1. **Trust the proxy in uvicorn.** `start_modified.sh` now passes
   `--forwarded-allow-ips=*` (override via the `FORWARDED_ALLOW_IPS` env var).
   This lets uvicorn read `X-Forwarded-Proto` / `X-Forwarded-For` from the
   loopback proxy so it generates correct `https://` redirects, sees the real
   client IP, and upgrades WebSockets correctly. It is safe here because the
   proxy is co-located on `127.0.0.1`; do **not** set `*` if uvicorn is exposed
   to untrusted networks directly.

2. **Run Caddy** (repo-root `Caddyfile`). Install Caddy, point your domain's DNS
   at this host, edit the `chat.example.com` line to your domain, then:

   ```bash
   ./start_modified.sh                 # uvicorn on 127.0.0.1:8081
   caddy run --config ./Caddyfile      # h2/h3 front on :443 (+ :443/udp for QUIC)
   ```

   Caddy provisions TLS automatically (h2 and h3 both require TLS) and advertises
   HTTP/3 via the `Alt-Svc` header; browsers upgrade to QUIC after the first h2
   response.

3. **Open the QUIC port.** Allow inbound **UDP/443** in the firewall in addition
   to TCP/443, otherwise HTTP/3 silently falls back to HTTP/2.

## Local / IP-only testing

For a quick local check without a real domain, replace the site line in the
`Caddyfile` with `:443` and add `tls internal` inside the block (self-signed), or
use `http://localhost` to skip TLS entirely (note: h2/h3 are unavailable over
plain HTTP, so this only validates proxying, not the multiplexing win).

## What is intentionally NOT changed

- The app still binds `0.0.0.0:8081` exactly as before; Caddy is additive. You
  can keep hitting uvicorn directly during development.
- No app code, headers, or caching behavior depends on the proxy — the service
  worker is correct with or without it.
