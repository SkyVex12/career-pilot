import os

import uvicorn
from fastapi import FastAPI

from copilot_core import __version__

SERVICE = "gateway"

app = FastAPI(title="Copilot gateway", version=__version__)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE, "version": __version__}


def run() -> None:
    uvicorn.run(
        "copilot_gateway.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8081")),
    )
