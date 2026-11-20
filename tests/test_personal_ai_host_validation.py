"""Evidence guards for the opt-in real-host harness; no providers or storage are contacted."""

import copy
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

SPEC = importlib.util.spec_from_file_location(
    "sourced_host_validation",
    Path(__file__).resolve().parents[1] / "scripts/validate_personal_ai_host.py",
)
assert SPEC is not None
assert SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def publication():
    excerpts = [
        "The fictional local widget has an identifier named widget_key.",
        "The fictional local widget advertises its demonstration capability.",
    ]
    source = {
        "url": "https://example.test/immutable-source",
        "text": "\n".join(excerpts),
        "sha256": "fixed-public-hash",
    }
    detail = {
        "active_brief": {
            "origin": "mcp",
            "brief": {
                "title": "Source requirements",
                "prepared_at": datetime.now(UTC).isoformat(),
                "items": [
                    {
                        "id": name,
                        "kind": "decision",
                        "text": text,
                        "provenance": "sourced",
                        "source_ids": ["mcp-tools"],
                    }
                    for name, text in zip(("identity", "capability"), excerpts, strict=True)
                ],
                "sources": [
                    {
                        "id": "mcp-tools",
                        "title": "Public MCP tools specification",
                        "url": source["url"],
                        "attribution": "Public specification",
                    }
                ],
                "coverage_gaps": ["Requirements only"],
            },
        }
    }
    items = [
        {
            "type": "mcp_tool_call",
            "server": "public_source",
            "tool": "read_public_excerpt",
            "status": "completed",
            "result": {"structured_content": source},
        }
    ] + [
        {"type": "mcp_tool_call", "server": "career_pilot", "status": "completed", "tool": tool}
        for tool in ("get_context_request", "publish_context_brief")
    ]
    output = "\n".join(json.dumps({"type": "item.completed", "item": item}) for item in items)
    return detail, source, output


def test_evidence_requires_real_read_exact_attribution_and_two_successful_tools():
    detail, source, output = publication()
    summary = harness.validate_sourced_publication(detail, source, output)
    assert summary["public_source_reads"] == 1
    assert len(summary["sourced_items"]) == 2
    with pytest.raises(AssertionError, match="source read"):
        harness.validate_sourced_publication(
            detail, source, output.replace('"sha256": "fixed-public-hash"', '"sha256": "wrong"')
        )
    with pytest.raises(AssertionError, match="source read"):
        harness.validate_sourced_publication(
            detail, source, output.replace('"public_source"', '"different_source"')
        )
    with pytest.raises(AssertionError, match="exactly one"):
        harness.validate_sourced_publication(
            detail,
            source,
            output.replace('"completed", "tool": "publish', '"failed", "tool": "publish'),
        )
    tampered = copy.deepcopy(detail)
    tampered["active_brief"]["brief"]["items"][0]["text"] = "Invented private account guarantee."
    with pytest.raises(AssertionError, match="Unsubstantiated"):
        harness.validate_sourced_publication(tampered, source, output)
    tampered = copy.deepcopy(detail)
    tampered["active_brief"]["brief"]["sources"][0]["url"] = "https://example.test/different"
    with pytest.raises(AssertionError, match="attribution"):
        harness.validate_sourced_publication(tampered, source, output)
    with pytest.raises(AssertionError, match="Shell execution"):
        harness.validate_sourced_publication(
            detail,
            source,
            output + '\n{"type":"item.completed","item":{"type":"command_execution"}}',
        )


@pytest.mark.asyncio
async def test_public_mcp_source_has_only_fixed_payload_and_no_file_or_url_arguments():
    app = FastAPI()
    source = harness.public_source()
    reads = []
    harness.mount_public_source(app, source, {"CAREER_PILOT_HOST_TEST_TOKEN": "local-test"}, reads)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        path = "/__validation/public-source"
        headers = {"Authorization": "Bearer local-test"}

        def message(name, arguments):
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments,
                },
            }

        assert (await client.post(path, json=message("read_public_excerpt", {}))).status_code == 401
        for arguments in ({"path": "private.md"}, {"url": "https://private.test"}):
            response = await client.post(
                path, headers=headers, json=message("read_public_excerpt", arguments)
            )
            assert "error" in response.json()
        response = await client.post(path, headers=headers, json=message("run_shell", {}))
        assert "error" in response.json()
        assert reads == []
        response = await client.post(path, headers=headers, json=message("read_public_excerpt", {}))
        data = response.json()["result"]["structuredContent"]
        assert data["sha256"] == harness.SOURCE_SHA256
        assert data["text"] == source["text"]
        assert "path" not in data
        assert reads == [harness.SOURCE_SHA256]


@pytest.mark.parametrize("endpoint", ["database", "valkey", "s3"])
def test_non_loopback_storage_rejected_before_any_fixture_mutation(monkeypatch, endpoint):
    monkeypatch.setattr(harness.teststack, "admin_url", lambda: "postgresql://local@127.0.0.1/test")
    monkeypatch.setattr(harness.teststack, "VALKEY_URL", "redis://localhost:6379/14")
    monkeypatch.setattr(harness.teststack, "S3_ENDPOINT", "http://localhost:8333")
    harness.require_local_stack()
    if endpoint == "database":
        monkeypatch.setattr(
            harness.teststack, "admin_url", lambda: "postgresql://private:secret@prod.test/live"
        )
    elif endpoint == "valkey":
        monkeypatch.setattr(harness.teststack, "VALKEY_URL", "redis://prod.test:6379/14")
    else:
        monkeypatch.setattr(harness.teststack, "S3_ENDPOINT", "https://prod.test")
    with pytest.raises(RuntimeError, match="Refusing non-loopback") as error:
        harness.require_local_stack()
    assert "secret" not in str(error.value)


def test_redacts_both_app_and_oauth_tokens_and_provider_secret():
    secrets = ["original-app-token", "mcp-grant-token", "provider-secret"]
    assert harness.redact(" ".join(secrets), secrets) == " ".join(
        ["[redacted-test-credential]"] * 3
    )
    assert harness.completed_items('not-json\n[]\n{"type":"item.started"}') == []


def test_public_payload_is_pinned_and_cannot_be_replaced_by_private_text(monkeypatch, tmp_path):
    assert harness.public_source()["url"] == harness.SOURCE_URL
    replacement = tmp_path / harness.SOURCE_PATH
    replacement.parent.mkdir(parents=True)
    replacement.write_text("Private repository content", encoding="utf-8")
    monkeypatch.setattr(harness, "ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="Public excerpt changed"):
        harness.public_source()
