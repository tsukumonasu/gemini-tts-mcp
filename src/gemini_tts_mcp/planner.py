"""
シーン（用途・場面）から「声」と「話し方」を考えるプランナー。

例: scene="AWS のイベントでのプレゼン" →
  voice_description: "A confident, articulate tech presenter in their 30s ..."（ボイスデザイン用）
  style            : "clear and energetic, measured pace"（speech_metadata.style 用）

Gemini のテキストモデル（既定 gemini-3.8-flash）を generateContent + JSON 出力で呼ぶ。
TTS 公式ガイドの書き分けルールに従う:
  - 年齢・性別・声質・なまりなど「変わらない特徴」→ voice_description（ボイスデザイン）
  - 感情・速さ・抑揚など「その場の話し方」→ style（短く）
  - 一瞬の出来事（間・息継ぎ・笑い）→ 本文中のタグ <short pause> 等
"""

import json
import re
from typing import Any, Optional

from .constants import VALID_GENDERS

# 本文に挿入してよいタグ（プレゼン等で使いやすいものに絞る）
ENHANCE_TAGS = ["<short pause>", "<long pause>", "<breath>", "<laugh>", "<chuckle>", "<sigh>"]

SCENE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "voice_description": {"type": "STRING"},
        "gender": {"type": "STRING", "enum": VALID_GENDERS},
        "language_code": {"type": "STRING"},
        "style": {"type": "STRING"},
        "rationale": {"type": "STRING"},
    },
    "required": ["voice_description", "gender", "language_code", "style", "rationale"],
}

ENHANCE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {"text": {"type": "STRING"}},
    "required": ["text"],
}


def build_scene_prompt(
    scene: str,
    sample_text: Optional[str],
    *,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
) -> str:
    hints = []
    if gender:
        hints.append(f"- The speaker's gender MUST be: {gender}")
    if language_code:
        hints.append(f"- The spoken language MUST be: {language_code}")
    hint_block = "\n".join(hints) if hints else "- (none)"
    sample = (sample_text or "").strip()[:2000] or "(not provided)"
    return f"""You are a voice director for a text-to-speech system (Gemini TTS with Voice design).
Given the SCENE where the audio will be used and a SAMPLE of the script, decide the ideal speaker voice
and delivery so that the audio sounds natural and effective for that scene.

SCENE (may be written in Japanese or any language):
{scene}

SCRIPT SAMPLE:
{sample}

Constraints from the user:
{hint_block}

Return JSON with:
- voice_description: 1-2 short English sentences describing the PERMANENT traits of the speaker:
  approximate age, gender, timbre / vocal texture, accent if relevant, and the persona's role
  (e.g. "A confident, articulate tech conference presenter in her early 30s with a clear, warm mid-range voice
  and a neutral standard Japanese accent."). Do not mention temporary emotions or speed here.
  Keep it short, concrete and non-contradictory.
- gender: one of {VALID_GENDERS}.
- language_code: BCP-47 code of the language the script is spoken in (detect from the sample, e.g. ja-JP, en-US).
- style: a SHORT English phrase (max ~12 words) for the situational delivery: emotion, pace, energy, prosody
  (e.g. "clear and confident, upbeat, measured pace with natural pauses"). Do NOT include age, gender or accent.
- rationale: 1-2 sentences, written in the same language as the SCENE text, explaining why this voice and
  delivery fit the scene.
"""


def build_enhance_prompt(text: str, scene: Optional[str], style: Optional[str]) -> str:
    return f"""You prepare scripts for a text-to-speech engine.
Insert pause / breath markup into the SCRIPT so it is delivered well in the given scene.

SCENE: {scene or "(not specified)"}
DELIVERY STYLE: {style or "(not specified)"}

Rules:
- Do NOT add, remove, translate or reorder any words. Keep every original character of the script.
- You MAY insert only these tags: {", ".join(ENHANCE_TAGS)}
  (mostly <short pause> between ideas and <long pause> before a key point or section change; use sparingly).
- You MAY put a few KEY words in CAPITAL letters for emphasis if they are written in Latin letters.
- Do not add stage directions, speaker labels or explanations.

SCRIPT:
{text}

Return JSON: {{"text": "<the script with tags inserted>"}}
"""


def parse_json_text(raw: str) -> dict[str, Any]:
    """モデル出力（JSON 文字列。```json フェンス付きにも対応）を dict にする。"""
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.DOTALL)
    if m:
        s = m.group(1).strip()
    if not s.startswith("{"):
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            s = s[start : end + 1]
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"プランナーの出力を JSON として解釈できませんでした: {raw[:300]}") from e
    if not isinstance(data, dict):
        raise ValueError(f"プランナーの出力が想定外の形式です: {raw[:300]}")
    return data


def normalize_scene_plan(
    data: dict[str, Any],
    *,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
) -> dict[str, Any]:
    """プランを検証・整形する。ユーザー指定（gender / language_code）は常に優先。"""
    desc = str(data.get("voice_description") or "").strip()
    if not desc:
        raise ValueError(f"プランナーが voice_description を返しませんでした: {data}")
    planned_gender = str(data.get("gender") or "").strip().lower()
    return {
        "voice_description": desc,
        "gender": gender or (planned_gender if planned_gender in VALID_GENDERS else None),
        "language_code": language_code or (str(data.get("language_code") or "").strip() or None),
        "style": str(data.get("style") or "").strip() or None,
        "rationale": str(data.get("rationale") or "").strip() or None,
    }


def _strip_markup(s: str) -> str:
    """比較用: タグと空白を除き、大文字小文字を無視した文字列。"""
    s = re.sub(r"<[^<>]{1,30}>", "", s)
    return re.sub(r"\s+", "", s).lower()


def validate_enhanced_text(original: str, enhanced: str) -> bool:
    """タグ挿入後の本文が、元の文章の言葉を変えていないか確認する。"""
    return bool(enhanced.strip()) and _strip_markup(original) == _strip_markup(enhanced)
