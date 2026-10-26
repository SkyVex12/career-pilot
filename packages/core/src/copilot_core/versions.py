"""The desktop app's version, as the gateway and the API see it, against the minimum they
support (`min_client_version`; README.md, "Releasing the desktop app").

The app names itself `desktop-<version>`: in `session.start`'s `client_version`, and in the
`x-copilot-client` header of every API request (from the first release after 0.1.0; 0.1.0
sends no header, so the API cannot hold it back, but the gateway can). A version that does
not parse (a test client, the eval replayer) and dev builds (0.0.0) are never held back:
the guard is for compatibility, not access.
"""

import re

CLIENT_HEADER = "x-copilot-client"
DESKTOP_PREFIX = "desktop-"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")

# The last release before close code 4426 existed: it reconnects on a close code it does not
# know, so the gateway closes such clients with 4410 (stop, no reconnect) instead.
LAST_WITHOUT_4426 = (0, 1, 0)

Version = tuple[int, int, int, tuple[tuple[int, int | str], ...] | None]


def parse(text: str) -> Version | None:
    m = _SEMVER.match(text.strip())
    if m is None:
        return None
    pre = None
    if m.group(4):
        pre = tuple((0, int(p)) if p.isdigit() else (1, p) for p in m.group(4).split("."))
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), pre


def _key(v: Version) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    return (v[0], v[1], v[2], 0, v[3]) if v[3] is not None else (v[0], v[1], v[2], 1, ())


def desktop_version(client: str | None) -> Version | None:
    """`desktop-0.1.0` -> (0, 1, 0, None); anything else, or a dev build (0.0.0), -> None."""
    if not client or not client.startswith(DESKTOP_PREFIX):
        return None
    v = parse(client.removeprefix(DESKTOP_PREFIX))
    if v is None or v[:3] == (0, 0, 0):
        return None
    return v


def is_outdated(client: str | None, minimum: str) -> bool:
    v, floor = desktop_version(client), parse(minimum)
    return v is not None and floor is not None and _key(v) < _key(floor)


# The last release whose QuestionType has no "respond": it cannot parse a question.detected
# that carries it, so the gateway sends "other" instead (the same short reply either way).
LAST_WITHOUT_RESPOND = (0, 1, 4)


def knows_respond(client: str | None) -> bool:
    v = desktop_version(client)
    return v is None or v[:3] > LAST_WITHOUT_RESPOND


def knows_4426(client: str) -> bool:
    v = desktop_version(client)
    return v is None or v[:3] > LAST_WITHOUT_4426


def outdated(client: str, minimum: str) -> dict[str, str]:
    """The `ClientOutdated` body (protocol.schema.json): the API's 426, the gateway's error."""
    version = client.removeprefix(DESKTOP_PREFIX)
    return {
        "code": "client_outdated",
        "message": (
            f"Copilot {version} is older than this server supports ({minimum} or newer). "
            "The app updates itself now."
        ),
        "min_version": minimum,
        "client_version": version,
    }
