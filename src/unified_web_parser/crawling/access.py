"""Recognize explicit access interstitials without interpreting product content."""

import re
from html.parser import HTMLParser

from unified_web_parser.crawling.errors import FetchError


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self._in_title = False
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript"}:
            self._hidden += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript"}:
            self._hidden = max(0, self._hidden - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        if not self._hidden:
            self.text.append(data)


def check_access_page(url: str, html: str, status_code: int) -> None:
    """Raise for a clear challenge page, including challenges served with HTTP 200."""
    parser = _VisibleText()
    parser.feed(html)
    title = re.sub(r"\s+", " ", " ".join(parser.title)).strip().lower()
    text = re.sub(r"\s+", " ", " ".join(parser.text)).strip().lower()
    challenge_titles = (
        "access denied",
        "attention required! | cloudflare",
        "just a moment...",
        "security check",
        "security verification",
        "captcha",
        "captcha challenge",
        "robot check",
        "robot or human?",
        "verify you are human",
        "проверка безопасности",
        "перевірка безпеки",
        "доступ заборонено",
        "доступ запрещен",
    )
    challenge_phrases = (
        "verify you are human by completing the action below",
        "please verify that you are a human",
        "please confirm you are not a robot",
        "performing security verification",
        "checking your browser before accessing",
        "підтвердіть, що ви не робот",
        "підтвердіть що ви не робот",
        "подтвердите, что вы не робот",
        "доступ до сайту тимчасово обмежено",
        "you don't have permission to access",
        "you do not have permission to access",
    )
    title_challenge = title in challenge_titles
    # Long articles can discuss security; concise interstitials with explicit
    # human-verification instructions should be treated as an access failure.
    body_challenge = len(text) < 15_000 and any(phrase in text for phrase in challenge_phrases)
    if title_challenge or body_challenge:
        raise FetchError(
            url,
            f"Access challenge/interstitial detected (HTTP {status_code}); manual access required",
            status_code=status_code,
        )
