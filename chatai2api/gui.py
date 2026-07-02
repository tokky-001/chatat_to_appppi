"""管理GUI (Webダッシュボード)。

`python -m chatai2api.cli gui` で起動する。機能:
- APIサーバ(serve)の起動/停止/状態表示
- 初回ログイン(login)の起動
- コンテキストチェッカー: プロンプト+添付ファイルが社内AIの
  コンテキスト最大値に収まるか/何分割になるかを表示
- GUIから直接質問(回答はファイル保存もされる)
- RAGインデックスの作成/状態表示
- 保存済み回答の一覧/閲覧
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .chat_flow import _split_text, flatten_messages
from .config import load_config
from .rag import FileIndex
from .tokens import count_tokens

PKG_ROOT = Path(__file__).resolve().parent.parent  # chatai2api の親 = プロジェクト
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ChatAI_to_API GUI")

# ---- サブプロセス管理 -------------------------------------------------------
_server_proc: Optional[subprocess.Popen] = None
_login_proc: Optional[subprocess.Popen] = None
_index_proc: Optional[subprocess.Popen] = None
_index_log: deque = deque(maxlen=50)


def _spawn(args: List[str]) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PKG_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    kwargs = {}
    if os.name == "nt":
        # Windows: CTRL_BREAK_EVENT で graceful shutdown できるよう
        # 新しいプロセスグループで起動する
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, "-m", "chatai2api.cli", *args],
        cwd=os.getcwd(),  # config.yaml のある場所で実行される想定
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    """サブプロセスを子プロセス(Chromium)ごと確実に終了させる。"""
    if os.name == "nt":
        import signal

        # まず CTRL_BREAK で graceful shutdown を試みる
        # (uvicorn が lifespan を実行し Playwright を閉じられる)
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            proc.wait(timeout=15)
            return
        except Exception:
            pass
        # 効かなければプロセスツリーごと強制終了(孤児Chromiumを残さない)
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
        )
        return
    # POSIX: SIGTERM → uvicorn が graceful shutdown でブラウザも閉じる
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _alive(proc: Optional[subprocess.Popen]) -> bool:
    return proc is not None and proc.poll() is None


async def _probe_server() -> bool:
    cfg = load_config()
    url = f"http://{cfg.server.host}:{cfg.server.port}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(url)
            return r.status_code == 200
    except Exception:
        return False


# ---- スキーマ ---------------------------------------------------------------
class AttachedFile(BaseModel):
    name: str
    text: str


class TokensRequest(BaseModel):
    prompt: str = ""
    files: List[AttachedFile] = []


class AskRequest(BaseModel):
    prompt: str
    files: List[AttachedFile] = []


class IndexRequest(BaseModel):
    directory: str
    no_ai: bool = True


# ---- ページ -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index_page():
    return (STATIC / "gui.html").read_text(encoding="utf-8")


# ---- 状態 -------------------------------------------------------------------
@app.get("/api/status")
async def api_status():
    cfg = load_config()
    running = _alive(_server_proc) or await _probe_server()
    idx = FileIndex(cfg.rag)
    saved_dir = Path(cfg.output.save_dir) if cfg.output.save_dir else None
    saved = sorted(saved_dir.glob("*.md")) if saved_dir and saved_dir.exists() else []
    profile_exists = Path(cfg.chat.user_data_dir).exists()
    return {
        "server_running": running,
        "server_managed": _alive(_server_proc),
        "server_url": f"http://{cfg.server.host}:{cfg.server.port}/v1",
        "login_running": _alive(_login_proc),
        "profile_exists": profile_exists,
        "chat_url": cfg.chat.url,
        "model_name": cfg.server.model_name,
        "max_tokens": cfg.context.max_tokens,
        "chunk_tokens": cfg.context.chunk_tokens,
        "rag_enabled": cfg.rag.enabled,
        "indexed_files": len(idx.entries),
        "saved_count": len(saved),
        "index_running": _alive(_index_proc),
    }


# ---- サーバ起動/停止 --------------------------------------------------------
@app.post("/api/server/start")
async def server_start():
    global _server_proc
    if _alive(_server_proc) or await _probe_server():
        return {"ok": True, "message": "既に起動しています"}
    _server_proc = _spawn(["serve"])
    # 起動確認(最大20秒)
    for _ in range(20):
        time.sleep(1)
        if await _probe_server():
            return {"ok": True, "message": "起動しました"}
        if not _alive(_server_proc):
            out = _server_proc.stdout.read()[-800:] if _server_proc.stdout else ""
            return {"ok": False, "message": f"起動に失敗しました: {out}"}
    return {"ok": True, "message": "起動中です（応答待ち）"}


@app.post("/api/server/stop")
async def server_stop():
    global _server_proc
    if not _alive(_server_proc):
        _server_proc = None
        if await _probe_server():
            return {"ok": False, "message": "このGUIの管理外で起動しています。起動元の端末で停止してください。"}
        return {"ok": True, "message": "停止済みです"}
    _kill_tree(_server_proc)
    _server_proc = None
    return {"ok": True, "message": "停止しました"}


@app.post("/api/login")
async def login_start():
    global _login_proc
    if _alive(_login_proc):
        return {"ok": True, "message": "ログイン用ブラウザは既に開いています"}
    if _alive(_server_proc):
        return {"ok": False, "message": "サーバ稼働中はログインできません(ブラウザプロファイル競合)。先にサーバを停止してください。"}
    _login_proc = _spawn(["login"])
    return {"ok": True, "message": "ログイン用ブラウザを開きました。ログイン完了後、自動で閉じます。"}


# ---- コンテキストチェッカー --------------------------------------------------
@app.post("/api/tokens")
async def api_tokens(req: TokensRequest):
    cfg = load_config()
    prompt_tokens = count_tokens(req.prompt)
    file_items = [
        {"name": f.name, "tokens": count_tokens(f.text)} for f in req.files
    ]
    files_tokens = sum(f["tokens"] for f in file_items)
    total = prompt_tokens + files_tokens
    max_t = cfg.context.max_tokens
    overflow = total > max_t
    chunks = 1
    if overflow:
        combined = "\n\n".join([f.text for f in req.files] + [req.prompt])
        chunks = len(_split_text(combined, cfg.context.chunk_tokens))
    return {
        "prompt_tokens": prompt_tokens,
        "files_tokens": files_tokens,
        "files": file_items,
        "total": total,
        "max_tokens": max_t,
        "chunk_tokens": cfg.context.chunk_tokens,
        "overflow": overflow,
        "chunks": chunks,
        "ratio": total / max_t if max_t else 0,
    }


# ---- GUIから質問 -------------------------------------------------------------
@app.post("/api/ask")
async def api_ask(req: AskRequest):
    cfg = load_config()
    if not await _probe_server():
        raise HTTPException(status_code=409, detail="APIサーバが起動していません。先に「サーバ起動」してください。")
    content = req.prompt
    if req.files:
        blocks = [f"【添付ファイル: {f.name}】\n{f.text}" for f in req.files]
        content = "\n\n".join(blocks + [req.prompt])
    url = f"http://{cfg.server.host}:{cfg.server.port}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=cfg.chat.response_timeout + 120) as c:
        r = await c.post(
            url,
            headers={"Authorization": f"Bearer {cfg.server.api_key}"},
            json={
                "model": cfg.server.model_name,
                "messages": [{"role": "user", "content": content}],
            },
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"APIサーバエラー: {r.text[:300]}")
    data = r.json()
    return {"answer": data["choices"][0]["message"]["content"], "usage": data.get("usage")}


# ---- インデックス ------------------------------------------------------------
def _watch_index(proc: subprocess.Popen) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        _index_log.append(line.rstrip())


@app.post("/api/index")
async def api_index(req: IndexRequest):
    global _index_proc
    if _alive(_index_proc):
        return {"ok": False, "message": "インデックス作成が既に実行中です"}
    d = Path(req.directory).expanduser()
    if not d.is_dir():
        return {"ok": False, "message": f"ディレクトリが見つかりません: {req.directory}"}
    if not req.no_ai and (_alive(_server_proc) or await _probe_server()):
        return {"ok": False, "message": "AI要約はサーバ停止中に実行してください(ブラウザプロファイル競合)。--no-AI(抽出モード)なら実行できます。"}
    args = ["index", str(d)] + (["--no-ai"] if req.no_ai else [])
    _index_log.clear()
    _index_proc = _spawn(args)
    threading.Thread(target=_watch_index, args=(_index_proc,), daemon=True).start()
    return {"ok": True, "message": "インデックス作成を開始しました"}


@app.get("/api/index/status")
async def api_index_status():
    return {"running": _alive(_index_proc), "log": list(_index_log)}


# ---- 保存済み回答 -------------------------------------------------------------
@app.get("/api/saved")
async def api_saved():
    cfg = load_config()
    if not cfg.output.save_dir:
        return {"dir": "", "files": []}
    d = Path(cfg.output.save_dir)
    files = []
    if d.exists():
        for p in sorted(d.glob("*.md"), reverse=True)[:100]:
            files.append({"name": p.name, "size": p.stat().st_size})
    return {"dir": str(d.resolve()), "files": files}


@app.get("/api/saved/{name}")
async def api_saved_file(name: str):
    cfg = load_config()
    if not cfg.output.save_dir or "/" in name or ".." in name:
        raise HTTPException(status_code=404)
    p = Path(cfg.output.save_dir) / name
    if not p.exists():
        raise HTTPException(status_code=404)
    return JSONResponse({"name": name, "content": p.read_text(encoding="utf-8")})
