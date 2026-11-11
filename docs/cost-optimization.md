# Оптимізація AI-вартості

На п'яти збережених OLX-сторінках cold replay зменшив **оцінену за токенами** вартість
на **93.704%**: з `$0.00468627` до `$0.00029504`. Це порівняння однакового корпусу,
а не гарантія такої економії на будь-якому сайті. Моделі залишилися Qwen extraction
та native Luna Decisions; скоротилися зайві виклики й обсяг відповіді.

## Що працює в коді

- `routing: hybrid` використовує `can_route_structured`: рівно один JSON-LD кандидат,
  непорожній `description`, явний `url`/`@id`, що збігається з canonical page URL.
  Суперечливі identities, кілька сутностей або брак опису передають routing до Decisions.
- `reasoning: false` передає chat-провайдеру `reasoning.enabled=false`.
  `null` не надсилає override; це не команда native Decisions і не автоматичний вибір іншої моделі.
  Параметр описаний у [OpenRouter Reasoning Tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
- `sparse_enrichment: true` просить лише missing/null поля та conflict fields у JSON-LD.
  Відомі source facts зберігаються локально, conflict lists об'єднуються, повний запис
  перевіряється оригінальною JSON Schema. References, open objects, composition і
  cross-field constraints вимикають projection і використовують full-schema extraction.
- Extraction і native routing cache залежать від endpoint, моделі, prompt/questions, schema,
  source URL й evidence, а не від мінливих snapshot/candidate IDs. Native routing хешує весь block content,
  включно з частиною, яку обрізано в prompt. Зміна суттєвого input створює cache miss.
  Альтернативний chat-router включає candidate IDs у запит, тому їх зміна може дати cache miss.

Обидва клієнти ділять `max_calls`; retries витрачають бюджет, local cache hits — ні.
Неоднозначні сторінки зберігають AI routing, тому 0 routing calls на цьому корпусі
не означає, що routing-модель більше не використовується.

## Виміряні cold і warm режими

| Метрика | Baseline | Оптимізований cold | Окремий warm |
| --- | --- | --- | --- |
| Run ID | `9874455546d04ebf89af533504984a6e` | `2ae1c7fe189f4e759e252bf77c717574` | `c09d7edf12474756815b454e4c041034` |
| AI calls | 10: routing 5 + extraction 5 | 5: extraction | 0 |
| Local cache hits | 0 | 0 | 5 |
| Input / output tokens | 30 883 / 20 026 | 7 460 / 548 | 0 / 0 |
| Estimated AI cost, USD | 0.00468627 | 0.00029504 | 0 нових AI-запитів |
| Provider-reported cost, USD | невідомо | 0.0003044885 | 0 нових AI-запитів |
| Зменшення estimated cost | — | 93.704% | 100% лише для повтору через local cache |

Cold-запуск не використовував cache і вже включав нову source/schema cache identity.
Наступний окремий warm replay підтвердив повторне використання тих самих 5 extraction
відповідей. Ці два результати не змішуються: економія cold не пояснюється cache hits.

Файли з результатами:

- [Baseline summary](../data/runs/9874455546d04ebf89af533504984a6e/summary.json)
- [Cold cost comparison](../data/runs/2ae1c7fe189f4e759e252bf77c717574/cost-comparison.json)
- [Warm cost comparison](../data/runs/c09d7edf12474756815b454e4c041034/cost-comparison.json)
- [Quality expectations](../tests/fixtures/benchmarks/olx_macbooks_expected.json)

Артефакти `data/` локальні й не входять у Git. Обидва optimized runs завершилися `success`:
**52 source-reviewed invariants**, ті самі **5 record IDs**, **3 релевантні пристрої**,
без змін immutable полів. Порівняння також показує допустимі відмінності: nominal `15`
у baseline став `15.3`, а формулювання SSD-конфлікту 512/500 змінилося. Це не обіцянка
побайтової однаковості всіх полів і не перевірка довільного нового домену.

## Оцінка та metadata

Benchmark використовує зафіксовані в script base token rates без fees; це **estimate**,
а не рахунок акаунта. Джерела ставок: [Qwen3.7 Flash](https://openrouter.ai/qwen/qwen3.7-flash)
і [GPT-6 Luna Decisions](https://openrouter.ai/openai/gpt-6-luna-decisions).
Baseline не містить повних billing metadata, тому його actual
cost невідомий. Для optimized cold provider повідомив `$0.0003044885`; різниця з
token estimate не приховується і не використовується для заяви про actual billing savings.

За тієї самої структури п'яти записів extrapolation дає приблизно **$0.059 / 1000 записів**
за base-rate estimate. Інші сторінки, складніша схема, native routing, retries, тарифи
чи довші відповіді змінять результат. Crawling/browser/storage у цю AI-оцінку не входять.

`ai.stages.*.cost_usd` і `reasoning_tokens` присутні лише коли metadata повні для всіх
фактичних attempts цього клієнта. Відсутність поля означає **unknown**, не нуль.
Для cache-only replay `cost_usd=0` означає відсутність нових provider-запитів;
це не повертає кошти за первинне створення cache.

Для live UI додатково публікуються `known_cost_usd`, `cost_complete`,
`cost_pending_calls` та `in_flight_calls`. Уже відома сума не зникає, якщо наступна
спроба не повернула usage. Події usage надходять після кожної відповіді,
а неповні дані мають окрему позначку. Ручна зупинка та вичерпання кредитів
зберігають готові записи: [вартість і зупинка в UI](dashboard.md#вартість-і-зупинка).

## Відтворення та керування

```powershell
uv run python scripts/benchmark_costs.py --config configs/jobs/macbooks-olx.yaml --baseline-run data/runs/9874455546d04ebf89af533504984a6e --data-dir data/benchmarks/cost-check
```

Script читає збережені snapshots, не відкриває сайти, порівнює з checked-in expectations
і записує `cost-comparison.json` у новий run. Потрібні локальний baseline corpus та
OpenRouter key; **cache miss викликає платний AI-запит**. Новий порожній `data-dir`
дає cold cache; повтор незмінного запиту в тому самому каталозі може бути warm.
Script не видаляє cache. Якщо schema відрізняється від baseline, порівняння зупиняється.

У job: `ai.reasoning: false`, `ai.sparse_enrichment: true`, `ai.routing: hybrid`,
`ai.max_output_tokens` (типово 6000). CLI overrides:

```powershell
uv run unified-parser run configs/jobs/macbooks-olx.yaml --reasoning off --routing hybrid --max-output-tokens 6000
# Явно порівняти full enrichment або повністю AI routing:
uv run unified-parser run configs/jobs/macbooks-olx.yaml --full-enrichment --routing ai --reasoning auto
```

`--reasoning auto|on|off` відповідає `null|true|false`. `--full-enrichment` вимикає sparse
projection; `--routing ai` примусово залучає routing-модель. Зменшення token limit
може обрізати JSON, тому його зміни слід перевіряти тим самим quality gate.
