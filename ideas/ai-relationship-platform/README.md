# HeartSignal — Milestone 1

Telegram-first приложение с сохраняемым в PostgreSQL онбордингом: подтверждение 18+,
версионированное согласие и базовое меню. Незавершённый сценарий восстанавливается из записи
пользователя после перезапуска, а уникальный Telegram ID и атомарный upsert защищают от дублей.
Анализ переписок, история, кредиты, платежи и LLM намеренно не входят в этот milestone.

## Требования

- Python 3.12
- Docker и Docker Compose (для полного локального запуска)

## Запуск через Docker Compose

```bash
cp .env.example .env
# Укажите настоящий TELEGRAM_BOT_TOKEN для запуска bot.
docker compose up --build
```

Перед первым запуском примените миграцию (при уже запущенной БД):

```bash
docker compose run --rm api alembic upgrade head
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

## Ручная проверка онбординга

1. Создайте бота через BotFather и запишите токен в `TELEGRAM_BOT_TOKEN`.
2. Запустите PostgreSQL, примените `alembic upgrade head`, затем запустите bot.
3. Отправьте `/start`, подтвердите возраст и примите условия версии 1.0.
4. Проверьте четыре пункта меню: пока каждый реализованный переход в раздел сообщает о
   следующем этапе.
5. Повторите `/start`: завершённый пользователь сразу увидит главное меню.

Для локального long polling нужны `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
`CONTENT_ENCRYPTION_KEY`, `APP_ENV` и `LOG_LEVEL`. `TELEGRAM_WEBHOOK_URL` оставьте пустым;
остальные переменные имеют безопасные значения-заглушки в `.env.example`.
