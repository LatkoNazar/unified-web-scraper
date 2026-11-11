# Аудит API та залежностей

Перевірка від 23 жовтня 2026 року охоплює Python-код, браузерний JavaScript,
запити OpenRouter та прямі runtime/browser/dev-залежності проєкту.

## Виправлені deprecated-використання

- Chat completions і web search передають `max_completion_tokens` замість
  `max_tokens`. Ліміт не змінився. Підстава: [OpenRouter Chat API](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request).
- Браузерний пошук через Enter використовує `keydown` і `keyup` з `key` та `code`.
  Видалені `keypress`, `keyCode` та `which`.
  Підстава: [KeyboardEvent](https://developer.mozilla.org/en-US/docs/Web/API/KeyboardEvent),
  [keyCode](https://developer.mozilla.org/en-US/docs/Web/API/KeyboardEvent/keyCode).
- Web search уже працює через Server Tool `openrouter:web_search`.
  Deprecated Web Search Plugin і `:online` не використовуються;
  [OpenRouter рекомендує Server Tool](https://openrouter.ai/docs/guides/features/plugins/overview).

Зміна token-параметра змінює hash chat-запиту. Пошук має окремий контракт кешу
`web-search.v4-server-tool`. Старі записи кешу залишаються на диску, але не підміняють
новий контракт; новий cold-запит потребує inference, наступний warm-запит — ні.
Історичні trace JSON зберігають справжні старі payload, включно з `max_tokens`.

## Оновлення залежностей

| Залежність | До | Після |
| --- | --- | --- |
| FastAPI | 0.159.1 | 0.161.1 |
| PyArrow | 24.0.0 | 26.0.0 |
| Ruff | 0.17.1 | 0.17.2 |

Перевірені [зміни FastAPI](https://fastapi.tiangolo.com/release-notes/) та
[PyArrow 26](https://arrow.apache.org/blog/2026/10/08/26.0.0-release/).
PyArrow 26 підтримує наш Python 3.12 і сумісний із встановленим NumPy 2.x.
Експорт використовує `pa.array`, `pa.table` та `pq.write_table`, без Feather чи
інших deprecated API. Обмеження PyArrow розширене до `<27`, версії зафіксовані в `uv.lock`.

Усі 15 прямих залежностей, включно з browser/dev, перевірені через метадані PyPI:
встановлені версії відповідають поточним stable-релізам, не відкликані (`yanked`)
і не мають класифікатора `Inactive`. Транзитивні залежності залишаються в межах
сумісності своїх батьківських бібліотек; номер новішого релізу сам по собі
не означає deprecated-використання.

## Очищення браузерних сесій

`crawler.crawler_strategy.kill_session` залишається документованим публічним API
[Crawl4AI session management](https://docs.crawl4ai.com/advanced/session-management/).
Crawl4AI 0.9.5 друкує для нього logger-попередження про auto-kill, але виклик
і далі закриває сторінку та звільняє сесію. Це не Python `DeprecationWarning`.
Очищення збережене для обмеження кількості відкритих вкладок; повідомлення SDK
не приховується й не замінюється зверненням до недокументованого нижчого рівня.

## Постійні перевірки

```powershell
uv sync --locked --dev --extra browser
uv pip check
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python tests/integration/check_browser_navigation.py
```

Pytest трактує `DeprecationWarning`, `PendingDeprecationWarning` і `FutureWarning`
як помилки. Винятків для приховування цих попереджень немає. Ruff перевіряє
модернізацію Python (`UP`), неекрановані regex в assertions (`RUF043`) та зайві
`noqa` (`RUF100`). Перевірки browser запускаються проти локального HTTP-сайту,
включно з фактичною послідовністю Enter-подій, без платного AI.
