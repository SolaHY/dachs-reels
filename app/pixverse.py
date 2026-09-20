"""PixVerse Open API クライアント（画像 → 動画）。

docs: https://docs.platform.pixverse.ai/
  POST /openapi/v2/image/upload          … 画像アップロード → img_id
  POST /openapi/v2/video/img/generate    … 生成タスク投入 → video_id
  GET  /openapi/v2/video/result/{id}     … 状態確認（status 1=成功 5=処理中 7=審査NG 8=失敗）
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import requests

from . import config

log = logging.getLogger(__name__)

BASE = "https://app-api.pixverse.ai/openapi/v2"

STATUS_SUCCESS = 1
STATUS_IN_PROGRESS = 5
STATUS_MODERATION_FAILED = 7
STATUS_FAILED = 8

STATUS_TEXT = {
    STATUS_SUCCESS: "生成成功",
    STATUS_IN_PROGRESS: "処理中",
    STATUS_MODERATION_FAILED: "審査で拒否されました",
    STATUS_FAILED: "生成に失敗しました",
}


class PixverseError(RuntimeError):
    pass


@dataclass
class GeneratedVideo:
    video_id: int
    url: str
    width: int
    height: int


class PixverseClient:
    def __init__(self, api_key: str, *, session: requests.Session | None = None):
        self._api_key = api_key
        self._session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        # Ai-trace-id はリクエストごとに一意にする。使い回すと新規生成されない。
        return {"API-KEY": self._api_key, "Ai-trace-id": str(uuid.uuid4())}

    @staticmethod
    def _unwrap(payload: dict) -> dict:
        """PixVerse は ErrCode/Resp と err_code/resp の両方の形を返す。"""
        code = payload.get("ErrCode", payload.get("err_code", 0))
        if code not in (0, 200):
            msg = payload.get("ErrMsg") or payload.get("err_msg") or "unknown error"
            raise PixverseError(f"PixVerse API エラー ({code}): {msg}")
        return payload.get("Resp") or payload.get("resp") or {}

    def upload_image(self, data: bytes, filename: str, content_type: str) -> int:
        resp = self._session.post(
            f"{BASE}/image/upload",
            headers=self._headers(),
            files={"image": (filename, data, content_type)},
            timeout=120,
        )
        resp.raise_for_status()
        img_id = self._unwrap(resp.json()).get("img_id")
        if not img_id:
            raise PixverseError(f"img_id が返りませんでした: {resp.text[:400]}")
        log.info("画像をアップロードしました img_id=%s (%s)", img_id, filename)
        return int(img_id)

    def submit(self, img_id: int, prompt: str) -> int:
        body = {
            "img_id": img_id,
            "prompt": prompt,
            "model": config.PIXVERSE_MODEL,
            "quality": config.PIXVERSE_QUALITY,
            "duration": config.PIXVERSE_DURATION,
        }
        resp = self._session.post(
            f"{BASE}/video/img/generate",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        video_id = self._unwrap(resp.json()).get("video_id")
        if not video_id:
            raise PixverseError(f"video_id が返りませんでした: {resp.text[:400]}")
        log.info("生成タスクを投入しました video_id=%s", video_id)
        return int(video_id)

    def result(self, video_id: int) -> dict:
        resp = self._session.get(
            f"{BASE}/video/result/{video_id}",
            headers=self._headers(),
            timeout=60,
        )
        resp.raise_for_status()
        return self._unwrap(resp.json())

    def wait(self, video_id: int, *, timeout: int | None = None,
             interval: int = 15) -> GeneratedVideo:
        deadline = time.monotonic() + (timeout or config.PIXVERSE_TIMEOUT_SEC)
        while True:
            data = self.result(video_id)
            status = int(data.get("status", STATUS_IN_PROGRESS))
            if status == STATUS_SUCCESS:
                url = data.get("url")
                if not url:
                    raise PixverseError("status=1 ですが url がありません")
                return GeneratedVideo(
                    video_id=video_id,
                    url=url,
                    width=int(data.get("outputWidth") or 0),
                    height=int(data.get("outputHeight") or 0),
                )
            if status != STATUS_IN_PROGRESS:
                raise PixverseError(
                    f"生成が中断されました (status={status}: "
                    f"{STATUS_TEXT.get(status, '不明')})"
                )
            if time.monotonic() > deadline:
                raise PixverseError(f"生成がタイムアウトしました video_id={video_id}")
            log.info("生成中… video_id=%s", video_id)
            time.sleep(interval)

    def download(self, url: str) -> bytes:
        resp = self._session.get(url, timeout=300)
        resp.raise_for_status()
        return resp.content

    def credits(self) -> dict | None:
        """残クレジットの取得を試みる（エンドポイント未提供なら None）。"""
        try:
            resp = self._session.get(
                f"{BASE}/account/balance", headers=self._headers(), timeout=30
            )
            if resp.status_code != 200:
                return None
            return self._unwrap(resp.json())
        except Exception:
            return None
