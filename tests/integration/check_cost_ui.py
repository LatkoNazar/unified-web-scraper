"""Cost, controlled stops, and responsive actions in the real dashboard.

Called by check_dashboard.py; all provider responses are local browser fixtures.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.async_api import expect


async def _scenario(browser, base: str, output: Path, width: int, *, cancel=False):
    context = await browser.new_context(viewport={"width": width, "height": 844})
    await context.add_init_script(
        "window.testStreams=[];window.EventSource=class extends EventTarget{"
        "constructor(){super();testStreams.push(this);"
        "setTimeout(()=>this.onopen?.(new Event('open')),0)}close(){}}"
    )
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    state = {
        "run_id": "cost-check",
        "question": "MacBook M5: 16/512, 24/1TB — як новий, мало циклів",
        "status": "running",
        "stage": "crawling",
        "started_at": datetime.now(UTC).isoformat(),
        "revision": 1,
        "records": [
            {
                "record_id": str(i),
                "data": {
                    "title": f"MacBook Air M5 {i} — розпакований",
                    "ram_gb": 24,
                    "storage_gb": 1024,
                    "battery_cycles": i,
                    "mdm_status": "unknown",
                },
            }
            for i in range(1, 10)
        ],
        "counters": {"records_found": 9, "pages_fetched": 9, "pages_attempted": 10},
        "limits": {"max_pages": 30, "max_records": 50, "max_calls": 30},
        "ai": {
            "calls": 1,
            "known_cost_usd": 0,
            "cost_complete": False,
            "cost_pending_calls": 1,
            "in_flight_calls": 1,
        },
        "errors": [],
        "warnings": [],
        "logs": [],
    }
    await page.route("**/jobs", lambda route: route.fulfill(json=[]))
    await page.route("**/runs?limit=*", lambda route: route.fulfill(json=[]))
    await page.route("**/runs/cost-check", lambda route: route.fulfill(json=state))
    cancelled = []

    async def cancel_request(route):
        cancelled.append(route.request.method)
        state.update(status="cancelling", revision=state["revision"] + 1)
        await route.fulfill(status=202, json=state)

    await page.route("**/runs/cost-check/cancel", cancel_request)
    await page.goto(base + "/?run=cost-check")
    await expect(page.locator("#cost-summary")).to_be_visible()
    await expect(page.locator("#cost-value")).to_have_text("—")
    await expect(page.locator("#cancel-button")).to_be_visible()
    # A new draft must not hide an ongoing run's spending or stop action.
    await page.locator("#new-search").click()
    await expect(page.locator("#query")).to_be_focused()
    await expect(page.locator("#cancel-button")).to_be_visible()
    await expect(page.locator(".record-card")).to_have_count(9)
    assert await page.evaluate("testStreams.length") == 1

    async def deliver(**update):
        state.update(update)
        state["revision"] += 1
        await page.evaluate(
            "state=>testStreams[0].dispatchEvent("
            "new MessageEvent('snapshot',{data:JSON.stringify(state)}))",
            state,
        )

    await deliver(
        ai={
            "calls": 1,
            "cost_usd": 0.0025,
            "known_cost_usd": 0.0025,
            "cost_complete": True,
            "cost_pending_calls": 0,
            "in_flight_calls": 0,
        }
    )
    await expect(page.locator("#cost-value")).to_contain_text("0.0025")
    await deliver(
        ai={
            "calls": 3,
            "known_cost_usd": 0.0075,
            "cost_complete": False,
            "cost_pending_calls": 1,
            "in_flight_calls": 1,
        }
    )
    await expect(page.locator("#cost-value")).to_contain_text("0.0075")
    assert (await page.locator("#cost-note").text_content()).strip()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
    await page.screenshot(path=str(output / f"live-{width}.png"), full_page=True)
    await page.evaluate("scrollTo(0, document.body.scrollHeight)")
    cost_box = await page.locator("#cost-summary").bounding_box()
    cancel_box = await page.locator("#cancel-button").bounding_box()
    assert cost_box and 0 <= cost_box["y"] < 150, cost_box
    assert cancel_box and 0 <= cancel_box["y"] < 150, cancel_box
    assert cost_box["x"] + cost_box["width"] <= width + 1
    assert cancel_box["x"] + cancel_box["width"] <= width + 1
    overlap_x = min(cost_box["x"] + cost_box["width"], cancel_box["x"] + cancel_box["width"]) - max(
        cost_box["x"], cancel_box["x"]
    )
    overlap_y = min(
        cost_box["y"] + cost_box["height"], cancel_box["y"] + cancel_box["height"]
    ) - max(cost_box["y"], cancel_box["y"])
    assert overlap_x <= 0 or overlap_y <= 0, (cost_box, cancel_box)
    if cancel:
        await page.locator("#cancel-button").click()
        await expect(page.locator("#cancel-button")).to_be_disabled()
        assert cancelled == ["POST"]
    await deliver(
        status="cancelled" if cancel else "stopped",
        stage="finished",
        stop_reason="user_cancelled" if cancel else "credits_exhausted",
        stop_message="Зупинено користувачем."
        if cancel
        else "Недостатньо кредитів OpenRouter. Зібрані записи збережено.",
        finished_at=datetime.now(UTC).isoformat(),
        persisted=True,
        records_persisted=9,
        ai={
            "calls": 3,
            "known_cost_usd": 0.0075,
            "cost_complete": False,
            "cost_pending_calls": 1,
            "in_flight_calls": 0,
        },
    )
    await expect(page.locator("#cancel-button")).to_be_hidden()
    await expect(page.locator("#cost-value")).to_contain_text("0.0075")
    await expect(page.locator("#download-button")).to_be_enabled()
    assert await page.locator(".issue.error").count() == 0
    if not cancel:
        assert "кредит" in (await page.locator("#run-stop-message").text_content()).lower()
    await page.evaluate("scrollTo(0,0)")
    await page.screenshot(
        path=str(output / f"stopped-{width}{'-manual' if cancel else ''}.png"), full_page=True
    )
    assert not errors, errors
    await context.close()
    return {
        "width": width,
        "cancel": cancel,
        "cost_accumulates": True,
        "unknown_not_zero": True,
        "sticky_stop": True,
        "no_horizontal_overflow": True,
        "javascript_errors": errors,
    }


async def cost_stop_check(browser, base: str, output: Path) -> list[dict]:
    results = [await _scenario(browser, base, output, width) for width in (1440, 390, 320)]
    results.append(await _scenario(browser, base, output, 390, cancel=True))
    (output / "cost-stop-summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results
