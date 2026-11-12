"""
Gemini TTS MCP Server — stdio 版（MCP 内蔵ブラウザ OAuth 方式）

自分の Google Cloud プロジェクトの OAuth クライアント（デスクトップアプリ型）で
ブラウザログインし、Gemini 3.8 Flash TTS（gemini-3.8-flash-tts）を
Gemini API の interactions エンドポイント経由で叩いて、文章から音声（WAV）を生成する MCP サーバー。
「どんな声にしたいか」を自然文で指定すると、ボイスデザイン（/v1beta/voices）で
その声を作ってから読み上げる。MCP Python SDK 2.x（MCPServer）を使用。
（gemini-image-mcp / gemini-video-mcp の姉妹プロジェクト。認証・トークン運用は同じパターン。）

前提:
  1. Google Cloud Console で OAuth クライアント（デスクトップアプリ型）を作成
     （gemini-image-mcp / gemini-video-mcp と同じクライアントを流用可）
  2. CLIENT_ID / CLIENT_SECRET を環境変数で渡す
  3. 対象プロジェクトで Generative Language API（generativelanguage.googleapis.com）を有効化
  4. 初回のツール実行時（または reauthorize）にブラウザが開きログイン → トークン保存

環境変数:
  GEMINI_TTS_CLIENT_ID     : OAuth クライアントID（必須）
  GEMINI_TTS_CLIENT_SECRET : クライアントシークレット（必須）
  GOOGLE_CLOUD_PROJECT / GEMINI_TTS_PROJECT_ID : 使用するGCPプロジェクトID（クォータ帰属）
  GEMINI_TTS_MODEL         : モデル。既定 gemini-3.8-flash-tts（設定時は model 引数より優先）
  GEMINI_TTS_VOICE         : 声の指定が無いときの既定プリビルトボイス。既定 Kore
  GEMINI_TTS_PLANNER_MODEL : scene から声・話し方を考えるテキストモデル。既定 gemini-3.8-flash
  GEMINI_TTS_SAMPLE_RATE   : 既定サンプルレート。既定 24000（他に 16000 / 8000）
  GEMINI_TTS_OUT_DIR       : out_path 省略時の出力先。既定 ~/gemini-tts
  GEMINI_TTS_VOICE_CACHE   : 声の説明 → voice_id のキャッシュ。既定 ~/.gemini_tts_mcp_voices.json
  GEMINI_TTS_TIMEOUT       : API タイムアウト秒。既定 300
  GEMINI_TTS_REDIRECT_PORT : 初回認可のローカルポート（未指定なら空きポートを自動選択）

登録例:
  uvx --from git+https://github.com/tsukumonasu/gemini-tts-mcp gemini-tts-mcp
"""

import asyncio
import functools
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api as tts_api
from . import auth as tts_auth
from . import planner
from .audio_saver import save_audio, save_base64_wav
from .constants import (
    DEFAULT_MODEL,
    DEFAULT_OUT_DIR,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TIMEOUT,
    DEFAULT_VOICE,
    MODEL_ALIASES,
    MODEL_FORCED,
    PLANNER_MODEL,
    PREBUILT_VOICES,
    SUPPORTED_MODELS,
    VALID_GENDERS,
    VALID_PITCHES,
    VALID_SAMPLE_RATES,
    VOICE_CACHE_FILE,
    VOICE_DESIGN_MODELS,
    canonical_prebuilt,
    is_custom_voice_id,
    resolve_model,
)
from .voice_cache import VoiceCache, make_key, make_scene_key

logger = logging.getLogger(__name__)

INLINE_TAGS = [
    "<short pause>", "<long pause>", "<breath>", "<sigh>", "<laugh>", "<chuckle>",
    "<giggle>", "<gasp>", "<cough>", "<yawn>", "<whispers>",
]


class Settings:
    def __init__(self) -> None:
        self.out_dir = Path(DEFAULT_OUT_DIR)
        self.timeout = DEFAULT_TIMEOUT
        self.voice_cache_file = VOICE_CACHE_FILE


# 想定内の失敗（入力不備・API エラー・認証エラー）。MCP SDK 2.x では ToolError 以外の
# 例外は「クラッシュ」扱いになりメッセージが隠れるため、ToolError に変換して届ける。
_EXPECTED_ERRORS: tuple[type[Exception], ...] = (
    ValueError,
    tts_api.RateLimitError,
    tts_api.CapacityError,
    tts_auth.AuthError,
    tts_auth.ReauthorizationRequired,
)


def _as_tool_error(fn):
    """ツール本体の想定内例外を ToolError に変換するデコレータ。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except _EXPECTED_ERRORS as e:
            raise ToolError(str(e)) from e

    return wrapper


def _looks_like_description(value: str) -> bool:
    """voice 引数に「声の説明文」が渡されたか（空白や日本語を含む＝名前/IDではない）。"""
    return not re.fullmatch(r"[A-Za-z0-9_\-]+", value.strip())


def _public_voice(v: dict[str, Any]) -> dict[str, Any]:
    """一覧表示用にサンプル音声・使用量などの大きいフィールドを除く。"""
    return {k: val for k, val in v.items() if k not in ("sample_audio", "usage")}


def create_server(
    settings: Settings,
    tokens: tts_auth.TokenManager,
    voice_cache: Optional[VoiceCache] = None,
) -> MCPServer:
    cache = voice_cache or VoiceCache(settings.voice_cache_file)

    app = MCPServer(
        name="Gemini TTS MCP Server",
        instructions=(
            "自分の Google Cloud プロジェクトの Gemini API 経由で Gemini 3.8 Flash TTS を使い、"
            "文章を音声（WAV）にする MCP サーバー。generate_speech に text（読み上げる文章）と "
            "voice_description（どんな声にしたいかの自然文。例: '落ち着いた30代女性のニュースキャスター'）"
            "を渡すと、その声を作って読み上げる。同じ説明なら同じ声が再利用される。"
            "話し方（感情・速さ）は style、プリビルトボイスを直接使うなら voice（例: Kore）。"
            "用途・場面が分かっているときは scene（例: 'AWS のイベントでのプレゼン'）を渡すと、"
            "その場面に合う声と話し方を自動で考える（同じ scene なら同じ声）。"
            "plan_speech で音声を作らずにプランだけ確認できる。"
            "声だけ試聴したいときは design_voice。認証エラー時は reauthorize を実行する。"
        ),
        version="0.1.0",
    )

    # ------------------------------------------------------------------
    # 共通ヘルパー
    # ------------------------------------------------------------------

    async def _get_token_or_raise() -> str:
        # 初回（トークン未取得）はその場でブラウザ認可を行う。
        if not tokens.has_any_token():
            try:
                await asyncio.to_thread(tokens.ensure_initial_auth)
            except tts_auth.AuthError as e:
                raise ToolError(str(e)) from None
        try:
            return await tokens.get_token()
        except tts_auth.ReauthorizationRequired as e:
            raise ToolError(
                f"{e} 認証が必要です。reauthorize ツールを実行してブラウザでログインしてください。"
            ) from None
        except tts_auth.AuthError as e:
            raise ToolError(str(e)) from None

    def _resolve_model_with_note(model: Optional[str]) -> tuple[str, Optional[str]]:
        resolved = resolve_model(model)
        note = None
        if MODEL_FORCED and model and MODEL_ALIASES.get(model, model) != resolved:
            note = (
                f"model={model} が指定されましたが、環境変数 GEMINI_TTS_MODEL の "
                f"{resolved} を使用します。"
            )
            logger.info(note)
        return resolved, note

    def _require_design_model(model: str) -> None:
        if model not in VOICE_DESIGN_MODELS:
            raise ToolError(
                f"モデル {model} はボイスデザイン（独自ボイス）に対応していません。"
                f"{VOICE_DESIGN_MODELS} のいずれかを使うか、プリビルトボイスを指定してください。"
            )

    def _validate_design_params(gender: Optional[str], pitch: Optional[str]) -> None:
        if gender and gender not in VALID_GENDERS:
            raise ToolError(f"無効な gender: {gender}。有効値: {VALID_GENDERS}")
        if pitch and pitch not in VALID_PITCHES:
            raise ToolError(f"無効な pitch: {pitch}。有効値: {VALID_PITCHES}")

    def _resolve_out(out_path: Optional[str]) -> tuple[str, Optional[str]]:
        """出力ディレクトリとファイル名（拡張子なし）を決める。"""
        if out_path:
            out = Path(os.path.expanduser(out_path))
            out.parent.mkdir(parents=True, exist_ok=True)
            return str(out.parent), out.stem
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        return str(settings.out_dir), None

    async def _create_designed_voice(
        access_token: str,
        *,
        description: str,
        model: str,
        project_id: Optional[str],
        display_name: Optional[str] = None,
        gender: Optional[str] = None,
        language_code: Optional[str] = None,
        pitch: Optional[str] = None,
        accent: Optional[str] = None,
        base_voice: Optional[str] = None,
    ) -> dict[str, Any]:
        """ボイスデザインで声を作り、キャッシュに登録して Voice を返す。"""
        name = display_name or " ".join(description.split())[:60]
        voice = await tts_api.create_voice(
            access_token,
            description,
            model=model,
            display_name=name,
            gender=gender,
            language_code=language_code,
            pitch=pitch,
            accent=accent,
            base_voice=base_voice,
            project_id=project_id,
            timeout=settings.timeout,
        )
        key = make_key(
            description,
            model=model,
            gender=gender,
            language_code=language_code,
            pitch=pitch,
            accent=accent,
            base_voice=base_voice,
        )
        cache.put(key, voice["id"], display_name=voice.get("display_name") or name)
        return voice

    def _normalize_base_voice(voice: Optional[str]) -> Optional[str]:
        if not voice:
            return None
        return canonical_prebuilt(voice) or voice.strip()

    async def _plan_scene(
        access_token: str,
        *,
        scene: str,
        sample_text: Optional[str],
        tts_model: str,
        gender: Optional[str],
        language_code: Optional[str],
        project_id: Optional[str],
        replan: bool,
    ) -> tuple[dict[str, Any], bool]:
        """scene から声・話し方のプランを作る（同じ条件ならキャッシュを返す）。"""
        key = make_scene_key(scene, model=tts_model, gender=gender, language_code=language_code)
        if not replan:
            cached = cache.get_scene(key)
            if cached:
                return cached, True
        raw = await tts_api.generate_json_text(
            access_token,
            PLANNER_MODEL,
            planner.build_scene_prompt(
                scene, sample_text, gender=gender, language_code=language_code
            ),
            response_schema=planner.SCENE_SCHEMA,
            project_id=project_id,
            timeout=settings.timeout,
        )
        plan = planner.normalize_scene_plan(
            planner.parse_json_text(raw), gender=gender, language_code=language_code
        )
        return cache.put_scene(key, plan), False

    async def _enhance_text(
        access_token: str,
        *,
        text: str,
        scene: Optional[str],
        style: Optional[str],
        project_id: Optional[str],
    ) -> tuple[str, Optional[str]]:
        """本文に間・息継ぎのタグを入れる。言葉が変わっていたら元の本文を使う。"""
        raw = await tts_api.generate_json_text(
            access_token,
            PLANNER_MODEL,
            planner.build_enhance_prompt(text, scene, style),
            response_schema=planner.ENHANCE_SCHEMA,
            project_id=project_id,
            timeout=settings.timeout,
        )
        enhanced = str(planner.parse_json_text(raw).get("text") or "")
        if planner.validate_enhanced_text(text, enhanced):
            return enhanced, None
        return text, "タグ挿入で本文の言葉が変わってしまったため、元の文章のまま読み上げました。"

    # ------------------------------------------------------------------
    # ツール
    # ------------------------------------------------------------------

    @app.tool()
    @_as_tool_error
    async def auth_status() -> dict[str, Any]:
        """認証・プロジェクト・モデルの準備状況を確認する。

        Returns:
            ready: 音声生成の準備が整っているか
            auth: 認証状態（valid / expired / needs_reauth）
            client_configured: OAuthクライアント(ID/SECRET)が設定済みか
            project_id: 解決されたプロジェクトID
            model / default_voice / out_dir: 現在の既定値
            message: 状態の説明と次のアクション
        """
        client_configured = bool(tokens.settings.client_id and tokens.settings.client_secret)
        project_id = tts_auth.resolve_project_id()
        auth_state = tokens.status()

        ready = client_configured and bool(project_id) and auth_state in ("valid", "expired")
        if not client_configured:
            msg = (
                "OAuth クライアントが未設定です。Google Cloud Console でデスクトップアプリ型の "
                "OAuth クライアントを作成し、GEMINI_TTS_CLIENT_ID / GEMINI_TTS_CLIENT_SECRET "
                "を設定してください。"
            )
        elif not project_id:
            msg = "プロジェクトIDが未設定です。GOOGLE_CLOUD_PROJECT か GEMINI_TTS_PROJECT_ID を設定してください。"
        elif auth_state == "needs_reauth":
            msg = "未認証です。generate_speech（または reauthorize）を実行するとブラウザが開きます。"
        else:
            msg = "準備完了。generate_speech を実行できます。"

        return {
            "ready": ready,
            "auth": auth_state,
            "client_configured": client_configured,
            "project_id": project_id,
            "model": DEFAULT_MODEL,
            "default_voice": DEFAULT_VOICE,
            "out_dir": str(settings.out_dir),
            "voice_cache_file": settings.voice_cache_file,
            "message": msg,
        }

    @app.tool()
    @_as_tool_error
    async def reauthorize() -> dict[str, Any]:
        """ブラウザで Google に再ログインしてトークンを取り直す。

        ブラウザが自動で開き、Google のログイン画面が表示されます。
        認可を完了すると新しいトークンが保存されます。
        認証エラー（401等）や Refresh Token 失効時に実行してください。
        """
        try:
            await tokens.reauthorize_async()
        except tts_auth.AuthError as e:
            raise ToolError(str(e)) from None
        return {"status": "ok", "message": "再認証が完了し、トークンを更新しました。"}

    @app.tool()
    @_as_tool_error
    async def list_models() -> dict[str, Any]:
        """利用可能な TTS モデル・別名・プリビルトボイス・出力設定の一覧を返す。"""
        return {
            "models": SUPPORTED_MODELS,
            "voice_design_models": VOICE_DESIGN_MODELS,
            "aliases": MODEL_ALIASES,
            "default": DEFAULT_MODEL,
            "forced_by_env": MODEL_FORCED,
            "prebuilt_voices": PREBUILT_VOICES,
            "default_voice": DEFAULT_VOICE,
            "sample_rates": VALID_SAMPLE_RATES,
            "default_sample_rate": DEFAULT_SAMPLE_RATE,
            "inline_tags_examples": INLINE_TAGS,
            "planner_model": PLANNER_MODEL,
            "note": (
                "モデルは環境変数 GEMINI_TTS_MODEL が最優先（設定時は model 引数を無視）。"
                f"未設定なら model 引数、無ければ既定 {DEFAULT_MODEL}。"
                "gemini-3.8-flash-tts は表現力重視、gemini-3.8-flash-lite-tts は速度・コスト重視。"
                "voice_description（ボイスデザイン）は 3.8 系のみ対応。旧世代はプリビルトボイスのみ。"
                "出力は WAV（モノラル 16bit）。言語は本文から自動判定（130 以上の言語）。"
                "入力は最大 8,192 トークン。"
                f"scene（場面）から声・話し方を考えるのは {PLANNER_MODEL}（GEMINI_TTS_PLANNER_MODEL で変更可）。"
            ),
        }

    @app.tool()
    @_as_tool_error
    async def list_voices(
        language_code: Optional[str] = None,
        gender: Optional[str] = None,
        voice_type: Optional[str] = None,
        search: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """使える声の一覧を返す（自分の保存ボイス → プリビルトボイスの順）。

        Args:
            language_code: 言語で絞り込み（例: ja-JP, en-US）。
            gender: 性別で絞り込み（female / male / neutral）。
            voice_type: 種類で絞り込み（prebuilt / prompted / replicated）。
                        prompted はボイスデザインで作った声。
            search: キーワード検索（例: warm, calm）。
            page_size: 1 ページの件数（既定 50、最大 1000）。
            page_token: 次ページ取得用のトークン（前回の next_page_token）。
            project_id: プロジェクトID上書き（任意）。

        Returns:
            voices: API から取得した声の一覧（id / display_name / type など）
            next_page_token: 続きがある場合のトークン
            designed_voices_local: この MCP がボイスデザインで作った声（説明文 → voice_id）
        """
        if gender and gender not in VALID_GENDERS:
            raise ToolError(f"無効な gender: {gender}。有効値: {VALID_GENDERS}")
        params: list[tuple[str, Any]] = [("page_size", max(1, min(page_size, 1000)))]
        for key, value in (
            ("language_code", language_code),
            ("gender", gender),
            ("type", voice_type),
            ("search", search),
            ("page_token", page_token),
        ):
            if value:
                params.append((key, value))

        local = cache.entries()
        proj = tts_auth.resolve_project_id(explicit=project_id)
        try:
            access_token = await _get_token_or_raise()
            resp = await tts_api.list_voices(
                access_token, params=params, project_id=proj, timeout=settings.timeout
            )
        except (ToolError, *_EXPECTED_ERRORS) as e:
            # API で取得できなくても、プリビルトボイスは静的な一覧で返す。
            return {
                "voices": [
                    {"id": name, "type": "prebuilt", "description": desc}
                    for name, desc in PREBUILT_VOICES.items()
                ],
                "designed_voices_local": local,
                "scene_plans_local": cache.scenes(),
                "warning": f"API から一覧を取得できなかったため、既知のプリビルトボイスを返します: {e}",
            }

        return {
            "voices": [_public_voice(v) for v in resp.get("voices") or []],
            "next_page_token": resp.get("next_page_token") or resp.get("nextPageToken"),
            "designed_voices_local": local,
            "scene_plans_local": cache.scenes(),
        }

    @app.tool()
    @_as_tool_error
    async def design_voice(
        description: str,
        display_name: Optional[str] = None,
        gender: Optional[str] = None,
        language_code: Optional[str] = None,
        pitch: Optional[str] = None,
        accent: Optional[str] = None,
        base_voice: Optional[str] = None,
        model: Optional[str] = None,
        sample_out_path: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """どんな声にしたいかを自然文で説明して、新しい声を作る（ボイスデザイン）。試聴用サンプルも保存する。

        作った声は voice_id（voice_...）でプロジェクトに保存され、generate_speech の voice に
        渡して何度でも使える（generate_speech に同じ条件の voice_description を渡した場合も
        この声が使われる）。呼ぶたびに新しい声を作るので、気に入るまで説明を変えて試せる。

        Args:
            description: 声の説明（年齢・性別・声質・話し方の雰囲気・なまりなど、恒常的な特徴を 1〜2 文で）。
                         例: "穏やかで落ち着いた 40 代男性のナレーター。低めで温かみのある声"
                         例: "A crisp, energetic sports announcer in her 30s with a slight Midwestern accent."
            display_name: 声の表示名（任意。省略時は説明文の先頭）。
            gender: 性別（任意。female / male / neutral）。
            language_code: 主に話す言語（任意。BCP-47。例: ja-JP, en-US）。
            pitch: 声の高さ（任意。low / medium / high）。
            accent: なまり・アクセント（任意。例: "Kansai", "British"）。
            base_voice: リミックス元の声（任意）。プリビルト名（例: Puck）または voice_... を指定すると、
                        description は「その声からどう変えるか」の指示として扱われる。
            model: 使用モデル（任意。gemini-3.8-flash-tts / gemini-3.8-flash-lite-tts）。
            sample_out_path: 試聴サンプル（WAV）の保存先（任意）。省略時は GEMINI_TTS_OUT_DIR。
            project_id: プロジェクトID上書き（任意）。

        Returns:
            voice_id / display_name / sample（試聴サンプルの保存先）/ model / expire_time
        """
        if not description or not description.strip():
            raise ToolError("description（声の説明）を指定してください。")
        _validate_design_params(gender, pitch)
        resolved_model, model_note = _resolve_model_with_note(model)
        _require_design_model(resolved_model)

        proj = tts_auth.resolve_project_id(explicit=project_id)
        access_token = await _get_token_or_raise()
        voice = await _create_designed_voice(
            access_token,
            description=description,
            model=resolved_model,
            project_id=proj,
            display_name=display_name,
            gender=gender,
            language_code=language_code,
            pitch=pitch,
            accent=accent,
            base_voice=_normalize_base_voice(base_voice),
        )

        sample_info = None
        sample = voice.get("sample_audio") or {}
        if sample.get("data"):
            if sample_out_path:
                path = os.path.expanduser(sample_out_path)
                if not path.lower().endswith(".wav"):
                    path = str(Path(path).with_suffix(".wav"))
            else:
                path = str(settings.out_dir / f"voice-sample-{voice['id']}.wav")
            sample_info = save_base64_wav(
                sample["data"], sample.get("mime_type") or "audio/wav", path
            )

        result: dict[str, Any] = {
            "success": True,
            "voice_id": voice["id"],
            "display_name": voice.get("display_name"),
            "description": description,
            "base_voice": (voice.get("prompted") or {}).get("base_voice") or base_voice,
            "sample": sample_info,
            "model": resolved_model,
            "expire_time": voice.get("expire_time"),
            "project_id": proj,
            "usage_hint": f"generate_speech(text=..., voice=\"{voice['id']}\") で、この声で読み上げられます。",
        }
        if model_note:
            result["note"] = model_note
        return result

    @app.tool()
    @_as_tool_error
    async def plan_speech(
        scene: str,
        text: Optional[str] = None,
        gender: Optional[str] = None,
        language_code: Optional[str] = None,
        model: Optional[str] = None,
        enhance_text: bool = False,
        replan: bool = False,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """用途・場面（scene）から、合う声と話し方を考える（音声は作らない・プレビュー用）。

        generate_speech に同じ scene を渡すと、ここで考えたプランがそのまま使われる
        （プランはキャッシュされ、同じ scene なら毎回同じ声になる）。
        気に入らなければ replan=True で考え直すか、voice_description / style を直接指定する。

        Args:
            scene: 音声を使う場面・用途・聞き手など（例: "AWS のイベントでのプレゼン。会場は数百人のエンジニア"）。
            text: 読み上げる予定の文章（任意。言語判定や雰囲気の参考にする）。
            gender: 話者の性別を固定したい場合（任意。female / male / neutral）。
            language_code: 言語を固定したい場合（任意。例: ja-JP）。
            model: 読み上げに使う TTS モデル（任意。キャッシュの区別に使う）。
            enhance_text: True なら text に間（<short pause> 等）を入れた版も返す（言葉は変えない）。
            replan: True ならキャッシュを使わずに考え直す。
            project_id: プロジェクトID上書き（任意）。

        Returns:
            voice_description（声の設計）/ style（話し方）/ gender / language_code / rationale（理由）/
            cached（キャッシュ再利用か）/ enhanced_text（enhance_text=True のとき）
        """
        if not scene or not scene.strip():
            raise ToolError("scene（用途・場面）を指定してください。")
        _validate_design_params(gender, None)
        resolved_model, _ = _resolve_model_with_note(model)
        proj = tts_auth.resolve_project_id(explicit=project_id)
        access_token = await _get_token_or_raise()
        plan, cached = await _plan_scene(
            access_token,
            scene=scene,
            sample_text=text,
            tts_model=resolved_model,
            gender=gender,
            language_code=language_code,
            project_id=proj,
            replan=replan,
        )
        result: dict[str, Any] = {
            "success": True,
            "scene": scene,
            **{k: plan.get(k) for k in ("voice_description", "style", "gender", "language_code", "rationale")},
            "cached": cached,
            "planner_model": PLANNER_MODEL,
            "tts_model": resolved_model,
        }
        if enhance_text and text and text.strip():
            enhanced, note = await _enhance_text(
                access_token, text=text, scene=scene, style=plan.get("style"), project_id=proj
            )
            result["enhanced_text"] = enhanced
            if note:
                result["enhance_note"] = note
        return result

    @app.tool()
    @_as_tool_error
    async def generate_speech(
        text: str,
        scene: Optional[str] = None,
        voice_description: Optional[str] = None,
        voice: Optional[str] = None,
        style: Optional[str] = None,
        enhance_text: bool = False,
        out_path: Optional[str] = None,
        gender: Optional[str] = None,
        language_code: Optional[str] = None,
        model: Optional[str] = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        replan: bool = False,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """文章を、指定した声（または場面に合う声）で読み上げた音声（WAV）を生成する。

        声の決め方（上ほど優先。どれも無ければ既定のプリビルトボイス）:
          1. voice_description: どんな声にしたいかを自然文で説明する（ボイスデザイン）。
             初回はその説明から声を作成して保存し、2 回目以降は同じ説明なら同じ声を再利用する。
             例: "明るく元気な 20 代女性。少し早口でハキハキした声"
          2. voice: プリビルトボイス名（例: Kore, Puck, Zephyr。list_voices / list_models で確認）
             または design_voice で作った voice_id（voice_...）。
             voice と voice_description の両方なら、voice を元に説明どおりに変えた声を作る（リミックス）。
          3. scene: 用途・場面（例: "AWS のイベントでのプレゼン"）。場面に合う声と話し方を自動で考える。
             プランはキャッシュされ、同じ scene なら何度呼んでも同じ声・話し方になる（スライドごとに
             分けて生成しても声が揃う）。voice / voice_description / style を指定した部分はそちらが優先。

        Args:
            text: 読み上げる文章（言語は自動判定）。強調したい語を大文字にする、句読点や「…」で間を作る、
                  <laugh> <sigh> <short pause> <long pause> <breath> などのタグ（英語表記）を文中に
                  入れると一瞬の笑い・ため息・間を表現できる。
            scene: 音声を使う場面・用途・聞き手（任意）。例: "AWS のイベントでのプレゼン。聴衆はエンジニア数百人"。
            voice_description: どんな声にしたいかの説明（年齢・性別・声質・雰囲気など恒常的な特徴）。
            voice: プリビルトボイス名、または voice_... の ID。
            style: 話し方の指示（任意。感情・速さ・抑揚・声量など、その場の話し方）。
                   例: "嬉しそうに、ややゆっくり" / "whispered urgently"。本文には混ぜずにここへ書く
                   （本文に書くと読み上げられてしまう）。短い方が効果的。scene より優先。
            enhance_text: True なら本文に間・息継ぎのタグ（<short pause> 等）を自動で入れてから読み上げる
                          （言葉は変えない。変わってしまった場合は元の文章のまま読む）。
            out_path: 出力先ファイルパス（.wav）。省略時は GEMINI_TTS_OUT_DIR に自動命名で保存。
            gender: 話者の性別（任意。female / male / neutral。声を作るとき・scene のプランに使う）。
            language_code: 主言語（任意。例: ja-JP。声を作るとき・scene のプランに使う）。
            model: 使用モデル（任意）。gemini-3.8-flash-tts（既定）/ gemini-3.8-flash-lite-tts（速い・安い）。
                   環境変数 GEMINI_TTS_MODEL 設定時はこの引数は無視。
            sample_rate: サンプルレート（24000（既定）/ 16000 / 8000）。
            replan: True なら scene のプランをキャッシュせずに考え直す（声も変わる）。
            project_id: プロジェクトID上書き（任意。クォータ帰属用）。

        Returns:
            success / out_path / files / duration_sec / voice / voice_source / voice_id / style /
            scene_plan（scene 指定時: 声の設計・話し方・理由）/ text_used（enhance_text 時）/ model / project_id
            voice_id（voice_...）を次回 voice に渡すと、同じ声で読み上げられる。
        """
        if not text or not text.strip():
            raise ToolError("text（読み上げる文章）を指定してください。")
        if sample_rate not in VALID_SAMPLE_RATES:
            raise ToolError(f"無効な sample_rate: {sample_rate}。有効値: {VALID_SAMPLE_RATES}")
        _validate_design_params(gender, None)

        # voice に説明文が渡された場合は voice_description として扱う（よくある取り違えの救済）。
        if voice and not voice_description and _looks_like_description(voice):
            voice_description, voice = voice, None

        resolved_model, model_note = _resolve_model_with_note(model)
        proj = tts_auth.resolve_project_id(explicit=project_id)
        access_token = await _get_token_or_raise()
        notes: list[str] = [model_note] if model_note else []

        # --- scene → 声・話し方のプラン（明示指定された項目はそちらを優先）---
        scene_plan: Optional[dict[str, Any]] = None
        if scene and scene.strip():
            plan, plan_cached = await _plan_scene(
                access_token,
                scene=scene,
                sample_text=text,
                tts_model=resolved_model,
                gender=gender,
                language_code=language_code,
                project_id=proj,
                replan=replan,
            )
            scene_plan = {
                **{k: plan.get(k) for k in ("voice_description", "style", "gender", "language_code", "rationale")},
                "cached": plan_cached,
            }
            if not voice_description and not voice:
                if resolved_model in VOICE_DESIGN_MODELS:
                    voice_description = plan["voice_description"]
                    gender = gender or plan.get("gender")
                    language_code = language_code or plan.get("language_code")
                else:
                    notes.append(
                        f"{resolved_model} はボイスデザイン非対応のため、scene からは話し方（style）のみ適用しました。"
                    )
            if style is None:
                style = plan.get("style")

        # --- 本文に間・息継ぎのタグを入れる（任意）---
        text_used = text
        if enhance_text:
            text_used, enhance_note = await _enhance_text(
                access_token, text=text, scene=scene, style=style, project_id=proj
            )
            if enhance_note:
                notes.append(enhance_note)

        # --- 声の決定 ---
        voice_name: str
        voice_source: str
        design_args: dict[str, Any] = {}

        if voice_description and voice_description.strip():
            _require_design_model(resolved_model)
            design_args = dict(
                description=voice_description,
                model=resolved_model,
                gender=gender,
                language_code=language_code,
                base_voice=_normalize_base_voice(voice),
            )
            cached = cache.get(make_key(**design_args))
            if cached:
                voice_name, voice_source = cached["voice_id"], "designed (cached)"
            else:
                created = await _create_designed_voice(
                    access_token, project_id=proj, **design_args
                )
                voice_name, voice_source = created["id"], "designed (new)"
        elif voice and voice.strip():
            prebuilt = canonical_prebuilt(voice)
            if prebuilt:
                voice_name, voice_source = prebuilt, "prebuilt"
            elif is_custom_voice_id(voice.strip()):
                _require_design_model(resolved_model)
                voice_name, voice_source = voice.strip(), "custom"
            else:
                # 拡張ボイスライブラリ等、静的一覧に無い名前はそのまま API に渡す。
                voice_name, voice_source = voice.strip(), "prebuilt (unlisted)"
        else:
            voice_name, voice_source = DEFAULT_VOICE, "default"

        async def _synthesize(v: str) -> dict[str, Any]:
            return await tts_api.synthesize_speech(
                access_token,
                resolved_model,
                text_used,
                voice=v,
                style=style,
                sample_rate=sample_rate,
                project_id=proj,
                timeout=settings.timeout,
            )

        async def _voice_missing(v: str) -> bool:
            try:
                await tts_api.get_voice(access_token, v, project_id=proj)
                return False
            except tts_api.ApiError as ge:
                return ge.status_code == 404

        try:
            response = await _synthesize(voice_name)
        except tts_api.ApiError as e:
            # キャッシュ済みの声が期限切れ・削除済みなら、作り直して 1 回だけ再試行する。
            # 声が存在する（＝失敗の原因が声以外）場合は元のエラーをそのまま返す。
            if voice_source != "designed (cached)" or e.status_code in (401, 403):
                raise
            if not await _voice_missing(voice_name):
                raise
            logger.info("キャッシュの声 %s が見つからないため作り直します", voice_name)
            cache.remove_voice_id(voice_name)
            created = await _create_designed_voice(access_token, project_id=proj, **design_args)
            voice_name, voice_source = created["id"], "designed (recreated)"
            response = await _synthesize(voice_name)

        audios = tts_api.extract_audio(response)
        output_dir, custom_name = _resolve_out(out_path)
        saved = save_audio(audios, text, output_dir, custom_name=custom_name, fallback_rate=sample_rate)

        result: dict[str, Any] = {
            "success": True,
            "out_path": saved[0]["path"] if saved else None,
            "files": saved,
            "duration_sec": saved[0].get("duration_sec") if saved else None,
            "voice": voice_name,
            "voice_source": voice_source,
            "voice_id": voice_name if is_custom_voice_id(voice_name) else None,
            "voice_description": voice_description,
            "style": style,
            "model": resolved_model,
            "sample_rate": sample_rate,
            "project_id": proj,
        }
        if scene_plan:
            result["scene"] = scene
            result["scene_plan"] = scene_plan
        if enhance_text:
            result["text_used"] = text_used
        if notes:
            result["note"] = " ".join(notes)
        return result

    @app.tool()
    @_as_tool_error
    async def delete_voice(voice_id: str, project_id: Optional[str] = None) -> dict[str, Any]:
        """design_voice / generate_speech で作った保存ボイス（voice_...）を削除する。

        保存ボイスはプロジェクトあたり 200 件まで（最終使用から 1 年で期限切れ）。
        不要になった声を削除して枠を空けるときに使う。ローカルキャッシュからも削除する。

        Args:
            voice_id: 削除する声の ID（voice_...）。
            project_id: プロジェクトID上書き（任意）。
        """
        voice_id = voice_id.strip()
        if not voice_id.startswith("voice_"):
            raise ToolError("削除できるのは保存ボイス（voice_... の ID）のみです。")
        proj = tts_auth.resolve_project_id(explicit=project_id)
        access_token = await _get_token_or_raise()
        try:
            await tts_api.delete_voice(access_token, voice_id, project_id=proj)
        except tts_api.ApiError as e:
            if e.status_code != 404:
                raise
        removed = cache.remove_voice_id(voice_id)
        return {
            "success": True,
            "voice_id": voice_id,
            "removed_from_local_cache": removed,
        }

    return app


def main() -> None:
    """uvx / コンソールスクリプトのエントリポイント。"""
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    tokens = tts_auth.TokenManager(tts_auth.Settings())
    mcp = create_server(settings, tokens)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
