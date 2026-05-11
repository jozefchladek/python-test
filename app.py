import random
import secrets
import smtplib
import string
import time
from collections import defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from hashlib import sha256
from urllib.parse import quote

import bcrypt
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from starlette.types import ASGIApp, Receive, Scope, Send

import config
from database import Base, engine, get_db
from models import GameInvitation, InviteCode, Pitch, PitchHole, PlannedGame
from models import Session as GameSession
from models import SessionPlayer, Score, User

# --- Config ---
app = FastAPI(title="Footgolfisti")
templates = Jinja2Templates(directory="templates")

serializer = URLSafeSerializer(config.SECRET_KEY)
timed_serializer = URLSafeTimedSerializer(config.SECRET_KEY)

Base.metadata.create_all(bind=engine)


# --- Rate Limiter ---
class RateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.attempts: dict[str, list[float]] = defaultdict(list)

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        self.attempts[key] = [t for t in self.attempts[key] if now - t < self.window_seconds]
        return len(self.attempts[key]) >= self.max_attempts

    def record(self, key: str):
        self.attempts[key].append(time.time())


login_limiter = RateLimiter(max_attempts=5, window_seconds=300)
reset_limiter = RateLimiter(max_attempts=3, window_seconds=300)


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --- CSRF Protection (pure ASGI middleware to avoid body consumption issues) ---
class CSRFMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        if request.method == "POST" and not request.url.path.startswith("/admin"):
            body = await request.body()
            csrf_cookie = request.cookies.get("csrf_token", "")

            # Check X-CSRF-Token header first (for AJAX/JSON requests)
            csrf_header = request.headers.get("x-csrf-token", "")
            if csrf_cookie and csrf_header == csrf_cookie:
                async def receive_body():
                    return {"type": "http.request", "body": body}
                await self.app(scope, receive_body, send)
                return

            # Parse form fields from URL-encoded body
            from urllib.parse import parse_qs
            params = parse_qs(body.decode("utf-8", errors="replace"))
            csrf_form = params.get("_csrf_token", [""])[0]

            if not csrf_cookie or csrf_cookie != csrf_form:
                response = JSONResponse(
                    status_code=403,
                    content={"detail": "CSRF token chýba alebo je neplatný. Skúste obnoviť stránku."},
                )
                await response(scope, receive, send)
                return

            # Put the body back so downstream handlers can read it
            async def receive_body():
                return {"type": "http.request", "body": body}

            await self.app(scope, receive_body, send)
        else:
            await self.app(scope, receive, send)


app.add_middleware(CSRFMiddleware)


# --- Force correct host/scheme behind proxy using APP_URL ---
from urllib.parse import urlparse

_parsed_app_url = urlparse(config.APP_URL)
_app_host = _parsed_app_url.hostname or "localhost"
_app_port = _parsed_app_url.port
_app_scheme = _parsed_app_url.scheme or "https"

if _app_host != "localhost" and config.APP_URL != "http://localhost:8000":
    @app.middleware("http")
    async def fix_proxy_headers(request: Request, call_next):
        # Override scope so Starlette/SQLAdmin sees the real host
        scope = request.scope
        if _app_port and _app_port not in (80, 443):
            scope["server"] = (_app_host, _app_port)
        else:
            scope["server"] = (_app_host, 443 if _app_scheme == "https" else 80)
        scope["scheme"] = _app_scheme
        # Set headers so other middleware also sees correct host
        headers = dict(scope.get("headers", []))
        new_headers = []
        for k, v in scope.get("headers", []):
            if k == b"host":
                new_headers.append((b"host", _app_host.encode()))
            else:
                new_headers.append((k, v))
        scope["headers"] = new_headers
        return await call_next(request)


app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Auth Helpers ---
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    if stored_hash.startswith("$2b$"):
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    # Legacy SHA256 fallback
    return sha256((password + config.SECRET_KEY).encode()).hexdigest() == stored_hash


def upgrade_password_if_needed(user, password: str, db: Session):
    """Transparently upgrade legacy SHA256 hash to bcrypt on login."""
    if not user.password_hash.startswith("$2b$"):
        user.password_hash = hash_password(password)
        db.commit()


def create_token(user_id: int) -> str:
    return serializer.dumps({"sub": user_id})


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get("token")
    if not token:
        return None
    try:
        payload = serializer.loads(token)
        user_id = int(payload.get("sub"))
        return db.query(User).filter(User.id == user_id).first()
    except (BadSignature, ValueError, TypeError):
        return None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def generate_invite_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def generate_user_invite_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


INVITES_PER_USER = 3


def create_invite_codes_for_user(user_id: int, db: Session):
    """Create invite codes for a new user if they don't have any."""
    existing = db.query(InviteCode).filter(InviteCode.owner_id == user_id).count()
    for _ in range(INVITES_PER_USER - existing):
        code = generate_user_invite_code()
        while db.query(InviteCode).filter(InviteCode.code == code).first():
            code = generate_user_invite_code()
        db.add(InviteCode(code=code, owner_id=user_id))
    db.commit()


def score_label(diff: int) -> tuple[str, str]:
    """Return (label, tailwind color class) for a score vs par difference."""
    if diff <= -3:
        return "Albatros", "text-yellow-500"
    elif diff == -2:
        return "Eagle", "text-emerald-600"
    elif diff == -1:
        return "Birdie", "text-green-500"
    elif diff == 0:
        return "Par", "text-gray-500"
    elif diff == 1:
        return "Bogey", "text-orange-500"
    elif diff == 2:
        return "Double Bogey", "text-red-500"
    else:
        return f"+{diff}", "text-red-700"


def build_gcal_url(title: str, location: str, date: datetime, description: str = "") -> str:
    """Build a Google Calendar event URL."""
    start = date.strftime("%Y%m%dT%H%M00")
    # Assume 3 hours for a game
    end_dt = date.replace(hour=date.hour + 3) if date.hour <= 21 else date
    end = end_dt.strftime("%Y%m%dT%H%M00")
    params = f"action=TEMPLATE&text={quote(title)}&dates={start}/{end}&location={quote(location)}&details={quote(description)}"
    return f"https://calendar.google.com/calendar/render?{params}"


def send_invitation_email(to_email: str, planned_game: PlannedGame, creator_name: str):
    """Send email invitation for a planned game."""
    if not config.SMTP_HOST or not config.SMTP_USER:
        return False

    pitch_name = planned_game.pitch.name
    location = planned_game.pitch.location or ""
    date_str = planned_game.planned_date.strftime("%d.%m.%Y o %H:%M")
    gcal_url = build_gcal_url(
        f"Footgolf - {pitch_name}",
        location,
        planned_game.planned_date,
        f"Organizuje: {creator_name}\n{planned_game.note or ''}",
    )

    subject = f"⚽🏌️ Pozvánka na footgolf - {pitch_name}"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #059669;">⚽🏌️ Footgolfisti</h2>
        <p><strong>{creator_name}</strong> ťa pozýva na footgolf!</p>
        <div style="background: #f0fdf4; padding: 16px; border-radius: 12px; margin: 16px 0;">
            <p style="margin: 4px 0;">⛳ <strong>{pitch_name}</strong></p>
            <p style="margin: 4px 0;">📅 <strong>{date_str}</strong></p>
            {"<p style='margin: 4px 0;'>📍 " + location + "</p>" if location else ""}
            {"<p style='margin: 4px 0;'>📝 " + planned_game.note + "</p>" if planned_game.note else ""}
        </div>
        <div style="margin: 20px 0;">
            <a href="{gcal_url}" style="background: #059669; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; display: inline-block;">
                📅 Pridať do Google Calendar
            </a>
        </div>
        <p style="color: #6b7280; font-size: 14px;">
            Zaregistruj sa na <a href="{config.APP_URL}">{config.APP_URL}</a> a sleduj svoje skóre!
        </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_USER, to_email, msg.as_string())
        return True
    except Exception:
        return False


def send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send password reset email."""
    if not config.SMTP_HOST or not config.SMTP_USER:
        return False

    subject = "⚽🏌️ Footgolfisti - Obnovenie hesla"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #059669;">⚽🏌️ Footgolfisti</h2>
        <p>Požiadal si o obnovenie hesla.</p>
        <div style="margin: 20px 0;">
            <a href="{reset_url}" style="background: #059669; color: white; padding: 12px 24px; border-radius: 8px; text-decoration: none; display: inline-block;">
                Obnoviť heslo
            </a>
        </div>
        <p style="color: #6b7280; font-size: 14px;">
            Odkaz je platný 1 hodinu. Ak si nepožiadal o obnovenie hesla, tento e-mail ignoruj.
        </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_USER, to_email, msg.as_string())
        return True
    except Exception:
        return False


# Make helpers available in templates
templates.env.globals["score_label"] = score_label
templates.env.globals["google_maps_api_key"] = config.GOOGLE_MAPS_API_KEY
templates.env.globals["smtp_configured"] = bool(config.SMTP_HOST and config.SMTP_USER)


# --- Auth exception handler ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(url=exc.headers.get("Location", "/login"), status_code=303)
    return templates.TemplateResponse(request, "error.html", {"detail": exc.detail, "status_code": exc.status_code}, status_code=exc.status_code)


# --- Auth Routes ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    client_ip = get_client_ip(request)
    if login_limiter.is_rate_limited(client_ip):
        return templates.TemplateResponse(request, "login.html", {"error": "Príliš veľa pokusov. Skúste znova o 5 minút."}, status_code=429)

    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        login_limiter.record(client_ip)
        return templates.TemplateResponse(request, "login.html", {"error": "Nesprávne meno alebo heslo."})

    upgrade_password_if_needed(user, password, db)
    token = create_token(user.id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("token", token, httponly=True, samesite="lax", secure=config.SECURE_COOKIES, max_age=60 * 60 * 24 * 90)
    return response


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, code: str = "", db: Session = Depends(get_db)):
    # First user can register without invite code
    user_count = db.query(User).count()
    return templates.TemplateResponse(request, "register.html", {"invite_code": code, "first_user": user_count == 0})


@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    email: str = Form(""),
    invite_code: str = Form(""),
    db: Session = Depends(get_db),
):
    user_count = db.query(User).count()
    ctx = {"invite_code": invite_code, "first_user": user_count == 0}

    # Require invite code unless first user
    invite = None
    if user_count > 0:
        if not invite_code.strip():
            return templates.TemplateResponse(request, "register.html", {**ctx, "error": "Pre registráciu potrebuješ kód pozvánky."})
        invite = db.query(InviteCode).filter(InviteCode.code == invite_code.strip().upper(), InviteCode.used_by_id == None).first()
        if not invite:
            return templates.TemplateResponse(request, "register.html", {**ctx, "error": "Neplatný alebo už použitý kód pozvánky."})

    if password != password2:
        return templates.TemplateResponse(request, "register.html", {**ctx, "error": "Heslá sa nezhodujú."})
    if len(password) < 4:
        return templates.TemplateResponse(request, "register.html", {**ctx, "error": "Heslo musí mať aspoň 4 znaky."})
    if db.query(User).filter(User.username == username).first():
        return templates.TemplateResponse(request, "register.html", {**ctx, "error": "Toto používateľské meno je už obsadené."})

    user = User(
        username=username,
        display_name=display_name,
        email=email if email else None,
        password_hash=hash_password(password),
        invited_by_id=invite.owner_id if invite else None,
        is_admin=user_count == 0,  # First user is admin
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Mark invite code as used
    if invite:
        invite.used_by_id = user.id
        invite.used_at = datetime.utcnow()
        db.commit()

    # Generate invite codes for the new user
    create_invite_codes_for_user(user.id, db)

    token = create_token(user.id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("token")
    return response


# --- Forgot Password ---
@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html")


@app.post("/forgot-password")
async def forgot_password(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    client_ip = get_client_ip(request)
    if reset_limiter.is_rate_limited(client_ip):
        return templates.TemplateResponse(request, "forgot_password.html", {"error": "Príliš veľa pokusov. Skúste znova o 5 minút."}, status_code=429)

    reset_limiter.record(client_ip)
    user = db.query(User).filter(User.email == email.strip()).first()
    # Always show success message to prevent email enumeration
    success_msg = "Ak existuje účet s touto e-mailovou adresou, bol odoslaný odkaz na obnovenie hesla."

    if user and user.email:
        token = timed_serializer.dumps({"user_id": user.id}, salt="password-reset")
        reset_url = f"{config.APP_URL}/reset-password?token={token}"
        send_reset_email(user.email, reset_url)

    return templates.TemplateResponse(request, "forgot_password.html", {"success": success_msg})


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    token = request.query_params.get("token", "")
    if not token:
        return templates.TemplateResponse(request, "forgot_password.html", {"error": "Neplatný odkaz na obnovenie hesla."})
    return templates.TemplateResponse(request, "reset_password.html", {"token": token})


@app.post("/reset-password")
async def reset_password(
    request: Request,
    token: str = Form(...),
    new_password: str = Form(...),
    new_password2: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        payload = timed_serializer.loads(token, salt="password-reset", max_age=3600)
        user_id = payload.get("user_id")
    except SignatureExpired:
        return templates.TemplateResponse(request, "forgot_password.html", {"error": "Odkaz na obnovenie hesla vypršal. Skús to znova."})
    except BadSignature:
        return templates.TemplateResponse(request, "forgot_password.html", {"error": "Neplatný odkaz na obnovenie hesla."})

    if new_password != new_password2:
        return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": "Heslá sa nezhodujú."})
    if len(new_password) < 4:
        return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": "Heslo musí mať aspoň 4 znaky."})

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return templates.TemplateResponse(request, "forgot_password.html", {"error": "Používateľ nenájdený."})

    user.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(request, "login.html", {"success": "Heslo bolo úspešne zmenené. Prihlás sa s novým heslom."})


# --- Profile ---
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    invite_codes = db.query(InviteCode).filter(InviteCode.owner_id == user.id).all()
    return templates.TemplateResponse(request, "profile.html", {"user": user, "invite_codes": invite_codes, "app_url": config.APP_URL})


@app.post("/profile")
async def profile_update(
    request: Request,
    display_name: str = Form(...),
    email: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    errors = []
    user = db.query(User).filter(User.id == user.id).first()
    user.display_name = display_name
    user.email = email if email else None

    if new_password:
        if not current_password or not verify_password(current_password, user.password_hash):
            errors.append("Nesprávne aktuálne heslo.")
        elif new_password != new_password2:
            errors.append("Nové heslá sa nezhodujú.")
        elif len(new_password) < 4:
            errors.append("Nové heslo musí mať aspoň 4 znaky.")
        else:
            user.password_hash = hash_password(new_password)

    invite_codes = db.query(InviteCode).filter(InviteCode.owner_id == user.id).all()
    ctx = {"user": user, "invite_codes": invite_codes, "app_url": config.APP_URL}

    if errors:
        return templates.TemplateResponse(request, "profile.html", {**ctx, "error": " ".join(errors)})

    db.commit()
    return templates.TemplateResponse(request, "profile.html", {**ctx, "success": "Profil bol aktualizovaný."})


# --- Dashboard ---
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    recent_sessions = (
        db.query(GameSession)
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id)
        .options(joinedload(GameSession.pitch), joinedload(GameSession.players).joinedload(SessionPlayer.user))
        .order_by(GameSession.started_at.desc())
        .limit(5)
        .all()
    )

    active_sessions = [s for s in recent_sessions if s.status == "active"]

    completed_count = (
        db.query(GameSession)
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id, GameSession.status == "completed")
        .count()
    )

    pitches_played = (
        db.query(func.count(func.distinct(GameSession.pitch_id)))
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id)
        .scalar()
    )

    # Upcoming planned games
    upcoming_games = (
        db.query(PlannedGame)
        .filter(
            PlannedGame.status == "planned",
            PlannedGame.planned_date >= datetime.utcnow(),
        )
        .filter(
            (PlannedGame.created_by_id == user.id)
            | PlannedGame.id.in_(
                db.query(GameInvitation.planned_game_id)
                .filter(
                    (GameInvitation.user_id == user.id)
                    | ((GameInvitation.email == user.email) & (user.email != None) & (user.email != ""))
                )
            )
        )
        .options(joinedload(PlannedGame.pitch), joinedload(PlannedGame.creator), joinedload(PlannedGame.invitations))
        .order_by(PlannedGame.planned_date.asc())
        .limit(3)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "recent_sessions": recent_sessions,
            "active_sessions": active_sessions,
            "completed_count": completed_count,
            "pitches_played": pitches_played,
            "upcoming_games": upcoming_games,
        },
    )


# --- Pitches ---
@app.get("/pitches", response_class=HTMLResponse)
async def pitches_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitches = db.query(Pitch).options(joinedload(Pitch.holes)).order_by(Pitch.name).all()
    return templates.TemplateResponse(request, "pitches.html", {"user": user, "pitches": pitches})


@app.get("/pitches/create", response_class=HTMLResponse)
async def pitch_create_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "pitch_create.html", {"user": user})


@app.post("/pitches/create")
async def pitch_create(
    request: Request,
    user: User = Depends(require_user),
    name: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    place_id: str = Form(""),
    num_holes: int = Form(...),
    db: Session = Depends(get_db),
):
    form_data = await request.form()

    # Check for duplicate pitch by place_id
    if place_id.strip():
        existing = db.query(Pitch).filter(Pitch.place_id == place_id.strip()).first()
        if existing:
            return templates.TemplateResponse(
                request,
                "pitch_create.html",
                {"user": user, "error": f"Toto ihrisko už existuje: {existing.name}", "existing_pitch_id": existing.id},
            )

    pitch = Pitch(
        name=name,
        description=description,
        location=location,
        place_id=place_id.strip() if place_id.strip() else None,
        num_holes=num_holes,
        created_by_id=user.id,
    )
    db.add(pitch)
    db.flush()

    for i in range(1, num_holes + 1):
        par = int(form_data.get(f"par_{i}", 3))
        hole = PitchHole(pitch_id=pitch.id, hole_number=i, par=par)
        db.add(hole)

    db.commit()
    return RedirectResponse(url=f"/pitches/{pitch.id}", status_code=303)


@app.get("/pitches/{pitch_id}", response_class=HTMLResponse)
async def pitch_detail(request: Request, pitch_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Ihrisko nenájdené")

    total_par = sum(h.par for h in pitch.holes)
    session_count = db.query(GameSession).filter(GameSession.pitch_id == pitch_id, GameSession.status == "completed").count()

    return templates.TemplateResponse(
        request,
        "pitch_detail.html",
        {"user": user, "pitch": pitch, "total_par": total_par, "session_count": session_count},
    )


@app.get("/pitches/{pitch_id}/edit", response_class=HTMLResponse)
async def pitch_edit_page(request: Request, pitch_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Ihrisko nenájdené")
    if pitch.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca ihriska môže upravovať.")
    return templates.TemplateResponse(request, "pitch_edit.html", {"user": user, "pitch": pitch})


@app.post("/pitches/{pitch_id}/edit")
async def pitch_edit(
    request: Request,
    pitch_id: int,
    user: User = Depends(require_user),
    name: str = Form(...),
    description: str = Form(""),
    location: str = Form(""),
    db: Session = Depends(get_db),
):
    pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Ihrisko nenájdené")
    if pitch.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca ihriska môže upravovať.")

    form_data = await request.form()
    pitch.name = name
    pitch.description = description
    pitch.location = location

    for hole in pitch.holes:
        par_val = form_data.get(f"par_{hole.hole_number}")
        if par_val:
            hole.par = int(par_val)

    db.commit()
    return RedirectResponse(url=f"/pitches/{pitch_id}", status_code=303)


# --- Sessions ---
@app.get("/sessions/new", response_class=HTMLResponse)
async def session_new_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitches = db.query(Pitch).options(joinedload(Pitch.holes)).order_by(Pitch.name).all()
    return templates.TemplateResponse(request, "session_new.html", {"user": user, "pitches": pitches})


@app.post("/sessions/new")
async def session_create(
    request: Request,
    pitch_id: int = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    pitch = db.query(Pitch).filter(Pitch.id == pitch_id).first()
    if not pitch:
        raise HTTPException(status_code=404, detail="Ihrisko nenájdené")

    invite_code = generate_invite_code()
    while db.query(GameSession).filter(GameSession.invite_code == invite_code).first():
        invite_code = generate_invite_code()

    session = GameSession(pitch_id=pitch_id, created_by_id=user.id, invite_code=invite_code)
    db.add(session)
    db.flush()

    player = SessionPlayer(session_id=session.id, user_id=user.id)
    db.add(player)
    db.commit()

    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@app.get("/sessions/join", response_class=HTMLResponse)
async def session_join_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "session_join.html", {"user": user})


@app.post("/sessions/join")
async def session_join(
    request: Request,
    invite_code: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = db.query(GameSession).filter(GameSession.invite_code == invite_code.upper().strip()).first()
    if not session:
        return templates.TemplateResponse(request, "session_join.html", {"user": user, "error": "Neplatný kód pozvania."})
    if session.status != "active":
        return templates.TemplateResponse(request, "session_join.html", {"user": user, "error": "Táto hra už bola ukončená."})

    existing = db.query(SessionPlayer).filter(SessionPlayer.session_id == session.id, SessionPlayer.user_id == user.id).first()
    if not existing:
        player = SessionPlayer(session_id=session.id, user_id=user.id)
        db.add(player)
        db.commit()

    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    session = (
        db.query(GameSession)
        .options(
            joinedload(GameSession.pitch).joinedload(Pitch.holes),
            joinedload(GameSession.players).joinedload(SessionPlayer.user),
            joinedload(GameSession.scores),
        )
        .filter(GameSession.id == session_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Hra nenájdená")

    player_ids = [p.user_id for p in session.players]
    if user.id not in player_ids:
        raise HTTPException(status_code=403, detail="Nie ste hráčom tejto hry.")

    holes = sorted(session.pitch.holes, key=lambda h: h.hole_number)
    par_map = {h.hole_number: h.par for h in holes}
    total_par = sum(h.par for h in holes)

    score_grid = {}
    for player in session.players:
        score_grid[player.user_id] = {}
    for score in session.scores:
        score_grid.setdefault(score.user_id, {})[score.hole_number] = score.strokes

    player_totals = {}
    for uid, scores in score_grid.items():
        total = sum(scores.values())
        diff = total - total_par if len(scores) == len(holes) else None
        player_totals[uid] = {"total": total, "diff": diff, "holes_played": len(scores)}

    template = "session_result.html" if session.status == "completed" else "session_active.html"
    return templates.TemplateResponse(
        request,
        template,
        {
            "user": user,
            "session": session,
            "holes": holes,
            "par_map": par_map,
            "total_par": total_par,
            "score_grid": score_grid,
            "player_totals": player_totals,
        },
    )


@app.post("/sessions/{session_id}/score")
async def session_save_score(
    request: Request,
    session_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not session or session.status != "active":
        raise HTTPException(status_code=400, detail="Hra nie je aktívna.")

    player = db.query(SessionPlayer).filter(SessionPlayer.session_id == session_id, SessionPlayer.user_id == user.id).first()
    if not player:
        raise HTTPException(status_code=403, detail="Nie ste hráčom tejto hry.")

    form_data = await request.form()
    pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == session.pitch_id).first()

    for hole in pitch.holes:
        key = f"hole_{hole.hole_number}"
        value = form_data.get(key)
        if value and value.strip():
            strokes = int(value)
            existing = db.query(Score).filter(
                Score.session_id == session_id, Score.user_id == user.id, Score.hole_number == hole.hole_number
            ).first()
            if existing:
                existing.strokes = strokes
            else:
                db.add(Score(session_id=session_id, user_id=user.id, hole_number=hole.hole_number, strokes=strokes))

    db.commit()
    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/score/auto")
async def session_auto_save_score(
    request: Request,
    session_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Auto-save a single hole score via AJAX."""
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not session or session.status != "active":
        return JSONResponse({"ok": False, "error": "Hra nie je aktívna."}, status_code=400)

    player = db.query(SessionPlayer).filter(SessionPlayer.session_id == session_id, SessionPlayer.user_id == user.id).first()
    if not player:
        return JSONResponse({"ok": False, "error": "Nie ste hráčom."}, status_code=403)

    data = await request.json()
    hole_number = data.get("hole_number")
    strokes = data.get("strokes")

    if not hole_number or not strokes or strokes < 1 or strokes > 15:
        return JSONResponse({"ok": False, "error": "Neplatné údaje."}, status_code=400)

    existing = db.query(Score).filter(
        Score.session_id == session_id, Score.user_id == user.id, Score.hole_number == hole_number
    ).first()
    if existing:
        existing.strokes = strokes
    else:
        db.add(Score(session_id=session_id, user_id=user.id, hole_number=hole_number, strokes=strokes))
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/sessions/{session_id}/complete")
async def session_complete(
    request: Request,
    session_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Hra nenájdená")
    if session.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca hry môže hru ukončiť.")

    session.status = "completed"
    session.completed_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)


@app.post("/sessions/{session_id}/delete")
async def session_delete(
    request: Request,
    session_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Hra nenájdená")
    if session.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca hry môže hru vymazať.")

    db.query(Score).filter(Score.session_id == session_id).delete()
    db.query(SessionPlayer).filter(SessionPlayer.session_id == session_id).delete()
    db.delete(session)
    db.commit()
    return RedirectResponse(url="/history", status_code=303)


# --- Planned Games ---
@app.get("/games/plan", response_class=HTMLResponse)
async def plan_game_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitches = db.query(Pitch).options(joinedload(Pitch.holes)).order_by(Pitch.name).all()

    # Get friends: users who played in the same sessions as this user
    friend_ids = (
        db.query(SessionPlayer.user_id)
        .filter(
            SessionPlayer.session_id.in_(
                db.query(SessionPlayer.session_id).filter(SessionPlayer.user_id == user.id)
            ),
            SessionPlayer.user_id != user.id,
        )
        .distinct()
        .all()
    )
    friend_ids = [fid[0] for fid in friend_ids]
    friends = db.query(User).filter(User.id.in_(friend_ids)).all() if friend_ids else []

    return templates.TemplateResponse(
        request, "game_plan.html", {"user": user, "pitches": pitches, "friends": friends}
    )


@app.post("/games/plan")
async def plan_game_create(
    request: Request,
    user: User = Depends(require_user),
    pitch_id: int = Form(...),
    planned_date: str = Form(...),
    planned_time: str = Form("10:00"),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    form_data = await request.form()
    dt = datetime.strptime(f"{planned_date} {planned_time}", "%Y-%m-%d %H:%M")

    game = PlannedGame(
        pitch_id=pitch_id,
        created_by_id=user.id,
        planned_date=dt,
        note=note if note else None,
    )
    db.add(game)
    db.flush()

    # Collect emails from friends checkboxes and manual email inputs
    emails = set()
    friend_ids = form_data.getlist("friend_ids")
    for fid in friend_ids:
        friend = db.query(User).filter(User.id == int(fid)).first()
        if friend and friend.email:
            inv = GameInvitation(planned_game_id=game.id, email=friend.email, user_id=friend.id)
            db.add(inv)
            emails.add(friend.email)
        elif friend:
            inv = GameInvitation(planned_game_id=game.id, email="", user_id=friend.id)
            db.add(inv)

    # Manual emails
    manual_emails = form_data.get("emails", "")
    for email in manual_emails.split(","):
        email = email.strip()
        if email and email not in emails:
            # Check if email belongs to an existing user
            existing_user = db.query(User).filter(User.email == email).first()
            inv = GameInvitation(
                planned_game_id=game.id,
                email=email,
                user_id=existing_user.id if existing_user else None,
            )
            db.add(inv)
            emails.add(email)

    db.commit()

    # Load pitch for email sending
    game = db.query(PlannedGame).options(joinedload(PlannedGame.pitch), joinedload(PlannedGame.invitations)).filter(PlannedGame.id == game.id).first()

    # Send emails
    sent_count = 0
    for inv in game.invitations:
        if inv.email:
            success = send_invitation_email(inv.email, game, user.display_name)
            if success:
                inv.sent = True
                sent_count += 1
    db.commit()

    return RedirectResponse(url=f"/games/{game.id}", status_code=303)


@app.get("/games/{game_id}", response_class=HTMLResponse)
async def planned_game_detail(request: Request, game_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    game = (
        db.query(PlannedGame)
        .options(
            joinedload(PlannedGame.pitch).joinedload(Pitch.holes),
            joinedload(PlannedGame.creator),
            joinedload(PlannedGame.invitations).joinedload(GameInvitation.user),
        )
        .filter(PlannedGame.id == game_id)
        .first()
    )
    if not game:
        raise HTTPException(status_code=404, detail="Plánovaná hra nenájdená")

    gcal_url = build_gcal_url(
        f"Footgolf - {game.pitch.name}",
        game.pitch.location or "",
        game.planned_date,
        f"Organizuje: {game.creator.display_name}\n{game.note or ''}",
    )

    return templates.TemplateResponse(
        request, "game_detail.html", {"user": user, "game": game, "gcal_url": gcal_url}
    )


@app.post("/games/{game_id}/start")
async def planned_game_start(
    request: Request,
    game_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    game = db.query(PlannedGame).filter(PlannedGame.id == game_id).first()
    if not game or game.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca môže začať hru.")

    # Create a real session from the planned game
    invite_code = generate_invite_code()
    while db.query(GameSession).filter(GameSession.invite_code == invite_code).first():
        invite_code = generate_invite_code()

    session = GameSession(pitch_id=game.pitch_id, created_by_id=user.id, invite_code=invite_code)
    db.add(session)
    db.flush()

    player = SessionPlayer(session_id=session.id, user_id=user.id)
    db.add(player)

    game.status = "started"
    game.session_id = session.id
    db.commit()

    return RedirectResponse(url=f"/sessions/{session.id}", status_code=303)


@app.post("/games/{game_id}/cancel")
async def planned_game_cancel(
    request: Request,
    game_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    game = db.query(PlannedGame).filter(PlannedGame.id == game_id).first()
    if not game or game.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Iba tvorca môže zrušiť hru.")

    game.status = "cancelled"
    db.commit()
    return RedirectResponse(url="/", status_code=303)


# --- Leaderboard ---
@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitch_id = request.query_params.get("pitch_id")

    pitches = db.query(Pitch).options(joinedload(Pitch.holes)).order_by(Pitch.name).all()

    rankings = []
    selected_pitch = None

    if pitch_id:
        selected_pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == int(pitch_id)).first()
        if selected_pitch:
            total_par = sum(h.par for h in selected_pitch.holes)
            num_holes = len(selected_pitch.holes)

            # Get all completed sessions on this pitch
            sessions = (
                db.query(GameSession)
                .filter(GameSession.pitch_id == int(pitch_id), GameSession.status == "completed")
                .options(joinedload(GameSession.scores), joinedload(GameSession.players).joinedload(SessionPlayer.user))
                .all()
            )

            # Aggregate per player
            player_data = {}
            for s in sessions:
                for sp in s.players:
                    uid = sp.user_id
                    user_scores = [sc for sc in s.scores if sc.user_id == uid]
                    if len(user_scores) == num_holes:
                        total = sum(sc.strokes for sc in user_scores)
                        if uid not in player_data:
                            player_data[uid] = {"user": sp.user, "totals": [], "best": total}
                        player_data[uid]["totals"].append(total)
                        if total < player_data[uid]["best"]:
                            player_data[uid]["best"] = total

            for uid, data in player_data.items():
                data["games"] = len(data["totals"])
                data["avg"] = round(sum(data["totals"]) / data["games"], 1)
                data["avg_diff"] = round(data["avg"] - total_par, 1)
                data["best_diff"] = data["best"] - total_par

            rankings = sorted(player_data.values(), key=lambda x: x["avg"])

    return templates.TemplateResponse(
        request,
        "leaderboard.html",
        {"user": user, "pitches": pitches, "selected_pitch": selected_pitch, "rankings": rankings, "selected_pitch_id": pitch_id},
    )


# --- History ---
@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitch_id = request.query_params.get("pitch_id")
    query = (
        db.query(GameSession)
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id, GameSession.status == "completed")
        .options(joinedload(GameSession.pitch).joinedload(Pitch.holes), joinedload(GameSession.scores))
    )

    if pitch_id:
        query = query.filter(GameSession.pitch_id == int(pitch_id))

    sessions = query.order_by(GameSession.started_at.desc()).all()

    pitches = (
        db.query(Pitch)
        .join(GameSession)
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id)
        .distinct()
        .all()
    )

    session_data = []
    for s in sessions:
        user_scores = [sc for sc in s.scores if sc.user_id == user.id]
        total = sum(sc.strokes for sc in user_scores)
        total_par = sum(h.par for h in s.pitch.holes)
        diff = total - total_par
        session_data.append({"session": s, "total": total, "total_par": total_par, "diff": diff})

    return templates.TemplateResponse(
        request,
        "history.html",
        {"user": user, "session_data": session_data, "pitches": pitches, "selected_pitch_id": pitch_id},
    )


# --- Stats ---
@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pitch_id = request.query_params.get("pitch_id")

    pitches = (
        db.query(Pitch)
        .join(GameSession)
        .join(SessionPlayer)
        .filter(SessionPlayer.user_id == user.id)
        .distinct()
        .all()
    )

    stats_data = None
    selected_pitch = None

    if pitch_id:
        selected_pitch = db.query(Pitch).options(joinedload(Pitch.holes)).filter(Pitch.id == int(pitch_id)).first()

        sessions = (
            db.query(GameSession)
            .join(SessionPlayer)
            .filter(
                SessionPlayer.user_id == user.id,
                GameSession.pitch_id == int(pitch_id),
                GameSession.status == "completed",
            )
            .options(joinedload(GameSession.scores))
            .order_by(GameSession.started_at.asc())
            .all()
        )

        if sessions:
            total_par = sum(h.par for h in selected_pitch.holes)

            session_scores = []
            for s in sessions:
                user_scores = {sc.hole_number: sc.strokes for sc in s.scores if sc.user_id == user.id}
                total = sum(user_scores.values())
                session_scores.append({
                    "session_id": s.id,
                    "date": s.started_at.strftime("%d.%m.%Y"),
                    "total": total,
                    "diff": total - total_par,
                    "hole_scores": user_scores,
                })

            hole_stats = []
            for hole in selected_pitch.holes:
                h_scores = [ss["hole_scores"].get(hole.hole_number) for ss in session_scores if hole.hole_number in ss["hole_scores"]]
                if h_scores:
                    hole_stats.append({
                        "number": hole.hole_number,
                        "par": hole.par,
                        "avg": round(sum(h_scores) / len(h_scores), 1),
                        "best": min(h_scores),
                        "worst": max(h_scores),
                    })

            best = min(session_scores, key=lambda x: x["total"])
            avg_total = round(sum(ss["total"] for ss in session_scores) / len(session_scores), 1)

            improvement = None
            if len(session_scores) >= 4:
                first3 = sum(ss["total"] for ss in session_scores[:3]) / 3
                last3 = sum(ss["total"] for ss in session_scores[-3:]) / 3
                improvement = round(last3 - first3, 1)

            stats_data = {
                "total_par": total_par,
                "session_scores": session_scores,
                "hole_stats": hole_stats,
                "best": best,
                "avg_total": avg_total,
                "improvement": improvement,
                "session_count": len(session_scores),
            }

    return templates.TemplateResponse(
        request,
        "stats.html",
        {"user": user, "pitches": pitches, "selected_pitch": selected_pitch, "stats_data": stats_data, "selected_pitch_id": pitch_id},
    )


# --- Admin helpers ---
def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Prístup len pre administrátorov.")
    return user


MONTH_NAMES_SK = {
    1: "Január", 2: "Február", 3: "Marec", 4: "Apríl",
    5: "Máj", 6: "Jún", 7: "Júl", 8: "August",
    9: "September", 10: "Október", 11: "November", 12: "December",
}


# --- Admin Dashboard ---
@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from datetime import timedelta

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    # Key metrics
    total_users = db.query(User).count()
    new_users_month = db.query(User).filter(User.created_at >= month_start).count()
    active_players = (
        db.query(func.count(func.distinct(SessionPlayer.user_id)))
        .join(GameSession)
        .filter(GameSession.started_at >= thirty_days_ago)
        .scalar()
    )
    total_sessions = db.query(GameSession).count()
    active_sessions = db.query(GameSession).filter(GameSession.status == "active").count()
    completed_month = db.query(GameSession).filter(
        GameSession.status == "completed", GameSession.completed_at >= month_start
    ).count()
    total_pitches = db.query(Pitch).count()
    total_scores = db.query(Score).count()

    # Planned games
    planned_games_upcoming = db.query(PlannedGame).filter(
        PlannedGame.status == "planned", PlannedGame.planned_date >= now
    ).count()
    planned_games_total = db.query(PlannedGame).count()

    # Most popular pitches (by completed game count)
    popular_pitches_raw = (
        db.query(Pitch.id, Pitch.name, Pitch.location, func.count(GameSession.id).label("game_count"))
        .outerjoin(GameSession, (GameSession.pitch_id == Pitch.id) & (GameSession.status == "completed"))
        .group_by(Pitch.id)
        .order_by(func.count(GameSession.id).desc())
        .limit(5)
        .all()
    )
    popular_pitches = [
        {"name": p.name, "location": p.location, "game_count": p.game_count}
        for p in popular_pitches_raw
    ]

    # Recent games
    recent_games_raw = (
        db.query(GameSession)
        .options(joinedload(GameSession.pitch), joinedload(GameSession.creator), joinedload(GameSession.players))
        .order_by(GameSession.started_at.desc())
        .limit(10)
        .all()
    )
    recent_games = [
        {
            "id": g.id,
            "pitch_name": g.pitch.name,
            "date": g.started_at.strftime("%d.%m.%Y %H:%M"),
            "status": g.status,
            "player_count": len(g.players),
            "creator": g.creator.display_name if g.creator else "",
        }
        for g in recent_games_raw
    ]

    # Upcoming planned games
    upcoming_planned_raw = (
        db.query(PlannedGame)
        .filter(PlannedGame.status == "planned", PlannedGame.planned_date >= now)
        .options(joinedload(PlannedGame.pitch), joinedload(PlannedGame.creator), joinedload(PlannedGame.invitations))
        .order_by(PlannedGame.planned_date.asc())
        .limit(5)
        .all()
    )
    upcoming_planned = [
        {
            "id": g.id,
            "pitch_name": g.pitch.name,
            "date": g.planned_date.strftime("%d.%m.%Y o %H:%M"),
            "creator": g.creator.display_name,
            "invite_count": len(g.invitations),
        }
        for g in upcoming_planned_raw
    ]

    stats = {
        "total_users": total_users,
        "new_users_month": new_users_month,
        "active_players": active_players,
        "total_sessions": total_sessions,
        "active_sessions": active_sessions,
        "completed_month": completed_month,
        "total_pitches": total_pitches,
        "total_scores": total_scores,
        "planned_games_upcoming": planned_games_upcoming,
        "planned_games_total": planned_games_total,
        "month_name": MONTH_NAMES_SK.get(now.month, ""),
        "popular_pitches": popular_pitches,
        "recent_games": recent_games,
        "upcoming_planned": upcoming_planned,
    }

    return templates.TemplateResponse(request, "admin_dashboard.html", {"user": user, "stats": stats})


# --- SQLAdmin ---
class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = form.get("username")
        password = form.get("password")
        db = next(get_db())
        user = db.query(User).filter(User.username == username).first()
        if user and verify_password(password, user.password_hash) and user.is_admin:
            request.session.update({"admin_user_id": user.id})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        user_id = request.session.get("admin_user_id")
        if not user_id:
            return False
        db = next(get_db())
        user = db.query(User).filter(User.id == user_id, User.is_admin == True).first()
        return user is not None


admin_auth = AdminAuth(secret_key=config.SECRET_KEY)
admin = Admin(
    app,
    engine,
    authentication_backend=admin_auth,
    title="Footgolfisti Admin",
    templates_dir="templates",
)


class UserAdmin(ModelView, model=User):
    column_list = [User.id, User.username, User.display_name, User.email, User.is_admin, User.created_at]
    column_searchable_list = [User.username, User.display_name, User.email]
    column_sortable_list = [User.id, User.username, User.display_name, User.created_at]
    form_excluded_columns = [User.password_hash, User.created_pitches, User.created_sessions, User.session_players, User.scores, User.planned_games]
    name = "Používateľ"
    name_plural = "Používatelia"
    icon = "fa-solid fa-users"


class PitchAdmin(ModelView, model=Pitch):
    column_list = [Pitch.id, Pitch.name, Pitch.location, Pitch.num_holes, Pitch.created_at]
    column_searchable_list = [Pitch.name, Pitch.location]
    column_sortable_list = [Pitch.id, Pitch.name, Pitch.num_holes, Pitch.created_at]
    name = "Ihrisko"
    name_plural = "Ihriská"
    icon = "fa-solid fa-flag"


class GameSessionAdmin(ModelView, model=GameSession):
    column_list = [GameSession.id, GameSession.pitch, GameSession.creator, GameSession.invite_code, GameSession.status, GameSession.started_at, GameSession.completed_at]
    column_sortable_list = [GameSession.id, GameSession.status, GameSession.started_at]
    name = "Hra"
    name_plural = "Hry"
    icon = "fa-solid fa-gamepad"


class ScoreAdmin(ModelView, model=Score):
    column_list = [Score.id, Score.session_id, Score.user, Score.hole_number, Score.strokes]
    column_sortable_list = [Score.id, Score.session_id, Score.hole_number, Score.strokes]
    name = "Skóre"
    name_plural = "Skóre"
    icon = "fa-solid fa-star"


class PlannedGameAdmin(ModelView, model=PlannedGame):
    column_list = [PlannedGame.id, PlannedGame.pitch, PlannedGame.creator, PlannedGame.planned_date, PlannedGame.status]
    column_sortable_list = [PlannedGame.id, PlannedGame.planned_date, PlannedGame.status]
    name = "Plánovaná hra"
    name_plural = "Plánované hry"
    icon = "fa-solid fa-calendar"


class GameInvitationAdmin(ModelView, model=GameInvitation):
    column_list = [GameInvitation.id, GameInvitation.planned_game, GameInvitation.email, GameInvitation.user, GameInvitation.status, GameInvitation.sent]
    name = "Pozvánka"
    name_plural = "Pozvánky"
    icon = "fa-solid fa-envelope"


class InviteCodeAdmin(ModelView, model=InviteCode):
    column_list = [InviteCode.id, InviteCode.code, InviteCode.owner, InviteCode.used_by, InviteCode.created_at, InviteCode.used_at]
    column_searchable_list = [InviteCode.code]
    name = "Kód pozvánky"
    name_plural = "Kódy pozvánok"
    icon = "fa-solid fa-ticket"


admin.add_view(UserAdmin)
admin.add_view(PitchAdmin)
admin.add_view(GameSessionAdmin)
admin.add_view(ScoreAdmin)
admin.add_view(PlannedGameAdmin)
admin.add_view(GameInvitationAdmin)
admin.add_view(InviteCodeAdmin)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
