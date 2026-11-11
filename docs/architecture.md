# Архітектура Unified Web Parser

Модульний моноліт, де одиниця роботи — schema-driven job. CLI й FastAPI викликають
один `PipelineRunner`; вебсервер не потрібен для збору чи повторної обробки даних.

## Структура репозиторію

```text
unified-web-scraper/
├── src/unified_web_parser/
│   ├── core/                # Загальні моделі, конфігурація та помилки
│   ├── discovery/           # Пошукові запити, джерела та sitemap
│   ├── crawling/            # Спільна обробка завантажених сторінок
│   ├── navigation/          # Спостережені форми, дії та план переходів
│   ├── routing/             # Вибір блоків для extraction
│   ├── schema_generation/   # Типізований план полів → JSON Schema
│   ├── extraction/          # JSON-LD / DOM / LLM → записи
│   ├── processing/          # Валідація та злиття записів
│   ├── assessment/          # Оцінка вже зібраних фактів
│   ├── profiles/            # Явно ввімкнені предметні евристики
│   ├── providers/           # HTTP, браузер і зовнішні AI/search API
│   ├── pipeline/            # Координація етапів і життєвий цикл запуску
│   ├── storage/             # Бази, історія та експорт
│   └── interfaces/
│       ├── cli/             # Аргументи → job → виконання команди
│       ├── live/            # Запити, стан, snapshots та supervised tasks
│       ├── api.py           # HTTP endpoints і створення FastAPI
│       ├── presentation.py  # Типи, колонки й статистика датасету
│       └── static/
│           ├── app.js       # З'єднання browser-модулів і запуск
│           ├── dataset.js   # Чисті функції типізованих значень/фільтрів
│           ├── js/          # Контролери, views, фільтри, діалоги
│           ├── app.css      # Упорядковані імпорти стилів
│           ├── styles/      # Токени, компоненти й responsive правила
│           └── assets/      # Графіка, локальні шрифти та їхні ліцензії
├── configs/
│   ├── jobs/                # Профілі запусків
│   └── schemas/             # Явні схеми даних
├── tests/
│   ├── unit/                # Ізольована поведінка компонентів
│   ├── integration/         # Сумісна робота етапів, API, CLI та browser checks
│   ├── frontend/            # Node-тести чистої browser-логіки
│   └── fixtures/            # Збережені приклади для відтворюваних тестів
├── scripts/                 # Навігаційні перевірки та cost benchmarks
├── docs/                    # Архітектура, інструкції та пояснення pipeline
├── data/                    # Локальні запуски, snapshots, cache; ігнорується Git
└── pyproject.toml           # Пакет, залежності та інструменти перевірки
```

Вхідні точки залишаються `unified-parser`, `python -m unified_web_parser`,
`interfaces.api.create_app` та `interfaces.live.LiveRunManager`.
`interfaces.cli` зберігає `main` і сумісні `_parser` / `_overrides`, а нові модулі
використовують явно названі `build_parser` та `apply_overrides`.

Новий тип даних зазвичай додається через `configs/jobs` і `configs/schemas`.
Новий зовнішній сервіс належить `providers`, правило для предметної області —
`profiles` або `assessment`, новий спосіб запуску — `interfaces`.
Компоненти UI змінюються в `static/js` і `static/styles`; `app.js` залишається
місцем їхнього з'єднання, а не місцем усієї прикладної логіки.

## Межі модулів

| Шлях у `src/unified_web_parser/` | Відповідальність |
| --- | --- |
| `core/config.py`, `schema.py` | Строгий YAML і runtime budgets; окрема локальна перевірка JSON Schema та references |
| `core/models.py` | Загальні Page, CandidateBlock, Record, перевірювані повідомлення |
| `pipeline/runner.py` | Lifecycle запуску, координація етапів, assessment, persistence й export |
| `pipeline/resources.py`, `report.py` | Створення й закриття клієнтів зі спільним бюджетом; агрегація usage і фінальний звіт |
| `pipeline/schema_lifecycle.py`, `discovery.py` | Підготовка/відновлення схеми; пошук джерел, effective config й аудит discovery |
| `pipeline/schema_session.py` | Буфер зразків, одноразова фіксація схеми й передача сторінок у processor |
| `pipeline/persistence.py` | Спільне збереження готових записів, assessment та експорту для звичайного завершення й контрольованої зупинки |
| `pipeline/pages.py` | Обробка однієї сторінки: candidates → routing → extraction → validation → merge |
| `pipeline/artifacts.py` | HTML snapshots, manifest ролей сторінок і replay |
| `pipeline/crawl.py` | Обмежений граф URL/станів, черга робіт, виконання, stop reasons |
| `navigation/models.py`, `inventory.py`, `planner.py` | Контракти, побудова inventory та rules/Decisions план навігації |
| `navigation/html.py`, `controls.py`, `policy.py`, `urls.py` | Спостереження DOM і форм, допустимі дії, розв’язання URL |
| `discovery/sitemap.py` | Розгортання sitemap та sitemap index |
| `discovery/search.py`, `providers/search.py` | Планування запитів, OpenRouter Web Search Server Tool із перевіркою фактичного пошуку, citation-only URL та TTL cache |
| `schema_generation/` | Обмежений план полів із питання, локальна компіляція й валідація JSON Schema |
| `sources/navigation.py` | Canonical URL identity без site-specific search paths |
| `crawling/` | Спільні DOM-евристики, snapshot identity, rate limit, access-page detection |
| `providers/http.py`, `crawl4ai.py` | HTTP, headless browser fetch і lifecycle браузерних сесій |
| `providers/browser_actions.py` | Виконання прив’язаних до snapshot дій через довірений JavaScript |
| `providers/openrouter.py`, `decisions.py` | Chat JSON / native Decisions, валідація, shared budget, cache |
| `routing/candidates.py`, `engines.py` | Кандидати та рішення, які блоки розбирати |
| `extraction/schema.py` | Координатор JSON-LD / DOM / LLM extraction |
| `extraction/projection.py` | Mapping полів, `x-source`, приведення типів і DOM → entity |
| `extraction/enrichment.py` | Sparse schema, model request, перевірка та merge доповнень |
| `extraction/identity.py` | Record identity, хеші, provenance й створення записів |
| `extraction/images.py` | Спостережені зображення конкретної сутності; універсальне nullable `img_url` |
| `profiles/commerce.py`, `assessment/device.py` | Явно обрані товарні евристики та перевірки пристроїв |
| `processing/validation.py` | Перевірка записів і детермінована дедуплікація |
| `assessment/rules.py` | Необов'язкова інтерпретація вже витягнутих даних |
| `storage/dataset.py`, `export.py` | SQLite/PostgreSQL, history, JSONL/Parquet/CSV |
| `interfaces/cli/__init__.py`, `arguments.py` | Точка входу, dotenv, exit codes і визначення аргументів |
| `interfaces/cli/jobs.py`, `commands.py` | Розв'язання конфігурації, CLI overrides і виконання команд |
| `interfaces/api.py` | HTTP endpoints, dependency wiring та static assets |
| `interfaces/live/requests.py`, `state.py` | Валідація запиту, початковий стан та in-memory envelope |
| `interfaces/live/snapshots.py`, `manager.py` | Відновлення/збереження snapshots, supervised tasks, cancellation і SSE |
| `interfaces/presentation.py` | Метадані полів і статистика за фактичними записами |
| `interfaces/static/js/transport.js` | Fetch, SSE, reconnect і вибір запуску |
| `interfaces/static/js/cost.js` | Відома вартість, повнота usage та безпечне відображення невідомої суми |
| `interfaces/static/js/views.js`, `dialog.js` | Прогрес, історія, журнал і деталі запису |
| `interfaces/static/js/results.js`, `filters.js` | Картки/таблиця, фільтри, колонки та збереження DOM-фокусу |
| `interfaces/static/js/actions.js` | Обробники форм, кнопок, експорту та mobile history |
| `interfaces/static/js/state.js`, `dom.js`, `format.js` | Спільний стан, DOM helpers і безпечне форматування |
| `interfaces/static/styles/` | Послідовні CSS-секції; порядок імпортів визначає `app.css` |

Моделі й контракти не залежать від SDK провайдерів. `PipelineRunner` приймає підмінні
fetcher, LLM і store для тестів; типові реалізації створюються на межі запуску.
Автоматичного plugin registry та окремих сервісів/черг немає.

Залежності йдуть від координаторів до конкретних етапів: `runner` викликає schema lifecycle,
discovery, crawl і page processor; page processor використовує routing, extraction та validation.
Extraction проєктує поля, за потреби доповнює їх через LLM, потім формує identity/provenance.
Inventory збирає спостереження через DOM/control helpers; browser provider виконує лише
перевірені action bindings. Ці етапи не імпортують `PipelineRunner` і не відкривають власні
неконтрольовані AI-клієнти. Черга станів та правила її завершення залишаються в `CrawlEngine`.

## Шлях даних

```text
Job YAML + JSON Schema
        ↓
Optional question → bounded field plan → validated generated JSON Schema
        ↓
Optional question → query plan → web search → deduplicated source URLs
        ↓
Source URL / sitemap → HTTP або retained browser session
        ↕
NavigationInventory → rules / Decisions → NavigationPlan → observed action
        ↓
    Extractable Page snapshot → candidates із відомими ID
        ↓
DecisionEngine → RoutingPlan → перевірка candidate IDs і snapshot
        ↓
JSON-LD / DOM / LLM → JSON Schema validation → merge / dedupe
        ↓
Record + Provenance → optional assessment
        ↓
SQLite + artifacts → optional PostgreSQL / Parquet / CSV
```

Є два різних плани. `NavigationPlan` обирає існуючі action IDs, extraction або stop;
`RoutingPlan` обирає існуючі candidate IDs та стратегію extraction. Пошуковий query є
в navigation state, але не в block-routing context. Деталі: [навігація](navigation.md).

Block Decision Engine працює **до extraction**. AI вибирає тільки існуючі ID й стратегію
`json_ld`, `dom`, `llm` або `skip`; він не вигадує селектори для виконання.
`RoutingPlan.validate_against` відхиляє невідомі/повторні ID та план іншого snapshot.
Порожній план допустимий, невибрані кандидати пропускаються.

Режим `rules` обирає JSON-LD, а за його відсутності DOM-кандидатів. `ai` використовує
окрему routing-модель. Оптимізовані MacBook-профілі задають `hybrid`: guard
`can_route_structured` дозволяє правила лише для одного JSON-LD із непорожнім description,
явним url/@id цієї сторінки та без суперечливих URL. Решта сторінок іде до native Decisions.
Qwen витягує поля; `routing: ai` дозволяє примусово класифікувати також структуровані сторінки.
Native `openai/gpt-6-luna-decisions` працює через `/api/alpha/decisions`: `state` та
іменовані `questions` типу `choice` → `answers`. Стратегії обмежені видом кандидата;
відповіді перевіряються, confidence/probabilities зберігаються у `routing.jsonl`.
Звичайні chat-моделі можуть замінити router лише за явним вибором користувача.
Помилка AI routing дає warning і fallback на правила, LLM extraction — error і спробу
детермінованого extraction. Перемикання на іншу модель автоматично не відбувається.

Ліміти охоплюють сторінки, глибину, записи, кандидатів, розмір блоків/сторінки, timeout,
retries й AI calls. `RequestBudget` спільний для schema generation, query planning, web search,
navigation, routing та extraction;
cache hits не витрачають calls. Звіт містить totals і `ai.stages` створених клієнтів;
rules/replay/no-AI можуть не мати частини або всіх stage entries.
HTTP fetch concurrent, browser послідовний. URL відкриваються з source root без вигадування
пошукового шляху. Після JS-мутації перебудовується inventory; viewport metrics враховані
в observation hash, тому scroll без зміни HTML також може бути новим станом.
Idle browser pages звільняються через `release(page)`; parent з pending UI-дією
утримується до її виконання. Release events і pages_released потрапляють в audit/report.
Rate limit діє для host. Досягнення заданого ліміту саме по собі не є доказом повноти сайту.
Discovery також задає per-site cap: усі URL одного зареєстрованого домену ділять один
ліміт спроб, включно з UI-діями; приватні PSL tenants рахуються окремо. Типово кілька
пошукових видач об'єднуються за цією ж ідентичністю, після чого обходиться корінь
кожного вибраного сайту. Пошук джерел та витрати описані в [discovery.md](discovery.md).

## Схема та факти

Доменні поля містяться у JSON Schema й `Record.data`. Типовий профіль — `generic`:
точні поля та явний `x-source` мають пріоритет перед загальними semantic aliases.
Товарні mappings, money extraction й обробка `offers` вмикаються явно через `commerce`.
Схема з полями `price`/`currency` сама по собі не обирає товарний профіль.
Довільні складні характеристики можуть потребувати LLM або додаткового extractor.
При JSON-LD enrichment явні структуровані факти мають пріоритет.
Спільне структурне розпізнавання карток і рядків таблиць не вимагає грошей чи зображень.
Відокремлення ціни конкретної пропозиції від діапазону каталогу належить commerce-профілю.
Відсутнє значення не замінюється вигаданою характеристикою; `null` можливий лише за схемою.
Default extraction — `qwen/qwen3.7-flash`; `response_mode: auto` передає йому `json_object`
зі схемою в prompt. Для інших chat-моделей auto обирає `json_schema`; GPT-6 не отримує
temperature. Повна локальна валідація виконується в обох режимах незалежно від провайдера.
`reasoning: false` явно вимикає optional reasoning у chat-запиті; `null` залишає поведінку моделі.
Sparse JSON-LD enrichment проєктує лише missing/null і conflict fields закритої схеми.
References, open objects і cross-field constraints повертають full-schema extraction.
Merge зберігає known facts і конфлікти, після чого перевіряється повна цільова схема.

Job config відхиляє невідомі опції, дублікати YAML-ключів, не-HTTP(S) джерела та невалідну
структуру JSON Schema. `schema_path` обчислюється відносно YAML. Дозволені лише внутрішні
`$ref`/`$dynamicRef`/`$recursiveRef`; зовнішні references не завантажуються.
`validate` перевіряє конфігурацію/схему, а processing — самі записи, включно з format checks.
Loader не доводить розв'язність усіх внутрішніх посилань. Довільні validation vocabularies
не підтримані; локальний `x-source` використовується лише як extraction mapping.

Assessment відокремлений від routing. Типовий `generic` зіставляє пошук із даними
запису; `relevance_fields` дозволяє явно обрати поля. У `assessment_profile: device`
`is_relevant` порівнює токени пошуку з назвою й характеристиками, не ціною/описом;
parts/accessory/service отримують `false` тільки в цьому профілі.
`has_mdm_risk` — `true` лише за явних claimed_managed/claimed_bypass, інакше `null`.
`requires_manual_review` позначає unknown/managed MDM, source conflicts і non-device;
`false` означає відсутність прапорців серед доступних полів, а не підтверджену безпеку.
Непідтримувані критерії — `null`. Це не MDM-діагностика чи ринкова оцінка.

## Ідентичність, збереження й replay

`page_id` — SHA-256 URL та HTML. HTML лежить у `data/snapshots/`, manifest запуску містить
роль сторінки: discovery та sitemap не повинні повторно трактуватися як окремі товари.
Replay читає лише extractable snapshots, створює новий запуск і не виконує crawling.
За ввімкненого AI повторна обробка може використати cache або зробити новий AI-запит.
Разом із manifest потрібно зберегти HTML; поточний формат використовує локальні paths.

`record_id` залежить від job, домену й identity сутності: URL, явного source identifier
або content fingerprint для кількох сутностей без окремих URL/ID; одиночна використовує page URL.
`Provenance` містить source URL, snapshot/page ID, candidate IDs, extractor, snapshot reference,
а також фактичну `model`, `prompt_version` й `identity_basis`; старі записи можуть мати `null`.
Content fingerprint змінюється з evidence й не гарантує наскрізної історії сутності після змін.
Товари різних сайтів не об'єднуються; для надійної variant identity може знадобитися адаптер.

Таблиці `parser_runs`, `parser_records`, `parser_snapshots`, `parser_assessments` створюються
автоматично. Ключ current record — `(job_name, record_id)`. Повторний збір оновлює provenance
і `last_seen_at`; snapshot додається при зміні доменних даних або nullable `img_url`. Повернення до попередньої
ціни також є новою подією. Один batch записів/assessments записується транзакційно.
Зникнення запису з обмеженого crawl поки не позначає його проданим або неактивним.

SQLite — завжди локальний датасет. При `outputs: [postgres, ...]` додаткова копія йде у
PostgreSQL через `DATABASE_URL`: JSONB для довільних даних, GIN index, record/history indexes.
Ці дві бази не мають спільної розподіленої транзакції; помилки сховища відображаються у звіті.
Експорт замінює файл атомарно; Parquet має typed scalar columns, вкладені значення — JSON.

`job.json`, `pages.jsonl`, `navigation.jsonl` (fresh crawl), `routing.jsonl`, `summary.json`
зберігають контекст запуску. Navigation audit містить inventory/plan/action/execute/result/stop;
`frontier_exhausted` означає вичерпання доступного планувальнику графа, а не всього сайту.
AI cache враховує endpoint, модель, prompts/questions, schema, canonical source URL й evidence.
Extraction/native routing не включають snapshot/candidate IDs; native blocks мають full-content digest.
Альтернативний chat-router включає candidate IDs у cache key, тому їх зміна може дати cache miss.
Відсутній `cost_usd`/`reasoning_tokens` означає unknown, не нуль; totals публікуються лише за повних metadata.
Для live-відображення `known_cost_usd` накопичує повернуту вартість навіть за неповних даних;
`cost_complete`, `cost_pending_calls` і `in_flight_calls` показують її повноту.
Provider usage публікується після кожної відповіді, до валідації модельного результату.
Відмова через кредити блокує наступні AI-запити через спільний бюджет і завершує запуск
як `stopped` з причиною; ручна зупинка — як `cancelled`. Обидва шляхи зберігають готові
валідні записи. Деталі: [контроль витрат](dashboard.md#вартість-і-зупинка).
Вимірювання cold/warm і quality gates: [вартість](cost-optimization.md). Повна система
версій provider/builder і міграцій збережених артефактів ще не реалізована.

## Новий домен

Створи `configs/schemas/vacancy.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["title"],
  "properties": {
    "title": {"type": "string"},
    "company": {"type": ["string", "null"]},
    "salary": {"type": ["number", "null"]}
  }
}
```

І `configs/jobs/vacancies.yaml`:

```yaml
name: vacancies
sources:
  - url: https://example.org/jobs
schema_path: ../schemas/vacancy.schema.json
outputs: [jsonl, parquet]
crawl:
  backend: http
  max_pages: 10
navigation:
  mode: hybrid
ai:
  enabled: true
  model: qwen/qwen3.7-flash
  decision_model: openai/gpt-6-luna-decisions
  response_mode: auto
  routing: hybrid
  reasoning: false
  sparse_enrichment: true
  max_calls: 20
```

Заміни example URL, потім `uv run unified-parser run configs/jobs/vacancies.yaml`.
Ядро змінювати не потрібно. Generic inventory знаходить форми/SearchAction, категорії,
повторювані item links і continuation controls. Нерозпізнана структура може потребувати
розширення `navigation`/DOM-евристик; окремого plugin registry немає. Спосіб отримання
сторінки — у provider, стратегія полів — в extraction. API викликає той самий pipeline:
старий endpoint профілю чекає завершення, новий `POST /runs` запускає керовану фонову task
та повертає 202. Dashboard отримує SSE-події й відокремлює preview від persisted records.
Деталі: [живий інтерфейс](dashboard.md). Scheduler, зовнішня черга та сповіщення
потребують окремої реалізації.
