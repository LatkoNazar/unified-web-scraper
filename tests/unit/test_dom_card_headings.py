import pytest

from unified_web_parser.extraction.projection import dom_entity, map_entity


@pytest.mark.parametrize("heading", ["h4", "h5", "h6"])
def test_lower_semantic_heading_preserves_observed_card_title_url_and_price(heading):
    content = f"""<article>
      <a href="/records/one"><{heading}>Travel guide 2025</{heading}></a>
      <p>100 GBP</p>
    </article>"""
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "url": {"type": "string", "format": "uri"},
            "price": {"type": "number"},
            "currency": {"type": "string"},
        },
        "required": ["title", "url", "price", "currency"],
        "additionalProperties": False,
    }
    entity = dom_entity(
        content,
        "https://example.org/catalog",
        card=True,
        content_profile="commerce",
        currency_aliases={"GBP": "GBP"},
    )
    data = map_entity(entity, schema, "https://example.org/catalog", content_profile="commerce")
    assert data == {
        "title": "Travel guide 2025",
        "url": "https://example.org/records/one",
        "price": 100,
        "currency": "GBP",
    }


@pytest.mark.parametrize("heading", ["h4", "h5", "h6"])
def test_heading_price_mention_does_not_become_an_observed_offer(heading):
    entity = dom_entity(
        f'<a href="/records/one"><{heading}>Guide to saving 100 GBP</{heading}></a>',
        "https://example.org/catalog",
        card=True,
        content_profile="commerce",
        currency_aliases={"GBP": "GBP"},
    )
    assert entity["name"] == "Guide to saving 100 GBP"
    assert entity["url"] == "https://example.org/records/one"
    assert "price" not in entity
    assert "priceCurrency" not in entity
