"""Current Chat Completions token limits do not reuse deprecated request caches."""

import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from unified_web_parser.core.exceptions import ProviderError
from unified_web_parser.providers.openrouter import OpenRouterClient, RequestBudget


async def test_chat_completion_limit_migrates_wire_and_preserves_free_warm_cache(tmp_path):
    requests = []
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
    }

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":7}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        first = OpenRouterClient(
            "test", "model", client=http, cache_dir=tmp_path, max_output_tokens=4096
        )
        assert await first.complete_json("Extract", {"fact": "observed"}, schema) == {"answer": 7}
        assert requests[0]["max_completion_tokens"] == 4096
        assert "max_tokens" not in requests[0]
        current_cache = next(tmp_path.glob("*.json"))
        legacy_request = deepcopy(requests[0])
        legacy_request["max_tokens"] = legacy_request.pop("max_completion_tokens")
        digest = hashlib.sha256(
            json.dumps(
                {
                    "endpoint": "https://openrouter.ai/api/v1/chat/completions",
                    "request": legacy_request,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        legacy_cache = tmp_path / f"{digest}.json"
        legacy_cache.write_text('{"answer":99}', encoding="utf-8")
        current_cache.unlink()
        empty = OpenRouterClient(
            "test",
            "model",
            client=http,
            cache_dir=tmp_path,
            max_output_tokens=4096,
            budget=RequestBudget(0),
        )
        with pytest.raises(ProviderError, match="budget exhausted"):
            await empty.complete_json("Extract", {"fact": "observed"}, schema)
        assert len(requests) == 1 and empty.calls == empty.cache_hits == 0
        fresh = OpenRouterClient(
            "test", "model", client=http, cache_dir=tmp_path, max_output_tokens=4096
        )
        assert await fresh.complete_json("Extract", {"fact": "observed"}, schema) == {"answer": 7}
        warm = OpenRouterClient(
            "test",
            "model",
            client=http,
            cache_dir=tmp_path,
            max_output_tokens=4096,
            budget=RequestBudget(0),
        )
        assert await warm.complete_json("Extract", {"fact": "observed"}, schema) == {"answer": 7}
    assert len(requests) == 2
    assert warm.calls == 0 and warm.cache_hits == 1
    assert warm.stats["cost_usd"] == 0
    assert json.loads(legacy_cache.read_text()) == {"answer": 99}
