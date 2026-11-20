"""Profile-scoped preferences survive account-wide settings sync."""

import pytest

from copilot_api.testing import Api

pytestmark = pytest.mark.stack


async def test_catalog_preference_rejection_and_history(api: Api) -> None:
    _, headers = api.user()
    await api.client.get("/v1/me", headers=headers)
    assert (await api.client.get("/v1/profile/call-preferences", headers=headers)).json() == {
        "spoken_language": "en"
    }
    assert (await api.client.get("/v1/call-languages", headers=headers)).json()["items"] == [
        {"id": "en", "name": "English"}
    ]
    for language in ("xx", "en-US", "es", "../en"):
        response = await api.client.patch(
            "/v1/profile/call-preferences", headers=headers, json={"spoken_language": language}
        )
        assert response.status_code == 422
    response = await api.client.patch(
        "/v1/profile/call-preferences", headers=headers, json={"spoken_language": "en"}
    )
    assert response.status_code == 200
    _, other = api.user("Other")
    assert (await api.client.get("/v1/profile/call-preferences", headers=other)).json()[
        "spoken_language"
    ] == "en"
