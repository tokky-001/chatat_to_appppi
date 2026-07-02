"""OpenAI互換 API サーバ。

Claude Code や OpenAI SDK から base_url を差し替えて使えるように、
/v1/chat/completions と /v1/models を実装する。裏では ChatBrowser が
社内チャットAIのWeb画面を操作して回答を取得する。
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from .browser import ChatBrowser
from .chat_flow import run_completion
from .config import Config, load_config
from .rag import FileIndex
from .tokens import count_tokens

CONFIG: Config = load_config()
BROWSER: Optional[ChatBrowser] = None
INDEX: Optional[FileIndex] = None


# ---- OpenAI 互換スキーマ -------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: object = ""  # str または マルチパート list


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global BROWSER, INDEX
    BROWSER = ChatBrowser(CONFIG.chat)
    await BROWSER.start()
    if CONFIG.rag.enabled:
        INDEX = FileIndex(CONFIG.rag)
    yield
    if BROWSER:
        await BROWSER.stop()


app = FastAPI(title="ChatAI_to_API", lifespan=lifespan)


def _check_auth(authorization: Optional[str]) -> None:
    expected = CONFIG.server.api_key
    if not expected:
        return
    if not authorization or authorization.replace("Bearer ", "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


def _save_answer(question: str, answer: str) -> None:
    if not CONFIG.output.save_dir:
        return
    d = Path(CONFIG.output.save_dir)
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    (d / f"{ts}.md").write_text(
        f"# {ts}\n\n## 質問\n\n{question}\n\n## 回答\n\n{answer}\n",
        encoding="utf-8",
    )


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": CONFIG.server.model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "internal",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    authorization: Optional[str] = Header(default=None),
):
    _check_auth(authorization)
    assert BROWSER is not None

    messages = [m.model_dump() for m in req.messages]

    last_user = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
    )

    # RAG: 質問に関連するローカルファイル要約をプロンプト先頭に注入する
    if INDEX is not None:
        block = INDEX.build_context_block(str(last_user))
        if block:
            messages = [{"role": "system", "content": block}] + messages

    answer = await run_completion(BROWSER, CONFIG, messages)
    _save_answer(str(last_user), answer)

    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = req.model or CONFIG.server.model_name

    if req.stream:
        return StreamingResponse(
            _stream_response(cid, created, model, answer),
            media_type="text/event-stream",
        )

    prompt_tokens = count_tokens(
        "\n".join(str(m.get("content", "")) for m in messages)
    )
    completion_tokens = count_tokens(answer)
    return JSONResponse(
        {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


async def _stream_response(cid: str, created: int, model: str, answer: str):
    """完成済みの回答を SSE チャンクに分割して疑似ストリーミングする。"""
    def chunk(delta: dict, finish=None) -> str:
        payload = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant"})
    step = 40
    for i in range(0, len(answer), step):
        yield chunk({"content": answer[i : i + step]})
    yield chunk({}, finish="stop")
    yield "data: [DONE]\n\n"
