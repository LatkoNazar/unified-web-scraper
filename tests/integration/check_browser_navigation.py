"""Explicit real Chromium check of local-only browser actions; no AI calls.

Run: uv run python tests/integration/check_browser_navigation.py
Requires the browser extra and installed Playwright Chromium. The HTTP fixture is
bound to loopback and intentionally contains ordinary visible catalog content.
"""

import asyncio
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.navigation.inventory import InventoryBuilder
from unified_web_parser.providers.crawl4ai import Crawl4AIFetcher

INTRO = """<p>This public catalog lists used computers and books with clearly displayed prices.
Visitors can reveal the search controls, select a condition filter, search the catalog,
load additional product cards, or open an individual item. All controls in this local
test fixture are read-only navigation and produce visible results for verification.</p>"""

CATALOG = """<!doctype html><html><head><title>Public catalog</title></head><body><main>
<h1>Computer and book catalog</h1>INTRO
<button id="reveal" type="button"
 onclick="document.getElementById('search-controls').hidden=false">Search catalog</button>
<section id="search-controls" hidden><form method="get" action="/results">
 <select name="condition" aria-label="Condition"><option value="all">All conditions</option>
 <option value="used">Used devices</option></select>
 <input type="search" name="q" aria-label="Search products">
 <button type="submit">Search products</button></form></section>
<section id="items"><article><h2><a href="/products/one">First used laptop</a></h2>
<p>Working laptop with published specifications, tested keyboard and display. Price 100 UAH.</p>
</article></section>
<button id="more" type="button" onclick="setTimeout(() => {
const item=document.createElement('article');item.id='second-product';
item.innerHTML='<h2>Second used laptop</h2><p>Additional available product, price 200 UAH.</p>';
document.getElementById('items').append(item);},150)">Load more products</button>
<div style="height:1800px">Scroll the public catalog for additional products below.</div>
</main><script>window.addEventListener('scroll',()=>{
if(window.scrollY>100 && !document.getElementById('scrolled')) {
const p=document.createElement('p');p.id='scrolled';p.textContent='Scroll revealed more content';
document.querySelector('main').append(p);}});</script></body></html>""".replace("INTRO", INTRO)

JS_SEARCH = """<!doctype html><html><head><title>Client search</title></head><body><main>
<h1>Client-side product search</h1>INTRO
<input type="search" name="q" aria-label="Search products" id="query">
<section id="results">No search has been submitted yet.</section>
<output id="key-events"></output>
<script>const keyEvents=[];
for (const type of ['keydown','keyup']) {
document.getElementById('query').addEventListener(type,event=>{
keyEvents.push({type:event.type,key:event.key,code:event.code});
document.getElementById('key-events').textContent=JSON.stringify(keyEvents);
if(event.type==='keydown' && event.key==='Enter') document.getElementById('results').textContent=
'Public search result for '+event.target.value;});}</script></main></body></html>
""".replace("INTRO", INTRO)

CLEAN_SEARCH = (
    CATALOG.replace('<option value="used">', '<option selected value="used">')
    .replace(
        '<form method="get" action="/results">',
        """<form method="get" action="/results">
    <input type="hidden" name="mode" value="catalog"><input type="hidden" name="lang" value="uk">
    <input type="hidden" name="csrf_token" value="fixture-token">
    <input type="hidden" name="sort" value="price">
    <input type="number" name="min_price" value="100">
    <label for="accept">Accept privacy terms</label>
    <input id="accept" type="checkbox" name="accepted" value="yes" checked>""",
    )
    .replace(
        "<body>",
        """<body>
<select name="locale" aria-label="Interface language"><option selected value="uk">Ukrainian</option>
<option value="en">English</option></select>
<form id="newsletter"><input type="email" name="email">
<input name="subscribe" type="checkbox" checked></form>""",
    )
    .replace(
        "</body>",
        """<script>
document.querySelector('form[action="/results"]').addEventListener('submit',event=>{
 for(const [name,value] of Object.entries({
  locale_was:document.querySelector('[name="locale"]').value,
  newsletter_was:String(document.querySelector('[name="subscribe"]').checked)
 })) {const input=document.createElement('input');input.type='hidden';input.name=name;
 input.value=value;event.target.append(input);}
});</script></body>""",
    )
)


class Handler(BaseHTTPRequestHandler):
    submitted: list[dict[str, list[str]]] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/catalog":
            content = CATALOG
        elif parsed.path == "/js":
            content = JS_SEARCH
        elif parsed.path == "/clean":
            content = CLEAN_SEARCH
        elif parsed.path == "/results":
            query = parse_qs(parsed.query)
            self.submitted.append(query)
            content = (
                "<html><head><title>Public search results</title></head><body><main>"
                "<h1>Matching catalog items</h1>"
                + INTRO
                + "<p id='submitted'>"
                + html.escape(str(query))
                + "</p></main></body></html>"
            )
        else:
            content = (
                "<html><head><title>Individual product</title></head><body><main>"
                "<h1>First used laptop</h1>"
                + INTRO
                + "<p>Product details and source evidence are retained separately.</p>"
                "</main></body></html>"
            )
        payload = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def choose(page, kind, *, label=None, option=None, allow_filters=True):
    return next(
        action
        for action in InventoryBuilder(allow_filters=allow_filters).build(page).actions
        if action.kind == kind
        and (label is None or label in action.label)
        and (option is None or action.option_value == option)
    )


async def check(base: str):
    checks = 0
    async with Crawl4AIFetcher(
        timeout=10, retries=0, delay=0, render_delay=0.2, allow_filters=True
    ) as fetcher:
        catalog = await fetcher.fetch(base + "/catalog")
        initial_session = fetcher._page_sessions[catalog.page_id]
        # Separate branch tab must not destroy the original list page or its controls.
        detail = await fetcher.interact(catalog, choose(catalog, "open"))
        assert "/products/one" in str(detail.url)
        assert fetcher._page_sessions[detail.page_id] != initial_session
        checks += 1
        expanded = await fetcher.interact(catalog, choose(catalog, "click", label="Search catalog"))
        assert (
            not BeautifulSoup(expanded.html, "html.parser")
            .select_one("#search-controls")
            .has_attr("hidden")
        )
        checks += 1
        try:
            await fetcher.interact(catalog, choose(catalog, "click", label="Search catalog"))
        except FetchError as exc:
            assert "Stale" in str(exc)
        else:
            raise AssertionError("Earlier mutated snapshot unexpectedly remained executable")
        checks += 1
        filtered = await fetcher.interact(expanded, choose(expanded, "select", option="used"))
        soup = BeautifulSoup(filtered.html, "html.parser")
        assert soup.select_one('select[name="condition"] option[selected]')["value"] == "used"
        assert not any(
            action.kind == "select" and action.option_value == "used"
            for action in InventoryBuilder().build(filtered).actions
        )
        checks += 1
        query = 'MacBook "Air" & books'
        results = await fetcher.interact(filtered, choose(filtered, "search"), query)
        assert "/results" in str(results.url)
        assert Handler.submitted[-1]["q"] == [query]
        assert Handler.submitted[-1]["condition"] == ["used"]
        assert fetcher.interaction_metadata[results.page_id]["http_status_observed"] is False
        checks += 1
        # The branch's detail page remains retained while its list's state evolves.
        assert detail.page_id in fetcher._page_sessions
        checks += 1
        dynamic = await fetcher.fetch(base + "/catalog")
        more = await fetcher.interact(dynamic, choose(dynamic, "click", label="Load more"))
        assert 'id="second-product"' in more.html
        checks += 1
        scrolled = await fetcher.interact(more, choose(more, "scroll"))
        assert 'id="scrolled"' in scrolled.html
        viewport = fetcher.interaction_metadata[scrolled.page_id]
        assert viewport["scroll_y"] > 0
        assert viewport["scroll_height"] > viewport["viewport_height"] > 0
        checks += 1
        javascript = await fetcher.fetch(base + "/js")
        submitted = await fetcher.interact(javascript, choose(javascript, "search"), "Python books")
        assert "Public search result for Python books" in submitted.html
        assert BeautifulSoup(submitted.html, "html.parser").select_one("#query")["value"] == (
            "Python books"
        )
        key_events = BeautifulSoup(submitted.html, "html.parser").select_one("#key-events")
        assert json.loads(key_events.get_text()) == [
            {"type": "keydown", "key": "Enter", "code": "Enter"},
            {"type": "keyup", "key": "Enter", "code": "Enter"},
        ]
        checks += 1
    async with Crawl4AIFetcher(timeout=10, retries=0, delay=0, render_delay=0.2) as fetcher:
        catalog = await fetcher.fetch(base + "/clean")
        expanded = await fetcher.interact(catalog, choose(catalog, "click", label="Search catalog"))
        submitted = await fetcher.interact(
            expanded, choose(expanded, "search", allow_filters=False), "guitar"
        )
        assert "/results" in str(submitted.url)
        assert Handler.submitted[-1] == {
            "mode": ["catalog"],
            "lang": ["uk"],
            "csrf_token": ["fixture-token"],
            "accepted": ["yes"],
            "q": ["guitar"],
            "locale_was": ["uk"],
            "newsletter_was": ["true"],
        }
        checks += 1
    print(f"Real Chromium local navigation: {checks} checks passed; zero AI requests")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(check(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
