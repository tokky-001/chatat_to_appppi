"""擬似ローカルファイル参照 (RAG)。

ディレクトリのファイルを要約してインデックス化し、質問に関連する要約を
プロンプト先頭に注入することで、API の無いチャットAIに「ローカルファイルを
参照できる」かのような動作をさせる。

- 要約は チャットAI自身(browser.ask) か、AIなしの抽出モード(先頭抜粋)で生成。
- 検索は 埋め込みAPI が使えないため、CJKバイグラム+英単語のキーワード
  スコアリングで行う。
- インデックスは JSON で保存し、mtime が変わったファイルだけ再要約する。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .browser import ChatBrowser
from .config import RagConfig
from .tokens import count_tokens

SUMMARY_PROMPT = (
    "次のファイルの内容を、後で検索・参照しやすいように日本語で要約してください。"
    "目的・主な内容・重要なキーワード(関数名/設定名/固有名詞)を箇条書きで含め、"
    "300字程度にまとめてください。要約のみを出力してください。\n\n"
    "ファイル名: {name}\n---\n{body}"
)


# ---- テキスト分かち(日本語対応の簡易トークナイザ) -------------------------
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")


def terms(text: str) -> List[str]:
    """英単語 + CJKバイグラム を検索語として抽出する。"""
    out = [w.lower() for w in _ASCII_WORD.findall(text)]
    for seq in _CJK.findall(text):
        if len(seq) == 1:
            out.append(seq)
        else:
            out.extend(seq[i : i + 2] for i in range(len(seq) - 1))
    return out


# ---- インデックス ----------------------------------------------------------
class FileIndex:
    def __init__(self, cfg: RagConfig):
        self.cfg = cfg
        self.path = Path(cfg.index_path)
        self.entries: Dict[str, dict] = {}  # abs path -> {mtime, summary, terms}
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # -- 対象ファイル列挙 --
    def _iter_files(self, root: Path) -> List[Path]:
        exts = set(self.cfg.include_ext)
        skip_dirs = {".git", ".venv", "node_modules", "__pycache__", ".browser_profile"}
        files: List[Path] = []
        for p in sorted(root.rglob("*")):
            if any(part in skip_dirs for part in p.parts):
                continue
            if p.is_file() and p.suffix.lower() in exts and p.stat().st_size <= self.cfg.max_file_bytes:
                files.append(p)
        return files

    # -- 構築/更新 --
    async def build(
        self,
        root: str,
        browser: Optional[ChatBrowser] = None,
        progress=print,
    ) -> Tuple[int, int]:
        """root 以下をインデックス化する。browser を渡すとAI要約、無ければ抽出要約。
        戻り値: (更新件数, 総件数)"""
        rootp = Path(root).expanduser().resolve()
        files = self._iter_files(rootp)
        updated = 0
        for p in files:
            key = str(p)
            mtime = p.stat().st_mtime
            ent = self.entries.get(key)
            if ent and ent.get("mtime") == mtime:
                continue  # 変更なし
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            summary = await self._summarize(p.name, body, browser)
            self.entries[key] = {
                "mtime": mtime,
                "summary": summary,
                "terms": sorted(set(terms(p.name) + terms(summary))),
                "indexed_at": time.time(),
            }
            updated += 1
            progress(f"  indexed: {p.name}")
        # 消えたファイルを掃除
        live = {str(p) for p in files}
        for key in [k for k in self.entries if k.startswith(str(rootp)) and k not in live]:
            del self.entries[key]
        self.save()
        return updated, len(files)

    async def _summarize(self, name: str, body: str, browser: Optional[ChatBrowser]) -> str:
        if browser is not None:
            # チャットAI自身に要約させる。長すぎる場合は先頭を切り出す。
            excerpt = body[: self.cfg.summary_input_chars]
            return await browser.ask(SUMMARY_PROMPT.format(name=name, body=excerpt))
        # 抽出モード: 先頭の意味のある行を抜粋
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        return "\n".join(lines[: self.cfg.extract_lines])

    # -- 検索 --
    def search(self, query: str, top_k: Optional[int] = None) -> List[Tuple[str, dict, float]]:
        q = set(terms(query))
        if not q:
            return []
        scored: List[Tuple[str, dict, float]] = []
        for key, ent in self.entries.items():
            ts = set(ent.get("terms", []))
            hit = q & ts
            if not hit:
                continue
            # マッチ語数を基本スコアに、ファイル名一致を加点
            score = float(len(hit))
            name_terms = set(terms(Path(key).name))
            score += 2.0 * len(q & name_terms)
            scored.append((key, ent, score))
        scored.sort(key=lambda x: -x[2])
        return scored[: top_k or self.cfg.top_k]

    # -- プロンプト注入ブロック生成 --
    def build_context_block(self, query: str) -> str:
        hits = self.search(query)
        if not hits:
            return ""
        budget = self.cfg.inject_tokens
        parts: List[str] = [
            "【参照情報】以下はユーザーのローカルファイルの要約です。"
            "回答の際、関連するファイルの内容として参照してください。"
        ]
        used = count_tokens(parts[0])
        for key, ent, _ in hits:
            block = f"\n■ ファイル: {key}\n{ent['summary']}"
            t = count_tokens(block)
            if used + t > budget:
                break
            parts.append(block)
            used += t
        if len(parts) == 1:
            return ""
        return "\n".join(parts)
