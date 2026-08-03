# HeartSignal — Milestone 2

Telegram-first приложение с сохраняемым в PostgreSQL онбордингом: подтверждение 18+,
версионированное согласие и базовое меню. Незавершённый сценарий восстанавливается из записи
пользователя после перезапуска, а уникальный Telegram ID и атомарный upsert защищают от дублей.
После согласия пользователь может создать или восстановить черновик разбора, отправить
текстовую переписку двух людей, выбрать себя, цель и необязательную стадию отношений.
Черновик и текущий шаг сохраняются в PostgreSQL и восстанавливаются после перезапуска.

Поддерживаются строки `Анна: сообщение`, `[12.07.2026 18:45] Анна: сообщение`, время без
даты `[18:45]`, а также скопированный Telegram-текст с заголовком
`Анна, [12.07.2026 18:45]` и многострочным сообщением. Исходный порядок, пунктуация,
эмодзи и переносы внутри сообщения сохраняются; идентификаторы имеют вид `m1`, `m2`.

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

Только parser и PostgreSQL-интеграция:

```bash
pytest tests/test_conversation_parser.py
TEST_DATABASE_URL=postgresql+asyncpg://heartsignal:heartsignal@localhost:5432/heartsignal pytest -m postgres
```

Лимиты настраиваются через `CONVERSATION_MIN_MESSAGES` (4),
`CONVERSATION_MAX_CHARACTERS` (30000), `CONVERSATION_MAX_PARTICIPANTS` (2) и
`ANALYSIS_GOAL_MAX_CHARACTERS` (500). Пустой, односторонний, слишком короткий,
слишком большой или нераспознанный текст отклоняется без сохранения как успешного.

Интеграционные тесты репозитория используют настоящий PostgreSQL и тот же async-драйвер,
что production-код:

```bash
docker compose up -d postgres
TEST_DATABASE_URL=postgresql+asyncpg://heartsignal:heartsignal@localhost:5432/heartsignal pytest -m postgres
```

Без `TEST_DATABASE_URL` PostgreSQL-тесты пропускаются. В CI переменная указывает на
отдельный service container, поэтому полный запуск `pytest` всегда выполняет эти тесты.

Настройки читаются из окружения; полный перечень и безопасные шаблонные значения находятся
в `.env.example`. Секреты и `.env` не следует коммитить.

## Ручная проверка онбординга

1. Создайте бота через BotFather и запишите токен в `TELEGRAM_BOT_TOKEN`.
2. Запустите PostgreSQL, примените `alembic upgrade head`, затем запустите bot.
3. Отправьте `/start`, подтвердите возраст и примите условия версии 1.0.
4. Нажмите «Разобрать переписку» и отправьте вымышленный пример из четырёх реплик:
   `Анна: Привет!`, `Иван: Привет!`, `Анна: Как день?`, `Иван: Отлично!`.
5. Выберите участника, быстрый или собственный вопрос и стадию отношений либо
   «Пропустить»; убедитесь, что бот честно сообщает о готовом черновике.
6. На любом шаге проверьте «Отменить», затем начните снова. Перезапустите bot посередине
   сценария и проверьте восстановление через `/start` и кнопку меню.
5. Повторите `/start`: завершённый пользователь сразу увидит главное меню.

Для локального long polling нужны `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
`CONTENT_ENCRYPTION_KEY`, `APP_ENV` и `LOG_LEVEL`. `TELEGRAM_WEBHOOK_URL` оставьте пустым;
остальные переменные имеют безопасные значения-заглушки в `.env.example`.

Текущий milestone принимает только одно текстовое Telegram-сообщение. Изображения, OCR,
голос, сбор нескольких обновлений, LLM-анализ, отчёты, кредиты и платежи не реализованы.
