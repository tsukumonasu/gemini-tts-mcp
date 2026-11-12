"""
生成音声の保存。

3.8 系 TTS は unary で WAV（RIFF ヘッダ付き）を返すのでそのまま保存する。
旧モデル等で生の PCM（audio/l16 / audio/pcm）が返ってきた場合は、
16bit・モノラルの WAV ヘッダを付けて保存する（再生できるファイルにするため）。
"""

import base64
import io
import os
import re
import wave
from datetime import datetime
from typing import Any, Optional


def _rate_from_mime(mime_type: str) -> Optional[int]:
    """'audio/L16;codec=pcm;rate=24000' のような MIME から rate を取り出す。"""
    m = re.search(r"rate=(\d+)", mime_type or "")
    return int(m.group(1)) if m else None


def ensure_wav(raw: bytes, mime_type: str, fallback_rate: int = 24000) -> bytes:
    """WAV ならそのまま、ヘッダ無し PCM なら WAV ヘッダを付けて返す。"""
    if raw[:4] == b"RIFF":
        return raw
    rate = _rate_from_mime(mime_type) or fallback_rate
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16bit
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


def wav_info(data: bytes) -> dict[str, Any]:
    """WAV の長さ・サンプルレートを返す（読めなければ空）。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return {
                "duration_sec": round(frames / float(rate), 2) if rate else None,
                "sample_rate": rate,
                "channels": w.getnchannels(),
            }
    except (wave.Error, EOFError):
        return {}


def slugify(text: str, max_length: int = 30) -> str:
    """ファイル名用に整形する。日本語などの文字は残し、記号・空白を - にする。"""
    s = re.sub(r"[^\w]+", "-", text.strip().lower(), flags=re.UNICODE)
    s = re.sub(r"^-+|-+$", "", s)
    return s[:max_length].rstrip("-")


def generate_filename(text: str, index: int, custom_name: Optional[str] = None) -> str:
    suffix = f"-{index + 1}" if index > 0 else ""
    if custom_name:
        return f"{custom_name}{suffix}.wav"
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    return f"{slugify(text) or 'speech'}-{timestamp}{suffix}.wav"


def save_audio(
    audios: list[dict[str, str]],
    text: str,
    output_dir: str,
    custom_name: Optional[str] = None,
    fallback_rate: int = 24000,
) -> list[dict[str, Any]]:
    """base64 音声群を WAV ファイルとして保存し、保存先情報を返す。"""
    os.makedirs(output_dir, exist_ok=True)
    saved: list[dict[str, Any]] = []
    for i, audio in enumerate(audios):
        data = ensure_wav(base64.b64decode(audio["data"]), audio["mimeType"], fallback_rate)
        path = os.path.join(output_dir, generate_filename(text, i, custom_name))
        with open(path, "wb") as f:
            f.write(data)
        saved.append(
            {
                "path": path,
                "mimeType": "audio/wav",
                "index": i,
                "bytes": len(data),
                **wav_info(data),
            }
        )
    return saved


def save_base64_wav(data_b64: str, mime_type: str, path: str) -> dict[str, Any]:
    """単一の base64 音声（ボイスのサンプル等）を指定パスに WAV で保存する。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = ensure_wav(base64.b64decode(data_b64), mime_type)
    with open(path, "wb") as f:
        f.write(data)
    return {"path": path, "mimeType": "audio/wav", "bytes": len(data), **wav_info(data)}
