import asyncio
import json

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Mock LLM provider")

CHUNK_DELAY_SECONDS = 0.05

SAMPLE_CHUNKS = [
    "Here are the ",
    "customer detai",
    "ls for the cafe account.\n",
    "Contact john.d",
    "oe@exa",
    "mple.",
    "com for support. ",
    "SSN on file: 123-4",
    "5-67",
    "89. ",
    "Card on file: 4111 11",
    "11 1111",
    " 1111 expires soon. ",
    "Order number 1234 5678 stays visible. ",
    "Escalations go to ann",
    "a@exam",
    "ple.org",
]


class CompletionRequest(BaseModel):
    prompt: str
    stream: bool = True


def sse_event(payload: dict | str) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n".encode()


async def completion_events():
    yield sse_event({"choices": [], "model": "mock-llm-1"})
    for text in SAMPLE_CHUNKS:
        event = sse_event({"choices": [{"text": text, "finish_reason": None}]})
        middle = len(event) // 2
        yield event[:middle]
        await asyncio.sleep(CHUNK_DELAY_SECONDS)
        yield event[middle:]
    yield sse_event({"choices": [{"text": "", "finish_reason": "stop"}]})
    yield sse_event({"choices": [], "usage": {"completion_tokens": len(SAMPLE_CHUNKS)}})
    yield sse_event("[DONE]")


@app.post("/v1/completions")
async def completions(request: CompletionRequest) -> StreamingResponse:
    return StreamingResponse(completion_events(), media_type="text/event-stream")
