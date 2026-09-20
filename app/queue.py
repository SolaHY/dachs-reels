"""GCS のプレフィックスを状態とみなす、極小の投稿キュー。

    images/pending/   … 未使用の元画像（任意で同名 .json に prompt/caption）
    images/used/      … 動画生成に使い終わった画像
    images/failed/    … 生成に失敗した画像
    videos/           … 投稿に使う動画（AI 生成・実写の両方）
    videos/rejected/  … 要件を満たさず取り込めなかった動画
    queue/ready/      … 投稿待ち
    queue/inflight/   … 投稿処理中（落ちた場合ここに残る＝二重投稿を防ぐ）
    queue/posted/     … 投稿済み
    queue/failed/     … 投稿失敗

同時実行は想定していない（1 日 1 件）。二重投稿だけは inflight で防ぐ。
"""
from __future__ import annotations

import datetime as dt
import posixpath
import uuid
from typing import Any

from . import store

IMAGES_PENDING = "images/pending/"
IMAGES_USED = "images/used/"
IMAGES_FAILED = "images/failed/"
VIDEOS = "videos/"
VIDEOS_REJECTED = "videos/rejected/"
READY = "queue/ready/"
INFLIGHT = "queue/inflight/"
POSTED = "queue/posted/"
FAILED = "queue/failed/"

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".m4v")

# 投稿の由来。実写と AI 生成を混ぜて運用し、成績を別々に測る
ORIGIN_LIVE = "live"
ORIGIN_AI = "pixverse"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def ready_names() -> list[str]:
    return [n for n in store.list_names(READY) if n.endswith(".json")]


def ready_count() -> int:
    return len(ready_names())


def ready_counts() -> dict[str, int]:
    """投稿待ちの内訳を由来ごとに数える。"""
    counts = {ORIGIN_LIVE: 0, ORIGIN_AI: 0, "total": 0}
    for name in ready_names():
        origin = store.read_json(name).get("origin", ORIGIN_AI)
        counts[origin] = counts.get(origin, 0) + 1
        counts["total"] += 1
    return counts


def pending_images() -> list[str]:
    """未使用の元画像を名前順で返す。サイドカー .json は除く。"""
    return [
        n for n in store.list_names(IMAGES_PENDING)
        if n.lower().endswith(IMAGE_EXTS)
    ]


def sidecar(image_name: str) -> dict[str, Any]:
    """画像と同名の .json があれば prompt / caption を読む。"""
    base = image_name.rsplit(".", 1)[0]
    name = f"{base}.json"
    if store.exists(name):
        return store.read_json(name)
    return {}


def enqueue_ready(item: dict[str, Any]) -> str:
    name = f"{READY}{item['id']}.json"
    store.write_json(name, item)
    return name


def next_ready() -> tuple[str, dict[str, Any]] | None:
    """最も古い投稿待ち項目を返す。id の先頭が時刻なので名前順＝古い順。"""
    names = ready_names()
    if not names:
        return None
    return names[0], store.read_json(names[0])


def recent_origins(limit: int = 10) -> list[str]:
    """直近に投稿した項目の由来を、新しい順で返す。"""
    names = sorted(
        (n for n in store.list_names(POSTED) if n.endswith(".json")), reverse=True
    )[:limit]
    return [store.read_json(n).get("origin", ORIGIN_AI) for n in names]


def next_ready_mixed(live_ratio: float,
                     history: int = 10) -> tuple[str, dict[str, Any]] | None:
    """実写と AI 生成の比率を保ちながら、次に投稿する項目を選ぶ。

    直近の投稿履歴での実写の割合が目標を下回っていれば実写を優先し、
    足りていれば AI 生成を出す。狙った側の在庫が無ければもう一方を使う。
    """
    names = ready_names()
    if not names:
        return None

    groups: dict[str, list[str]] = {}
    for name in names:
        origin = store.read_json(name).get("origin", ORIGIN_AI)
        groups.setdefault(origin, []).append(name)

    past = recent_origins(history)
    live_share = past.count(ORIGIN_LIVE) / len(past) if past else 0.0
    preferred = ORIGIN_LIVE if live_share < live_ratio else ORIGIN_AI
    other = ORIGIN_AI if preferred == ORIGIN_LIVE else ORIGIN_LIVE

    for origin in (preferred, other):
        if groups.get(origin):
            name = sorted(groups[origin])[0]
            return name, store.read_json(name)

    # 由来が上記どちらでもない項目（手動投入など）は最後に拾う
    return names[0], store.read_json(names[0])


def to_inflight(ready_name: str) -> str:
    dst = ready_name.replace(READY, INFLIGHT, 1)
    store.move(ready_name, dst)
    return dst


def finish(inflight_name: str, item: dict[str, Any], *, prefix: str) -> str:
    dst = inflight_name.replace(INFLIGHT, prefix, 1)
    store.write_json(dst, item)
    store.delete(inflight_name)
    return dst


def stale_inflight() -> list[str]:
    return [n for n in store.list_names(INFLIGHT) if n.endswith(".json")]


def iter_ready_preview(limit: int = 10) -> list[tuple[str, dict[str, Any]]]:
    """投稿待ちの先頭数件を古い順に返す（status 表示用）。"""
    names = [n for n in store.list_names(READY) if n.endswith(".json")][:limit]
    return [(n, store.read_json(n)) for n in names]
