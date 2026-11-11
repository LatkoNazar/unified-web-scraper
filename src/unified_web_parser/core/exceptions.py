"""Errors exposed at the application boundary."""


class ConfigurationError(ValueError):
    """A job or its extraction schema cannot be loaded safely."""


class RoutingError(ValueError):
    """A routing plan references invalid or repeated candidate IDs."""


class ProviderError(RuntimeError):
    """An external AI or infrastructure provider could not complete a request."""


class CreditsExhausted(ProviderError):
    """A structured OpenRouter credit constraint stops a run without more requests."""

    stop_reason = "credits_exhausted"

    def __init__(self, limit_source: str | None = None) -> None:
        self.limit_source = (
            limit_source
            if isinstance(limit_source, str)
            and limit_source
            in {"openrouter_credits", "openrouter_key_limit", "openrouter_in_flight_budget"}
            else None
        )
        self.stop_message = {
            "openrouter_key_limit": "Досягнуто кредитного ліміту ключа OpenRouter.",
            "openrouter_in_flight_budget": (
                "Тимчасово досягнуто ліміту одночасних витрат OpenRouter."
            ),
        }.get(
            self.limit_source,
            "OpenRouter бракує доступного кредитного ліміту для наступного запиту.",
        )
        super().__init__("OpenRouter HTTP 402: insufficient available credits or spending capacity")


class ExtractionError(ValueError):
    """Page evidence could not be transformed into a valid record."""


class PipelineError(RuntimeError):
    """A pipeline cannot start or complete its requested operation."""


class DurabilityError(PipelineError):
    """Required recovery evidence could not be persisted; execution must stop."""
