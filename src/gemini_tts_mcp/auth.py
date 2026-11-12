"""
Google OAuth 認証（MCP 内蔵のブラウザフロー版）。

gcloud / google-auth には依存しない。初回起動時にブラウザで自分の Google
アカウントにログイン → 認可コードを受け取り access/refresh token を取得して
`~/.gemini_tts_mcp_token.json` に保存する。以降は refresh token で自動更新。
access token 自動更新・refresh token 失効時は再認可の流儀。

必要な環境変数:
  GEMINI_TTS_CLIENT_ID     : OAuth クライアントID（デスクトップアプリ型）
  GEMINI_TTS_CLIENT_SECRET : クライアントシークレット
  GEMINI_TTS_REDIRECT_PORT : 任意。初回認可で使うローカルポート（未指定なら空きポートを自動選択）

Google Cloud Console 側:
  「認証情報」→「OAuthクライアントID」→ アプリの種類「デスクトップアプリ」を作成。
  デスクトップアプリ型は loopback リダイレクト（http://localhost:PORT/...）が許可される。
  ※ gemini-image-mcp / gemini-video-mcp と同じ OAuth クライアントを流用しても構わない（スコープは同じ）。
"""

import os
import json
import time
import logging
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Gemini API（generativelanguage）を OAuth で叩くのに必要なスコープ。
# 出所（公式 OAuth クイックスタート）: https://ai.google.dev/gemini-api/docs/oauth
#   gcloud auth application-default login --scopes=
#     'https://www.googleapis.com/auth/cloud-platform,
#      https://www.googleapis.com/auth/generative-language.retriever'
# ・スコープは必須。無し/不足だと 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT になる。
# ・cloud-platform は汎用（generateContent 等をカバー）。
# ・generative-language.retriever はセマンティック検索など Gemini API 固有機能用。
#   （※ "generative-language"（.retriever 無し）は無効なスコープで 400 invalid_scope）
# interactions（音声生成）・voices（ボイスデザイン）は cloud-platform で叩けるが、公式手順に合わせて両方要求しておく。
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/generative-language.retriever",
]

TOKEN_FILE = os.path.expanduser("~/.gemini_tts_mcp_token.json")


class AuthError(Exception):
    """認証情報が不足しているときに送出。"""


class ReauthorizationRequired(Exception):
    """Refresh Token 失効などでブラウザ再認可が必要なときに送出。"""

    def __init__(self, message: str = "再認証が必要です。reauthorize ツールを実行してください。"):
        super().__init__(message)


class Settings:
    def __init__(self) -> None:
        self.client_id = os.environ.get("GEMINI_TTS_CLIENT_ID", "")
        self._cs = os.environ.get("GEMINI_TTS_CLIENT_" + "SECRET", "")
        # 既定は 0（OS が空きポートを自動割り当て）。他プロセスとの衝突を避ける。
        # 明示的に固定したい場合のみ GEMINI_TTS_REDIRECT_PORT を指定する。
        env_port = os.environ.get("GEMINI_TTS_REDIRECT_PORT", "").strip()
        self.redirect_port = int(env_port) if env_port else 0

    @property
    def client_secret(self) -> str:
        return self._cs

    def require_client(self) -> None:
        if not self.client_id or not self._cs:
            raise AuthError(
                "GEMINI_TTS_CLIENT_ID / GEMINI_TTS_CLIENT_SECRET が未設定です。"
                "Google Cloud Console で OAuth クライアント（デスクトップアプリ型）を作成し、"
                "環境変数に設定してください。"
            )


def _run_local_auth(settings: Settings) -> dict:
    """ブラウザで認可し、トークン辞書を返す。初回のみ実行。

    重要な順序: 先にローカルサーバーを起動してからブラウザを開く。
    （ログイン済みだと Google が即座にコールバックへリダイレクトするため、
    サーバーが起動していないと Unauthorized 等になる。）
    ポートは既定で OS に自動割り当てさせ、実際に確保できたポートから
    redirect_uri を組み立てる。これにより他プロセス（別の loopback OAuth
    サーバー等）が特定ポートを握っていても衝突しない。
    無関係なリクエスト（/favicon.ico 等）は無視し、認可コードが来るまで待つ。
    """
    settings.require_client()
    state_value = "gemini-tts-mcp"
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            # コールバック以外（favicon 等）は 204 で軽く返し、待機を続ける
            if parsed.path != "/oauth-callback":
                self.send_response(204)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [""])[0]
            err = qs.get("error", [""])[0]
            state = qs.get("state", [""])[0]

            if err:
                result["error"] = err
                body = f"認可が拒否されました: {err}。このタブを閉じてください。"
            elif not code:
                result["error"] = "no_code"
                body = "認可コードを取得できませんでした。このタブを閉じてください。"
            elif state != state_value:
                result["error"] = "state_mismatch"
                body = "state が一致しません（セキュリティ検証エラー）。このタブを閉じてください。"
            else:
                result["code"] = code
                body = "認可が完了しました。このタブを閉じてください。"

            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass  # stdio を汚さない

    # 1) 先にサーバーを起動。127.0.0.1 に固定して IPv4/IPv6(localhost) の揺れを回避。
    #    redirect_port=0 の場合は OS が空きポートを自動選択する。
    try:
        server = HTTPServer(("127.0.0.1", settings.redirect_port), CallbackHandler)
    except OSError as e:
        raise AuthError(
            f"ローカルポート {settings.redirect_port} を確保できませんでした（{e}）。"
            "別プロセスが使用中の可能性があります。GEMINI_TTS_REDIRECT_PORT の指定を外して"
            "自動割り当てにするか、占有プロセスを終了してください。"
        ) from e

    # 実際に確保できたポートから redirect_uri を組み立てる（認可・トークン交換で共通）。
    actual_port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{actual_port}/oauth-callback"

    # 2) その後でブラウザを開く
    params = {
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "response_type": "code",
        "access_type": "offline",       # refresh_token を得るため
        "prompt": "consent",            # 毎回 refresh_token を確実に得る
        "state": state_value,
    }
    auth_url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    logger.info("ブラウザで認可してください: %s", auth_url)
    try:
        webbrowser.open(auth_url)

        # 3) 認可コード（または明示エラー）が来るまでリクエストを捌き続ける
        while "code" not in result and "error" not in result:
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise AuthError(f"認可に失敗しました: {result['error']}")

    code = result.get("code")
    if not code:
        raise AuthError("認可コードを取得できませんでした。")

    data = {
        "client_id": settings.client_id,
        "client_secret": settings.client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    resp = httpx.post(
        GOOGLE_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise AuthError(f"トークン取得に失敗しました ({resp.status_code}): {resp.text}")
    return resp.json()


class TokenManager:
    """access/refresh token の保存・自動更新・再認可を管理する。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._load()

    def _load(self) -> None:
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE) as f:
                    d = json.load(f)
                self._access_token = d.get("access_token")
                self._refresh_token = d.get("refresh_token")
                self._expires_at = d.get("expires_at", 0.0)
            except Exception as e:
                logger.warning("トークンファイルの読み込みに失敗: %s", e)

    def _save(self) -> None:
        with open(TOKEN_FILE, "w") as f:
            json.dump(
                {
                    "access_token": self._access_token,
                    "refresh_token": self._refresh_token,
                    "expires_at": self._expires_at,
                },
                f,
            )
        os.chmod(TOKEN_FILE, 0o600)

    def _store_response(self, body: dict) -> None:
        self._access_token = body["access_token"]
        if body.get("refresh_token"):
            self._refresh_token = body["refresh_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        self._save()

    def has_any_token(self) -> bool:
        return bool(self._access_token or self._refresh_token)

    def ensure_initial_auth(self) -> None:
        """トークンが無ければブラウザ認可を実行（起動時に1回）。"""
        if not self.has_any_token():
            body = _run_local_auth(self.settings)
            self._store_response(body)

    async def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        self.settings.require_client()
        data = {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    GOOGLE_TOKEN_URL, data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            body = resp.json()
        except Exception as e:
            logger.warning("トークン更新の通信に失敗: %s", e)
            return False

        if resp.status_code != 200 or "error" in body:
            logger.warning(
                "トークン更新に失敗 (%s): %s",
                resp.status_code, body.get("error_description", body.get("error", "unknown")),
            )
            return False

        self._store_response(body)
        return True

    def _reauthorize(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0
        try:
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
        except OSError:
            pass
        body = _run_local_auth(self.settings)
        self._store_response(body)

    def status(self) -> str:
        """'valid' / 'expired' / 'needs_reauth'。"""
        if self._access_token and time.time() < self._expires_at - 60:
            return "valid"
        if self._refresh_token:
            return "expired"
        return "needs_reauth"

    async def get_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        if await self._refresh():
            return self._access_token
        raise ReauthorizationRequired()

    async def reauthorize_async(self) -> None:
        import asyncio
        await asyncio.to_thread(self._reauthorize)


def resolve_project_id(explicit: str | None = None) -> str | None:
    """使用するプロジェクトIDを解決する（quota project ヘッダ用）。

    Gemini API を OAuth で叩く場合、課金/クォータ帰属のため
    x-goog-user-project ヘッダにプロジェクトIDを付ける必要がある
    （API キー方式では不要）。
    """
    return (
        explicit
        or os.environ.get("GEMINI_TTS_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT")
    )
