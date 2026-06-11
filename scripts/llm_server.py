"""
Lightweight OpenAI-compatible LLM server.
Serves OpsLLM-7B locally so the dashboard's OpenAI SDK calls just work.
"""
import asyncio
import json
import os
import time
import uuid
from typing import List, Dict, Any, Optional

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

MODEL_PATH = os.environ.get("MODEL_PATH", "/root/LLM/OpsLLM-7B")
SERVED_MODEL = os.environ.get("SERVED_MODEL", "OpsLLM-7B")
PORT = int(os.environ.get("PORT", "8000"))

print(f"Loading model from {MODEL_PATH} ...")
from transformers import AutoModelForCausalLM, AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.float16,
    device_map="auto",
    trust_remote_code=True,
)
model.eval()
print(f"Loaded {SERVED_MODEL} on {next(model.parameters()).device}")


app = FastAPI(title=f"Local LLM Server ({SERVED_MODEL})")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 1024
    stream: bool = False
    top_p: float = 0.9


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": SERVED_MODEL, "object": "model", "owned_by": "local"}],
    }


def _build_prompt(messages: List[ChatMessage]) -> str:
    msgs = [{"role": m.role, "content": m.content} for m in messages]
    return tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)


def _generate(prompt: str, max_tokens: int, temperature: float, top_p: float) -> str:
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=temperature > 0,
            temperature=max(temperature, 0.01),
            top_p=top_p,
            pad_token_id=tokenizer.eos_token_id,
        )
    new_tokens = out[0][inputs["input_ids"].shape[1]:]
    return tokenizer.decode(new_tokens, skip_special_tokens=True)


def _generate_streaming(prompt, max_tokens, temperature, top_p):
    """Yield tokens one-by-one as the model generates them."""
    from transformers import TextIteratorStreamer
    from threading import Thread
    inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
    streamer = TextIteratorStreamer(
        tokenizer, skip_prompt=True, skip_special_tokens=True,
    )
    gen_kwargs = dict(
        **inputs,
        max_new_tokens=max_tokens,
        do_sample=temperature > 0,
        temperature=max(temperature, 0.01),
        top_p=top_p,
        pad_token_id=tokenizer.eos_token_id,
        streamer=streamer,
    )
    thread = Thread(target=model.generate, kwargs=gen_kwargs)
    thread.start()
    for piece in streamer:
        yield piece
    thread.join()


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    prompt = _build_prompt(req.messages)
    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if not req.stream:
        text = await asyncio.to_thread(
            _generate, prompt, req.max_tokens, req.temperature, req.top_p
        )
        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created,
            "model": SERVED_MODEL,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Real token-level streaming
    async def stream_gen():
        head = {
            "id": req_id, "object": "chat.completion.chunk",
            "created": created, "model": SERVED_MODEL,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(head)}\n\n"

        import queue as _q
        q: "_q.Queue" = _q.Queue()
        SENTINEL = object()

        def _producer():
            try:
                for piece in _generate_streaming(prompt, req.max_tokens, req.temperature, req.top_p):
                    q.put(piece)
            except Exception as exc:
                q.put(("ERR", str(exc)))
            finally:
                q.put(SENTINEL)

        import threading
        threading.Thread(target=_producer, daemon=True).start()

        loop = asyncio.get_running_loop()
        while True:
            piece = await loop.run_in_executor(None, q.get)
            if piece is SENTINEL:
                break
            if isinstance(piece, tuple) and piece[0] == "ERR":
                ch = {"id": req_id, "object": "chat.completion.chunk", "created": created,
                      "model": SERVED_MODEL,
                      "choices": [{"index": 0, "delta": {"content": f"\n[generation error: {piece[1]}]"}, "finish_reason": "error"}]}
                yield f"data: {json.dumps(ch)}\n\n"
                break
            ch = {"id": req_id, "object": "chat.completion.chunk", "created": created,
                  "model": SERVED_MODEL,
                  "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            yield f"data: {json.dumps(ch)}\n\n"

        tail = {"id": req_id, "object": "chat.completion.chunk", "created": created,
                "model": SERVED_MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_gen(), media_type="text/event-stream")


@app.get("/")
async def root():
    return {"served_model": SERVED_MODEL, "endpoints": ["/v1/models", "/v1/chat/completions"]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
