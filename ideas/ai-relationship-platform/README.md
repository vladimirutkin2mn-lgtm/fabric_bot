# HeartSignal — Milestone 0

Рабочий каркас Telegram-first приложения: FastAPI, aiogram 3, async SQLAlchemy,
PostgreSQL и Alembic. Анализ переписок, платежи и продуктовые Telegram-сценарии намеренно
не входят в этот milestone.

## Требования

- Python 3.12
- Docker и Docker Compose (для полного локального запуска)

## Запуск через Docker Compose

```bash
cp .env.example .env
# Укажите настоящий TELEGRAM_BOT_TOKEN для запуска bot.
docker compose up --build
```

API доступен на `http://localhost:8000`. Проверки состояния:

```bash
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
```

Пустой `TELEGRAM_WEBHOOK_URL` включает long polling. Если URL задан, bot регистрирует
webhook с `TELEGRAM_WEBHOOK_SECRET`; HTTP transport webhook будет подключен на milestone 8.

## Локальная разработка

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Команды API, bot и миграций:

```bash
uvicorn app.api.main:app --reload
python -m app.bot.main
alembic upgrade head
alembic revision --autogenerate -m "describe change"
```

## Проверки качества

```bash
ruff format --check .
ruff check .
mypy app tests
pytest
docker compose config --quiet
```

Настройки читаются из окружения; полный перечень и безопасные шаблонные значения находятся
в `.env.example`. Секреты и `.env` не следует коммитить.
