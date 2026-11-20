"""Authenticated API contract discovery, without probing providers or integrations.

Revision 1 is the first advertised mobile contract. These numbers describe implemented,
backward-compatible response/operation contracts, not credentials, proxy deployment,
account entitlements or speech quality. Runtime feature requests remain authoritative.
"""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from copilot_api.deps import AccountDep
from copilot_core import __version__

router = APIRouter()


class ContractRevisions(BaseModel):
    # Active-session discovery/attachment includes its retained spoken_language.
    companion: Literal[1] = 1
    # Profile-scoped conversations, generation status/cancel/retry and source resolution.
    ask: Literal[1] = 1
    # Practice state/actions and per-session voice_available/speech_available flags.
    practice: Literal[1] = 1
    # Meeting preparation uses consented calendar data and editable private meeting context.
    preparation: Literal[1] = 1
    # /call-languages returns the configured catalog, native names and text direction.
    languages: Literal[1] = 1


class ClientCapabilities(BaseModel):
    schema_version: Literal[1] = 1
    server_version: str = __version__
    contracts: ContractRevisions = Field(default_factory=ContractRevisions)


@router.get("/client-capabilities", response_model=ClientCapabilities)
async def client_capabilities(account: AccountDep, response: Response) -> ClientCapabilities:
    # Authentication follows the existing account dependency; no profile selection, external
    # provider calls, feature probes, writes or application services are used by this handler.
    response.headers["Cache-Control"] = "private, no-store"
    return ClientCapabilities()
