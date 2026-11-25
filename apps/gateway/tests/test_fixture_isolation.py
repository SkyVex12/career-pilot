"""Scripted gateway worlds must not auto-open an ambient database connection."""

import pytest

from .conftest import make_settings


@pytest.mark.parametrize("name", ["COPILOT_DATABASE_URL", "COPILOT_GATEWAY_DATABASE_URL"])
def test_scripted_settings_ignore_ambient_database(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, "postgresql://unused:unused@127.0.0.1:1/never_connect")
    assert make_settings().database_url is None


def test_explicit_fixture_database_is_preserved() -> None:
    url = "postgresql://fixture:fixture@127.0.0.1:1/explicit_fixture"
    assert make_settings(database_url=url).database_url == url
