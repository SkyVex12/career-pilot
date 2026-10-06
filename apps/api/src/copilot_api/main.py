import os

import uvicorn
from fastapi import FastAPI

from copilot_core import __version__

SERVICE = "api"

app = FastAPI(title="Copilot API", version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE, "version": __version__}


def run() -> None:
    uvicorn.run(
        "copilot_api.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8080")),
    )
