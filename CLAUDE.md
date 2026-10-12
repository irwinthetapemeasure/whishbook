# Whishbook ("Wishbook"): project memory

Self-hosted Christmas wish-list site for the owner's gift exchanges. Repo: `irwinthetapemeasure/whishbook`. It runs as a Docker container on the owner's **NAS**, exposed publicly only through a **Cloudflare Tunnel**.

## Decisions already made (don't re-ask)
- **Spelling:** user-facing text says **Wishbook**. "whishbook" is kept only for service identifiers (repo, GHCR image, container, Dockge stack, dataset). Never show "Whishbook" to users.
- **Multiple gift exchanges ("groups") in one instance.** The owner is in 2–3 exchanges. One URL; the passphrase typed at the gate decides which group you enter. Groups are fully isolated (people, lists, claims, admins). Every passphrase must be unique across groups.
- **Shared passphrase per exchange** at the gate (not Cloudflare Access), plus **name + 4–6 digit PIN** per person inside the group.
- **Hidden claims:** others mark "I'm getting this"; the list owner never sees claims or claimers. `db.items_for_owner` deliberately never touches the `claims` table, and tests enforce this.
- Lists are per **calendar year**; past years are viewable but read-only. "Start from last year's list" copies *all* items (copying only unclaimed ones would leak claims).
- The **first person to join a group is its admin.** Groups are created only from the NAS CLI (`app.manage`), never from the web.
- The owner is a separate person in each exchange; "Switch to another gift exchange" on the home page clears the session.

## Stack and layout
Python 3.12, FastAPI, Jinja2 server-rendered pages, SQLite (`/data/christmas.db`), no JS framework (`static/app.js` only does confirm dialogs).
- `app/main.py`: `create_app(settings)` factory holding all routes, the session middleware, the CSRF dependency, the `BodyLimit` ASGI middleware and security headers. Uvicorn entry is `app.main:get_app --factory`.
- `app/db.py`: schema and all queries (parameterised). Tables: groups, people(group_id), items, claims, login_attempts.
- `app/security.py`: argon2 hashing, signed-cookie sessions (itsdangerous), CSRF, `Limiter` (escalating lockouts), Turnstile verification (fails closed).
- `app/manage.py`: CLI: `list`, `add-group`, `set-passphrase`, `rename-group`, `make-admin`, `reset-pin`, `delete-group`.
- `app/config.py`: env vars `SECRET_KEY` (required, 32+ chars), `DATA_DIR`, `DB_PATH`, `COOKIE_SECURE`, `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY`, `TRUST_CF_CONNECTING_IP`, `BACKUP_DIR`, `BACKUP_KEEP`, `SESSION_DAYS`.
- `tests/`: pytest, 32 tests (gate, group isolation, lockouts, claim privacy, authorization, CSRF, headers, CLI).

Session cookie holds `gid`, `gv` (the group's gate_version, bumped when its passphrase changes, which logs everyone out), `pid`, `ph` (last 16 chars of the PIN hash, so a PIN change logs that person out elsewhere) and `csrf`.

Lockouts: gate 10 fails/IP/hour gives a 1h lock (doubling, max 24h); PIN 5 fails/person gives 15 min (doubling); PIN 20 fails/IP gives 1h; join 5/IP/day. The client IP comes from `CF-Connecting-IP`, which is trusted because the app has no published ports.

## Commands
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
# local container at http://localhost:8000 (needs .env with SECRET_KEY)
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build app
```

## Build and publish
`.github/workflows/docker.yml`: on push to `main` it runs the tests, then builds `linux/amd64,linux/arm64` and pushes `ghcr.io/irwinthetapemeasure/whishbook:latest` (plus `sha-…` and semver tags on `v*` tags). The GHCR package is **private by default**: either make it public in GitHub → Packages → whishbook → settings, or `docker login ghcr.io` on the NAS with a `read:packages` token.

## NAS deployment (live)
Deployed 2026-10-12 to the owner's TrueNAS SCALE 25.10 box **blackbox** (Tailscale `blackbox.tail648932.ts.net`), managed in **Dockge**. Public URL: **https://wishbook.awiebe.ca**.

- Stack: `/mnt/.ix-apps/app_mounts/dockge/stacks/whishbook/` (`compose.yaml` + `.env`, root-only). It differs from the repo's `docker-compose.yml`: no cloudflared of its own; container `whishbook` joins the existing external network `cloudflared_default`, runs as `568:568` (TrueNAS `apps`), same hardening, no ports.
- Data: ZFS dataset `fastboi/whishbook`, mounted at `/mnt/fastboi/whishbook/data` → `/data`.
- Tunnel: the owner's existing remotely-managed `cloudflared` Dockge stack. Public hostname `wishbook.awiebe.ca` → `http://whishbook:8000`. Do **not** point it at `10.69.69.69:8000`: that's the NAS LAN IP, and host port 8000 belongs to Tunarr.
- GHCR package is public, so no `docker login` needed. Update: `docker compose pull && docker compose up -d` in the stack folder (or Dockge "Update").

Remaining:
1. [ ] Create the exchanges: `docker exec -it whishbook python -m app.manage add-group "<Name>"` (passphrase 12+ chars, unique).
2. [ ] Turnstile widget for `wishbook.awiebe.ca` (Managed); put both keys in the stack `.env`, then recreate the container.
3. [ ] Cloudflare hardening: WAF rate-limit rule (POST to `/gate`, `/join`, `/login/*`: more than 5 per 10s per IP blocks for 10 min), Bot Fight Mode on, Always Use HTTPS, optional country block.
4. [ ] Phone test on mobile data: gate, join, add an item, claim from a second account, confirm the owner can't see the claim.
5. [ ] Confirm snapshots/backups cover `fastboi/whishbook` (the app also keeps 14 daily copies in `data/backups/`).

## Gotchas
- The compose file has **no `ports:` on purpose**; only cloudflared reaches the app. For LAN-only testing use `docker-compose.local.yml` (binds 127.0.0.1:8000, `COOKIE_SECURE=false`).
- The container runs with a read-only root filesystem and only `/data` writable. If it crashes with permission errors, `PUID`/`PGID` don't match the owner of `data/`.
- Behind the tunnel, `COOKIE_SECURE` must stay true (the default). Over plain `http://` on the LAN it must be false or logins won't stick.
- The app needs outbound internet for Turnstile verification. If Turnstile keys are set and it can't reach Cloudflare, logins fail (fails closed on purpose).
- The CSP is `style-src 'self'; font-src 'self'; img-src 'self' data:`, so styling must live in `static/style.css`: no `style=""` attributes, no web fonts from a CDN. Decorations are SVG data URIs in the CSS. Look: pine-green header with a candy-stripe edge, cranberry buttons, gold accents, pill nav with `aria-current`; light and dark themes via `prefers-color-scheme`.
- Jinja escapes apostrophes (`&#39;`), so test assertions on page text should avoid matching across an apostrophe.
