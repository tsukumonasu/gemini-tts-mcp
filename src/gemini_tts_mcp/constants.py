"""
Gemini 3.8 Flash TTS 音声生成の定数。

認証は MCP 内蔵のブラウザ OAuth を使う（auth.py 参照）。gcloud / google-auth には依存しない。
gemini-video-mcp と同じく Gemini API（generativelanguage.googleapis.com）を叩く。
  - 音声合成  : POST /v1beta/interactions（response_format.type = "audio"）
  - ボイス管理: POST/GET/DELETE /v1beta/voices（ボイスデザイン＝自然文から声を作る）

出所（公式ドキュメント）:
  - https://aistudio.google.com/docs/models/gemini-3.8-flash-tts
    （モデルID、入力 8,192 / 出力 16,384 トークン、130+ 言語・自動言語判定、
      3.8 では unary の既定出力が WAV（RIFF ヘッダ付き）に変更）
  - https://aistudio.google.com/docs/speech-generation
    （interactions のリクエスト形式、speech_config、speech_metadata.style、
      インラインタグ <laugh> 等、プリビルトボイス 30 種、出力フォーマット）
  - https://aistudio.google.com/docs/voice-design（ボイスデザイン: POST /v1beta/voices）
  - https://aistudio.google.com/api-reference/voices（Voice リソース定義）
"""

import os

# --- Gemini API エンドポイント ---
GEMINI_API_HOST = os.environ.get(
    "GEMINI_TTS_API_HOST", "https://generativelanguage.googleapis.com"
)
API_VERSION = os.environ.get("GEMINI_TTS_API_VERSION", "v1beta")


def build_interactions_url() -> str:
    """interactions（音声合成）エンドポイントの完全な URL。"""
    return f"{GEMINI_API_HOST}/{API_VERSION}/interactions"


def build_voices_url(voice_id: str | None = None) -> str:
    """voices（ボイス作成・取得・一覧・削除）エンドポイントの完全な URL。"""
    base = f"{GEMINI_API_HOST}/{API_VERSION}/voices"
    return f"{base}/{voice_id}" if voice_id else base


def build_generate_content_url(model: str) -> str:
    """テキストモデルの generateContent（シーンからの声・話し方の設計に使う）。"""
    return f"{GEMINI_API_HOST}/{API_VERSION}/models/{model}:generateContent"


# --- シーン → 声・話し方を考えるプランナー（テキストモデル）---
# 出所: https://aistudio.google.com/docs/models（gemini-3.8-flash: 最新の Flash）
PLANNER_MODEL = os.environ.get("GEMINI_TTS_PLANNER_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"


# --- モデル（GEMINI_TTS_MODEL / MCP 設定で切替可能）---
FLASH_TTS = "gemini-3.8-flash-tts"            # 表現力重視（既定）
FLASH_LITE_TTS = "gemini-3.8-flash-lite-tts"  # 速度・コスト重視
FLASH_TTS_3_1 = "gemini-3.1-flash-tts-preview"  # 旧世代（プリビルトボイスのみ）
PRO_TTS_2_5 = "gemini-2.5-pro-preview-tts"      # 旧世代（プリビルトボイスのみ）

MODEL_ALIASES = {
    "flash-tts": FLASH_TTS,
    "tts": FLASH_TTS,
    "gemini-flash-tts": FLASH_TTS,
    "3.8-flash-tts": FLASH_TTS,
    "flash-lite-tts": FLASH_LITE_TTS,
    "lite-tts": FLASH_LITE_TTS,
    "lite": FLASH_LITE_TTS,
    "gemini-flash-lite-tts": FLASH_LITE_TTS,
    "3.8-flash-lite-tts": FLASH_LITE_TTS,
    "3.1-flash-tts": FLASH_TTS_3_1,
    "gemini-3.1-flash-tts": FLASH_TTS_3_1,
}
SUPPORTED_MODELS = [FLASH_TTS, FLASH_LITE_TTS, FLASH_TTS_3_1, PRO_TTS_2_5]
# ボイスデザイン（voice_... の独自ボイス）を使えるモデル。旧世代はプリビルトボイスのみ。
VOICE_DESIGN_MODELS = [FLASH_TTS, FLASH_LITE_TTS]

# 環境変数 GEMINI_TTS_MODEL が設定されていれば、それを常に使う（呼び出し側の
# model 引数より優先＝強制）。gemini-image-mcp / gemini-video-mcp と同じ流儀。
_ENV_MODEL_RAW = os.environ.get("GEMINI_TTS_MODEL", "").strip()
MODEL_FORCED = bool(_ENV_MODEL_RAW)
_DEFAULT_MODEL_RAW = _ENV_MODEL_RAW or FLASH_TTS
DEFAULT_MODEL = MODEL_ALIASES.get(_DEFAULT_MODEL_RAW, _DEFAULT_MODEL_RAW)


def resolve_model(model: str | None) -> str:
    """別名を実モデルIDに解決する（GEMINI_TTS_MODEL 設定時は常にそれを返す）。"""
    if MODEL_FORCED:
        return DEFAULT_MODEL
    if not model:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(model, model)


# --- プリビルトボイス（公式 30 種。名前: 特徴）---
PREBUILT_VOICES: dict[str, str] = {
    "Zephyr": "Bright（明るい）",
    "Puck": "Upbeat（アップビート）",
    "Charon": "Informative（情報的）",
    "Kore": "Firm（しっかり）",
    "Fenrir": "Excitable（興奮気味）",
    "Leda": "Youthful（若々しい）",
    "Orus": "Firm（しっかり）",
    "Aoede": "Breezy（軽やか）",
    "Callirrhoe": "Easy-going（おおらか）",
    "Autonoe": "Bright（明るい）",
    "Enceladus": "Breathy（息まじり）",
    "Iapetus": "Clear（クリア）",
    "Umbriel": "Easy-going（おおらか）",
    "Algieba": "Smooth（なめらか）",
    "Despina": "Smooth（なめらか）",
    "Erinome": "Clear（クリア）",
    "Algenib": "Gravelly（しゃがれ声）",
    "Rasalgethi": "Informative（情報的）",
    "Laomedeia": "Upbeat（アップビート）",
    "Achernar": "Soft（やわらか）",
    "Alnilam": "Firm（しっかり）",
    "Schedar": "Even（落ち着いた）",
    "Gacrux": "Mature（大人びた）",
    "Pulcherrima": "Forward（積極的）",
    "Achird": "Friendly（親しみやすい）",
    "Zubenelgenubi": "Casual（カジュアル）",
    "Vindemiatrix": "Gentle（穏やか）",
    "Sadachbia": "Lively（生き生き）",
    "Sadaltager": "Knowledgeable（知的）",
    "Sulafat": "Warm（温かい）",
}
_PREBUILT_BY_LOWER = {name.lower(): name for name in PREBUILT_VOICES}


def canonical_prebuilt(name: str) -> str | None:
    """プリビルトボイス名を大文字小文字を無視して正規化する（該当しなければ None）。"""
    return _PREBUILT_BY_LOWER.get(name.strip().lower())


def is_custom_voice_id(voice: str) -> bool:
    """ボイスデザイン/複製で作られた独自ボイスIDか（voice_... / voicekey_...）。"""
    return voice.startswith("voice_") or voice.startswith("voicekey_")


DEFAULT_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Kore").strip() or "Kore"

# --- ボイスデザインのパラメータ（Voice リソースより）---
VALID_GENDERS = ["female", "male", "neutral"]
VALID_PITCHES = ["low", "medium", "high"]

# --- 出力 ---
# 3.8 系は unary で WAV（24kHz / mono / 16bit PCM、RIFF ヘッダ付き）を返す。
# sample_rate は 24000（既定）/ 16000 / 8000。
OUTPUT_MIME_TYPE = "audio/wav"
VALID_SAMPLE_RATES = [24000, 16000, 8000]
DEFAULT_SAMPLE_RATE = int(os.environ.get("GEMINI_TTS_SAMPLE_RATE", "24000"))

# 環境変数の値も ~ を展開する（MCP クライアントの env はシェルを通らず ~ が展開されないため）。
DEFAULT_OUT_DIR = os.path.expanduser(os.environ.get("GEMINI_TTS_OUT_DIR", "").strip() or "~/gemini-tts")

# ボイスデザインで作った声の「説明文 → voice_id」対応を保存するキャッシュ。
# 同じ説明文で何度呼んでも同じ声（同じ voice_id）を再利用し、プロジェクト上限
# （保存ボイス 200 件）を無駄に消費しないようにする。
VOICE_CACHE_FILE = os.path.expanduser(
    os.environ.get("GEMINI_TTS_VOICE_CACHE", "").strip() or "~/.gemini_tts_mcp_voices.json"
)

# --- タイムアウト ---
DEFAULT_TIMEOUT = float(os.environ.get("GEMINI_TTS_TIMEOUT", "300"))
