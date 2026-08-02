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
| GET    | `/api/health/`          | Health check             |

Send JWT as `Authorization: Bearer <token>`. Entries are owner-scoped; search with `?search=...` and paginate with `?page=1`.

## Run with Docker (production-style)

```bash
cp .env.example .env    # set SECRET_KEY, DEBUG=False, ALLOWED_HOSTS
docker compose up --build
```
