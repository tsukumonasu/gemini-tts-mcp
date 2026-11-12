"""
ボイスデザインで作った声のローカルキャッシュ。

「声の説明文（＋性別・言語などの条件）→ voice_id」を JSON に保存し、
同じ説明で generate_speech を何度呼んでも同じ声を再利用する。
  - 毎回新しい声を作ると声質がブレる（一貫性が失われる）
  - 保存ボイスはプロジェクトあたり 200 件までなので、無駄に消費しない
"""

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def make_key(
    description: str,
    *,
    model: str,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
    pitch: Optional[str] = None,
    accent: Optional[str] = None,
    base_voice: Optional[str] = None,
) -> str:
    """キャッシュキー（条件が 1 つでも違えば別の声として扱う）。"""
    return json.dumps(
        {
            "description": " ".join(description.split()),
            "model": model,
            "gender": gender or "",
            "language_code": language_code or "",
            "pitch": pitch or "",
            "accent": accent or "",
            "base_voice": base_voice or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def make_scene_key(
    scene: str,
    *,
    model: str,
    gender: Optional[str] = None,
    language_code: Optional[str] = None,
) -> str:
    """シーンプランのキャッシュキー（同じシーン＝同じ声で統一するため）。"""
    return json.dumps(
        {
            "scene": " ".join(scene.split()),
            "model": model,
            "gender": gender or "",
            "language_code": language_code or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class VoiceCache:
    def __init__(self, path: str) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] = {}
        self._scenes: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._entries = data.get("voices", {}) or {}
                self._scenes = data.get("scenes", {}) or {}
        except Exception as e:  # 壊れていても動作は継続（キャッシュなし扱い）
            logger.warning("ボイスキャッシュの読み込みに失敗: %s", e)
            self._entries, self._scenes = {}, {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"voices": self._entries, "scenes": self._scenes},
                f, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, self.path)

    def get(self, key: str) -> Optional[dict[str, Any]]:
        return self._entries.get(key)

    def put(self, key: str, voice_id: str, **meta: Any) -> dict[str, Any]:
        entry = {"voice_id": voice_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), **meta}
        self._entries[key] = entry
        self._save()
        return entry

    def remove_voice_id(self, voice_id: str) -> int:
        """指定 voice_id を参照するエントリを削除し、削除件数を返す。"""
        keys = [k for k, v in self._entries.items() if v.get("voice_id") == voice_id]
        for k in keys:
            del self._entries[k]
        if keys:
            self._save()
        return len(keys)

    def entries(self) -> list[dict[str, Any]]:
        out = []
        for key, entry in self._entries.items():
            try:
                cond = json.loads(key)
            except ValueError:
                cond = {}
            out.append({**cond, **entry})
        return out

    # --- シーンプラン ---

    def get_scene(self, key: str) -> Optional[dict[str, Any]]:
        return self._scenes.get(key)

    def put_scene(self, key: str, plan: dict[str, Any]) -> dict[str, Any]:
        entry = {**plan, "planned_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._scenes[key] = entry
        self._save()
        return entry

    def scenes(self) -> list[dict[str, Any]]:
        out = []
        for key, plan in self._scenes.items():
            try:
                cond = json.loads(key)
            except ValueError:
                cond = {}
            out.append({"scene": cond.get("scene"), "model": cond.get("model"), **plan})
        return out
