# Whishbook: project memory

Self-hosted Christmas wish-list site for the owner's gift exchanges. Repo: `irwinthetapemeasure/whishbook`. It runs as a Docker container on the owner's **NAS**, exposed publicly only through a **Cloudflare Tunnel**.

## Decisions already made (don't re-ask)
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

## NAS deployment: status and checklist
Status when this was written (2026-10-12): code done and tested (unit tests, hardened container run, Playwright browser run-through). The first CI run was in progress. **Nothing is deployed yet.**

1. [ ] Confirm the GitHub Actions run is green and the image exists in GHCR; decide public package vs `docker login`.
2. [ ] Cloudflare Zero Trust → Networks → Tunnels → create tunnel "christmas" (cloudflared), copy the token. Public hostname e.g. `christmas.<domain>` → service **HTTP** `app:8000`.
3. [ ] Cloudflare → Turnstile → add widget for that hostname (Managed) and copy the site and secret keys.
4. [ ] On the NAS, make a folder (e.g. `/volume1/docker/whishbook`) containing `docker-compose.yml`, `.env` (from `.env.example`) and an empty `data/` folder. Fill in `SECRET_KEY`, `TUNNEL_TOKEN`, the Turnstile keys, and `PUID`/`PGID` (from `id` on the NAS; must own `data/`).
5. [ ] `docker compose pull && docker compose up -d` (or a Container Manager / Portainer project). The app healthcheck gates cloudflared starting.
6. [ ] Create the exchanges: `docker compose exec app python -m app.manage add-group "<Name>"` for each (passphrase 12+ chars, unique).
7. [ ] Cloudflare hardening: WAF rate-limit rule (POST to `/gate`, `/join`, `/login/*`: more than 5 per 10s per IP blocks for 10 min), Bot Fight Mode on, Always Use HTTPS, optional country block.
8. [ ] Test from a phone on mobile data: passphrase gate, join, add an item, claim from a second account, confirm the owner can't see the claim.
9. [ ] Make sure the NAS backup job covers `data/` (the app also writes daily copies to `data/backups/`, keeping 14).

## Gotchas
- The compose file has **no `ports:` on purpose**; only cloudflared reaches the app. For LAN-only testing use `docker-compose.local.yml` (binds 127.0.0.1:8000, `COOKIE_SECURE=false`).
- The container runs with a read-only root filesystem and only `/data` writable. If it crashes with permission errors, `PUID`/`PGID` don't match the owner of `data/`.
- Behind the tunnel, `COOKIE_SECURE` must stay true (the default). Over plain `http://` on the LAN it must be false or logins won't stick.
- The app needs outbound internet for Turnstile verification. If Turnstile keys are set and it can't reach Cloudflare, logins fail (fails closed on purpose).
- Jinja escapes apostrophes (`&#39;`), so test assertions on page text should avoid matching across an apostrophe.
