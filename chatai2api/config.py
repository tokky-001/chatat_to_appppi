"""設定ファイル(config.yaml)の読み込み。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass
class Selectors:
    input: str = "textarea, div[contenteditable='true']"
    send_button: str = "button[type='submit']"
    assistant_message: str = ".assistant-message"
    new_chat: str = ""


@dataclass
class ChatConfig:
    url: str = "https://internal-chat.example.com/"
    user_data_dir: str = "./.browser_profile"
    headless: bool = False
    selectors: Selectors = field(default_factory=Selectors)
    send_key: str = "Enter"
    stable_seconds: float = 2.5
    response_timeout: int = 180


@dataclass
class ContextConfig:
    max_tokens: int = 8000
    chunk_tokens: int = 6000


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8100
    api_key: str = "local-dummy-key"
    model_name: str = "internal-chat"


@dataclass
class OutputConfig:
    save_dir: str = ""


@dataclass
class RagConfig:
    enabled: bool = False
    index_path: str = "./.file_index.json"
    include_ext: list = field(
        default_factory=lambda: [
            ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml",
            ".java", ".go", ".rb", ".sh", ".sql", ".html", ".css", ".csv",
        ]
    )
    max_file_bytes: int = 200_000
    # AI要約に渡すファイル本文の最大文字数
    summary_input_chars: int = 6000
    # 抽出モード(AIなし)のときに使う先頭行数
    extract_lines: int = 30
    # 注入する要約ブロックの最大トークン
    inject_tokens: int = 2000
    top_k: int = 5


@dataclass
class Config:
    chat: ChatConfig = field(default_factory=ChatConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    rag: RagConfig = field(default_factory=RagConfig)


def _merge(dc: Any, data: Dict[str, Any]) -> None:
    """ネストした dataclass に dict の値を上書きする。"""
    for key, value in (data or {}).items():
        if not hasattr(dc, key):
            continue
        current = getattr(dc, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        else:
            setattr(dc, key, value)


def load_config(path: str | os.PathLike = "config.yaml") -> Config:
    cfg = Config()
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _merge(cfg, raw)
    # user_data_dir は絶対パス化しておく
    cfg.chat.user_data_dir = str(Path(cfg.chat.user_data_dir).expanduser().resolve())
    return cfg
