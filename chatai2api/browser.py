"""Playwright で社内チャットAIのWeb画面を自動操作するドライバ。

- launch_persistent_context を使い、user_data_dir にログイン状態を保存する。
  → 初回だけ手動ログインすれば、以降は自動でログイン済みになる。
- 1つのタブを直列に使う(personal use 想定)。asyncio.Lock で排他する。
- 回答の完了は「回答テキストが一定時間変化しなくなったら完了」で判定する。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page, Playwright

from .config import ChatConfig


class ChatBrowser:
    def __init__(self, cfg: ChatConfig):
        self.cfg = cfg
        self._pw: Optional[Playwright] = None
        self._ctx = None  # BrowserContext (persistent)
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    # ---- ライフサイクル ------------------------------------------------
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # persistent context = プロファイル永続化(Cookie/SSO維持)
        self._ctx = await self._pw.chromium.launch_persistent_context(
            self.cfg.user_data_dir,
            headless=self.cfg.headless,
            viewport={"width": 1280, "height": 900},
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._page.goto(self.cfg.url, wait_until="domcontentloaded")

    async def stop(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def wait_for_login(self, poll: float = 2.0, timeout: float = 600) -> None:
        """入力欄が現れる=ログイン完了 とみなして待機する(初回セットアップ用)。"""
        assert self._page is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            el = await self._page.query_selector(self.cfg.selectors.input)
            if el:
                return
            await asyncio.sleep(poll)
        raise TimeoutError("ログイン(入力欄の出現)を確認できませんでした。")

    # ---- 送信/受信 ----------------------------------------------------
    async def _assistant_count(self) -> int:
        assert self._page is not None
        return len(await self._page.query_selector_all(self.cfg.selectors.assistant_message))

    async def _last_assistant_text(self) -> str:
        assert self._page is not None
        els = await self._page.query_selector_all(self.cfg.selectors.assistant_message)
        if not els:
            return ""
        return (await els[-1].inner_text()) or ""

    async def new_chat(self) -> None:
        if not self.cfg.selectors.new_chat:
            return
        assert self._page is not None
        btn = await self._page.query_selector(self.cfg.selectors.new_chat)
        if btn:
            await btn.click()
            await asyncio.sleep(1.0)

    async def ask(self, prompt: str) -> str:
        """1メッセージを送って回答テキストを返す。呼び出しは直列化される。"""
        async with self._lock:
            return await self._ask_locked(prompt)

    async def _ask_locked(self, prompt: str) -> str:
        assert self._page is not None
        sel = self.cfg.selectors

        before = await self._assistant_count()

        # 入力欄に入力
        inp = await self._page.wait_for_selector(sel.input, timeout=30000)
        await inp.click()
        # contenteditable / textarea どちらでも動くよう fill を試し、
        # ダメなら type でフォールバック
        try:
            await inp.fill(prompt)
        except Exception:
            await inp.type(prompt)

        # 送信
        sent = False
        if sel.send_button:
            btn = await self._page.query_selector(sel.send_button)
            if btn and await btn.is_enabled():
                await btn.click()
                sent = True
        if not sent:
            await inp.press(self.cfg.send_key)

        # 新しい回答要素が増えるのを待つ
        await self._wait_new_message(before)
        # 回答が安定(=生成完了)するまで待つ
        return await self._wait_stable()

    async def _wait_new_message(self, before: int) -> None:
        deadline = time.time() + self.cfg.response_timeout
        while time.time() < deadline:
            if await self._assistant_count() > before:
                return
            # 一部UIは同じ要素を使い回す。テキストが出ていればOKとみなす。
            if before == 0 and (await self._last_assistant_text()).strip():
                return
            await asyncio.sleep(0.3)
        raise TimeoutError("回答要素の出現を確認できませんでした。selectors を確認してください。")

    async def _wait_stable(self) -> str:
        deadline = time.time() + self.cfg.response_timeout
        last_text = ""
        last_change = time.time()
        while time.time() < deadline:
            text = await self._last_assistant_text()
            if text != last_text:
                last_text = text
                last_change = time.time()
            elif text and (time.time() - last_change) >= self.cfg.stable_seconds:
                return text.strip()
            await asyncio.sleep(0.3)
        # タイムアウトでも取得できたぶんは返す
        return last_text.strip()
