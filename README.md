# gemini-tts-mcp

自分の **Google Cloud プロジェクト**の OAuth クライアントで**ブラウザログイン**し、
**Gemini API**（`generativelanguage.googleapis.com`）経由で
**Gemini 3.8 Flash TTS**（`gemini-3.8-flash-tts`）を叩いて**文章を音声（WAV）にする**
**stdio な MCP サーバー**。MCP Python SDK **2.x**（`MCPServer`）+ stdio 構成、`uvx` 対応。Python 3.10 以上。

**「どんな声にしたいか」を自然文で指定 → 文章を渡す → その声で読み上げた音声ができる**、が基本の使い方です。
声の説明からは Gemini の**ボイスデザイン**（`/v1beta/voices`）で専用の声を作り、
同じ説明なら次回以降も**同じ声を再利用**します。

声を自分で考えなくても、**「AWS のイベントのプレゼンで使いたい」のように場面（`scene`）を伝えるだけ**で、
その場面に合う声と話し方を Gemini が考えて読み上げます。

> 認証は **MCP 内蔵のブラウザ OAuth フロー**です。gcloud や google-auth には依存しません。
> 初回利用時にブラウザが開き、自分の Google アカウントでログイン → トークンを
> MCP 自身が `~/.gemini_tts_mcp_token.json` に保存・自動更新します。
> （gemini-image-mcp / gemini-video-mcp と同じ方式。OAuth クライアントも流用できます。）

## 仕組み

```
MCP クライアント (Amazon Quick / Claude Desktop 等)
        │  stdio (MCP)
        ▼
gemini-tts-mcp
        │  ① 初回: ブラウザOAuthログイン → access/refresh token を保存
        │  ② 以降: refresh token で access token を自動更新
        │  ③ scene があれば gemini-3.8-flash で「声の説明＋話し方」を設計（キャッシュ）
        │  ④ 声の説明があれば POST /v1beta/voices（ボイスデザイン）→ voice_... を作成・キャッシュ
        │  ⑤ POST /v1beta/interactions（Authorization: Bearer）で読み上げ
        ▼
https://generativelanguage.googleapis.com/v1beta/{voices,interactions}
   model: gemini-3.8-flash-tts
        │
        ▼
   音声 (WAV 24kHz / mono / 16bit) ──▶ ファイル保存
```

## セットアップ

Google Cloud Console（https://console.cloud.google.com/）で以下を行います。
**gemini-image-mcp / gemini-video-mcp で作成済みの OAuth クライアント・プロジェクトがあればそのまま使えます**
（手順 1・2 は不要。手順 3 の API 有効化だけ確認してください）。

### 1. OAuth 同意画面の設定（初回のみ）

1. 画面上部のプロジェクト選択で対象プロジェクトを選ぶ。
2. 「**API とサービス**」→「**OAuth 同意画面**」。
3. User Type を選択：
   - Google Workspace 組織内なら「**内部**」（テストユーザー登録が不要）
   - 個人 Gmail 等なら「**外部**」
4. アプリ名・ユーザーサポートメール・デベロッパー連絡先を入力。
5. スコープはここでは追加不要（そのまま進む）。
6. 「**外部**」を選んだ場合は「**テストユーザー**」に自分の Google アカウントを追加
   （これが無いとログイン時に「アクセスをブロック」されます）。

### 2. OAuth クライアントID を作成（デスクトップアプリ型）

1. 「**API とサービス**」→「**認証情報**」。
2. 上部「**+ 認証情報を作成**」→「**OAuth クライアント ID**」。
3. アプリケーションの種類：「**デスクトップアプリ**」を選択（← 重要）。
4. 名前（例: `gemini-tts-mcp-desktop`）を入力して「作成」。
5. 表示される **クライアントID** と **クライアントシークレット** を控える。

> **なぜ「デスクトップアプリ」型か**: この種類だけが `http://127.0.0.1:ポート/...` への
> ループバックリダイレクトを標準で許可します。本 MCP は初回認可時に空きポートを自動選択して
> コールバックを受けるため、デスクトップアプリ型が必須です。

### 3. Generative Language API を有効化

1. 「**API とサービス**」→「**ライブラリ**」。
2. 「**Generative Language API**」（＝ `generativelanguage.googleapis.com`）を検索して有効化。
   - これが Gemini 3.8 Flash TTS（音声合成・ボイスデザイン）を叩く API です。

### 4. 環境変数を設定

| 変数 | 説明 |
|------|------|
| `GEMINI_TTS_CLIENT_ID` | 手順2で作成した OAuth クライアントID（必須） |
| `GEMINI_TTS_CLIENT_SECRET` | 手順2のクライアントシークレット（必須） |
| `GOOGLE_CLOUD_PROJECT` または `GEMINI_TTS_PROJECT_ID` | 使用するプロジェクトID（クォータ帰属。必須） |
| `GEMINI_TTS_MODEL` | 既定モデル。**設定するとこのモデルが最優先**され、ツールの `model` 引数は無視される。未設定なら `model` 引数、無ければ `gemini-3.8-flash-tts`。別名 `flash-tts` / `flash-lite-tts` も可 |
| `GEMINI_TTS_VOICE` | 声の指定が無いときの既定プリビルトボイス。既定 `Kore` |
| `GEMINI_TTS_PLANNER_MODEL` | `scene` から声・話し方を考えるテキストモデル。既定 `gemini-3.8-flash` |
| `GEMINI_TTS_SAMPLE_RATE` | 既定サンプルレート。既定 `24000`（他に `16000` / `8000`） |
| `GEMINI_TTS_OUT_DIR` | `out_path` 省略時の出力先。既定 `~/gemini-tts`（例: `~/Downloads`。`~` はホームに展開される） |
| `GEMINI_TTS_VOICE_CACHE` | 「声の説明 → voice_id」「scene → プラン」キャッシュの保存先。既定 `~/.gemini_tts_mcp_voices.json` |
| `GEMINI_TTS_TIMEOUT` | API タイムアウト秒。既定 `300` |
| `GEMINI_TTS_REDIRECT_PORT` | 任意。初回認可のローカルポート（未指定なら空きポートを自動選択） |

### 5. 初回認証

登録後に `generate_speech`（または `reauthorize`）を実行すると **ブラウザが自動で開き**、
Google ログイン画面が表示されます。自分のアカウントでログイン・許可すると、トークンが
`~/.gemini_tts_mcp_token.json` に保存され、以降は refresh token で自動更新されます。

## 声の指定方法

`generate_speech` では次のどれかで声を指定します（どれも無ければ既定のプリビルトボイス `Kore`）。

| 指定 | 動作 |
|------|------|
| `scene="AWS のイベントのプレゼンで使いたい"` | **場面から自動で考える**。場面に合う声（ボイスデザイン用の説明）と話し方（`style`）を Gemini が設計して読み上げる。**同じ scene なら毎回同じ声・話し方** |
| `voice_description="落ち着いた40代男性のナレーター。低めで温かい声"` | **ボイスデザイン**。説明から声を作って読み上げる。作った声は保存され、**同じ説明なら次回以降も同じ声**を使う |
| `voice="Kore"` | **プリビルトボイス**（30 種）をそのまま使う。最速・追加コストなし |
| `voice="voice_xxxx"` | `design_voice` などで作った**保存ボイス**を ID で使う |
| `voice="Puck"` + `voice_description="もう少し低く落ち着いた感じに"` | **リミックス**。`voice` を元に、説明のとおりに変えた声を作る |

**話し方**（感情・速さ・抑揚など、その場の話し方）は `style` で指定します。

- 場面・用途・聞き手（どこで誰に向けて使うか） → `scene`（声と話し方をおまかせ）
- 恒常的な特徴（年齢・性別・声質・なまり） → `voice_description`
- その場の話し方（嬉しそうに、ささやくように、ゆっくり） → `style`
- 一瞬の出来事（笑い・ため息・間） → 本文中のタグ `<laugh>` `<sigh>` `<short pause>` `<long pause>` `<breath>` など（英語表記）

> `style` の内容を本文に「嬉しそうに言って：」のように書くと、その指示まで読み上げられてしまいます。
> 話し方は必ず `style` に分けてください。`style` は短い方が効果的です（まず空で試し、必要なところだけ足す）。

### シーン（場面）から声と話し方を考える

`scene` に「どこで・誰に向けて使うか」を書くと、`gemini-3.8-flash` が TTS の書き分けルールに沿って
次を設計し、そのまま読み上げに使います。

| 設計される項目 | 例（scene="AWS のイベントのプレゼンで使いたい"） |
|----------------|------|
| 声の説明（→ ボイスデザインで声を作成） | A confident, articulate tech conference presenter in her early 30s with a clear, warm mid-range voice. |
| 話し方 `style` | clear and confident, upbeat, measured pace |
| 性別・言語 | female / ja-JP（言語は本文から判定） |
| 理由 `rationale` | 技術イベントの登壇に合う、聞き取りやすく自信のある声です。 |

- **同じ scene なら同じ声**: プランはキャッシュされるので、スライドごとに分けて生成しても声と話し方が揃います。
  考え直したいときは `replan=True`（新しい声になります）。
- **部分的に上書きできる**: `voice` / `voice_description` / `style` / `gender` / `language_code` を指定した項目は
  そちらが優先されます（例: 声は `Kore` に固定し、話し方だけ scene に合わせる）。
- **事前に確認できる**: `plan_speech(scene=..., text=...)` で音声を作らずにプランだけ見られます。
  そのあと同じ scene で `generate_speech` を呼ぶと、確認したプランがそのまま使われます。
- **間の自動挿入（任意）**: `enhance_text=True` にすると、本文に `<short pause>` などのタグを入れてから読み上げます
  （言葉は一切変えません。万一変わってしまった場合は元の文章のまま読み上げます）。

### プリビルトボイス（30 種）

| 声 | 特徴 | 声 | 特徴 |
|----|------|----|------|
| Zephyr | Bright（明るい） | Achernar | Soft（やわらか） |
| Puck | Upbeat（アップビート） | Alnilam | Firm（しっかり） |
| Charon | Informative（情報的） | Schedar | Even（落ち着いた） |
| Kore | Firm（しっかり） | Gacrux | Mature（大人びた） |
| Fenrir | Excitable（興奮気味） | Pulcherrima | Forward（積極的） |
| Leda | Youthful（若々しい） | Achird | Friendly（親しみやすい） |
| Orus | Firm（しっかり） | Zubenelgenubi | Casual（カジュアル） |
| Aoede | Breezy（軽やか） | Vindemiatrix | Gentle（穏やか） |
| Callirrhoe | Easy-going（おおらか） | Sadachbia | Lively（生き生き） |
| Autonoe | Bright（明るい） | Sadaltager | Knowledgeable（知的） |
| Enceladus | Breathy（息まじり） | Sulafat | Warm（温かい） |
| Iapetus | Clear（クリア） | Laomedeia | Upbeat（アップビート） |
| Umbriel | Easy-going（おおらか） | Algieba | Smooth（なめらか） |
| Despina | Smooth（なめらか） | Erinome | Clear（クリア） |
| Algenib | Gravelly（しゃがれ声） | Rasalgethi | Informative（情報的） |

### モデル

| モデルID | 説明 |
|---------|------|
| `gemini-3.8-flash-tts` | **既定**。表現力・声の忠実度重視。ボイスデザイン対応。130 以上の言語（自動判定） |
| `gemini-3.8-flash-lite-tts` | 速度・コスト重視。ボイスデザイン対応。100 以上の言語 |
| `gemini-3.1-flash-tts-preview` / `gemini-2.5-pro-preview-tts` | 旧世代。プリビルトボイスのみ（`voice_description` 不可） |

優先順位は gemini-image-mcp / gemini-video-mcp と同じく、**環境変数 `GEMINI_TTS_MODEL`（最優先）→ `model` 引数 → 既定**。
別名: `flash-tts` / `tts` → `gemini-3.8-flash-tts`、`flash-lite-tts` / `lite` → `gemini-3.8-flash-lite-tts`。

## ツール

| ツール | 説明 |
|--------|------|
| `generate_speech(text, scene?, voice_description?, voice?, style?, enhance_text?, out_path?, gender?, language_code?, model?, sample_rate?, replan?, project_id?)` | **メイン**。文章を指定の声（または場面に合う声）で読み上げた WAV を生成する |
| `plan_speech(scene, text?, gender?, language_code?, model?, enhance_text?, replan?, project_id?)` | 場面に合う声・話し方のプランだけを作る（音声は作らない。確認用） |
| `design_voice(description, display_name?, gender?, language_code?, pitch?, accent?, base_voice?, model?, sample_out_path?, project_id?)` | 声の説明から新しい声を作り、試聴サンプル WAV を保存する。返った `voice_id` を `generate_speech(voice=...)` で使う |
| `list_voices(language_code?, gender?, voice_type?, search?, page_size?, page_token?, project_id?)` | 使える声の一覧（自分の保存ボイス → プリビルト）と、この MCP で作った声（説明文 → voice_id）・scene のプラン |
| `delete_voice(voice_id, project_id?)` | 保存ボイス（`voice_...`）を削除する（上限 200 件の枠を空ける） |
| `list_models()` | モデル・別名・プリビルトボイス・サンプルレート・タグ例の一覧 |
| `auth_status()` | 認証・プロジェクト・モデルの準備状況を確認する |
| `reauthorize()` | ブラウザで再ログインしてトークンを取り直す |

### `generate_speech` の詳細

- **引数**:
  - `text`（必須）: 読み上げる文章。言語は自動判定。強調したい語は大文字、句読点や「…」で間、`<laugh>` 等のタグで一瞬の表現。
  - `scene`: 使う場面・用途・聞き手（例: `"AWS のイベントでのプレゼン。聴衆はエンジニア数百人"`）。声と話し方を自動で設計。
  - `voice_description`: どんな声にしたいかの説明（年齢・性別・声質・雰囲気など恒常的な特徴を 1〜2 文で）。
  - `voice`: プリビルトボイス名（例 `Kore`）または `voice_...`。`voice_description` と併用するとリミックスの元になる。
    ※ `voice` に説明文（空白や日本語を含む文字列）を渡した場合は `voice_description` として扱います。
  - `style`: 話し方の指示（例: `"嬉しそうに、ややゆっくり"` / `"whispered urgently"`）。`scene` より優先。
  - `enhance_text`: `True` で本文に間（`<short pause>` 等）を自動挿入してから読み上げる（言葉は変えない）。
  - `out_path`: 出力先（.wav）。省略時は `GEMINI_TTS_OUT_DIR` に自動命名で保存。
  - `gender` / `language_code`: `voice_description` で声を作るときの補助情報（`female` / `male` / `neutral`、`ja-JP` 等）。
  - `model`: 使用モデル（`GEMINI_TTS_MODEL` 設定時は無視）。
  - `sample_rate`: `24000`（既定）/ `16000` / `8000`。
  - `replan`: `True` で scene のプランを考え直す（声も新しくなる）。
  - `project_id`: プロジェクトID上書き（クォータ帰属用）。
- **戻り値**: `success` / `out_path` / `files` / `duration_sec` / `voice` / `voice_source`（`prebuilt` / `designed (new)` / `designed (cached)` / `custom` / `default` 等）/ `voice_id` / `style` / `scene_plan`（scene 指定時: 声の説明・話し方・理由）/ `text_used`（enhance_text 時）/ `model` / `sample_rate` / `project_id`

### 使い方の例

- **場面を伝えておまかせ（AWS イベントのプレゼン）**:
  `generate_speech(text="皆さん、こんにちは。本日は Amazon Bedrock の最新機能をご紹介します。", scene="AWS のイベントのプレゼンで使いたい", enhance_text=True)`
  - スライドごとに分けるなら、同じ `scene` のまま `text` だけ変えて呼ぶ（声と話し方が揃う）。`out_path="~/Downloads/slide01.wav"` のように名前を付けると管理しやすい。
  - 先にどんな声になるか見たいなら `plan_speech(scene="AWS のイベントのプレゼンで使いたい", text="...")`。
  - 声は固定したまま話し方だけ合わせるなら `generate_speech(text="...", scene="...", voice="Kore")`。
- **声を説明して読み上げ（基本）**:
  `generate_speech(text="本日はご来場いただき、誠にありがとうございます。", voice_description="落ち着いた30代女性のアナウンサー。明瞭で上品な声", language_code="ja-JP")`
- **話し方も指定**:
  `generate_speech(text="やったー！ついに完成したよ！", voice_description="元気な10代の男の子", style="大喜びで、少し早口")`
- **プリビルトボイスで手早く**: `generate_speech(text="Have a wonderful day!", voice="Puck", style="cheerful and friendly")`
- **タグで間や笑いを入れる**: `generate_speech(text="えっと… <short pause> 実はね <laugh> 忘れてたんだ。", voice="Leda")`
- **声を試聴してから使う**:
  1. `design_voice(description="しゃがれた低い声の老人の魔法使い。ゆったり話す", gender="male", language_code="ja-JP")` → 試聴サンプルと `voice_id` が返る
  2. 気に入ったら `generate_speech(text="...", voice="voice_xxxx")`、気に入らなければ説明を変えて 1 をやり直す
- **既存の声をアレンジ（リミックス）**: `generate_speech(text="...", voice="Kore", voice_description="もう少し柔らかく、若々しく")`
- **速さ・コスト優先**: `generate_speech(text="...", voice="Kore", model="gemini-3.8-flash-lite-tts")`

> **ボイスデザインのコツ**: 説明は 1〜2 文の短く明確なものが最も効きます（長すぎ・矛盾する説明は逆効果）。
> 年齢・性別・声質・なまりなど**変わらない特徴は説明に**、感情や速さなど**その場の話し方は `style` に**書き分けてください。
> 例: "A crisp, energetic sports announcer in her 30s with a slight Midwestern accent."

### 保存ボイスの上限と再利用

- ボイスデザインで作った声はプロジェクトに**保存ボイス（`voice_...`）**として保存されます。
  上限は**プロジェクトあたり 200 件**、**最終使用から 1 年**で期限切れ（使うたびに延長）。
- 本 MCP は「声の説明＋条件（モデル・性別・言語・リミックス元）→ voice_id」を
  `~/.gemini_tts_mcp_voices.json` にキャッシュし、同じ説明では新しい声を作らずに再利用します
  （声質のブレを防ぎ、枠も節約）。説明を 1 文字でも変えると別の声になります。
- キャッシュした声が期限切れ・削除済みだった場合は、自動で作り直して再試行します。
- 不要な声は `delete_voice(voice_id)` で削除できます（`list_voices(voice_type="prompted")` で一覧）。

### 出力先の注意（Amazon Quick）

`out_path` は Amazon Quick の許可フォルダ内（例 `/Users/<you>/PycharmProjects/...`）に
指定してください。既定の `~/gemini-tts` はエージェントのアクセス許可外で再生できない場合があります。

## 起動 / 登録

```
uvx --from git+https://github.com/tsukumonasu/gemini-tts-mcp gemini-tts-mcp
```

ローカルから:

```
cd /path/to/gemini-tts-mcp
uvx --from . gemini-tts-mcp
```

> **ローカルのソースを編集したとき**: `uvx --from <フォルダ>` は一度ビルドした結果をキャッシュするため、
> コードを変えても古い版が起動し続けることがあります。改修を反映させたいときは
> `uvx --no-cache --from <フォルダ> gemini-tts-mcp`（MCP 設定なら `args` の先頭に `"--no-cache"`）で起動してください。
> GitHub から起動する場合（`git+https://...`）は push した最新のコミットが使われます。

## JSON での登録例

MCP クライアント（Amazon Quick / Claude Desktop 等）の設定ファイルに、以下のように
`mcpServers` エントリを追加します。`env` の値は自分の OAuth クライアント・プロジェクトに置き換えてください。

### ローカルのソースから起動する場合

```json
{
  "mcpServers": {
    "gemini-tts": {
      "command": "uvx",
      "args": [
        "--from",
        "/path/to/gemini-tts-mcp",
        "gemini-tts-mcp"
      ],
      "env": {
        "GEMINI_TTS_CLIENT_ID": "xxxxxxxx.apps.googleusercontent.com",
        "GEMINI_TTS_CLIENT_SECRET": "GOCSPX-xxxxxxxx",
        "GOOGLE_CLOUD_PROJECT": "your-gcp-project-id",
        "GEMINI_TTS_MODEL": "gemini-3.8-flash-tts",
        "GEMINI_TTS_VOICE": "Kore",
        "GEMINI_TTS_OUT_DIR": "~/Downloads"
      }
    }
  }
}
```

### GitHub から直接起動する場合

```json
{
  "mcpServers": {
    "gemini-tts": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/tsukumonasu/gemini-tts-mcp",
        "gemini-tts-mcp"
      ],
      "env": {
        "GEMINI_TTS_CLIENT_ID": "xxxxxxxx.apps.googleusercontent.com",
        "GEMINI_TTS_CLIENT_SECRET": "GOCSPX-xxxxxxxx",
        "GOOGLE_CLOUD_PROJECT": "your-gcp-project-id"
      }
    }
  }
}
```

> **補足**
> - `env` に必要なのは最低限 `GEMINI_TTS_CLIENT_ID` / `GEMINI_TTS_CLIENT_SECRET` /
>   `GOOGLE_CLOUD_PROJECT`（または `GEMINI_TTS_PROJECT_ID`）の3つ。残りは任意（既定値あり）。
> - gemini-image-mcp / gemini-video-mcp と同じクライアントID・シークレットを入れて構いません
>   （トークンファイルは別なので、初回だけ本 MCP 用にブラウザログインが走ります）。
> - シークレットを設定ファイルに直書きしたくない場合は、シェルの環境変数として export しておき
>   `env` から該当キーを省くこともできます（クライアントが親プロセスの環境変数を引き継ぐ場合）。

## つまずきポイント

| 症状 | 原因・対処 |
|------|-----------|
| ログイン時「アクセスをブロック」 | OAuth同意画面が「外部」でテストユーザー未登録 → セットアップ手順1-6で自分を追加 |
| `redirect_uri_mismatch` | クライアントが「デスクトップアプリ」型でない → セットアップ手順2で作り直す |
| `403 PERMISSION_DENIED` | Generative Language API 未有効化 → セットアップ手順3を実施 |
| `voice_description` で「ボイスデザインに対応していません」 | `GEMINI_TTS_MODEL` / `model` が旧世代 → `gemini-3.8-flash-tts` か `gemini-3.8-flash-lite-tts` にする |
| 話し方の指示まで読み上げられる | 指示を本文に書いている → `style` に分ける |
| scene の声がイメージと違う | `plan_speech` で確認 → `replan=True` で考え直す、または `gender` / `voice_description` / `style` で一部を指定 |
| scene 指定時に `404` / モデルが見つからない | プランナーのモデルが使えない → `GEMINI_TTS_PLANNER_MODEL` で別のテキストモデル（例 `gemini-3.6-flash`）を指定 |
| 保存ボイスが作れない（上限） | 保存ボイス 200 件の上限 → `list_voices(voice_type="prompted")` で確認し `delete_voice` で整理 |
| `reauthorize` が `Address already in use` | 前回の OAuth ローカルサーバーがポートを占有。占有ポートを解放（`lsof -ti :<port> \| xargs kill -9`）→ `rm ~/.gemini_tts_mcp_token.json` → 再実行 |

## 注記

- 出力は **WAV（24kHz / モノラル / 16bit PCM、RIFF ヘッダ付き）**。3.8 系の unary 応答の既定形式をそのまま保存します。
  旧モデル等でヘッダ無しの PCM が返った場合は、WAV ヘッダを付けて保存します。
- 入力は最大 **8,192 トークン**。長い原稿は段落ごとなどに分けて生成してください。
- 本 MCP は REST の `steps` 配列（`model_output` の `content` 内 `type=audio`）から音声を取り出します。
  SDK 専用の `interaction.output_audio` は使いません。API の仕様変更に備えて抽出処理（`api.extract_audio`）は防御的に実装しています。
- 1 リクエスト 1 話者です（複数話者の掛け合いは未対応）。
- `scene` を使うと、プラン作成（scene ごとに初回 1 回）と `enhance_text`（毎回）でテキストモデルの呼び出しが加わります。
- 本 MCP は **MCP Python SDK 2.x**（`mcp>=2,<3`）を使用しています。入力不備・API エラー・認証エラーは
  `ToolError` に変換してメッセージをクライアントに返します。

出所（公式ドキュメント）:
[Gemini 3.8 Flash TTS](https://aistudio.google.com/docs/models/gemini-3.8-flash-tts) /
[Speech generation](https://aistudio.google.com/docs/speech-generation) /
[Voice design](https://aistudio.google.com/docs/voice-design) /
[Voices API](https://aistudio.google.com/api-reference/voices) /
[Models](https://aistudio.google.com/docs/models)

## ライセンス

MIT
