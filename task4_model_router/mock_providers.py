import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DEFAULT_DELAY_SECONDS = 5.0


class CompletionRequest(BaseModel):
    prompt: str
    max_tokens: int


def completion_body(provider_name: str, request: CompletionRequest) -> dict:
    text = f"Mock completion from {provider_name}: {request.prompt[:40]}"
    prompt_tokens = len(request.prompt.split())
    completion_tokens = min(len(text.split()), request.max_tokens)
    return {
        "text": text,
        "model": f"mock-{provider_name}-1",
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def create_provider(provider_name: str, mode_variable: str) -> FastAPI:
    app = FastAPI(title=f"Mock {provider_name} LLM provider")

    @app.post("/v1/completions")
    async def completions(request: CompletionRequest) -> JSONResponse:
        mode = os.environ.get(mode_variable, "success")
        if mode == "rate_limit":
            return JSONResponse({"error": "too many requests"}, status_code=429)
        if mode == "fail":
            return JSONResponse(
                {"error": "Traceback: /srv/provider/app.py line 42 secret=sk-mock"},
                status_code=500,
            )
        if mode == "timeout":
            delay = float(os.environ.get("MOCK_DELAY_SECONDS", DEFAULT_DELAY_SECONDS))
            await asyncio.sleep(delay)
        return JSONResponse(completion_body(provider_name, request))

    return app


primary_app = create_provider("primary", "MOCK_PRIMARY_MODE")
backup_app = create_provider("backup", "MOCK_BACKUP_MODE")
