"""Exercise real HTTP/browser navigation against a local, domain-independent site.

The default run is deterministic and makes no AI calls. --ai explicitly enables
paid model requests using the local .env. Artifacts remain under data/ for review.
"""

import argparse
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from dotenv import load_dotenv

from unified_web_parser.core.config import JobConfig, LoadedJob
from unified_web_parser.pipeline.runner import PipelineRunner

REQUESTS: list[str] = []


def card(prefix: str, number: int) -> str:
    return (
        f'<article><h2><a href="/{prefix}/item/{number}">'
        f"Львів квартира {number}</a></h2><p>Ціна: {number * 1000} UAH</p></article>"
    )


def page(body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Independent housing directory</title></head><body><main>"
        + body
        + "</main></body></html>"
    )


class Site(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        REQUESTS.append(self.path)
        parsed = urlsplit(self.path)
        segments = parsed.path.strip("/").split("/")
        prefix = segments[0]
        values = parse_qs(parsed.query)
        if len(segments) == 3 and segments[1] == "item":
            number = int(segments[2])
            entity = {
                "@context": "https://schema.org",
                "@type": "Apartment",
                "name": f"Львів квартира {number}",
                "url": f"http://{self.headers['Host']}{parsed.path}",
                "description": "Двокімнатна квартира у Львові. Тестові дані.",
                "offers": {"price": number * 1000, "priceCurrency": "UAH"},
            }
            body = page(
                f"<h1>{entity['name']}</h1>"
                f"<p>Ціна оренди: {number * 1000} UAH.</p>"
                "<p>Двокімнатна квартира у Львові. Просторе помешкання із двома окремими "
                "кімнатами, кухнею та балконом. У цьому тестовому каталозі кожне оголошення "
                "має власну сторінку, опис та явну ціну у гривнях.</p>"
                '<script type="application/ld+json">'
                + json.dumps(entity, ensure_ascii=False)
                + "</script>"
            )
        elif parsed.path == "/form/search":
            if values.get("needle") != ["Львів"] or values.get("kind") != ["apartment"]:
                self.send_error(400, "Search did not preserve query and hidden field")
                return
            number = int(values.get("page", ["1"])[0])
            body = page(
                "<h1>Search results: Львів</h1>"
                + (
                    card(prefix, 1)
                    + card(prefix, 2)
                    + '<a rel="next" href="?needle=Львів&amp;kind=apartment&amp;page=2">Next</a>'
                    if number == 1
                    else card(prefix, 3)
                )
            )
        elif prefix == "form" and len(segments) == 1:
            body = page(
                '<form action="/form/search" method="get" role="search">'
                '<input type="hidden" name="kind" value="apartment">'
                '<label>Search apartments<input type="search" name="needle"></label>'
                '<button type="submit">Search</button></form>'
                '<article><a href="/form/unrelated">Featured Tokyo apartment</a></article>'
            )
        elif prefix in {"js", "scroll"} and len(segments) == 1:
            first = card(prefix, 1) + card(prefix, 2)
            last = card(prefix, 3)
            load = (
                '<button id="more" type="button" onclick="more()">Load more results</button>'
                if prefix == "js"
                else '<div style="height:2400px">Scroll to discover more results</div>'
            )
            body = page(
                "<p>Independent housing directory. Search apartments by city, compare the "
                "available listings and open an individual apartment to inspect its published "
                "price and description. Results are loaded after a search. Additional listings "
                "appear when you continue through the result list.</p>"
                '<label for="needle">Search apartments</label>'
                '<input type="search" id="needle" placeholder="Search apartments">'
                '<button type="button" id="search" onclick="search()">Search</button>'
                '<div id="results"></div>'
                + "<script>let searched=false;let expanded=false;"
                + "function search(){if(document.querySelector('#needle').value!=='Львів')"
                + "throw new Error('Wrong query');searched=true;"
                + "document.querySelector('#results').innerHTML="
                + json.dumps("<h1>Search results: Львів</h1>" + first + load, ensure_ascii=False)
                + ";}function more(){if(!searched||expanded)return;expanded=true;"
                + "document.querySelector('#results').insertAdjacentHTML('beforeend',"
                + json.dumps(last, ensure_ascii=False)
                + ");document.querySelector('#more')?.remove();}"
                + (
                    "window.addEventListener('scroll',()=>{if(window.scrollY>500)more();});"
                    if prefix == "scroll"
                    else ""
                )
                + "</script>"
            )
        else:
            self.send_error(404, "Not a selected result")
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


async def check(arguments, origin: str) -> None:
    schema = json.loads(
        Path("configs/schemas/catalog-item.schema.json").read_text(encoding="utf-8")
    )
    data_dir = Path(arguments.data_dir) / uuid4().hex
    outcomes = []
    for scenario in arguments.scenarios:
        start = len(REQUESTS)
        config = JobConfig.model_validate(
            {
                "name": f"navigation-{scenario}",
                "sources": [{"url": f"{origin}/{scenario}/"}],
                "search": "Львів",
                "schema_path": "configs/schemas/catalog-item.schema.json",
                "content_profile": "commerce",
                "currency_aliases": {"UAH": "UAH"},
                "outputs": ["jsonl", "parquet"],
                "crawl": {
                    "backend": "http" if scenario == "form" else "browser",
                    "max_pages": 15,
                    "max_records": 3,
                    "max_depth": 4,
                    "delay": 0,
                    "concurrency": 1,
                },
                "ai": {
                    "enabled": arguments.ai,
                    "max_calls": 40,
                    "reasoning": False,
                    "routing": "hybrid",
                },
                "navigation": {"mode": "ai" if arguments.ai else "rules"},
            }
        )
        job = LoadedJob(
            config=config,
            config_path=Path("scripts/check_navigation.py").resolve(),
            schema_path=Path("configs/schemas/catalog-item.schema.json").resolve(),
            extraction_schema=schema,
        )
        report = await PipelineRunner(job, data_dir=data_dir).run()
        records_path = Path(report.artifacts["directory"]) / "records.jsonl"
        records = (
            [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
            if records_path.is_file()
            else []
        )
        prices = sorted(record["data"]["price"] for record in records)
        passed = report.status == "success" and prices == [1000, 2000, 3000]
        outcomes.append(
            {
                "scenario": scenario,
                "passed": passed,
                "run_id": report.run_id,
                "status": report.status,
                "prices": prices,
                "navigation": report.navigation,
                "ai": report.ai,
                "errors": report.errors,
                "warnings": report.warnings,
                "website_gets": REQUESTS[start:],
            }
        )
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "navigation-check.json"
    path.write_text(json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outcomes, ensure_ascii=False, indent=2))
    print(f"Report: {path.resolve()}")
    if not all(outcome["passed"] for outcome in outcomes):
        raise SystemExit("Navigation check failed; inspect report and navigation.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai", action="store_true", help="Explicitly enable paid AI requests")
    parser.add_argument("--data-dir", default="data/navigation-checks")
    parser.add_argument(
        "--scenarios", nargs="+", choices=["form", "js", "scroll"], default=["form", "js", "scroll"]
    )
    arguments = parser.parse_args()
    if arguments.ai:
        load_dotenv(".env")
        if not os.getenv("OPENROUTER_API_KEY"):
            raise SystemExit("--ai requires OPENROUTER_API_KEY; no model check was performed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(check(arguments, f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
