"""取り込みジョブ: リリースに手で添付されたファイルをキューに入れる。

スマホからの投入経路はこれ 1 本。
GitHub の「Releases → media → Edit → ファイルを添付」で上げるだけで、
このジョブが種類を判別し、動画なら要件を検査してから投稿キューに積む。

  IMG_1234.MOV   →（検査OK）→ videos/<id>.mov + queue/ready/<id>.json（実写）
  ai_walk.mp4    →（検査OK）→ 同上。ただし AI 生成として記録する
  IMG_1234.MOV   →（検査NG）→ videos/rejected/IMG_1234.MOV（理由をログに残す）
  photo.jpg      →           images/pending/<時刻>-photo.jpg
"""
from __future__ import annotations

import logging
import posixpath

import requests

from .. import config, prompts, queue, store, video

log = logging.getLogger(__name__)

REJECTED = "videos/rejected/"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# ファイル名でAI生成かどうかを見分ける。PixVerse アプリで作った動画を手で
# アップロードする運用があるため、実写と混ざると成績比較が意味を失う。
#   ai_散歩.mp4 / AI-sunset.mov / pixverse_0921.mp4  → AI 生成
#   IMG_1234.MOV                                      → 実写
AI_PREFIXES = ("ai_", "ai-")
AI_KEYWORDS = ("pixverse",)


def origin_of(filename: str) -> str:
    """添付ファイル名から動画の出どころを判定する。"""
    name = filename.lower()
    if name.startswith(AI_PREFIXES) or any(k in name for k in AI_KEYWORDS):
        return queue.ORIGIN_AI
    return queue.ORIGIN_LIVE


def _probe_asset(asset: dict) -> video.VideoInfo:
    """添付ファイルを全部落とさずに、必要な範囲だけ読んで検査する。"""
    url = asset["browser_download_url"]

    def read(start: int, end: int) -> bytes:
        resp = requests.get(url, headers={"Range": f"bytes={start}-{end}"},
                            timeout=120)
        resp.raise_for_status()
        return resp.content

    return video.probe_remote(int(asset.get("size") or 0), read)


def _ingest_video(asset: dict, pool: dict) -> str | None:
    name = asset["name"]
    info = _probe_asset(asset)

    if not info.ok:
        log.error("%s は要件を満たしません: %s", name, "／".join(info.errors))
        store.rename_asset(asset["id"], f"{REJECTED}{name}")
        return None

    for warning in info.warnings:
        log.warning("%s: %s", name, warning)

    item_id = queue.new_id()
    ext = posixpath.splitext(name)[1].lower() or ".mp4"
    path = f"{queue.VIDEOS}{item_id}{ext}"
    store.rename_asset(asset["id"], path)

    origin = origin_of(name)
    entry = prompts.pick(pool) or {}
    queue.enqueue_ready({
        "id": item_id,
        "origin": origin,
        "source": name,
        "video_path": path,
        "caption": entry.get("caption", config.DEFAULT_CAPTION),
        "hashtags": entry.get("hashtags", []),
        "prompt_id": entry.get("id", ""),
        "theme": entry.get("theme", ""),
        "motion": "live" if origin == queue.ORIGIN_LIVE else entry.get("motion", ""),
        "video_info": {
            "duration_sec": round(info.duration_sec, 1),
            "width": info.width,
            "height": info.height,
            "size_mb": round(info.size_bytes / 1e6, 1),
        },
        "created_at": queue.now_iso(),
    })
    if entry:
        prompts.mark_used(pool, entry["id"])
    label = "実写" if origin == queue.ORIGIN_LIVE else "AI 生成"
    log.info("取り込みました %s ← %s（%s・%s）",
             item_id, name, label, info.summary())
    return item_id


def _ingest_image(asset: dict) -> str:
    name = asset["name"]
    stamp = queue.new_id()
    path = f"{queue.IMAGES_PENDING}{stamp}-{name}"
    store.rename_asset(asset["id"], path)
    log.info("写真を取り込みました: %s", path)
    return path


def run() -> int:
    pending = store.unmanaged_assets()
    if not pending:
        log.info("新しく添付されたファイルはありません")
        return 0

    log.info("未処理の添付が %d 件あります", len(pending))
    pool = prompts.load()
    videos, images, skipped = 0, 0, 0
    pool_touched = False

    for asset in pending:
        name = asset["name"]
        ext = posixpath.splitext(name)[1].lower()
        try:
            if ext in video.VIDEO_EXTS:
                if _ingest_video(asset, pool):
                    videos += 1
                    pool_touched = True
            elif ext in IMAGE_EXTS:
                _ingest_image(asset)
                images += 1
            else:
                log.warning("扱えない形式のためスキップします: %s", name)
                skipped += 1
        except Exception:
            log.exception("取り込みに失敗しました: %s", name)
            skipped += 1

    if pool_touched:
        prompts.save(pool)

    counts = queue.ready_counts()
    log.info("動画 %d 件 / 写真 %d 件を取り込みました（スキップ %d）",
             videos, images, skipped)
    log.info("投稿待ち: 実写 %d 件 / AI %d 件",
             counts.get(queue.ORIGIN_LIVE, 0), counts.get(queue.ORIGIN_AI, 0))
    return 0
