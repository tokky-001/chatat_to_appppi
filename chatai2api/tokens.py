"""トークン数の推定。tiktoken があれば正確に、無ければ文字数から概算。"""
from __future__ import annotations

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # tiktoken 未インストール / データ取得失敗
    _ENC = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    # 概算: 日本語は1文字≒1トークン、英字は約4文字で1トークン。
    # 安全側に倒して「max(文字数/2, 単語数)」で見積もる。
    chars = len(text)
    words = len(text.split())
    return max(chars // 2, words)
