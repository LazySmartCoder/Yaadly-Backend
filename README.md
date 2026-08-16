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
| POST   | `/api/push/token/deactivate/` | Deactivate the device's FCM token (on logout) |
| GET    | `/api/health/`          | Health check             |

Send JWT as `Authorization: Bearer <token>`. Entries are owner-scoped; search with `?search=...` and paginate with `?page=1`.

## Firebase Cloud Messaging (FCM)

The app registers each device's FCM token with the backend (`POST
/api/push/token/`, and `POST /api/push/token/deactivate/` on logout), so
push notifications can be delivered to any registered device — including
directly from the Firebase console (Cloud Messaging → Send test message).

Sends are best-effort with fault tolerance built in (`apps/push/services.py`):

- Transient FCM failures (HTTP 429/5xx) are retried with exponential backoff.
- Unregistered/revoked tokens are auto-deactivated so they are never retried.
- Background/terminated Android notifications land on the high-importance
  `yaadly_messages` channel (`FCM_CHANNEL_ID`), matching the channel the app
  creates, so they arrive with sound.

### Setup

Add a Firebase service-account JSON to the server (e.g.
`/root/yaadly/firebase-service-account.json`) and set it in `.env` via
`FCM_SERVICE_ACCOUNT_PATH` (or paste the JSON into `FCM_SERVICE_ACCOUNT_JSON`),
plus `FCM_PROJECT_ID`. Under `docker compose` the file is mounted into the
containers automatically (`docker-compose.yml`), so the host path in `.env`
stays correct.

## Run with Docker (production-style)

```bash
cp .env.example .env    # set SECRET_KEY, DEBUG=False, ALLOWED_HOSTS
docker compose up --build
```
