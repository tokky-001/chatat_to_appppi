# ChatAI_to_API

社内チャットAI（**Web画面のみ・API非公開**）を、ブラウザ自動操作(Playwright)経由で
**OpenAI互換API**として公開するツール。Claude Code や OpenAI SDK から `base_url` を
差し替えるだけで、社内チャットAIをAPIのように呼び出せます。

```
[Claude Code / OpenAI SDK]
        │  OpenAI互換 HTTP (/v1/chat/completions)
        ▼
[ChatAI_to_API サーバ (FastAPI)]
        │  ブラウザ操作 (Playwright)
        ▼
[社内チャットAI の Web画面]
```

## できること
- ✅ 回答結果をファイル保存（`output.save_dir`）
- ✅ Claude Code 等から API として利用（OpenAI互換 `/v1/chat/completions`）
- ✅ コンテキスト最大値を登録 → 超過時は自動で分割・別チャットへ拡張投入
- ✅ ディレクトリのファイル要約を渡して擬似ローカル参照（RAG）
- ✅ 管理GUI（サーバ起動/停止・コンテキスト溢れチェック・質問送信・インデックス管理・回答閲覧）

## セットアップ

**macOS / Linux:**
```bash
# 1. 依存インストール
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. 設定を編集（URL とセレクタを社内チャットAIに合わせる）
#    → config.yaml の chat.url と chat.selectors を調整

# 3. 初回ログイン（ブラウザが開くのでSSO等でログイン）
python -m chatai2api.cli login
```

**Windows (PowerShell):**
```powershell
# 1. 依存インストール（Python 3.9+ が必要）
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

# 2. config.yaml を編集（chat.url / chat.selectors）

# 3. 初回ログイン
python -m chatai2api.cli login
```
※ `Activate.ps1` が実行ポリシーで弾かれる場合は
`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` を一度実行するか、
コマンドプロンプトで `.venv\Scripts\activate.bat` を使ってください。

`config.yaml` の **selectors** は社内チャットAIのHTMLに合わせて必ず調整してください。
ブラウザのF12（開発者ツール）で、入力欄・送信ボタン・回答要素のセレクタを確認します。

## 使い方

### 管理GUI（推奨の入口）
```bash
python -m chatai2api.cli gui
# → http://127.0.0.1:8101/ がブラウザで開く
```
GUIでできること：
- **サーバ起動/停止・初回ログイン** をボタンで操作（状態はヘッダーに常時表示）
- **コンテキストチェッカー**: プロンプト＋添付ファイルのトークン量をリアルタイム計測し、
  社内AIのコンテキスト上限に「収まる / 残りわずか / 溢れる→N分割送信」をメーター表示
- **質問送信**: GUIから直接質問（添付ファイル込み）。回答は自動でファイル保存
- **RAGインデックス管理**: ディレクトリ指定でインデックス作成、進捗ログ表示
- **保存済み回答の一覧/閲覧**

### API サーバとして起動
```bash
python -m chatai2api.cli serve
# → http://127.0.0.1:8100/v1
```

### Claude Code から使う
環境変数で OpenAI 互換エンドポイントとして指定：
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8100/v1"
export OPENAI_API_KEY="local-dummy-key"   # config.yaml の server.api_key と一致させる
```
OpenAI SDK からの例：
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8100/v1", api_key="local-dummy-key")
r = client.chat.completions.create(
    model="internal-chat",
    messages=[{"role": "user", "content": "こんにちは"}],
)
print(r.choices[0].message.content)
```

### 1回だけ質問（CLI）
```bash
python -m chatai2api.cli ask "この関数の意味を説明して"
```

### 擬似ローカルファイル参照（RAG）
ディレクトリのファイルを要約インデックス化し、質問に関連する要約を自動で
プロンプトに注入します。チャットAIがあたかもローカルファイルを参照できる
かのように回答します。

```bash
# 1. インデックス作成（チャットAI自身に各ファイルを要約させる）
python -m chatai2api.cli index ~/projects/myapp

#    AIを使わず先頭抜粋だけで高速にインデックスする場合
python -m chatai2api.cli index ~/projects/myapp --no-ai

# 2. config.yaml で有効化
#    rag:
#      enabled: true

# 3. serve を起動すると、以降のAPIリクエストで関連ファイル要約が自動注入される
python -m chatai2api.cli serve
```

- インデックスは mtime 差分で更新されます（再実行すると変更ファイルだけ再要約）。
- 検索は日本語対応のキーワードスコアリング（埋め込みAPI不要）。
- 注入量は `rag.inject_tokens` / 件数は `rag.top_k` で調整できます。

## 動作の注意
- チャット画面は1タブを直列に使います（同時リクエストは順番待ちになります）。
- 回答完了は「テキストが `stable_seconds` 秒間変化しない」で判定します。
  途中で止まる/切れる場合は `config.yaml` の `stable_seconds` / `response_timeout` を調整。
- `stream: true` は完成済み回答を分割送信する疑似ストリーミングです。

## 構成
| ファイル | 役割 |
|---|---|
| `config.yaml` | URL・セレクタ・コンテキスト上限などの設定 |
| `chatai2api/browser.py` | Playwrightで画面を操作（入力→送信→回答取得） |
| `chatai2api/chat_flow.py` | メッセージ整形＋コンテキスト超過時の分割投入 |
| `chatai2api/server.py` | OpenAI互換 FastAPI サーバ（RAG注入もここ） |
| `chatai2api/rag.py` | ファイル要約インデックス＋日本語キーワード検索 |
| `chatai2api/gui.py` + `static/gui.html` | 管理GUI（Webダッシュボード, port 8101） |
| `chatai2api/cli.py` | login / serve / ask / index / gui のCLI |
