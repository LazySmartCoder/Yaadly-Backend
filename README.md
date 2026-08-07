# Yaadly Backend

Production-grade Django REST Framework API for Yaadly (journaling app).

## Stack

- Django 6 + Django REST Framework + SimpleJWT auth
- PostgreSQL (SQLite fallback for local dev)
- Gunicorn + WhiteNoise (static)
- Docker Compose for production

## Local setup

```bash
cd Backend
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # then edit SECRET_KEY, etc.
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

## API

| Method | Endpoint                | Description              |
|--------|-------------------------|--------------------------|
| POST   | `/api/auth/token/`      | Login, returns JWT pair  |
| POST   | `/api/auth/token/refresh/` | Refresh access token  |
| GET    | `/api/entries/`         | List own journal entries |
| POST   | `/api/entries/`         | Create an entry          |
| GET    | `/api/entries/{id}/`    | Retrieve one entry       |
| PUT/PATCH | `/api/entries/{id}/`  | Update an entry          |
| DELETE | `/api/entries/{id}/`    | Delete an entry          |
| POST   | `/api/push/token/`      | Register the device's FCM token |
| GET    | `/api/health/`          | Health check             |

Send JWT as `Authorization: Bearer <token>`. Entries are owner-scoped; search with `?search=...` and paginate with `?page=1`.

## Automated daily push notifications

Three FCM pushes go out to every user each day, in batches, from the backend.
All copy is generated per user with Gemini **gemini-2.5-flash-lite** and every
body is exactly two lines.

| Time (server `TIME_ZONE`) | Slot        | Message |
|---------------------------|-------------|---------|
| 09:00                     | `morning`   | "Everyone believes in you", echoed from a moment in the user's past journal entries |
| 13:00                     | `afternoon` | A simple random memory drawn from an entry in the last 10 days |
| 22:00                     | `evening`   | Invitation to jot down the day, plus a good thing from their past entries |

### Setup

1. Add a Firebase service-account JSON to the server (e.g.
   `/root/yaadly/firebase-service-account.json`) and set it in `.env` via
   `FCM_SERVICE_ACCOUNT_PATH` (or paste the JSON into
   `FCM_SERVICE_ACCOUNT_JSON`), plus `FCM_PROJECT_ID`.
2. Make sure `TIME_ZONE` is set to the timezone you want the slots to fire in.
3. Run the scheduler (Docker) or the cron jobs (below).

### Sending

The `send_daily_notifications` command processes users in batches and never
sends the same slot twice per user per day (tracked by `PushLog`):

```bash
python manage.py send_daily_notifications                     # auto-pick slot by hour
python manage.py send_daily_notifications --slot morning      # 9 AM
python manage.py send_daily_notifications --slot afternoon    # 1 PM
python manage.py send_daily_notifications --slot evening      # 10 PM
python manage.py send_daily_notifications --slot morning --batch-size 100 --dry-run
```

### Scheduling

**Option A — scheduler daemon (recommended, used by `docker compose`):**

```bash
python manage.py push_scheduler
```

Runs forever and fires each slot at its configured local time
(`FCM_MORNING_AT`/`FCM_AFTERNOON_AT`/`FCM_EVENING_AT`, default 09:00/13:00/22:00).
With Docker this is the `scheduler` service in `docker-compose.yml`.

**Option B — cron:**

```cron
0 9  * * * cd /path/to/Backend && .venv/bin/python manage.py send_daily_notifications --slot morning
0 13 * * * cd /path/to/Backend && .venv/bin/python manage.py send_daily_notifications --slot afternoon
0 22 * * * cd /path/to/Backend && .venv/bin/python manage.py send_daily_notifications --slot evening
```

Both are safe to run repeatedly — `PushLog` dedupes per user/slot/day.

## Run with Docker (production-style)

```bash
cp .env.example .env    # set SECRET_KEY, DEBUG=False, ALLOWED_HOSTS
docker compose up --build
```
