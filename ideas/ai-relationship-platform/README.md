# HeartSignal — Milestone 2

Privacy architecture, deletion semantics, backfill rollout, and retention scheduling are documented
in [`docs/privacy-deletion-retention.md`](docs/privacy-deletion-retention.md).

Analytics event semantics, correlation IDs, privacy rules and admin metrics are documented in
[`docs/analytics-admin-observability.md`](docs/analytics-admin-observability.md).

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

Telegram-рендеринг и синхронный запуск анализа подключены в Milestone 4. Не реализованы
кредиты, платежи, Grok/xAI и OpenRouter; xAI остаётся возможным будущим адаптером.
Изображения, OCR, голос и фоновые workers остаются вне scope.

## Milestone 4: отчёты и доставка

После выбора стадии отношений бот сохраняет ввод, очищает FSM, сразу показывает сообщение о
обработке и синхронно запускает анализ. Структурированный результат превращается в русский
отчёт: «Общий вывод», «Что видно в переписке», «Куда движется общение», «Наблюдаемая
взаимность», «Возможные объяснения», «Чего нельзя понять по этой переписке», «Что делать
дальше» и «Варианты ответа». При риске или недостатке данных перед ними появляется честное
предупреждение. Уверенность 0.00–0.39 показывается как низкая, 0.40–0.69 как средняя, а
0.70–1.00 как высокая. Текст делится около 3700 символов с жёстким пределом Telegram 4096;
кнопки действий получает только последний фрагмент.

«Мои разборы» показывает до восьми завершённых записей на странице, от новых к старым, и
открывает сохранённый результат без нового LLM-запроса. Кнопка вариантов ответа также
показывает уже сохранённые предложения. Уточняющий вопрос пока честно сообщает, что функция
не подключена. Новый фрагмент создаёт отдельный черновик. Удаление требует подтверждения и
очищает переписку, участников, цель, стадию, результат и оценку. Первая оценка 1–5 побеждает
атомарно и далее неизменна.

Локально и в CI используется `LLM_PROVIDER=stub` и `LLM_MODEL=stub`. Для OpenAI в production
нужен `OPENAI_API_KEY`; вызовы API платные. Ошибки таймаута, перегрузки, невалидного ответа и
конфигурации переводятся в безопасные сообщения без внутренних кодов и частного содержимого.
История и callback-данные не содержат текста переписки, имён, цели или резюме.

Текущие ограничения: генерация выполняется синхронно; production-воркеры остаются Milestone
8. Credits, платежи, новый LLM-вызов для уточнений или ответов, delete-all, retention cleanup,
Grok, OpenRouter, OCR, изображения и голос не реализованы.

При одновременной отправке нескольких разных допустимых оценок действует атомарное правило
first-commit-wins: итоговый балл может быть недетерминированным, но сохраняется ровно один раз.
LLM-клиент создаётся один раз на жизненный цикл dispatcher и закрывается при остановке приложения.

## Milestone 5: кредиты и тестовые платежи

Стоимость полного разбора задаётся `ANALYSIS_PRICE_CREDITS` (по умолчанию один целый
кредит). Баланс — исключительно сумма неизменяемых строк `credit_transactions`: ожидающие
платежи его не увеличивают, списание сериализуется блокировкой пользовательской строки, а
повторное списание использует ключ `analysis_full_access:<analysis_id>`. Технический сбой
возвращает точную сумму одной строкой `refund:<spend_transaction_id>`. Ошибка доставки в
Telegram не является основанием для возврата. Кредиты не дробятся и не истекают.

У каждого пользователя отдельно от платных кредитов есть одно превью со состояниями
`available → reserved → consumed`; техническая ошибка возвращает `reserved → available`.
Превью показывает качество данных, два сильнейших наблюдаемых сигнала, одну неопределённость
и обобщённое направление. Полный результат при этом уже сохранён: разблокировка использует
его без второго LLM-вызова.

Серверный каталог включает `analysis_single` (1 стоимость разбора, 199,00 RUB),
`analysis_pack_5` (5 стоимостей, 699,00 RUB) и `subscription_monthly` (30 кредитов,
990,00 RUB). Цены вымышленные и для production должны быть настроены явно. Месячный продукт
— разовое начисление без автоматического продления.

Локальный mock checkout доступен по непрозрачному адресу
`/payments/mock/checkout/{token}`; generic webhook — `POST /payments/webhooks/mock`.
Событие подписывается HMAC-SHA256 от `timestamp + "." + raw_body`, проверяется постоянным по
времени сравнением и отклоняется вне `PAYMENT_WEBHOOK_MAX_AGE_SECONDS`. В mock-режиме реальные
деньги не списываются, реального платёжного провайдера нет и данные карты никогда не
собираются.

Демонстрационный путь: пройти onboarding → отправить вымышленную переписку → открыть превью
→ увидеть paywall → открыть mock checkout → завершить оплату → вернуться в бот → обновить
баланс → разблокировать полный сохранённый отчёт. Миграция проверяется командами
`alembic upgrade head`, `alembic downgrade -1`, `alembic upgrade head`.

Финансовые строки и аналитика не содержат переписку, имена, цель, отчёт, Telegram identity,
подпись или checkout token. Не реализованы реальные платежи, автопродление, provider refunds,
фоновые workers и stale-job reconciliation (последнее отложено до Milestone 8), новые
LLM-вызовы для ответов/уточнений, OCR, голос, Grok и OpenRouter.

## Milestone 7: аналитика и административная наблюдаемость

Локальная продуктовая аналитика включается через `ANALYTICS_BACKEND=postgres`. События
проверяются по строгому allow-list, используют идемпотентные ключи переходов и не содержат
переписок, отчётов, Telegram identity, receipt contact, checkout URL или секретов провайдера.
Значение `noop` полностью отключает durable analytics.

HTTP-запросы принимают короткий безопасный `X-Correlation-ID` либо получают случайный
идентификатор; выбранное значение возвращается в response header. Telegram использует только
`update_id`, без user/chat identity. Correlation ID добавляется в структурированные логи и
безопасный error-reporting boundary.

Агрегированные метрики скрыты по умолчанию. Для локальной проверки задайте:

```dotenv
ANALYTICS_BACKEND=postgres
ERROR_REPORTING_BACKEND=logging
ADMIN_METRICS_ENABLED=true
ADMIN_API_TOKEN=replace-with-local-admin-token
```

После запуска API:

```bash
curl --fail \
  -H 'X-Admin-Token: replace-with-local-admin-token' \
  -H 'X-Correlation-ID: local-admin-check-1' \
  http://localhost:8000/admin/metrics
```

Endpoint возвращает только агрегаты: статусы разборов, completion rate, latency/tokens/cost,
покупки, воронку, категории validation/technical failures и состояния billing jobs/outbox.
Полное описание контрактов и privacy-ограничений находится в
[`docs/analytics-admin-observability.md`](docs/analytics-admin-observability.md).
