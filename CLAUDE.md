# Footgolfisti ⚽🏌️

Mobile-first footgolf scoring web app for tracking scores, comparing performance, and planning games with friends.

## Tech Stack

- **Backend:** Python 3.14 + FastAPI
- **Database:** SQLite (file: `footgolfisti.db`)
- **Frontend:** Jinja2 templates + Tailwind CSS (CDN) + Alpine.js (CDN) + Chart.js (CDN)
- **Auth:** Cookie-based sessions with signed tokens (itsdangerous) + bcrypt password hashing
- **Admin:** SQLAdmin panel at `/admin`, custom dashboard at `/admin-panel`
- **Language:** Slovak UI, golf terms in English

## Project Structure

```
app.py          # Main FastAPI app with all routes + SQLAdmin
models.py       # SQLAlchemy models (User, Pitch, PitchHole, Session, SessionPlayer, Score, PlannedGame, GameInvitation, InviteCode)
database.py     # SQLite database setup
config.py       # Configuration via .env (python-dotenv)
.env            # Environment variables (not committed to git)
.env.example    # Template for .env
DEPLOY.md       # Production deployment guide
templates/      # Jinja2 HTML templates
static/         # CSS, PWA manifest, service worker, icons
```

## Running Locally

```bash
cp .env.example .env    # Then edit with your values
pip install -r requirements.txt
python app.py
# Open http://localhost:8000
# Admin dashboard at http://localhost:8000/admin-panel
# SQLAdmin CRUD at http://localhost:8000/admin
```

## Key Concepts

- **Pitch** = footgolf course (9 or 18 holes with par per hole, unique by Google Maps place_id)
- **Session** = one game played on a pitch, has invite code for multiplayer
- **PlannedGame** = future game with date, email invitations, Google Calendar link
- **InviteCode** = registration invite (3 per user, invite-only registration)
- **Session statuses:** active → completed
- **PlannedGame statuses:** planned → started/cancelled

## Configuration

All config is loaded from `.env` file via `python-dotenv`. See `.env.example` for all available variables:
- `SECRET_KEY` - token signing (required, use `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`)
- `GOOGLE_MAPS_API_KEY` - Places Autocomplete on pitch creation
- `SMTP_*` - Gmail SMTP for email invitations
- `APP_URL` - base URL for email links
- `SECURE_COOKIES` - set to `true` in production (requires HTTPS)

## Security Features

- **Invite-only registration** - new users need an invite code, each user gets 3 invites
- **bcrypt** password hashing (auto-upgrades legacy SHA256 hashes on login)
- **CSRF protection** on all POST forms (cookie + hidden field, auto-injected via JavaScript)
- **Rate limiting** on login (5 attempts/5min) and forgot-password (3 attempts/5min)
- **Secure cookies** - `secure=True` flag when `SECURE_COOKIES=true` (for HTTPS)
- **Admin panel** protected by `is_admin` flag on User model
- **First user** is automatically admin and can register without invite code

## Admin Panel

- Custom dashboard at `/admin-panel` with stats, popular pitches, recent activity
- SQLAdmin CRUD at `/admin` - only users with `is_admin=True` can access
- First registered user is automatically admin
- To manually make a user admin: `sqlite3 footgolfisti.db "UPDATE users SET is_admin=1 WHERE username='yourname';"`

## Development Notes

- Templates use Starlette 1.0 TemplateResponse signature: `TemplateResponse(request, name, context)`
- No build step needed - all frontend via CDN
- Database auto-creates on first run via `Base.metadata.create_all()`
- Delete `footgolfisti.db` to reset all data
- PWA with service worker supports offline scorecard entry (queues and replays on reconnect)
- CSRF tokens are auto-injected into all `<form method="post">` elements via `base.html` JavaScript
- Duplicate pitches prevented by unique Google Maps `place_id` constraint
- See `DEPLOY.md` for production deployment instructions
