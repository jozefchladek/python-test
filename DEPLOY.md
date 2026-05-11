# Deployment Guide - Footgolfisti

## Server Requirements

- Python 3.11+
- nginx (reverse proxy + HTTPS)
- certbot (Let's Encrypt SSL)

## Initial Setup

### 1. Clone the repo

```bash
cd /var/www
git clone https://github.com/tonydubravec/footgolfisti.git
cd footgolfisti
```

### 2. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
nano .env
```

Set these values in `.env`:

```
SECRET_KEY=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))">
GOOGLE_MAPS_API_KEY=<your new API key>
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=<your email>
SMTP_PASSWORD=<your app password>
APP_URL=https://yourdomain.com
SECURE_COOKIES=true
```

**IMPORTANT:** Generate a new SECRET_KEY and rotate your Google Maps API key and Gmail app password before going live.

### 4. Create systemd service

```bash
sudo nano /etc/systemd/system/footgolfisti.service
```

```ini
[Unit]
Description=Footgolfisti
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/footgolfisti
ExecStart=/var/www/footgolfisti/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
Environment=PATH=/var/www/footgolfisti/venv/bin

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable footgolfisti
sudo systemctl start footgolfisti
```

### 5. Configure nginx

```bash
sudo nano /etc/nginx/sites-available/footgolfisti
```

```nginx
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static/ {
        alias /var/www/footgolfisti/static/;
        expires 7d;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/footgolfisti /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 6. Enable HTTPS

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

### 7. Make yourself admin

```bash
cd /var/www/footgolfisti
sqlite3 footgolfisti.db "UPDATE users SET is_admin=1 WHERE username='yourusername';"
```

## Deploying Updates

```bash
cd /var/www/footgolfisti
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart footgolfisti
```

### Database schema changes

Since we don't use Alembic yet, apply schema changes manually:

```bash
# Example: adding a new column
sqlite3 footgolfisti.db "ALTER TABLE users ADD COLUMN new_field TEXT;"
```

**Never delete the database on the live server!**

## Monitoring

```bash
# Check status
sudo systemctl status footgolfisti

# View logs
sudo journalctl -u footgolfisti -f

# Check nginx errors
sudo tail -f /var/log/nginx/error.log
```
