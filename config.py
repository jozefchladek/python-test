import os
import secrets

from dotenv import load_dotenv

load_dotenv()

# --- App ---
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    print("WARNING: SECRET_KEY not set - generated a temporary one. Set SECRET_KEY in environment variables for production.")

# --- Google Maps ---
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

# --- Email (SMTP) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Footgolfisti")

# --- App URL (for email links) ---
APP_URL = os.environ.get("APP_URL", "http://localhost:8000")

# --- Security ---
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "false").lower() in ("true", "1", "yes")
