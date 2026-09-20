"""Instagram Graph API クライアント（Instagram API with Instagram Login 方式）。

Facebook ページ連携は不要だが、対象アカウントはプロアカウント
（ビジネス／クリエイター）である必要がある。

投稿の流れ:
  1. POST /{ig-user-id}/media           … media_type=REELS + 公開 URL でコンテナ作成
  2. GET  /{container-id}?fields=status_code … FINISHED になるまでポーリング
  3. POST /{ig-user-id}/media_publish   … creation_id を渡して公開
"""
from __future__ import annotations

import logging
import time

import requests

from . import config

log = logging.getLogger(__name__)

GRAPH = "https://graph.instagram.com"

# リールで取得を試みるメトリクス。未対応のものは実行時に自動で除外する。
REEL_METRICS = [
    "views", "reach", "likes", "comments", "saved", "shares",
    "total_interactions", "ig_reels_avg_watch_time",
]


class InstagramError(RuntimeError):
    pass


def _check(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise InstagramError(f"JSON 以外の応答: {resp.text[:400]}")
    if "error" in payload:
        err = payload["error"]
        raise InstagramError(
            f"Instagram API エラー: {err.get('message')} "
            f"(type={err.get('type')} code={err.get('code')} "
            f"subcode={err.get('error_subcode')})"
        )
    resp.raise_for_status()
    return payload


class InstagramClient:
    def __init__(self, access_token: str, ig_user_id: str = "",
                 *, session: requests.Session | None = None):
        self._token = access_token
        self._session = session or requests.Session()
        self._user_id = ig_user_id or self._resolve_user_id()

    @property
    def user_id(self) -> str:
        return self._user_id

    def _url(self, path: str) -> str:
        return f"{GRAPH}/{config.IG_API_VERSION}/{path.lstrip('/')}"

    def _resolve_user_id(self) -> str:
        data = _check(self._session.get(
            self._url("me"),
            params={"fields": "user_id,username", "access_token": self._token},
            timeout=30,
        ))
        # Instagram Login では user_id が投稿用 ID
        return str(data.get("user_id") or data["id"])

    def me(self) -> dict:
        return _check(self._session.get(
            self._url("me"),
            params={"fields": "user_id,username,account_type",
                    "access_token": self._token},
            timeout=30,
        ))

    def publishing_limit(self) -> dict:
        """24 時間あたりの投稿枠の消費状況（上限 50 件）。"""
        return _check(self._session.get(
            self._url(f"{self._user_id}/content_publishing_limit"),
            params={"fields": "config,quota_usage", "access_token": self._token},
            timeout=30,
        ))

    def create_reel_container(self, video_url: str, caption: str,
                              *, share_to_feed: bool = True) -> str:
        params = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": self._token,
        }
        data = _check(self._session.post(
            self._url(f"{self._user_id}/media"), data=params, timeout=120
        ))
        container_id = str(data["id"])
        log.info("メディアコンテナを作成しました id=%s", container_id)
        return container_id

    def wait_container(self, container_id: str, *, timeout: int | None = None,
                       interval: int = 10) -> None:
        """コンテナが FINISHED になるまで待つ。Instagram 側が video_url を取得する。"""
        deadline = time.monotonic() + (timeout or config.IG_PUBLISH_TIMEOUT_SEC)
        while True:
            data = _check(self._session.get(
                self._url(container_id),
                params={"fields": "status_code,status", "access_token": self._token},
                timeout=30,
            ))
            code = data.get("status_code")
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(
                    f"コンテナが {code} になりました: {data.get('status')}"
                )
            if time.monotonic() > deadline:
                raise InstagramError(
                    f"コンテナ処理がタイムアウトしました id={container_id} "
                    f"(最終状態 {code})"
                )
            log.info("Instagram 側で処理中… status=%s", code)
            time.sleep(interval)

    def publish(self, container_id: str) -> str:
        data = _check(self._session.post(
            self._url(f"{self._user_id}/media_publish"),
            data={"creation_id": container_id, "access_token": self._token},
            timeout=120,
        ))
        media_id = str(data["id"])
        log.info("投稿しました media_id=%s", media_id)
        return media_id

    # --- インサイト取得（市場調査用） --------------------------------
    # Instagram Login 方式でも /insights は利用できる（ハッシュタグ検索は不可）

    def recent_media(self, limit: int = 50) -> list[dict]:
        """自分の直近メディアを新しい順で返す。"""
        data = _check(self._session.get(
            self._url(f"{self._user_id}/media"),
            params={
                "fields": "id,caption,media_type,media_product_type,timestamp,permalink",
                "limit": limit,
                "access_token": self._token,
            },
            timeout=60,
        ))
        return data.get("data", [])

    def media_insights(self, media_id: str,
                       metrics: list[str] | None = None) -> dict[str, int]:
        """メディア単位のインサイト。

        利用できるメトリクスは媒体種別や API バージョンで変わるため、
        拒否されたものを落として再試行する（全滅したら空 dict）。
        """
        remaining = list(metrics or REEL_METRICS)
        while remaining:
            resp = self._session.get(
                self._url(f"{media_id}/insights"),
                params={"metric": ",".join(remaining),
                        "access_token": self._token},
                timeout=30,
            )
            payload = resp.json()
            if "error" not in payload:
                return {
                    row["name"]: (row.get("values") or [{}])[0].get("value", 0)
                    for row in payload.get("data", [])
                }
            message = payload["error"].get("message", "")
            dropped = [m for m in remaining if m in message]
            if not dropped:
                log.warning("インサイト取得に失敗 %s: %s", media_id, message)
                return {}
            for m in dropped:
                remaining.remove(m)
            log.info("未対応のメトリクスを除外して再試行: %s", dropped)
        return {}

    def account_insights(self, days: int = 7) -> dict[str, int]:
        """アカウント単位のリーチなど。取れなければ空 dict を返す。"""
        try:
            data = _check(self._session.get(
                self._url(f"{self._user_id}/insights"),
                params={"metric": "reach", "period": "day",
                        "metric_type": "total_value",
                        "access_token": self._token},
                timeout=30,
            ))
        except InstagramError as exc:
            log.warning("アカウントインサイトを取得できません: %s", exc)
            return {}
        return {
            row["name"]: row.get("total_value", {}).get("value", 0)
            for row in data.get("data", [])
        }

    def permalink(self, media_id: str) -> str:
        data = _check(self._session.get(
            self._url(media_id),
            params={"fields": "permalink", "access_token": self._token},
            timeout=30,
        ))
        return data.get("permalink", "")


def refresh_long_lived_token(token: str) -> tuple[str, int]:
    """長期トークンを延長する（発行から 24 時間以上経過していれば可、有効期間 60 日）。

    Returns: (新しいトークン, 有効秒数)
    """
    data = _check(requests.get(
        f"{GRAPH}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=30,
    ))
    return data["access_token"], int(data.get("expires_in", 0))
