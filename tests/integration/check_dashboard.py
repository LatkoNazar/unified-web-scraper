"""Actual dashboard + HTTP pipeline + SSE in Chromium, entirely on loopback.

Run: uv run python tests/integration/check_dashboard.py
Requires the browser extra and installed Chromium; no inference or web search.
"""

import asyncio
import json
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from unified_web_parser.interfaces.api import create_app

UNTRUSTED_TITLE = "<script>window.sourceExecuted=true</script> Перший запис"


def fixture_server(release: threading.Event) -> ThreadingHTTPServer:
    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/image.svg":
                body = (
                    b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400">'
                    b'<rect width="640" height="400" fill="#e8eefb"/>'
                    b'<rect x="155" y="75" width="330" height="210" rx="14" fill="#314768"/>'
                    b'<rect x="168" y="88" width="304" height="183" rx="5" fill="#a9c4f0"/>'
                    b'<path d="M115 285h410l-30 25H145z" fill="#93a6c3"/></svg>'
                )
                mime = "image/svg+xml"
            elif path in {"/one", "/two"}:
                if path == "/two":
                    release.wait(timeout=45)
                entity = {
                    "@context": "https://schema.org",
                    "@type": "Article",
                    "headline": UNTRUSTED_TITLE if path == "/one" else "Другий запис",
                    "url": f"http://{self.headers['Host']}{path}",
                    "description": (
                        "Публічний опис із локального джерела для перевірки живого збору."
                    ),
                }
                if path == "/one":
                    entity["image"] = {"@type": "ImageObject", "contentUrl": "/image.svg"}
                body = (
                    '<!doctype html><html><head><meta charset="utf-8"></head><body><main>'
                    '<script type="application/ld+json">'
                    + json.dumps(entity, ensure_ascii=False).replace("</", r"<\/")
                    + "</script></main></body></html>"
                ).encode()
                mime = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", 0), Source)


def write_profile(directory: Path) -> None:
    directory.mkdir()
    (directory / "schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "x-entity-types": ["Article"],
                "additionalProperties": False,
                "required": ["title", "url"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": ["string", "null"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "web.yaml").write_text(
        "name: dashboard-check\nschema_path: schema.json\n"
        "discovery: {enabled: true}\nsearch: articles\noutputs: [jsonl, parquet]\n"
        "crawl: {concurrency: 1, delay: 0, timeout: 60}\n"
        "navigation: {mode: rules}\nai: {enabled: false, routing: rules}\n",
        encoding="utf-8",
    )


async def snapshot_ordering_check(browser, base: str) -> None:
    """Delay/order the browser transport, without exposing test hooks in the app."""
    context = await browser.new_context()
    await context.add_init_script(
        """window.testStreams=[];
        window.EventSource=class extends EventTarget {
          constructor(){super();window.testStreams.push(this);
            setTimeout(()=>this.onopen?.(new Event('open')),0);}
          close(){}
        };"""
    )
    page = await context.new_page()
    record = {"record_id": "one", "data": {"title": "Перший"}, "img_url": None}
    initial = {
        "run_id": "client-order",
        "question": "Перевірка порядку подій",
        "status": "running",
        "stage": "crawling",
        "revision": 1,
        "records": [record],
        "counters": {"records_found": 1, "pages_fetched": 1, "pages_attempted": 1},
        "limits": {"max_pages": 10},
    }

    async def snapshot(route):
        await route.fulfill(json=initial)

    await page.route("**/runs/client-order", snapshot)
    await page.goto(base + "?run=client-order")
    await page.wait_for_function("window.testStreams.length === 1")
    later = {
        **initial,
        "revision": 3,
        "records": [record, {**record, "record_id": "two", "data": {"title": "Другий"}}],
        "counters": {"records_found": 2, "pages_fetched": 2, "pages_attempted": 2},
    }

    async def deliver(state):
        await page.evaluate(
            "state=>testStreams[0].dispatchEvent("
            "new MessageEvent('snapshot',{data:JSON.stringify(state)}))",
            state,
        )

    await deliver(later)
    await deliver({**initial, "revision": 2})
    assert await page.locator(".record-card").count() == 2
    await deliver({**later, "status": "success", "revision": 4, "persisted": True})
    await deliver({**initial, "revision": 3})
    await deliver({**initial, "run_id": "another-run", "revision": 100})
    assert await page.locator("#run-status").text_content() == "Завершено"
    assert await page.locator(".record-card").count() == 2
    await context.close()


async def browser_check(base: str, source: str, release: threading.Event, output: Path):
    from check_cost_ui import cost_stop_check
    from playwright.async_api import async_playwright, expect

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1100})
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(base)
        await expect(page.locator("#connection")).to_contain_text("Готовий")
        await page.screenshot(path=str(output / "empty-desktop.png"), full_page=True)
        await page.locator("#query").fill("Статті: назва, опис і посилання")
        await page.locator(".advanced summary").click()
        await page.locator("#sources").fill(source + "/one\n" + source + "/two")
        await page.locator("#backend").select_option("http")
        await page.locator("#ai-enabled").uncheck()
        await page.locator("#max-calls").fill("0")
        await page.locator("#start-button").click()
        await expect(page.locator(".record-card")).to_have_count(1, timeout=15000)
        await expect(page.locator("#run-status")).to_have_text("Збираємо дані")
        await expect(page.locator("#dataset-note")).to_contain_text("Попередній перегляд")
        await expect(page.locator(".record-title")).to_have_text(UNTRUSTED_TITLE)
        assert await page.evaluate("window.sourceExecuted") is None
        await expect(page.locator(".record-media img")).to_be_visible()
        await page.wait_for_function(
            "document.querySelector('.record-media img')?.naturalWidth > 0"
        )
        run_id = await page.evaluate("localStorage.getItem('unified:selected-run')")
        snapshot = await (await page.request.get(base + f"/runs/{run_id}")).json()
        assert snapshot["counters"]["records_found"] == 1
        assert snapshot["persisted"] is False and snapshot["records_persisted"] == 0
        assert snapshot["records"][0]["img_url"] == source + "/image.svg"
        assert snapshot["records"][0]["data"].get("img_url") is None
        await page.screenshot(path=str(output / "live-desktop.png"), full_page=True)
        # Reconnect while the second HTTP response is deliberately still pending.
        await page.reload()
        await expect(page.locator(".record-card")).to_have_count(1)
        await expect(page.locator("#run-status")).to_have_text("Збираємо дані")
        release.set()
        await expect(page.locator("#run-status")).to_have_text("Завершено", timeout=15000)
        await expect(page.locator(".record-card")).to_have_count(2)
        await expect(page.locator("#dataset-note")).to_have_text(
            "Збережений датасет цього запуску."
        )
        await expect(
            page.locator(".record-card").nth(1).locator(".image-placeholder")
        ).to_be_visible()
        await page.locator(".record-card").first.locator("button").click()
        await expect(page.locator("#record-dialog")).to_be_visible()
        await expect(page.locator("#detail-title")).to_have_text(UNTRUSTED_TITLE)
        await page.locator("#close-dialog").click()
        await page.locator("#result-filter").fill("Другий")
        await expect(page.locator(".record-card")).to_have_count(1)
        await page.locator("#result-filter").fill("нічого-такого-немає")
        await expect(page.locator("#no-matches")).to_be_visible()
        await page.locator("#result-filter").fill("")
        async with page.expect_download() as download_info:
            await page.locator("#download-button").click()
        download = await download_info.value
        downloaded = json.loads(Path(await download.path()).read_text(encoding="utf-8"))
        assert len(downloaded) == 2 and downloaded[0]["img_url"] == source + "/image.svg"
        await page.locator("#new-search").click()
        await expect(page.locator(".record-card")).to_have_count(0)
        await page.locator(".history-item").first.click()
        await expect(page.locator(".record-card")).to_have_count(2)
        await page.goto(base + f"?run={run_id}")
        await expect(page.locator(".record-card")).to_have_count(2)
        await page.screenshot(path=str(output / "complete-desktop.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await expect(page.locator("#start-button")).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.screenshot(path=str(output / "complete-mobile.png"), full_page=True)
        final = await (await page.request.get(base + f"/runs/{run_id}")).json()
        assert final["persisted"] and final["records_persisted"] == 2
        assert final["errors"] == [] and final["warnings"] == []
        assert final["ai"] == {} and not errors
        await snapshot_ordering_check(browser, base)
        await typed_dataset_check(browser, base, output)
        await cost_stop_check(browser, base, output)
        await browser.close()
        return {
            "run_id": run_id,
            "records": 2,
            "live_before_commit": True,
            "reload_reconnect": True,
            "image_loaded": True,
            "xss_inert": True,
            "history": True,
            "filter_and_download": True,
            "responsive": True,
            "stale_snapshot_protection": True,
            "typed_filters_table_and_live_metadata": True,
            "cumulative_cost_stop_and_sticky_controls": True,
            "javascript_errors": errors,
            "ai_requests": 0,
        }


async def typed_dataset_check(browser, base: str, output: Path) -> None:
    """Verify actual controls from typed metadata, including late SSE values and focus."""
    from playwright.async_api import expect

    from unified_web_parser.interfaces.presentation import build_presentation

    schema = {
        "properties": {
            "title": {"type": "string"},
            "price": {"type": ["number", "null"], "title": "Ціна", "x-unit": "UAH"},
            "cycles": {"type": ["integer", "null"], "title": "Цикли"},
            "mdm": {"type": ["boolean", "null"], "title": "MDM"},
            "category": {"type": "string", "title": "Категорія"},
            "published": {"type": "string", "format": "date", "title": "Дата"},
        },
        "x-display": {"columns": ["title", "price", "cycles", "mdm", "category"]},
    }
    records = [
        {
            "record_id": "a",
            "data": {
                "title": "A",
                "price": 0,
                "cycles": 0,
                "mdm": False,
                "category": "Air",
                "published": "2026-10-21",
            },
        },
        {
            "record_id": "b",
            "data": {
                "title": "B",
                "price": 100,
                "cycles": 8,
                "mdm": True,
                "category": "Pro",
                "published": "2026-10-22",
            },
        },
        {
            "record_id": "c",
            "data": {
                "title": "C",
                "price": None,
                "cycles": None,
                "mdm": None,
                "category": "Air",
                "published": "2026-10-23",
            },
        },
    ]

    def snapshot(revision, current):
        # A manual schema may use an explicit title role on a different key.
        schema["properties"]["headline"] = {
            "type": "string",
            "x-display-role": "title",
            "title": "Назва",
        }
        schema["properties"].pop("title", None)
        for record in current:
            if "title" in record["data"]:
                record["data"]["headline"] = record["data"].pop("title")
        return {
            "run_id": "typed-dataset",
            "question": "Пристрої та їх характеристики",
            "status": "running",
            "stage": "extraction",
            "revision": revision,
            "started_at": "2026-10-23T00:00:00Z",
            "records": current,
            "counters": {"records_found": len(current)},
            "limits": {"max_pages": 10},
            "presentation": build_presentation(schema, current),
        }

    context = await browser.new_context(viewport={"width": 1440, "height": 1100})
    await context.add_init_script(
        "window.testStreams=[];window.EventSource=class extends EventTarget {"
        "constructor(){super();testStreams.push(this);}close(){}};"
    )
    page = await context.new_page()
    await page.route(
        "**/runs/typed-dataset", lambda route: route.fulfill(json=snapshot(1, records))
    )
    await page.goto(base + "?run=typed-dataset")
    await expect(page.locator(".record-card")).to_have_count(3)
    await page.locator(".dataset-options").first.locator("summary").click()
    price = page.locator('.facet-control[data-field="price"]')
    await price.locator('[data-bound="max"]').fill("0")
    await expect(page.locator("#result-count")).to_have_text("1 / 3")
    await expect(page.locator(".record-title")).to_have_text("A")
    await page.locator("#reset-filters").click()
    mdm = page.locator('.facet-control[data-field="mdm"]')
    await mdm.locator(".facet-values").select_option("false")
    await expect(page.locator(".record-title")).to_have_text("A")
    await mdm.locator(".facet-missing").select_option("missing")
    await expect(page.locator(".record-title")).to_have_text("C")
    await page.locator("#reset-filters").click()
    maximum = price.locator('[data-bound="max"]')
    await maximum.fill("80")
    await maximum.focus()
    later = records + [
        {
            "record_id": "d",
            "data": {
                "title": "D",
                "price": 50,
                "cycles": 2,
                "mdm": False,
                "category": "<img src=x onerror=alert(1)>",
                "published": "2026-10-23",
            },
        }
    ]
    await page.evaluate(
        "state=>testStreams[0].dispatchEvent("
        "new MessageEvent('snapshot',{data:JSON.stringify(state)}))",
        snapshot(2, later),
    )
    await expect(maximum).to_be_focused()
    await expect(maximum).to_have_value("80")
    await expect(page.locator("#result-count")).to_have_text("2 / 4")
    await expect(
        page.locator('.facet-control[data-field="category"] .facet-values option')
    ).to_have_count(4)
    await page.locator("#view-table").click()
    await expect(page.locator("#result-table-wrapper")).to_be_visible()
    await expect(page.locator("#result-table tbody tr")).to_have_count(2)
    await expect(page.locator("#result-table thead")).to_contain_text("Цикли")
    assert await page.locator("#dataset-controls img").count() == 0
    details = page.locator('#result-table tr[data-record-id="a"] button')
    await details.focus()
    await details.evaluate("node=>window.focusedTableDetails=node")
    await page.locator("#result-table thead tr").evaluate("node=>window.stableTableHeader=node")
    progress = {
        **snapshot(3, later),
        "stage": "crawling",
        "logs": [{"message": "Progress without record changes"}],
    }
    await page.evaluate(
        "state=>testStreams[0].dispatchEvent("
        "new MessageEvent('snapshot',{data:JSON.stringify(state)}))",
        progress,
    )
    await expect(details).to_be_focused()
    assert await page.evaluate("document.activeElement===window.focusedTableDetails")
    inserted = [
        {
            "record_id": "e",
            "data": {
                "title": "E",
                "price": 25,
                "cycles": 1,
                "mdm": False,
                "category": "Air",
                "published": "2026-10-20",
            },
        },
        *later,
    ]
    await page.evaluate(
        "state=>testStreams[0].dispatchEvent("
        "new MessageEvent('snapshot',{data:JSON.stringify(state)}))",
        snapshot(4, inserted),
    )
    await expect(page.locator("#result-table tbody tr")).to_have_count(3)
    await expect(details).to_be_focused()
    assert await page.evaluate("document.activeElement===window.focusedTableDetails")
    assert await page.evaluate(
        "document.querySelector('#result-table thead tr')===window.stableTableHeader"
    )
    await page.locator("#reset-filters").click()
    published = page.locator('.facet-control[data-field="published"]')
    await published.locator('[data-bound="min"]').fill("2026-10-23")
    await expect(page.locator("#result-table tbody tr")).to_have_count(2)
    await page.locator("#reset-filters").click()
    await page.locator(".dataset-options").nth(1).locator("summary").click()
    await page.locator('.column-choice[data-field="cycles"] input').uncheck()
    await expect(page.locator("#result-table thead")).not_to_contain_text("Цикли")
    await page.locator("#auto-columns").click()
    await expect(page.locator("#result-table thead")).to_contain_text("Цикли")
    await page.screenshot(path=str(output / "typed-dataset-desktop.png"), full_page=True)
    await page.set_viewport_size({"width": 390, "height": 844})
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.screenshot(path=str(output / "typed-dataset-mobile.png"), full_page=True)
    await context.close()


def main():
    output = Path("data/dashboard-check").resolve()
    output.mkdir(parents=True, exist_ok=True)
    release = threading.Event()
    source = fixture_server(release)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    with tempfile.TemporaryDirectory(prefix="unified-dashboard-") as temporary:
        root = Path(temporary)
        profiles = root / "profiles"
        write_profile(profiles)
        app = create_app(root / "data", profiles)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            with httpx.Client() as client:
                for _ in range(100):
                    if server.started:
                        client.get(base + "/health").raise_for_status()
                        break
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Local dashboard server did not start")
            result = asyncio.run(
                browser_check(base, f"http://127.0.0.1:{source.server_port}", release, output)
            )
            (output / "summary.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print(f"Screenshots: {output}")
        finally:
            release.set()
            server.should_exit = True
            thread.join(timeout=15)
            listener.close()
            source.shutdown()
            source.server_close()
            source_thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("Local dashboard server did not shut down")


if __name__ == "__main__":
    main()
