import pytest

from unified_web_parser.crawling.pages import make_page
from unified_web_parser.profiles.commerce import read_price
from unified_web_parser.routing.candidates import CandidateBuilder


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Price £45.17", ("45.17", "GBP")),
        ("Ціна 49 000 грн.", ("49 000", "UAH")),
        ("EUR 50", None),
        ("Offer: $100.50", ("100.50", "USD")),
        ("Available for 24.99 GBP", ("24.99", "GBP")),
        ("Battery 61 cycles, health 100%", None),
    ],
)
def test_explicit_prices_preserve_source_currency(text, expected):
    assert (
        read_price(text, currency_aliases={"£": "GBP", "грн": "UAH", "$": "USD", "GBP": "GBP"})
        == expected
    )


def test_primary_detail_excludes_nested_recommendation_cards():
    page = make_page(
        "https://example.org/catalog/item-one",
        """<main><article><h1>The actual book</h1><p>£45.17</p>
        <p>A complete description of the actual book on this detail page.</p>
        <section><h2>Recommendations</h2>
          <article><h3><a href="/other">Unrelated book</a></h3><p>£90.00</p></article>
        </section></article></main>""",
        200,
    )
    candidates = CandidateBuilder().build(page)
    assert len(candidates) == 1
    assert candidates[0].locator == "entity"
    assert "The actual book" in candidates[0].content
    assert "45.17" in candidates[0].content
    assert "Unrelated book" not in candidates[0].content
    assert "90.00" not in candidates[0].content


def test_listing_heading_does_not_turn_multiple_cards_into_one_detail():
    page = make_page(
        "https://example.org/catalog",
        """<main><article><h1>Travel books</h1>
        <article><h2><a href="/one">The first travel book with a long title</a></h2>
          <p>£45.17</p></article>
        <article><h2><a href="/two">Another travel book with a long title</a></h2>
          <p>£20.00</p></article></article></main>""",
        200,
    )
    candidates = CandidateBuilder().build(page)
    assert len(candidates) == 2
    assert all(candidate.locator != "entity" for candidate in candidates)
    assert any("The first" in candidate.content for candidate in candidates)
    assert any("Another" in candidate.content for candidate in candidates)


@pytest.mark.parametrize("wrapper", ["main", "div"])
def test_plain_detail_without_recommendations_has_one_scoped_candidate(wrapper):
    page = make_page(
        "https://example.org/view?id=one",
        f"<{wrapper}><h1>Actual item</h1><p>$100</p>"
        f"<p>A detailed description of this actual individual item.</p></{wrapper}>",
        200,
    )
    candidates = CandidateBuilder(content_profile="commerce", currency_aliases={"$": "USD"}).build(
        page
    )
    assert len(candidates) == 1
    assert candidates[0].locator == "entity"


def test_generic_homepage_does_not_borrow_a_div_card_price():
    page = make_page(
        "https://example.org/",
        '<main><h1>Welcome</h1><div><h2><a href="/one">Featured item</a></h2>'
        "<p>$100</p></div></main>",
        200,
    )
    assert all(candidate.locator != "entity" for candidate in CandidateBuilder().build(page))


def test_listing_does_not_borrow_filter_price_before_its_seo_heading():
    page = make_page(
        "https://example.org/search?q=flats",
        '<div><p>0 грн delivery</p><div><h4><a href="/one">First apartment</a></h4>'
        "<p>250000 грн</p></div><h1>Results for apartments</h1><p>Browse the listings.</p></div>",
        200,
    )
    assert all(candidate.locator != "entity" for candidate in CandidateBuilder().build(page))
