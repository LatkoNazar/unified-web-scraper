# Unified Web Parser

Модульний Python-рушій: URL, пошук або sitemap → сторінки → вибір потрібних блоків
Decision Engine → JSON-LD / DOM / LLM extraction → перевірка JSON Schema → датасет.
MacBook — перший профіль; доменні поля задаються конфігурацією, а не моделями ядра.

**[Повний технічний опис для передачі агенту та підготовки резюме](docs/PROJECT_HANDOFF_UK.md)** —
реалізований алгоритм, приклад руху даних, стек, відновлення, дозбір,
перевірені результати та межі коректних тверджень про проєкт.

**[Структура проєкту й межі модулів](docs/architecture.md)** — карта папок,
відповідальність компонентів і куди додавати нові профілі, провайдери та UI-функції.

**[Універсальне ядро та предметні профілі](docs/domain-profiles.md)** — типово `generic`:
статті, вакансії, події, організації та довільні поля схеми. Товарні евристики
й перевірки пристроїв вмикаються явно через профілі `commerce` та `device`.
Список валют не визначає, які сторінки можуть стати записами.

Працюють CLI, локальний API, навігація за спостережуваними діями, HTTP і Crawl4AI,
native Decisions navigation/routing та extraction через OpenRouter,
SQLite, історія змін, JSONL/Parquet/CSV, PostgreSQL і повторна обробка snapshot.

**[Живий інтерфейс і зображення](docs/dashboard.md)** — запусти `serve` та відкрий `/`:
питання → фоновий збір → картки або таблиця, `img_url`, реальні лічильники й журнал
наживо. Типи характеристик визначають діапазони, категорії, boolean і фільтри дат;
межі та унікальні значення рахуються за вибіркою. Є вибір колонок, історія,
деталі запису й завантаження JSON.

**[Захист від збоїв і продовження](docs/recovery.md)** — записи та usage зберігаються
під час роботи, перерваний запуск можна продовжити кнопкою в UI або командою
`unified-parser resume RUN_ID`. Невідомі результати платних запитів не повторюються автоматично.

**[Релевантність і фотографії](docs/data-quality.md)** — відбір перед збереженням,
окремий аудит відхилень, прив'язка власних фото та виправлення історичних вибірок.

**[Дозбір нових сторінок](docs/incremental.md)** — кнопка «Дозібрати»,
`update RUN_ID` та `--incremental`: каталоги перечитуються, уже завершені
деталі пропускаються до завантаження й AI-обробки.

**[Пошук від питання без списку сайтів](docs/discovery.md)** — `discover` формує кілька
варіантів запиту, об'єднує знайдені домени й починає пошук від кореня кожного сайту.
Типово це три запити, до 25 результатів на запит і максимум 25 унікальних сайтів.
Навігаційні фільтри ціни, характеристик і сортування на сайтах типово вимкнені;
це не впливає на локальні фільтри вже зібраного датасету в UI.
Пошук використовує `openrouter:web_search` Server Tool з лімітом одного виконання
на HTTP-спробу та перевіркою actual usage; deprecated Web Search Plugin не використовується.

**[Автоматична схема з питання й елементів](docs/schema-generation.md)** — без `--config`
`discover` та `search --source` готують попередню схему, оглядають до трьох знайдених
сторінок і уточнюють типи та колонки перед extraction. Готова схема фіксується на весь запуск.

**[Навігація: поточна поведінка, команди й межі](docs/navigation.md)** — пошук від кореня сайту,
категорії, групи результатів, пагінація, JS-контроли та аудит станів.

**[Повний pipeline README українською](docs/PIPELINE_README.md)** — покроково: що запускається,
які запити йдуть на сайт/Luna/Qwen, що повертається та куди зберігається, зі справжніми trace assets.

## Встановлення

Потрібні Python 3.12+ і [uv](https://docs.astral.sh/uv/getting-started/installation/).
Команди PowerShell із кореня репозиторію:

```powershell
uv sync --dev --extra browser
uv run playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
uv run unified-parser validate configs/jobs/macbooks.yaml
```

Додай `OPENROUTER_API_KEY` у локальний `.env` для AI. Без ключа працює deterministic
extraction із попередженням у звіті; `--no-ai` явно вимикає AI. Для HTTP без браузера
достатньо `uv sync --dev` та `--backend http`.

CLI автоматично читає `.env`, наявні змінні середовища мають пріоритет.
Інший файл: `uv run unified-parser --env-file .env.local run configs/jobs/macbooks.yaml`.
Дві моделі налаштовуються окремо: extraction — `qwen/qwen3.7-flash`, routing —
`openai/gpt-6-luna-decisions`. Пріоритет extraction: `--model` → `OPENROUTER_MODEL` →
`ai.model`; routing: `--decision-model` → `OPENROUTER_DECISION_MODEL` → `ai.decision_model`.
Extraction `--response-mode auto` використовує для Qwen `json_object` зі схемою в prompt і локальною
перевіркою; `json_schema` / `json_object` можна задати явно. Інші chat-моделі в `auto`
використовують `json_schema`. Зміна extraction-моделі не перемикає routing.

## Збір і пошук

```powershell
uv run unified-parser run configs/jobs/macbooks.yaml
uv run unified-parser run configs/jobs/articles-web.yaml
uv run unified-parser discover "Знайди вакансії Python. Збери назву, компанію, зарплату, місто й URL." --search-results 3
uv run unified-parser discover "Знайди MacBook Air 15 M4 24GB у продажу в Україні" --config configs/jobs/macbooks-web.yaml --search-results 3 --pages-per-site 4
uv run unified-parser discover "Книги про подорожі ціна" --max-queries 3 --search-results 10 --max-sites 15 --pages-per-site 3
uv run unified-parser run configs/jobs/macbooks-olx.yaml --model qwen/qwen3.7-flash --decision-model openai/gpt-6-luna-decisions
uv run unified-parser search "MacBook Air 15 M4" --source https://www.olx.ua/ --config configs/jobs/macbooks.yaml --backend browser --navigation hybrid --max-pages 12 --max-records 4
uv run unified-parser run configs/jobs/books.yaml --data-dir data/live-navigation
uv run unified-parser extract "https://example.org/products/item-1" --config configs/jobs/macbooks.yaml --backend http --no-ai
```

У `extract` підстав URL справжньої сторінки. `--source` приймає **абсолютний HTTP(S) URL**;
параметр можна повторювати. Скорочень `olx`/`prom` немає. Рушій починає з переданого URL,
шукає спостережувані форми, schema.org SearchAction, категорії та посилання результатів;
пошукові шляхи під конкретні сайти не зашиті. HTTP виконує URL-дії, browser також працює
з JS-пошуком, select, load-more і scroll. Покриття залежить від доступного контенту й лімітів.

Основні обмеження: `--max-pages`, `--max-records`, `--max-calls`, `--backend http|browser`.
`--navigation rules|ai|hybrid` визначає рух між станами сайту; окремий
`--routing rules|ai|hybrid` визначає вибір блоків для extraction. Оптимізовані MacBook-профілі — `hybrid`:
правила обирають лише один JSON-LD із непорожнім описом та явним URL цієї сторінки;
неоднозначні сторінки передаються native Decisions. `--routing ai` примусово залучає AI.
`rules` не вимикає LLM-доповнення полів; для повного вимкнення потрібен `--no-ai`.
Явний `--config` зберігає схему профілю; `--auto-schema` вмикає генерацію для нього.
`--content-profile generic|commerce` обирає extraction-політику;
`--assessment-profile generic|device` — правила оцінювання. Типово обидва `generic`.
Старі спеціалізовані YAML для MacBook і книжкового каталогу явно обирають commerce.
`--fixed-schema` вимикає автоматичну генерацію для запуску без власного профілю.
Нова генерація схеми потребує AI; для `--no-ai` обери фіксовану схему або replay.
У `search --source` без `--config` типово використовується універсальний профіль
і генерація схеми з питання. Для детермінованого запуску додай `--fixed-schema`
або явно обери власний YAML; прихованого MacBook-профілю більше немає.
У `discover` із фіксованою схемою навіть з `--no-ai` сам OpenRouter web search залишається платним;
AI-планування вимикається, пошук використовує literal query. Режим без пошуку — `search --source`.
AI planner розділяє web-запити з ринком/бюджетом і короткий `site_search` із предметом та
характеристиками. Це інструкція моделі, не окремий гарантований фільтр ціни чи географії;
оригінальне питання й фактичний план зберігаються в `discovery.json`.
`--search-results` задає кількість URL **на кожен запит**, `--max-sites` — спільний ліміт
унікальних сайтів. Типовий `--entrypoint site_root` прибирає шлях і query знайденого URL;
`--entrypoint result_url` починає з повернутих сторінок. У site-root режимі піддомени одного
зареєстрованого домену об'єднуються; незалежні сайти на приватних суфіксах зберігаються окремо.
`--allow-filters` явно дозволяє навігаційні фільтри; без нього використовується чистий пошук.
`--search-context-size low|medium|high` задає обсяг пошукового контексту, типово `low`.
Три пошуки по 25 результатів у `parallel/basic` можуть коштувати до $0.06 плюс токени;
три по 10 — $0.015 плюс токени за тарифом на момент реалізації.
`max_calls` — спільний бюджет **schema generation + query planning + web search + navigation + routing + extraction**, включно з retries;
cache hits не витрачають його. Звіт `ai` містить totals, активні `stages` і `budget_used`;
набір stages залежить від discovery, rules, replay і AI-параметрів.
`routing.jsonl` зберігає вибір, confidence та probabilities, коли їх повернув Decisions.

Native routing звертається до `POST https://openrouter.ai/api/alpha/decisions`,
передаючи `state` + `questions` і читаючи `answers` ([модель](https://openrouter.ai/openai/gpt-6-luna-decisions/providers)).
Звичайний Luna як chat-router: `--decision-model openai/gpt-6-luna`.
Явна альтернатива extraction: `--model deepseek/deepseek-v4.1-flash-1017`.
Автоматичного перемикання на іншу модель немає; при помилці routing діє fallback на правила.

Профілі також задають `reasoning: false` і sparse enrichment: Qwen повертає лише відсутні
поля/конфлікти, відомі факти зберігаються локально, результат перевіряється повною схемою.
CLI: `--reasoning auto|on|off`, `--max-output-tokens N`, `--full-enrichment`.
Cold benchmark: **−93.704% estimated AI cost**, 5 calls, 0 cache hits, 52 quality invariants.
Окремий warm replay: 0 calls, 5 cache hits. Ці вимірювання стосуються п'яти OLX-сторінок;
умови, actual provider metadata та відтворення — [оптимізація вартості](docs/cost-optimization.md).

Код завершення збору: **0** — `success`, **2** — `partial` із записами та помилками,
**1** — `failed`. JSON-звіт містить помилки, попередження, AI usage і шляхи артефактів.
Некоректний синтаксис CLI також повертає 2; переривання користувачем — 130.

## Результати, історія й повторна обробка

За замовчуванням усе зберігається в `data/`; змінити шлях можна через `--data-dir`.

```text
data/
├── dataset.sqlite           # current records, runs, changed snapshots, assessments
├── snapshots/<page_id>.html # вихідний HTML / rendered HTML
├── ai_cache/               # перевірені структуровані відповіді
├── search_cache/           # перевірена пошукова видача з TTL
└── runs/<run_id>/
    ├── job.json
    ├── schema.json          # фактична автоматично згенерована/відновлена схема
    ├── schema-generation.json # питання → поля → схема; auto-schema run
    ├── discovery.json       # питання → запити → знайдені URL; discovery-run
    ├── pages.jsonl          # manifest із ролями сторінок і snapshot references
    ├── navigation.jsonl     # inventory → plan → action → result → stop; fresh crawl
    ├── routing.jsonl        # фактично обрані candidate IDs та стратегії
    ├── records.jsonl        # portable dataset успішно обробленого запуску
    ├── records.parquet      # коли обрано parquet
    └── summary.json
```

```powershell
uv run unified-parser runs --limit 10
uv run unified-parser records --job macbook-monitor --limit 20
uv run unified-parser export macbook-monitor --format parquet --output data/exports/macbooks.parquet
uv run unified-parser export macbook-monitor --format jsonl --output data/exports/macbooks.jsonl
uv run unified-parser reprocess configs/jobs/macbooks.yaml --from-run data/runs/RUN_ID --no-ai
```

Заміни `RUN_ID` на ID запуску. Replay використовує збережені extractable-сторінки,
без повторного crawling. Без `--no-ai` він може звернутися до OpenRouter.
Зберігай `snapshots/` разом із manifest: поточні references містять локальні шляхи.
Новий run оновлює current dataset; незмінні дані не дублюють історію, зміни додають snapshot.

JSONL зберігає вкладені record envelopes. Parquet/CSV мають доменні поля верхнього рівня
і метадані `_record_id`, `_job_name`, `_source_url`, `_provenance_json`, `_assessments_json`,
`_first_seen_at`, `_last_seen_at`, `_img_url`. Ці назви зарезервовані. Parquet зберігає типи однорідних
скалярних колонок; вкладені та змішані значення кодуються JSON-рядками.
Перелік таких колонок збережений у Parquet metadata `unified_web_parser.json_columns`.

## PostgreSQL і API

```powershell
docker compose up -d --wait postgres
uv run unified-parser run configs/jobs/macbooks.yaml --outputs jsonl parquet postgres
uv run unified-parser serve --host 127.0.0.1 --port 8000
```

Для `postgres` потрібен `DATABASE_URL`; `.env.example` містить URL локального Compose
на порту 55432. `PARSER_POSTGRES_PASSWORD` змінює dev-пароль Compose; узгодь його з URL.
SQLite залишається локальним датасетом, PostgreSQL отримує додаткову копію.

API: `GET /health`, `GET /jobs`, `POST /jobs/{job_id}/run`, `GET /runs`,
`GET /records?job=macbook-monitor`, `GET /records/{record_id}/history?job=macbook-monitor`.
`job_id` — ім'я YAML без розширення, наприклад `macbooks`. POST чекає завершення pipeline
і повертає його звіт; статус перевіряй у полі `status`. Swagger: `http://127.0.0.1:8000/docs`.
UI доступний на `http://127.0.0.1:8000/`. Для фонового запуску з питання:
`POST /runs` → HTTP 202, `GET /runs/{id}` → snapshot,
`GET /runs/{id}/events` → SSE, `POST /runs/{id}/cancel` → скасування.
Під час збору записи є попереднім переглядом; UI показує фактичне збереження окремо.
У верхній панелі накопичується відома вартість запуску й доступна кнопка зупинки.
Ручна зупинка та відмова через кредити зберігають уже готові записи;
неповні cost metadata позначаються окремо. Деталі: [вартість і зупинка](docs/dashboard.md#вартість-і-зупинка).

## Межі й перевірка

HTTP-приклад `books.yaml` (тепер явно `commerce`) пройшов від кореня Books to Scrape через категорію
Travel до трьох книжок: 5 сторінок, 3 записи, `success`, без errors/warnings.
Локальні GET-form і реальний Chromium JS/search/select/load-more/scroll перевіряються
окремими fixtures без AI. Деталі й артефакти — [навігація](docs/navigation.md).

Replay початкового Qwen + Luna Decisions профілю пройшов: 5 сторінок, 5 записів,
5 routing + 5 extraction calls, спільний бюджет 10/15, без errors/warnings.
Live-запуски OLX, Prom і Rozetka та replay з `openai/gpt-4.1-mini` пройшли; це історичні
перевірки попереднього профілю. Replay відділив 13-дюймову модель й запчастини від цілі
та зберіг суперечність SSD 512/500. Пошук Rozetka повернув M5 замість M4: запис отримано,
але він нерелевантний. Успішний збір не означає відповідності кожного запису пошуку.
Були також HTTP 403/429 і `partial`; доступність сайту/моделі може змінитися.
Метрики, моделі й артефакти кожної перевірки: [live-перевірки](docs/live-validation.md).
Сторінки блокування й captcha повертаються як помилки доступу; їх обхід не реалізований.

Невідомі характеристики залишаються `null`, якщо це дозволяє схема, або пропускаються.
У `assessment_profile: device` заява про MDM — неперевірена заява джерела. Цей профіль підтримує `is_relevant`
за токенами назви/характеристик, відхиляючи parts/accessory/service; ціна й опис не доводять
відповідність. `has_mdm_risk` повертає `true` для заявленого managed/bypass, інакше `null`.
`requires_manual_review` враховує невідомий/managed MDM, source conflicts і тип пропозиції;
`false` означає лише відсутність прапорців цих правил. Непідтримувані критерії — `null`.
У типовому `generic` ці device-перевірки не активуються; поля релевантності можна
задати через `relevance_fields`, не прив'язуючи їх до RAM, SSD або інших товарних назв.
Buy/Reject, оцінка шахрайства, Telegram і scheduler не реалізовані. Native Decisions
інтегрований через OpenRouter; прямого клієнта OpenAI API немає.

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/integration/check_postgres.py
```

Остання команда потребує запущеного Compose PostgreSQL або `PARSER_TEST_DATABASE_URL`.
Вона перевіряє справжні JSONB/upsert/history й видаляє лише власні UUID-scoped тестові записи.
Звичайний pytest використовує локальні fixtures/mock transports без платних AI-запитів.
`DeprecationWarning`, `PendingDeprecationWarning` та `FutureWarning` завершують тести
помилкою; попередження не приховуються. Перевірка залежностей: `uv pip check`.
Останні зміни API й залежностей: [аудит deprecated-використань](docs/dependency-audit.md).

Модулі, контракти та приклад іншого домену: [архітектура](docs/architecture.md).
