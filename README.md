# dazzy_api

DAZZY Django 5.2 API。

```bash
uv sync
cp .env.example .env
uv run --env-file .env python manage.py migrate
uv run --env-file .env python manage.py runserver
```

存活检查：`GET /api/v1/health/`；就绪检查：`GET /api/v1/health/ready/`；OpenAPI：`GET /api/schema/`。
