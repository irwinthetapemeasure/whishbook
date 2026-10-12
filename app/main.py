"""Family Christmas list web app."""
import asyncio
import datetime as dt
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .db import Database
from .questionnaire import TEMPLATES
from .security import (
    SESSION_COOKIE,
    Limiter,
    Sessions,
    csrf_ok,
    hash_secret,
    new_csrf,
    turnstile_ok,
    verify_secret,
)

log = logging.getLogger("christmas")
HERE = os.path.dirname(__file__)

MAX_BODY = 32 * 1024
MAX_ITEMS_PER_YEAR = 100
MAX_QUESTIONS = 40
QUESTION_LEN = 200
ANSWER_LEN = 500
NAME_RE = re.compile(r"^[\w][\w '.-]{0,39}$")
PIN_RE = re.compile(r"^\d{4,6}$")
PRIORITIES = {1: "Really want", 2: "Would like", 3: "Nice to have"}

# Lockout policy: (max failures, window seconds, first lock seconds, max lock seconds)
GATE_IP_POLICY = (10, 3600, 3600, 86400)
PIN_PERSON_POLICY = (5, 3600, 15 * 60, 86400)
PIN_IP_POLICY = (20, 3600, 3600, 86400)
JOIN_IP_LIMIT = (5, 86400)


def current_year() -> int:
    return dt.date.today().year


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
class BodyLimit:
    """Reject request bodies larger than MAX_BODY (spam / resource abuse)."""

    def __init__(self, app, limit: int = MAX_BODY):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None and (not cl.isdigit() or int(cl) > self.limit):
            resp = PlainTextResponse("Request too large", status_code=413)
            return await resp(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    raise StarletteHTTPException(413, "Request too large")
            return msg

        return await self.app(scope, limited_receive, send)


def security_headers(settings: Settings) -> dict:
    cf = "https://challenges.cloudflare.com"
    csp = (
        "default-src 'self'; "
        f"script-src 'self' {cf}; frame-src {cf}; connect-src 'self' {cf}; "
        "style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    )
    h = {
        "Content-Security-Policy": csp,
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",
    }
    if settings.cookie_secure:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return h


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def clean_name(raw: str) -> Optional[str]:
    name = " ".join((raw or "").split())
    return name if NAME_RE.match(name) else None


def clean_link(raw: str) -> Optional[str]:
    """Return '' for no link, the link if it's a safe http(s) URL, or None if invalid."""
    link = (raw or "").strip()
    if not link:
        return ""
    if len(link) > 500:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:(?!\d)", link, re.I) and not re.match(r"^https?://", link, re.I):
        return None  # javascript:, data:, mailto: etc.
    if not re.match(r"^https?://", link, re.I):
        link = "https://" + link
    p = urlparse(link)
    if p.scheme.lower() not in ("http", "https") or not p.netloc or " " in link:
        return None
    return link


def clean_item(title, link, notes, price, priority):
    title = (title or "").strip()
    notes = (notes or "").strip()
    price = (price or "").strip()
    errors = []
    if not 1 <= len(title) <= 200:
        errors.append("Give the item a name (up to 200 characters).")
    cleaned_link = clean_link(link)
    if cleaned_link is None:
        errors.append("That link doesn't look like a web address.")
    if len(notes) > 1000:
        errors.append("Notes are limited to 1000 characters.")
    if len(price) > 30:
        errors.append("Price is limited to 30 characters.")
    try:
        prio = int(priority)
    except (TypeError, ValueError):
        prio = 2
    if prio not in PRIORITIES:
        prio = 2
    return errors, (title, cleaned_link or "", notes, price, prio)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    sessions = Sessions(settings.secret_key, settings.session_days)
    limiter = Limiter(db)
    templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
    templates.env.globals["PRIORITIES"] = PRIORITIES
    headers = security_headers(settings)

    async def backup_loop():
        while True:
            try:
                if db.count_people():
                    path = await asyncio.to_thread(db.backup, settings.backup_dir, settings.backup_keep)
                    log.info("Backup written to %s", path)
            except Exception:
                log.exception("Backup failed")
            await asyncio.sleep(86400)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(backup_loop())
        yield
        task.cancel()

    # ---- session plumbing ----------------------------------------------
    def client_ip(request: Request) -> str:
        if settings.trust_cf_header:
            ip = request.headers.get("cf-connecting-ip")
            if ip:
                return ip.strip()[:64]
        return request.client.host if request.client else "unknown"

    def sess(request: Request) -> dict:
        return request.state.session

    def current_group(request: Request):
        """The gift exchange this browser unlocked, if its passphrase hasn't changed since."""
        s = sess(request)
        gid = s.get("gid")
        if not gid:
            return None
        group = db.get_group(gid)
        if not group or s.get("gv") != group["gate_version"]:
            return None
        return group

    def is_gated(request: Request) -> bool:
        return current_group(request) is not None

    def current_person(request: Request):
        s = sess(request)
        pid = s.get("pid")
        group = current_group(request)
        if not pid or not group:
            return None
        person = db.get_person(pid)
        if not person or person["group_id"] != group["id"]:
            return None
        # The stored PIN fingerprint logs people out everywhere when their PIN changes.
        if s.get("ph") != person["pin_hash"][-16:]:
            return None
        return person

    def require_gate(request: Request):
        group = current_group(request)
        if not group:
            raise Redirect("/gate")
        return group

    def person_in_group(group, person_id: int):
        """Look up someone, but only inside the caller's own group (404 otherwise)."""
        person = db.get_person(person_id)
        if not person or person["group_id"] != group["id"]:
            raise StarletteHTTPException(404)
        return person

    def require_person(request: Request):
        require_gate(request)
        me = current_person(request)
        if not me:
            raise Redirect("/")
        return me

    def require_admin(request: Request):
        me = require_person(request)
        if not me["is_admin"]:
            raise StarletteHTTPException(403, "Admins only.")
        return me

    def flash(request: Request, msg: str, kind: str = "info"):
        sess(request).setdefault("flash", []).append([kind, msg])

    def render(request: Request, template: str, status: int = 200, **ctx):
        s = sess(request)
        ctx.update(
            me=current_person(request),
            group=current_group(request),
            csrf=s.get("csrf", ""),
            flashes=s.pop("flash", []),
            year=current_year(),
            turnstile_site_key=settings.turnstile_site_key,
        )
        return templates.TemplateResponse(request, template, ctx, status_code=status)

    def go(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    async def csrf_protect(request: Request):
        if request.method == "POST":
            form = await request.form()
            if not csrf_ok(sess(request), form.get("csrf")):
                raise StarletteHTTPException(400, "Your session expired. Go back, refresh the page and try again.")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(csrf_protect)])
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        data = sessions.load(request.cookies.get(SESSION_COOKIE))
        if "csrf" not in data:
            data["csrf"] = new_csrf()
        request.state.session = data
        before = sessions.dump(data)
        response = await call_next(request)
        after = sessions.dump(request.state.session)
        if after != before or SESSION_COOKIE not in request.cookies:
            response.set_cookie(
                SESSION_COOKIE, after, max_age=settings.session_days * 86400,
                httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
            )
        for k, v in headers.items():
            response.headers.setdefault(k, v)
        return response

    app.add_middleware(BodyLimit)

    @app.exception_handler(Redirect)
    async def on_redirect(request: Request, exc: Redirect):
        resp = go(exc.url)
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        if not hasattr(request.state, "session"):
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        msg = exc.detail if exc.status_code != 404 else "That page doesn't exist."
        return render(request, "message.html", status=exc.status_code, message=msg)

    # ---- routes -------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/gate", response_class=HTMLResponse)
    async def gate_page(request: Request):
        if is_gated(request):
            return go("/")
        return render(request, "gate.html", locked=limiter.locked_for(f"gate:{client_ip(request)}"))

    @app.post("/gate")
    async def gate_submit(request: Request, passphrase: str = Form(""),
                          cf_turnstile_response: str = Form("", alias="cf-turnstile-response")):
        ip = client_ip(request)
        key = f"gate:{ip}"
        if limiter.locked_for(key):
            return render(request, "gate.html", status=429, locked=limiter.locked_for(key))
        if not await turnstile_ok(settings.turnstile_secret, cf_turnstile_response, ip):
            flash(request, "Please complete the 'I'm human' check.", "error")
            return go("/gate")
        # Check every group (not stopping early) so timing doesn't hint at which matched.
        matched = None
        for g in db.list_groups():
            if verify_secret(g["passphrase_hash"], passphrase[:200]) and matched is None:
                matched = g
        if not matched:
            limiter.fail(key, *GATE_IP_POLICY)
            log.warning("Bad family passphrase from %s", ip)
            flash(request, "That's not a passphrase we know.", "error")
            return go("/gate")
        limiter.reset(key)
        log.info("Gate opened for group %s from %s", matched["id"], ip)
        request.state.session = {"gid": matched["id"], "gv": matched["gate_version"], "csrf": new_csrf()}
        return go("/")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        group = require_gate(request)
        return render(request, "people.html", people=db.list_people(group["id"]))

    @app.get("/join", response_class=HTMLResponse)
    async def join_page(request: Request):
        require_gate(request)
        return render(request, "join.html", name="")

    @app.post("/join")
    async def join_submit(request: Request, name: str = Form(""), pin: str = Form(""),
                          pin2: str = Form(""),
                          cf_turnstile_response: str = Form("", alias="cf-turnstile-response")):
        group = require_gate(request)
        ip = client_ip(request)
        if not await turnstile_ok(settings.turnstile_secret, cf_turnstile_response, ip):
            flash(request, "Please complete the 'I'm human' check.", "error")
            return render(request, "join.html", name=name[:40])
        clean = clean_name(name)
        error = None
        if not clean:
            error = "Names can be up to 40 letters, numbers, spaces, apostrophes, dots or dashes."
        elif db.person_by_name(group["id"], clean):
            error = f"There's already someone called {clean}. Pick your name from the list instead."
        elif not PIN_RE.match(pin):
            error = "Your PIN must be 4 to 6 digits."
        elif pin != pin2:
            error = "The two PINs don't match."
        if error:
            flash(request, error, "error")
            return render(request, "join.html", name=(clean or name[:40]))
        if not limiter.hit(f"join:{ip}", *JOIN_IP_LIMIT):
            flash(request, "Too many new people from this connection today. Try again tomorrow.", "error")
            return go("/")
        # The first person to join a gift exchange runs it.
        is_admin = db.count_people(group["id"]) == 0
        pin_hash = hash_secret(pin)
        pid = db.create_person(group["id"], clean, pin_hash, is_admin)
        sess(request).update(pid=pid, ph=pin_hash[-16:])
        if db.quiz_pending(pid, group["id"]):
            flash(request, f"Welcome, {clean}! First, a few questions so people know what you like. You can skip this.")
            return go("/questionnaire")
        flash(request, f"Welcome, {clean}! Add things to your list below.")
        return go(f"/p/{pid}")

    @app.get("/login/{person_id}", response_class=HTMLResponse)
    async def login_page(request: Request, person_id: int):
        group = require_gate(request)
        person = person_in_group(group, person_id)
        return render(request, "login.html", person=person,
                      locked=limiter.locked_for(f"pin:{person_id}"))

    @app.post("/login/{person_id}")
    async def login_submit(request: Request, person_id: int, pin: str = Form(""),
                           cf_turnstile_response: str = Form("", alias="cf-turnstile-response")):
        group = require_gate(request)
        person = person_in_group(group, person_id)
        ip = client_ip(request)
        pkey, ikey = f"pin:{person_id}", f"pinip:{ip}"
        locked = max(limiter.locked_for(pkey), limiter.locked_for(ikey))
        if locked:
            return render(request, "login.html", status=429, person=person, locked=locked)
        if not await turnstile_ok(settings.turnstile_secret, cf_turnstile_response, ip):
            flash(request, "Please complete the 'I'm human' check.", "error")
            return go(f"/login/{person_id}")
        if not verify_secret(person["pin_hash"], pin[:12]):
            limiter.fail(pkey, *PIN_PERSON_POLICY)
            limiter.fail(ikey, *PIN_IP_POLICY)
            log.warning("Bad PIN for person %s from %s", person_id, ip)
            flash(request, "Wrong PIN.", "error")
            return go(f"/login/{person_id}")
        limiter.reset(pkey)
        sess(request).update(pid=person_id, ph=person["pin_hash"][-16:])
        if db.quiz_pending(person_id, group["id"]):
            return go("/questionnaire")
        return go(f"/p/{person_id}")

    @app.post("/logout")
    async def logout(request: Request):
        s = sess(request)
        s.pop("pid", None)
        s.pop("ph", None)
        return go("/")

    @app.post("/forget")
    async def forget(request: Request):
        request.state.session = {"csrf": new_csrf()}
        return go("/gate")

    # ---- lists ----------------------------------------------------------------
    @app.get("/p/{person_id}", response_class=HTMLResponse)
    async def person_list(request: Request, person_id: int, year: Optional[int] = None):
        me = require_person(request)
        person = person_in_group(require_gate(request), person_id)
        this_year = current_year()
        year = year or this_year
        years = sorted(set(db.years_for(person_id)) | {this_year}, reverse=True)
        is_owner = me["id"] == person_id
        items = db.items_for_owner(person_id, year) if is_owner else db.items_for_viewer(person_id, year)
        about, _ = (db.quiz_answers_shown(person_id, person["group_id"])
                                if db.quiz_enabled(person["group_id"]) else ([], None))
        return render(
            request, "list.html", person=person, items=items, years=years, shown_year=year,
            about=about, quiz_on=db.quiz_enabled(person["group_id"]),
            is_owner=is_owner, editable=(year == this_year),
            can_copy=is_owner and year == this_year and not items and (this_year - 1) in years,
        )

    def own_current_item(me, item_id: int):
        item = db.get_item(item_id)
        if not item or item["person_id"] != me["id"]:
            raise StarletteHTTPException(404)
        if item["year"] != current_year():
            raise StarletteHTTPException(403, "Lists from past years can't be changed.")
        return item

    @app.post("/items")
    async def add_item(request: Request, title: str = Form(""), link: str = Form(""),
                       notes: str = Form(""), price: str = Form(""), priority: str = Form("2")):
        me = require_person(request)
        year = current_year()
        errors, values = clean_item(title, link, notes, price, priority)
        if db.count_items(me["id"], year) >= MAX_ITEMS_PER_YEAR:
            errors.append(f"Lists are limited to {MAX_ITEMS_PER_YEAR} items.")
        for e in errors:
            flash(request, e, "error")
        if not errors:
            db.add_item(me["id"], year, *values)
        return go(f"/p/{me['id']}")

    @app.post("/items/copy-last-year")
    async def copy_last_year(request: Request):
        me = require_person(request)
        year = current_year()
        n = db.copy_year(me["id"], year - 1, year, MAX_ITEMS_PER_YEAR)
        flash(request, f"Copied {n} item{'s' if n != 1 else ''} from {year - 1}.")
        return go(f"/p/{me['id']}")

    @app.get("/items/{item_id}/edit", response_class=HTMLResponse)
    async def edit_page(request: Request, item_id: int):
        me = require_person(request)
        item = own_current_item(me, item_id)
        return render(request, "edit.html", item=item)

    @app.post("/items/{item_id}/edit")
    async def edit_submit(request: Request, item_id: int, title: str = Form(""),
                          link: str = Form(""), notes: str = Form(""), price: str = Form(""),
                          priority: str = Form("2")):
        me = require_person(request)
        own_current_item(me, item_id)
        errors, values = clean_item(title, link, notes, price, priority)
        if errors:
            for e in errors:
                flash(request, e, "error")
            return go(f"/items/{item_id}/edit")
        db.update_item(item_id, *values)
        return go(f"/p/{me['id']}")

    @app.post("/items/{item_id}/delete")
    async def delete_item(request: Request, item_id: int):
        me = require_person(request)
        own_current_item(me, item_id)
        db.delete_item(item_id)
        return go(f"/p/{me['id']}")

    @app.post("/items/{item_id}/move")
    async def move_item(request: Request, item_id: int, direction: str = Form("")):
        me = require_person(request)
        own_current_item(me, item_id)
        db.move_item(item_id, -1 if direction == "up" else 1)
        return go(f"/p/{me['id']}")

    # ---- claims ---------------------------------------------------------------
    def claimable(me, item_id: int):
        item = db.get_item(item_id)
        owner = db.get_person(item["person_id"]) if item else None
        if not owner or owner["group_id"] != me["group_id"]:
            raise StarletteHTTPException(404)
        if item["person_id"] == me["id"]:
            raise StarletteHTTPException(403, "You can't claim things on your own list.")
        if item["year"] != current_year():
            raise StarletteHTTPException(403, "Lists from past years can't be changed.")
        return item

    @app.post("/items/{item_id}/claim")
    async def claim(request: Request, item_id: int):
        me = require_person(request)
        item = claimable(me, item_id)
        if not db.claim(item_id, me["id"]):
            flash(request, "Someone else already got that one.", "error")
        return go(f"/p/{item['person_id']}")

    @app.post("/items/{item_id}/unclaim")
    async def unclaim(request: Request, item_id: int, back: str = Form("")):
        me = require_person(request)
        item = claimable(me, item_id)
        db.unclaim(item_id, me["id"])
        return go("/shopping" if back == "shopping" else f"/p/{item['person_id']}")

    @app.get("/shopping", response_class=HTMLResponse)
    async def shopping(request: Request):
        me = require_person(request)
        return render(request, "shopping.html", claims=db.my_claims(me["id"], current_year()))

    # ---- account --------------------------------------------------------------
    @app.get("/account", response_class=HTMLResponse)
    async def account(request: Request):
        require_person(request)
        return render(request, "account.html")

    @app.post("/account/pin")
    async def change_pin(request: Request, current: str = Form(""), pin: str = Form(""),
                         pin2: str = Form("")):
        me = require_person(request)
        pkey = f"pin:{me['id']}"
        if limiter.locked_for(pkey):
            flash(request, "Too many wrong PINs. Try again later.", "error")
        elif not verify_secret(me["pin_hash"], current[:12]):
            limiter.fail(pkey, *PIN_PERSON_POLICY)
            flash(request, "Your current PIN is wrong.", "error")
        elif not PIN_RE.match(pin) or pin != pin2:
            flash(request, "New PIN must be 4 to 6 digits and match.", "error")
        else:
            new_hash = hash_secret(pin)
            db.set_pin(me["id"], new_hash)
            sess(request)["ph"] = new_hash[-16:]
            flash(request, "PIN changed.")
        return go("/account")

    # ---- questionnaire -----------------------------------------------------------
    @app.get("/questionnaire", response_class=HTMLResponse)
    async def questionnaire(request: Request):
        me = require_person(request)
        if not db.quiz_enabled(me["group_id"]):
            raise StarletteHTTPException(404)
        return render(request, "questionnaire.html", questions=db.quiz_questions(me["group_id"]),
                      answers=db.quiz_answers(me["id"]), first=db.quiz_pending(me["id"], me["group_id"]),
                      ANSWER_LEN=ANSWER_LEN)

    @app.post("/questionnaire")
    async def questionnaire_save(request: Request):
        me = require_person(request)
        if not db.quiz_enabled(me["group_id"]):
            raise StarletteHTTPException(404)
        form = await request.form()
        # Only this group's current questions are saved; anything else in the form is ignored.
        answers = {q["id"]: str(form.get(f"q{q['id']}", "")).strip()[:ANSWER_LEN]
                   for q in db.quiz_questions(me["group_id"])}
        db.save_quiz_answers(me["id"], answers)
        flash(request, "Thanks! Your answers are saved. Everyone in the exchange can see them on your list.")
        return go(f"/p/{me['id']}")

    # ---- admin ----------------------------------------------------------------
    @app.get("/admin", response_class=HTMLResponse)
    async def admin(request: Request):
        me = require_admin(request)
        return render(request, "admin.html", people=db.list_people(me["group_id"]),
                      quiz_on=db.quiz_enabled(me["group_id"]),
                      quiz_count=len(db.quiz_questions(me["group_id"])))

    @app.get("/admin/questionnaire", response_class=HTMLResponse)
    async def admin_quiz(request: Request):
        me = require_admin(request)
        return render(request, "admin_quiz.html", questions=db.quiz_questions(me["group_id"]),
                      quiz_on=db.quiz_enabled(me["group_id"]), TEMPLATES=TEMPLATES,
                      MAX_QUESTIONS=MAX_QUESTIONS, QUESTION_LEN=QUESTION_LEN)

    def own_question(me, question_id: int):
        q = db.get_quiz_question(question_id)
        if not q or q["group_id"] != me["group_id"]:
            raise StarletteHTTPException(404)
        return q

    def clean_question(text: str) -> str:
        return " ".join(text.split())[:QUESTION_LEN]

    @app.post("/admin/questionnaire/enabled")
    async def admin_quiz_enabled(request: Request, enabled: str = Form(""), back: str = Form("")):
        me = require_admin(request)
        on = enabled == "1"
        if on and not db.quiz_questions(me["group_id"]):
            db.replace_quiz_questions(me["group_id"], TEMPLATES["family"]["questions"])
            flash(request, f"Loaded the \"{TEMPLATES['family']['name']}\" questions to start you off.")
        db.set_quiz_enabled(me["group_id"], on)
        flash(request, "Questionnaire turned on. People will be asked to fill it in when they log in."
              if on else "Questionnaire turned off. Answers are kept but hidden.")
        return go("/admin/questionnaire" if back == "quiz" else "/admin")

    @app.post("/admin/questionnaire/template")
    async def admin_quiz_template(request: Request, template: str = Form("")):
        me = require_admin(request)
        t = TEMPLATES.get(template)
        if not t:
            flash(request, "Pick a template.", "error")
        else:
            db.replace_quiz_questions(me["group_id"], t["questions"])
            flash(request, f"Loaded \"{t['name']}\". Everyone will be asked to fill it in at their next login.")
        return go("/admin/questionnaire")

    @app.post("/admin/questionnaire/questions")
    async def admin_quiz_add(request: Request, text: str = Form(""), long: str = Form("")):
        me = require_admin(request)
        clean = clean_question(text)
        if not clean:
            flash(request, "Type a question first.", "error")
        elif len(db.quiz_questions(me["group_id"])) >= MAX_QUESTIONS:
            flash(request, f"That's the limit of {MAX_QUESTIONS} questions.", "error")
        else:
            db.add_quiz_question(me["group_id"], clean, long == "1")
            flash(request, "Question added.")
        return go("/admin/questionnaire")

    @app.post("/admin/questionnaire/questions/{question_id}/edit")
    async def admin_quiz_edit(request: Request, question_id: int, text: str = Form(""), long: str = Form("")):
        me = require_admin(request)
        own_question(me, question_id)
        clean = clean_question(text)
        if not clean:
            flash(request, "A question can't be empty. Use Remove to delete it.", "error")
        else:
            db.update_quiz_question(question_id, clean, long == "1")
            flash(request, "Question saved.")
        return go("/admin/questionnaire")

    @app.post("/admin/questionnaire/questions/{question_id}/move")
    async def admin_quiz_move(request: Request, question_id: int, direction: str = Form("")):
        me = require_admin(request)
        own_question(me, question_id)
        db.move_quiz_question(question_id, -1 if direction == "up" else 1)
        return go("/admin/questionnaire")

    @app.post("/admin/questionnaire/questions/{question_id}/delete")
    async def admin_quiz_delete(request: Request, question_id: int):
        me = require_admin(request)
        own_question(me, question_id)
        db.delete_quiz_question(question_id)
        flash(request, "Question removed, along with everyone's answers to it.")
        return go("/admin/questionnaire")

    def admin_target(me, person_id: int):
        person = db.get_person(person_id)
        if not person or person["group_id"] != me["group_id"]:
            raise StarletteHTTPException(404)
        return person

    @app.post("/admin/people/{person_id}/pin")
    async def admin_reset_pin(request: Request, person_id: int, pin: str = Form("")):
        me = require_admin(request)
        person = admin_target(me, person_id)
        if not PIN_RE.match(pin):
            flash(request, "PIN must be 4 to 6 digits.", "error")
        else:
            db.set_pin(person_id, hash_secret(pin))
            limiter.reset(f"pin:{person_id}")
            flash(request, f"PIN reset for {person['name']}. Tell them the new PIN.")
        return go("/admin")

    @app.post("/admin/people/{person_id}/rename")
    async def admin_rename(request: Request, person_id: int, name: str = Form("")):
        me = require_admin(request)
        person = admin_target(me, person_id)
        clean = clean_name(name)
        existing = db.person_by_name(me["group_id"], clean) if clean else None
        if not clean or (existing and existing["id"] != person_id):
            flash(request, "That name is invalid or already taken.", "error")
        else:
            db.rename_person(person_id, clean)
            flash(request, f"Renamed {person['name']} to {clean}.")
        return go("/admin")

    @app.post("/admin/people/{person_id}/admin")
    async def admin_toggle(request: Request, person_id: int):
        me = require_admin(request)
        person = admin_target(me, person_id)
        if person_id == me["id"]:
            flash(request, "You can't remove your own admin rights.", "error")
        else:
            db.set_admin(person_id, not person["is_admin"])
        return go("/admin")

    @app.post("/admin/people/{person_id}/delete")
    async def admin_delete(request: Request, person_id: int, confirm_name: str = Form("")):
        me = require_admin(request)
        person = admin_target(me, person_id)
        if person_id == me["id"]:
            flash(request, "You can't delete yourself.", "error")
        elif confirm_name.strip().casefold() != person["name"].casefold():
            flash(request, "Type the person's name exactly to confirm deleting them.", "error")
        else:
            db.delete_person(person_id)
            flash(request, f"Deleted {person['name']} and all their lists.")
        return go("/admin")

    @app.post("/admin/passphrase")
    async def admin_passphrase(request: Request, passphrase: str = Form(""),
                               passphrase2: str = Form("")):
        me = require_admin(request)
        if len(passphrase) < 12 or len(passphrase) > 200 or passphrase != passphrase2:
            flash(request, "The new passphrase must be 12+ characters and match.", "error")
            return go("/admin")
        # Each passphrase must lead to exactly one group.
        if any(verify_secret(g["passphrase_hash"], passphrase)
               for g in db.list_groups() if g["id"] != me["group_id"]):
            flash(request, "Pick a different passphrase.", "error")
            return go("/admin")
        new_version = db.set_group_passphrase(me["group_id"], hash_secret(passphrase))
        sess(request)["gv"] = new_version
        flash(request, "Passphrase changed. Everyone else in this exchange will need the new one.")
        return go("/admin")

    @app.post("/admin/backup")
    async def admin_backup(request: Request):
        require_admin(request)
        path = await asyncio.to_thread(db.backup, settings.backup_dir, settings.backup_keep)
        flash(request, f"Backup saved: {os.path.basename(path)}")
        return go("/admin")

    app.state.db = db
    app.state.settings = settings
    return app


def get_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
