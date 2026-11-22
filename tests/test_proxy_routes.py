"""The public display socket must reach the gateway, not the REST fallback."""

import re
from pathlib import Path


def test_live_websocket_routes_preserve_upgrade_and_gateway():
    text = (Path(__file__).resolve().parents[1] / "docker/proxy/nginx.conf").read_text()
    for path in ("/v1/session", "/v1/seat", "/v1/companion"):
        match = re.search(r"location\s+=\s+" + re.escape(path) + r"\s*\{([^}]+)\}", text)
        assert match, f"{path} would fall through to the REST API"
        block = match[1]
        for required in (
            "proxy_pass http://gateway;",
            "proxy_http_version 1.1;",
            "proxy_set_header Upgrade $http_upgrade;",
            "proxy_set_header Connection $connection_upgrade;",
            "proxy_buffering off;",
        ):
            assert required in block, (path, required)


def test_companion_rest_paths_remain_on_api():
    text = (Path(__file__).resolve().parents[1] / "docker/proxy/nginx.conf").read_text()
    assert "location = /v1/companion {" in text
    fallback = re.search(r"location /\s*\{([^}]+)\}", text)
    assert fallback
    assert "proxy_pass http://api;" in fallback[1]
    assert not re.search(r"location\s+(?:\^~\s+)?/v1/companion\s*\{", text)
