"""Bounded sitemap expansion; fetching and scheduling remain with the pipeline."""

from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree


def extract_sitemap_urls(xml: str, *, base_url: str, max_urls: int = 50) -> list[str]:
    if max_urls <= 0:
        return []
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("Sitemap entities and document types are unsupported")
    root = ElementTree.fromstring(xml)
    if root.tag.rsplit("}", 1)[-1] not in {"urlset", "sitemapindex"}:
        raise ValueError("Expected a sitemap urlset or sitemapindex")
    urls = []
    for entry in root:
        for element in entry:
            if element.tag.rsplit("}", 1)[-1] != "loc" or not element.text:
                continue
            url = urljoin(base_url, element.text.strip())
            if urlsplit(url).scheme in {"http", "https"} and url not in urls:
                urls.append(url)
                if len(urls) >= max_urls:
                    return urls
    return urls
