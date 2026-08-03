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

## Milestone 3: LLM-анализ

Ядро теперь атомарно переводит завершённый черновик `draft → processing → completed`
или `failed`, получает строгий JSON Schema из Pydantic-модели, проверяет ссылки на сообщения
и сохраняет только полностью валидный результат. Провайдеры изолированы интерфейсом
`LLMClient`: локально и в CI используется детерминированный `stub`; первый рекомендуемый
production-вариант — `LLM_PROVIDER=openai` и настраиваемый `LLM_MODEL=gpt-5.4-mini`.
Реальные запросы OpenAI оплачиваются отдельно по тарифам API.

Настройки: `LLM_TIMEOUT_SECONDS=45`, `LLM_MAX_TRANSPORT_ATTEMPTS=2` (1–5),
`LLM_MAX_REPAIR_ATTEMPTS=1` (0–1), `LLM_PROMPT_VERSION=analysis_v1`. Для OpenAI задайте
`OPENAI_API_KEY`; stub ключа не требует. Транспортные/server-сбои повторяются в заданном
пределе, ошибки авторизации и неверного запроса — нет. Невалидный результат получает не
более одной коррекции; ни первый невалидный ответ, ни prompts не сохраняются.
Если исправленный ответ снова невалиден, категория второго ответа определяет итоговый
failure code: семантические ошибки ссылок дают `invalid_evidence_refs`, остальные ошибки —
`invalid_model_output`. Перед OpenAI Pydantic JSON Schema детерминированно приводится к
официальному strict-подмножеству (все object-поля обязательны, unsupported validation
keywords удалены), а полный строгий Pydantic-контракт повторно проверяется после ответа.

После запуска PostgreSQL и `alembic upgrade head` безопасная демонстрация на вымышленных
данных выполняется командой:

```bash
python -m app.cli.demo_analysis
```

Опциональная ручная smoke-проверка OpenAI (никогда не запускается в CI) использует только
вымышленный диалог и печатает только безопасные метаданные:

```bash
LLM_PROVIDER=openai LLM_MODEL=gpt-5.4-mini OPENAI_API_KEY=... \
  python -m app.cli.smoke_openai
```

Миграцию можно проверить цепочкой:

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

В БД сохраняются результат, provider/model/prompt version, число LLM-попыток, токены,
latency, безопасный request ID и временные метки. Сообщения, цели, prompts, невалидные
ответы, ключи и stack traces не попадают в логи, аналитику или failure metadata.

Telegram-рендеринг отчёта пока не реализован и текущий bot не вызывает платную модель
автоматически. Также не реализованы кредиты, платежи, Grok/xAI и OpenRouter; xAI остаётся
возможным будущим адаптером. Изображения, OCR, голос и фоновые workers остаются вне scope.
