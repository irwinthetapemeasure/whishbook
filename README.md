# 🎄 Whishbook

Christmas lists for the family: a small self-hosted wish-list site for gift exchanges. One site can host several separate exchanges (your family, the in-laws, the office); each has its own passphrase, and the passphrase someone types decides which exchange they land in. Exchanges never see each other.

- Everyone adds themselves with a name and a 4–6 digit PIN, then builds their list (link, price, notes, how much they want it).
- Others tap **I'm getting this** so nobody doubles up. **The list owner never sees what's been claimed or by whom.**
- **My shopping** shows everything you've said you'll buy.
- Past years stay viewable (read-only) per person, and you can start a new year from last year's list.
- The first person to join an exchange is its admin. Admins can reset PINs, rename/delete people, and change that exchange's passphrase.

## How it's protected

The site is public on the internet through a Cloudflare Tunnel, so it's locked down in layers:

| Layer | What it does |
|---|---|
| Cloudflare Tunnel | No ports opened on your router; your home IP is never exposed. The app container has no published ports at all. |
| Cloudflare WAF + Bot Fight Mode | Blocks bots and floods before they reach the NAS (setup below). |
| Turnstile | Cloudflare's free "I'm human" check on the passphrase, join and PIN forms. |
| Exchange passphrase | Nothing beyond the passphrase page is reachable without it. 10 wrong tries locks that IP out for an hour, doubling each time (max 24h). |
| Personal PIN | Stops people in your exchange editing your list. 5 wrong tries locks that person for 15 min, doubling each time. |
| App hardening | argon2 hashes (passphrases and PINs are never stored), signed HttpOnly/Secure cookies, CSRF tokens on every form, strict Content-Security-Policy, escaped output, only `http(s)` links, size limits on everything, read-only non-root container. |

Changing a passphrase logs everyone in that exchange out; changing a PIN logs that person out on other devices.

## Install on the NAS

You need a domain on Cloudflare (free plan is fine) and Docker / Container Manager on the NAS.

### 1. Create the Cloudflare Tunnel

1. Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels** → **Create a tunnel** → *Cloudflared*. Name it `christmas`.
2. On the "install connector" step, copy the **token** (the long string after `--token`). You don't need to run their install command.
3. **Public hostname**: subdomain `christmas`, your domain, service type **HTTP**, URL **`app:8000`**.

### 2. Create a Turnstile widget (recommended)

Cloudflare dashboard → **Turnstile** → **Add widget**. Hostname: `christmas.yourdomain.com`, mode: *Managed*. Copy the **site key** and **secret key**.

### 3. Set up the folder on the NAS

Make a folder (e.g. `/volume1/docker/whishbook`) and put two files in it:

- `docker-compose.yml` from this repo
- `.env`, copied from `.env.example` and filled in:
  - `SECRET_KEY`: run `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` (or any 48+ random characters)
  - `TUNNEL_TOKEN`: from step 1
  - `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY`: from step 2
  - `PUID` / `PGID`: run `id` on the NAS as the user who owns the folder

Create an empty `data` folder next to them. That's where the database and daily backups live, so your normal NAS backups cover it.

### 4. Start it

```sh
# Only needed while the GitHub package is private:
docker login ghcr.io -u irwinthetapemeasure   # password = a GitHub token with read:packages

docker compose pull
docker compose up -d
```

On Synology Container Manager / Portainer / Unraid, create a **Project/Stack** from the same `docker-compose.yml` and `.env`.

### 5. Create your gift exchanges

```sh
docker compose exec app python -m app.manage add-group "Smith Family"
docker compose exec app python -m app.manage add-group "In-laws"
docker compose exec app python -m app.manage list
```

Each asks for that exchange's passphrase (12+ characters; a few random words is good, e.g. `reindeer-pickle-sweater-42`). Every exchange needs a different passphrase. Send each group its passphrase and the link; the first person in becomes that exchange's admin. You'll be a separate person in each exchange you're part of. To switch exchanges, use **Switch to another gift exchange** on the home page and enter the other passphrase.

Other commands: `set-passphrase`, `rename-group`, `make-admin`, `reset-pin`, `delete-group` (run with `--help`).

### 6. Lock down Cloudflare (free plan)

In the Cloudflare dashboard for your domain:

- **Security → WAF → Rate limiting rules** → create rule:
  - *If* URI Path is in `/gate`, `/join`, or starts with `/login/` **and** Request Method equals `POST`
  - *Then* Block for 10 minutes when more than **5 requests per 10 seconds** from the same IP.
- **Security → Bots** → turn on **Bot Fight Mode**.
- Optional: **Security → WAF → Custom rules** → block when *Country* is not in the countries your family lives in.
- **SSL/TLS → Edge Certificates** → **Always Use HTTPS** on.

## Updating

Pushing to `main` runs the tests and publishes `ghcr.io/irwinthetapemeasure/whishbook:latest` (amd64 + arm64). On the NAS:

```sh
docker compose pull && docker compose up -d
```

## Backups

A copy of the database is written to `data/backups/` once a day (the newest 14 are kept), and admins can click **Back up now**. To restore, stop the app and copy a backup over `data/christmas.db`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q

# Run the real container locally at http://localhost:8000 (no Cloudflare)
cp .env.example .env   # set SECRET_KEY
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build app
docker compose -f docker-compose.yml -f docker-compose.local.yml exec app python -m app.manage add-group "Test"
```
