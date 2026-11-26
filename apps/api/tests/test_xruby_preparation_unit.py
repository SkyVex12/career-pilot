"""Provider transport and policy tests: synthetic transport, never a paid request."""

import json
from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from copilot_api import xruby_preparation as prep
from copilot_api.deps import Services
from copilot_api.routes import xruby_oauth as auth


def fixture_service(**updates) -> Services:
    config = dict(
        xruby_preparation_enabled=True,
        xruby_preparation_daily_limit=2,
        xruby_preparation_global_daily_limit=100,
        openai_api_key=SecretStr("synthetic-key"),
        openai_pack_model="explicit-configured-model",
        openai_pack_reasoning=None,
        openai_base_url=None,
        xruby_preparation_max_output_tokens=2000,
    )
    return cast(Services, SimpleNamespace(settings=SimpleNamespace(**(config | updates))))


def snapshot():
    return {
        "source": {"content": {"interview": {"company": "Synthetic", "role": "Engineer"}}},
        "job_description": "Saved requirement.",
        "resume_text": "Saved evidence.",
    }


def response_data(**updates):
    draft = {
        "job_summary": "Recorded requirement",
        "requirements": ["Practice"],
        "prepared_answers": [
            {"question": "Describe a saved example.", "talking_points": ["Use recorded evidence."]}
        ],
        "gaps": [],
    }
    value = {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(draft)}]}
        ],
        "usage": {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36},
    }
    return value | updates


@pytest.fixture
def transport(monkeypatch):
    real = httpx2.AsyncClient
    calls = []
    state = SimpleNamespace(status=200, data=response_data(), error=None, body=None, kwargs=None)

    def handle(request):
        calls.append(request)
        if state.error:
            raise state.error
        return (
            httpx2.Response(state.status, json=state.data)
            if state.body is None
            else httpx2.Response(state.status, content=state.body)
        )

    def client(**kwargs):
        state.kwargs = kwargs
        return real(transport=httpx2.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    return state, calls


async def test_direct_provider_one_fixed_request_no_storage_tools_or_fallback(transport):
    state, calls = transport
    draft, usage = await prep.direct_provider(fixture_service(), snapshot())
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://api.openai.com/v1/responses"
    body = json.loads(request.content)
    assert body["store"] is False
    assert body["max_output_tokens"] == 2000
    assert body["model"] == "explicit-configured-model"
    assert "tools" not in body
    assert body["text"]["format"]["strict"] is True
    assert state.kwargs == {"timeout": 30.0, "follow_redirects": False}
    assert draft.prepared_answers[0].question
    assert usage == {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36}


@pytest.mark.parametrize("status", [301, 400, 401, 429, 500, 503])
async def test_non_success_never_retries_or_follows_redirect(transport, status):
    state, calls = transport
    state.status = status
    with pytest.raises(RuntimeError, match="not confirmed"):
        await prep.direct_provider(fixture_service(), snapshot())
    assert len(calls) == 1


async def test_transport_timeout_never_retries(transport):
    state, calls = transport
    state.error = httpx2.ReadTimeout("synthetic timeout")
    with pytest.raises(httpx2.ReadTimeout):
        await prep.direct_provider(fixture_service(), snapshot())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "data",
    [
        response_data(status="incomplete"),
        response_data(output=[]),
        response_data(
            output=[{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]
        ),
        response_data(
            output=[{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]
        ),
    ],
)
async def test_incomplete_refusal_or_invalid_shape_never_publishes(transport, data):
    state, calls = transport
    state.data = data
    with pytest.raises(prep.ProviderFailure) as exc:
        await prep.direct_provider(fixture_service(), snapshot())
    assert exc.value.usage["total_tokens"] == 36
    assert len(calls) == 1


async def test_response_bytes_bounded(transport):
    state, calls = transport
    state.body = b"x" * 65537
    with pytest.raises(RuntimeError, match="bound"):
        await prep.direct_provider(fixture_service(), snapshot())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"xruby_preparation_enabled": False}, 404),
        ({"xruby_preparation_daily_limit": 0}, 503),
        ({"xruby_preparation_global_daily_limit": 0}, 503),
        ({"openai_api_key": None}, 503),
        ({"openai_pack_model": None}, 503),
        ({"openai_base_url": "https://other.test"}, 503),
    ],
)
def test_enabled_requires_explicit_payer_and_reviewed_provider(updates, code):
    with pytest.raises(HTTPException) as exc:
        prep.enabled(fixture_service(**updates))
    assert exc.value.status_code == code


def test_write_scope_is_opt_in_and_separate_from_read_scopes():
    assert auth.PREPARE_SCOPE not in auth.SCOPES
    assert auth.PREPARE_SCOPE not in auth.supported_scopes(
        fixture_service(xruby_preparation_enabled=False)
    )
    assert auth.PREPARE_SCOPE in auth.supported_scopes(fixture_service())
