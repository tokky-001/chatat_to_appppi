"""コマンドライン入口。

  python -m chatai2api.cli login   # 初回ログイン(ブラウザでSSO) → プロファイル保存
  python -m chatai2api.cli serve   # OpenAI互換APIサーバを起動
  python -m chatai2api.cli ask "質問"   # 1回だけ質問して結果を表示/保存
  python -m chatai2api.cli index <dir> [--no-ai]  # 擬似ローカル参照用インデックス作成
  python -m chatai2api.cli gui    # 管理GUI(Webダッシュボード)を起動
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from .browser import ChatBrowser
from .config import load_config
from .rag import FileIndex


async def _cmd_login() -> None:
    cfg = load_config()
    if cfg.chat.headless:
        print("※ login は headless: false で実行してください(config.yaml)。", file=sys.stderr)
    b = ChatBrowser(cfg.chat)
    await b.start()
    print("ブラウザが開きました。社内チャットAIにログインしてください…")
    await b.wait_for_login()
    print(f"ログイン確認OK。プロファイルを保存しました: {cfg.chat.user_data_dir}")
    await b.stop()


async def _cmd_ask(text: str) -> None:
    cfg = load_config()
    b = ChatBrowser(cfg.chat)
    await b.start()
    answer = await b.ask(text)
    await b.stop()
    print(answer)
    if cfg.output.save_dir:
        d = Path(cfg.output.save_dir)
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        (d / f"{ts}.md").write_text(
            f"# {ts}\n\n## 質問\n\n{text}\n\n## 回答\n\n{answer}\n", encoding="utf-8"
        )
        print(f"\n(保存: {d / (ts + '.md')})", file=sys.stderr)


async def _cmd_index(directory: str, no_ai: bool) -> None:
    cfg = load_config()
    index = FileIndex(cfg.rag)
    browser = None
    if not no_ai:
        browser = ChatBrowser(cfg.chat)
        await browser.start()
        print("チャットAIでファイルを要約します（時間がかかります）…")
    else:
        print("抽出モード（AIなし・先頭抜粋）でインデックスします…")
    try:
        updated, total = await index.build(directory, browser=browser)
    finally:
        if browser:
            await browser.stop()
    print(f"完了: {updated} 件更新 / 対象 {total} 件 → {index.path}")
    print("config.yaml の rag.enabled を true にすると、serve 時に自動注入されます。")


def _cmd_serve() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "chatai2api.server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=False,
    )


def _cmd_gui(port: int, no_browser: bool) -> None:
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if not no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"管理GUI: {url}")
    uvicorn.run("chatai2api.gui:app", host="127.0.0.1", port=port, reload=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="chatai2api")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="初回ログイン(プロファイル保存)")
    sub.add_parser("serve", help="OpenAI互換APIサーバを起動")
    p_ask = sub.add_parser("ask", help="1回だけ質問する")
    p_ask.add_argument("text", help="質問文")
    p_idx = sub.add_parser("index", help="ディレクトリを要約インデックス化(擬似ローカル参照用)")
    p_idx.add_argument("directory", help="対象ディレクトリ")
    p_idx.add_argument("--no-ai", action="store_true", help="AI要約を使わず先頭抜粋でインデックス")
    p_gui = sub.add_parser("gui", help="管理GUI(Webダッシュボード)を起動")
    p_gui.add_argument("--port", type=int, default=8101)
    p_gui.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")

    args = parser.parse_args()
    if args.cmd == "login":
        asyncio.run(_cmd_login())
    elif args.cmd == "ask":
        asyncio.run(_cmd_ask(args.text))
    elif args.cmd == "index":
        asyncio.run(_cmd_index(args.directory, args.no_ai))
    elif args.cmd == "serve":
        _cmd_serve()
    elif args.cmd == "gui":
        _cmd_gui(args.port, args.no_browser)


if __name__ == "__main__":
    main()
