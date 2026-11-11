# Live-перевірки

Нижче — фактичні результати локальних запусків 22 жовтня 2026 року. Історичні запуски виконані з моделлю
`openai/gpt-4.1-mini`. Це перевірка роботи pipeline, а не гарантія повноти сайту,
відповідності всіх знайдених пропозицій або правдивості тверджень продавців.

Поточний профіль уже інший: extraction `qwen/qwen3.7-flash`, routing
`openai/gpt-6-luna-decisions`, оптимізований hybrid. Модель попередніх запусків не змінюється заднім числом.
Окрема native Decisions probe отримала HTTP 200 із choice `json_ld`, confidence `1`
і probabilities; це реальний `/api/alpha/decisions`, без chat-completions підміни.

## Початковий Qwen + Luna Decisions baseline

Replay `9874455546d04ebf89af533504984a6e` завершився `success`: 5 сторінок, 5 записів,
без errors/warnings. Native routing зробив 5 запитів, Qwen extraction — 5; разом
10 із 15 дозволених attempts, 0 cache hits. Усі записи мають Qwen у provenance.
Збережено 5 routing plans, 10 candidate targets, кожен із confidence та probabilities.
Ці оцінки стосуються вибору стратегії, а не перевірки правдивості оголошення.

- [Звіт двох моделей](../data/runs/9874455546d04ebf89af533504984a6e/summary.json)
- [Native routing plans](../data/runs/9874455546d04ebf89af533504984a6e/routing.jsonl)
- [Qwen dataset](../data/runs/9874455546d04ebf89af533504984a6e/records.jsonl)

Оптимізований cold replay `2ae1c7fe189f4e759e252bf77c717574` зробив 5 extraction calls,
0 routing calls і 0 cache hits; estimated cost менший на 93.704%. Окремий warm replay
`c09d7edf12474756815b454e4c041034` зробив 0 calls і 5 cache hits. Обидва зберегли
5 record IDs, 3 релевантні пристрої та пройшли 52 invariants. Деталі й обмеження:
[cost benchmark](cost-optimization.md).

## Історичний профіль GPT-4.1 Mini

| Перевірка | Run ID | Сторінки / записи / AI calls | Результат |
| --- | --- | --- | --- |
| OLX browser, hybrid, PostgreSQL | `a25af68253144721aba23ba8915f3977` | 6 / 5 / 5 | `success`, без errors/warnings |
| Replay OLX, розширена schema | `56f2f63475004b9d97aa5c67331bbbd8` | 5 / 5 / 5 | `success`; 3 релевантні пристрої |
| Prom browser | `eaa9b2e1a102468a895076b7cea6005f` | 4 / 2 / 2 | `success`; отримані аксесуари |
| Rozetka browser | `91c987cedd9145cab1762cbfe2adda1a` | 2 / 1 / 1 | `success`; M5 Air 13.6 замість M4, `is_relevant=false` |
| Прямий OLX URL, AI routing | `d274ed14f96041cc9bc114015e768a03` | 1 / 1 / 2 | `success`; окремо перевірені поля оголошення |
| Фінальний acceptance replay | `b4b1528df22a4299b91b3ce1711e0b31` | 5 / 5 / 0 | `success`; 5 cache hits, 3 релевантні, JSONL/Parquet узгоджені |

Розширена MacBook-схема зберігає URL, опис, external ID, тип пропозиції, діагональ,
характеристики батареї та суперечності джерела. Replay відрізнив 13-дюймову модель
й запчастини від цільових пристроїв. Розбіжність SSD 512 GB у назві та 500 GB в описі
потрапила у `source_conflicts`; збережена номінальна місткість із назви — 512 GB.

Для прямого OLX extract перевірені значення: M4, RAM 16 GB, SSD 512 GB, 116 циклів,
67 000 UAH. MDM залишився `null`, бо відсутність iCloud lock не доводить відсутності MDM.
Це дані оголошення, без незалежної перевірки пристрою.

Раніший запуск `a15d55bad61947c5b4af7e922fa353bb` зберіг 4 записи з 5 сторінок,
але повернув `partial` через HTTP 429 / AI budget. Звичайний HTTP-запит до OLX повертав
403; успішні live-запуски використовували стандартний Crawl4AI browser.
Для Rozetka browser очікує DOM rendering і враховує статус та URL кінцевого redirect.
Сторінки явного блокування залишаються помилками доступу.

## Артефакти

Для кожного ID вище локально існує `data/runs/<run_id>/summary.json` з метриками,
`job.json` зі schema/config, `pages.jsonl`, `routing.jsonl` і `records.jsonl`.
Parquet збережено там, де він обраний у job. Наприклад:

- [OLX summary](../data/runs/a25af68253144721aba23ba8915f3977/summary.json)
- [Розширений replay dataset](../data/runs/56f2f63475004b9d97aa5c67331bbbd8/records.jsonl)
- [Rozetka summary](../data/runs/91c987cedd9145cab1762cbfe2adda1a/summary.json)
- [Прямий OLX dataset](../data/runs/d274ed14f96041cc9bc114015e768a03/records.jsonl)

Файли `data/` локальні й виключені з Git. HTML знаходиться у `data/snapshots/`;
оцінювання та поточний стан записів — у `data/dataset.sqlite`, додатково в PostgreSQL
для запусків із відповідним output. Старі артефакти відображають schema/код свого запуску.

## API та перевірки

Локальний API запущений на `http://127.0.0.1:8000`; `/health`, `/jobs`, `/runs`,
`/records`, `/openapi.json` відповіли HTTP 200. [Swagger UI](http://127.0.0.1:8000/docs)
доступний, доки процес сервера працює. POST запуску job очікує завершення pipeline.

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/integration/check_postgres.py
uv run python tests/integration/check_live_olx.py
# Повторити acceptance без нового crawling, використовуючи локальні snapshots/cache:
uv run python tests/integration/check_live_olx.py --replay-run data/runs/a25af68253144721aba23ba8915f3977
```

Окрема PostgreSQL-перевірка виконана на Compose PostgreSQL і пройшла JSONB roundtrip,
idempotent upsert, історію лише змінених даних, assessments і run history.
Її повторний запуск потребує локальної БД; звичайні тести не роблять платних AI-запитів.
Незалежна фінальна перевірка: 217 pytest-тестів, Ruff lint/format, source distribution
і wheel пройшли. `check_live_olx.py` запускається лише явно й потребує OpenRouter-ключа;
він перевіряє унікальність записів, релевантні пристрої, ціни, HTML evidence і Parquet.
