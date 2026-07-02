"""OpenAIのメッセージ配列を、画面チャットAI向けの1本のプロンプトに変換し、
コンテキスト最大値を超える場合は複数チャットに分割して送る(ウィンドウ拡張)。
"""
from __future__ import annotations

from typing import List, Dict

from .browser import ChatBrowser
from .config import Config
from .tokens import count_tokens

ROLE_LABEL = {
    "system": "システム指示",
    "user": "ユーザー",
    "assistant": "アシスタント",
    "tool": "ツール",
}


def flatten_messages(messages: List[Dict[str, str]]) -> str:
    """[{role, content}, ...] を読みやすい1本のテキストに変換する。"""
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # OpenAIのマルチパート形式に一応対応
            content = "".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )
        label = ROLE_LABEL.get(role, role)
        parts.append(f"【{label}】\n{content}")
    return "\n\n".join(parts)


def _split_line(line: str, chunk_tokens: int) -> List[str]:
    """1行が chunk_tokens を超える場合に、文字位置の二分探索で強制分割する。"""
    pieces: List[str] = []
    rest = line
    while rest:
        lo, hi, best = 1, len(rest), 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(rest[:mid]) <= chunk_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        pieces.append(rest[:best])
        rest = rest[best:]
    return pieces


def _split_text(text: str, chunk_tokens: int) -> List[str]:
    """行単位で chunk_tokens 以下のチャンクに分割する。
    改行のない長大な1行は文字単位で強制分割する。"""
    lines = text.split("\n")
    chunks: List[str] = []
    cur: List[str] = []
    cur_tok = 0

    def flush():
        nonlocal cur, cur_tok
        if cur:
            chunks.append("\n".join(cur))
            cur, cur_tok = [], 0

    for line in lines:
        t = count_tokens(line) + 1
        if t > chunk_tokens:
            # 行そのものが上限超え → 現在のチャンクを確定して強制分割
            flush()
            chunks.extend(_split_line(line, chunk_tokens))
            continue
        if cur and cur_tok + t > chunk_tokens:
            flush()
        cur.append(line)
        cur_tok += t
    flush()
    return chunks


async def run_completion(browser: ChatBrowser, cfg: Config, messages: List[Dict[str, str]]) -> str:
    """メッセージ配列を送信して回答を返す。長すぎる場合は分割送信する。"""
    prompt = flatten_messages(messages)
    total = count_tokens(prompt)

    if total <= cfg.context.max_tokens:
        return await browser.ask(prompt)

    # --- コンテキスト最大値を超過 → 分割して段階的に投入(ウィンドウ拡張) ---
    await browser.new_chat()
    chunks = _split_text(prompt, cfg.context.chunk_tokens)
    n = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        if i < n:
            primer = (
                f"[分割入力 {i}/{n}] これは長い入力の一部です。"
                f"まだ質問はしません。内容を記憶し、「受領 {i}/{n}」とだけ返答してください。\n\n"
                f"{chunk}"
            )
            await browser.ask(primer)
        else:
            final = (
                f"[分割入力 {i}/{n} 最終] 以上がすべての入力です。"
                f"これまでの全内容を踏まえて回答してください。\n\n{chunk}"
            )
            return await browser.ask(final)
    return ""
