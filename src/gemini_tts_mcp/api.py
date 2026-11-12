"""
Gemini 3.8 Flash TTS の API クライアント（interactions / voices エンドポイント）。

認証は auth.py の OAuth access token を使用（Authorization: Bearer）。
OAuth 方式では課金/クォータ帰属のため x-goog-user-project ヘッダを付ける。

■ 音声合成（POST /v1beta/interactions）
  {
    "model": "gemini-3.8-flash-tts",
    "input": [{
      "type": "user_input",
      "content": [{
        "type": "text",
        "text": "Have a wonderful day!",
        "annotations": [{"type": "speech_metadata", "style": "cheerful and friendly"}]
      }]
    }],
    "response_format": {"type": "audio", "mime_type": "audio/wav", "sample_rate": 24000},
    "generation_config": {"speech_config": [{"voice": "Kore"}]}   # voice_... も可
  }
  レスポンス（REST）: steps[] の model_output → content[] の type=="audio" に
  {"mime_type": "audio/wav", "data": "<base64>"}。

■ ボイスデザイン（POST /v1beta/voices）
  {
    "store": true,
    "voice": {
      "model": "gemini-3.8-flash-tts",
      "type": "prompted",
      "display_name": "...", "gender": "female", "language_code": "ja-JP",
      "prompted": {"input": "<声の説明>", "base_voice": "Puck"}   # base_voice は任意（リミックス）
    }
  }
  レスポンス: {"id": "voice_...", "sample_audio": {"mime_type": "audio/wav", "data": "<base64>"}, ...}
"""

import logging
from typing import Any, Optional

import httpx

from .constants import (
    OUTPUT_MIME_TYPE,
    build_generate_content_url,
    build_interactions_url,
    build_voices_url,
)

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after_ms: int = 60000) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class CapacityError(Exception):
    def __init__(self, message: str, retry_after_ms: int = 60000) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class ApiError(ValueError):
    """API が 2xx 以外を返したとき。status_code を保持する。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _headers(access_token: str, project_id: Optional[str]) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if project_id:
        headers["x-goog-user-project"] = project_id
    return headers


def _retry_after_ms(resp: httpx.Response) -> int:
    retry_after = resp.headers.get("Retry-After")
    try:
        return int(retry_after) * 1000 if retry_after else 60 * 1000
    except ValueError:
        return 60 * 1000


async def _request(
    method: str,
    url: str,
    access_token: str,
    *,
    project_id: Optional[str] = None,
    json_body: Optional[dict[str, Any]] = None,
    params: Optional[list[tuple[str, Any]]] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """共通の HTTP 呼び出しとエラー変換。"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(
                method,
                url,
                headers=_headers(access_token, project_id),
                json=json_body,
                params=params,
            )
        except httpx.HTTPError as e:
            raise ValueError(f"API への接続に失敗しました: {e}") from e

    if resp.status_code == 429:
        retry_ms = _retry_after_ms(resp)
        raise RateLimitError(
            f"レート制限を超過しました。約 {max(1, retry_ms // 1000)} 秒後に再試行してください。",
            retry_ms,
        )

    if resp.status_code == 503:
        raise CapacityError(f"モデル容量が不足しています（503）: {resp.text[:500]}")

    if resp.status_code in (401, 403):
        raise ApiError(
            f"認証/権限エラー ({resp.status_code}): {resp.text[:500]}\n"
            "OAuth ログイン状態（必要なら reauthorize）、プロジェクトの Generative Language API "
            "（generativelanguage.googleapis.com）有効化、および当該モデルへのアクセス権を"
            "確認してください。",
            resp.status_code,
        )

    if resp.status_code < 200 or resp.status_code >= 300:
        raise ApiError(
            f"API リクエストに失敗しました ({resp.status_code}): {resp.text[:800]}",
            resp.status_code,
        )

    if not resp.content:
        return {}
    return resp.json()


# ---------------------------------------------------------------------------
# 音声合成（interactions）
# ---------------------------------------------------------------------------


def build_speech_input(text: str, style: Optional[str] = None) -> list[dict[str, Any]]:
    """interactions の input（単一話者）を構築する。

    style（話し方の指示）は本文に混ぜると読み上げられてしまうため、
    speech_metadata アノテーションとして付ける（3.8 系の推奨方式）。
    """
    content: dict[str, Any] = {"type": "text", "text": text}
    if style:
        content["annotations"] = [{"type": "speech_metadata", "style": style}]
    return [{"type": "user_input", "content": [content]}]


def build_speech_body(
    model: str,
    text: str,
    *,
    voice: str,
    style: Optional[str] = None,
    sample_rate: Optional[int] = None,
) -> dict[str, Any]:
    response_format: dict[str, Any] = {"type": "audio", "mime_type": OUTPUT_MIME_TYPE}
    if sample_rate:
        response_format["sample_rate"] = sample_rate
    return {
        "model": model,
        "input": build_speech_input(text, style),
        "response_format": response_format,
        "generation_config": {"speech_config": [{"voice": voice}]},
    }


async def synthesize_speech(
    access_token: str,
    model: str,
    text: str,
    *,
    voice: str,
    style: Optional[str] = None,
    sample_rate: Optional[int] = None,
    project_id: Optional[str] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """テキストを音声に変換する（interactions を叩く）。"""
    body = build_speech_body(model, text, voice=voice, style=style, sample_rate=sample_rate)
    return await _request(
        "POST",
        build_interactions_url(),
        access_token,
        project_id=project_id,
        json_body=body,
        timeout=timeout,
    )


def _audio_part(part: dict[str, Any]) -> Optional[dict[str, str]]:
    if part.get("type") == "audio" and part.get("data"):
        return {
            "mimeType": part.get("mime_type") or part.get("mimeType") or OUTPUT_MIME_TYPE,
            "data": part["data"],
        }
    return None


def extract_audio(response: dict[str, Any]) -> list[dict[str, str]]:
    """レスポンスから音声(base64)を取り出す。

    REST では steps[] の model_output ステップの content 内、type == "audio" のパート。
    仕様変更に備え、トップレベルの outputs[] 形式にも対応しておく。
    """
    if response.get("error"):
        err = response["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ValueError(f"API エラー: {msg}")

    status = response.get("status")
    if status and status not in ("completed", "succeeded"):
        raise ValueError(f"音声生成が完了しませんでした（status={status}）: {str(response)[:500]}")

    audios: list[dict[str, str]] = []
    for step in response.get("steps") or []:
        if step.get("type") and step.get("type") != "model_output":
            continue  # user_input / thought などはスキップ
        for part in step.get("content") or []:
            audio = _audio_part(part)
            if audio:
                audios.append(audio)

    if not audios:
        for part in response.get("outputs") or []:
            audio = _audio_part(part)
            if audio:
                audios.append(audio)

    if not audios:
        raise ValueError(f"レスポンスに音声データが含まれていませんでした: {str(response)[:500]}")
    return audios


# ---------------------------------------------------------------------------
# テキスト生成（シーンプランナー用 generateContent・JSON 出力）
# ---------------------------------------------------------------------------


async def generate_json_text(
    access_token: str,
    model: str,
    prompt: str,
    *,
    response_schema: Optional[dict[str, Any]] = None,
    project_id: Optional[str] = None,
    timeout: float = 120.0,
) -> str:
    """テキストモデルに JSON で答えさせ、その文字列を返す。"""
    generation_config: dict[str, Any] = {"responseMimeType": "application/json"}
    if response_schema:
        generation_config["responseSchema"] = response_schema
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    resp = await _request(
        "POST",
        build_generate_content_url(model),
        access_token,
        project_id=project_id,
        json_body=body,
        timeout=timeout,
    )
    pf = resp.get("promptFeedback") or {}
    if pf.get("blockReason"):
        raise ValueError(f"安全フィルタによりブロックされました: {pf['blockReason']}")
    for cand in resp.get("candidates") or []:
        parts = (cand.get("content") or {}).get("parts") or []
        # 思考パート（thought: true）は除き、回答テキストだけを連結する
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if text.strip():
            return text
    raise ValueError(f"プランナーモデルから回答が得られませんでした: {str(resp)[:500]}")


# ---------------------------------------------------------------------------
# ボイス（voices）
# ---------------------------------------------------------------------------


def build_create_voice_body(
    description: str,
    *,
    model: Optional[str] = None,
    display_name: Optional[str] = None,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
    pitch: Optional[str] = None,
    accent: Optional[str] = None,
    base_voice: Optional[str] = None,
) -> dict[str, Any]:
    prompted: dict[str, Any] = {"input": description}
    if base_voice:
        prompted["base_voice"] = base_voice
    voice: dict[str, Any] = {"type": "prompted", "prompted": prompted}
    optional = {
        "model": model,
        "display_name": display_name,
        "gender": gender,
        "language_code": language_code,
        "pitch": pitch,
        "accent": accent,
    }
    voice.update({k: v for k, v in optional.items() if v})
    # store=true で保存ボイス（voice_...）として作成し、合成時に ID で参照する。
    return {"store": True, "voice": voice}


async def create_voice(
    access_token: str,
    description: str,
    *,
    model: Optional[str] = None,
    display_name: Optional[str] = None,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
    pitch: Optional[str] = None,
    accent: Optional[str] = None,
    base_voice: Optional[str] = None,
    project_id: Optional[str] = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """自然文の説明から声を作る（ボイスデザイン）。base_voice 指定でリミックス。"""
    body = build_create_voice_body(
        description,
        model=model,
        display_name=display_name,
        gender=gender,
        language_code=language_code,
        pitch=pitch,
        accent=accent,
        base_voice=base_voice,
    )
    resp = await _request(
        "POST",
        build_voices_url(),
        access_token,
        project_id=project_id,
        json_body=body,
        timeout=timeout,
    )
    # 念のため {"voice": {...}} で包まれて返るケースにも対応する。
    voice = resp.get("voice") if isinstance(resp.get("voice"), dict) else resp
    if not voice.get("id"):
        raise ValueError(f"ボイスIDが返されませんでした: {str(resp)[:500]}")
    return voice


async def list_voices(
    access_token: str,
    *,
    params: Optional[list[tuple[str, Any]]] = None,
    project_id: Optional[str] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """ボイス一覧（自分の保存ボイス → プリビルトの順）。"""
    return await _request(
        "GET",
        build_voices_url(),
        access_token,
        project_id=project_id,
        params=params,
        timeout=timeout,
    )


async def get_voice(
    access_token: str,
    voice_id: str,
    *,
    project_id: Optional[str] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """保存ボイスを取得する（存在確認にも使う）。"""
    return await _request(
        "GET",
        build_voices_url(voice_id),
        access_token,
        project_id=project_id,
        timeout=timeout,
    )


async def delete_voice(
    access_token: str,
    voice_id: str,
    *,
    project_id: Optional[str] = None,
    timeout: float = 60.0,
) -> None:
    """保存ボイスを削除する。"""
    await _request(
        "DELETE",
        build_voices_url(voice_id),
        access_token,
        project_id=project_id,
        timeout=timeout,
    )
