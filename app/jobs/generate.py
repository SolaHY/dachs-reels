"""動画生成ジョブ: 元画像 + 企画プロンプト → PixVerse → 投稿キュー。

処理する順序:
  1. Web UI から積まれた明示リクエスト（requests/generate/*.json）
  2. 在庫が目標に満たない分の自動生成
"""
from __future__ import annotations

import logging
import mimetypes
import posixpath

from .. import config, store, prompts, queue, secrets
from ..pixverse import PixverseClient, PixverseError

log = logging.getLogger(__name__)

REQUESTS = "requests/generate/"


def _content_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "image/jpeg"


def _compose(entry: dict | None, sidecar: dict) -> tuple[str, str, list[str], dict]:
    """企画候補とサイドカーからプロンプト・キャプション・タグを決める。

    サイドカー（画像ごとの .json）の指定が常に優先される。
    """
    entry = entry or {}
    prompt = sidecar.get("prompt") or entry.get("prompt") or config.DEFAULT_PROMPT
    caption = sidecar.get("caption") or entry.get("caption") or config.DEFAULT_CAPTION
    hashtags = sidecar.get("hashtags") or entry.get("hashtags") or []
    return prompt, caption, hashtags, entry


def _generate_one(client: PixverseClient, image_name: str,
                  entry: dict | None) -> str:
    sidecar = queue.sidecar(image_name)
    prompt, caption, hashtags, entry = _compose(entry, sidecar)

    data = store.read_bytes(image_name)
    basename = posixpath.basename(image_name)
    img_id = client.upload_image(data, basename, _content_type(basename))

    video_id = client.submit(img_id, prompt)
    result = client.wait(video_id)

    item_id = queue.new_id()
    video_path = f"{queue.VIDEOS}{item_id}.mp4"
    store.write_bytes(video_path, client.download(result.url), "video/mp4")

    queue.enqueue_ready({
        "id": item_id,
        "origin": "pixverse",
        "source_image": f"{queue.IMAGES_USED}{basename}",
        "video_path": video_path,
        "prompt": prompt,
        "caption": caption,
        "hashtags": hashtags,
        "prompt_id": entry.get("id", ""),
        "motion": entry.get("motion", ""),
        "theme": entry.get("theme", ""),
        "pixverse_video_id": result.video_id,
        "resolution": f"{result.width}x{result.height}",
        "created_at": queue.now_iso(),
    })

    store.move(image_name, f"{queue.IMAGES_USED}{basename}")
    base = image_name.rsplit(".", 1)[0]
    if store.exists(f"{base}.json"):
        store.move(f"{base}.json",
                 f"{queue.IMAGES_USED}{posixpath.basename(base)}.json")
    return item_id


def _pending_requests() -> list[tuple[str, dict]]:
    return [(n, store.read_json(n)) for n in store.list_names(REQUESTS)
            if n.endswith(".json")]


def run() -> int:
    pool = prompts.load()
    if not pool["prompts"]:
        log.warning("企画プールが空です。先に research ジョブを実行してください"
                    "（既定プロンプトで続行します）")

    requests_ = _pending_requests()
    counts = queue.ready_counts()
    live, ai = counts.get(queue.ORIGIN_LIVE, 0), counts.get(queue.ORIGIN_AI, 0)

    # LIVE_RATIO=1.0 は「実写だけで運用する」の意思表示なので、在庫が尽きても
    # 勝手に AI を作らない（PixVerse のキーが無い構成でも成立させるため）。
    # 管理画面から明示的に頼まれた生成だけは実行する。
    live_only = config.LIVE_RATIO >= 1.0

    # AI 側は「比率を保つのに必要な在庫」だけ持つ。実写が潤沢な週は生成せず、
    # クレジットを使わない。ただし全体の在庫が尽きかけたら比率を無視して埋める
    # ——投稿が途切れるほうが損失が大きいため。
    ai_target = round(config.QUEUE_TARGET * (1 - config.LIVE_RATIO))
    room = config.QUEUE_TARGET - (live + ai)
    wanted = max(ai_target - ai, config.MIN_RUNWAY - (live + ai))
    auto_need = 0 if live_only else max(
        0, min(wanted, room, config.GENERATE_MAX_PER_RUN - len(requests_)))
    log.info("投稿待ち: 実写 %d 件 / AI %d 件（全体目標 %d・AI 目標 %d・空き %d）",
             live, ai, config.QUEUE_TARGET, ai_target, room)
    if live_only:
        log.info("LIVE_RATIO=1.0（実写のみ運用）のため自動生成は行いません")
    log.info("明示リクエスト %d 件 → 自動生成 %d 件", len(requests_), auto_need)

    if not requests_ and auto_need <= 0:
        if live_only and live < config.MIN_RUNWAY:
            log.warning("実写の在庫が %d 件しかありません。動画を追加してください",
                        live)
        else:
            log.info("在庫が足りているため生成しません")
        return 0

    api_key = secrets.get(config.SECRET_PIXVERSE_KEY,
                          env_fallback="PIXVERSE_API_KEY")
    client = PixverseClient(api_key)

    ok = 0
    used_prompt_ids: set[str] = set()

    # 1. Web UI からの明示リクエスト
    for req_name, req in requests_[:config.GENERATE_MAX_PER_RUN]:
        image_name = req.get("image", "")
        if not store.exists(image_name):
            log.error("リクエストの画像が見つかりません: %s", image_name)
            store.delete(req_name)
            continue
        entry = prompts.get(pool, req.get("prompt_id", "")) if req.get("prompt_id") else None
        if entry is None:
            entry = prompts.pick(pool, exclude=used_prompt_ids)
        try:
            item_id = _generate_one(client, image_name, entry)
            if entry:
                prompts.mark_used(pool, entry["id"])
                used_prompt_ids.add(entry["id"])
            log.info("生成完了（リクエスト） %s ← %s", item_id, image_name)
            ok += 1
            store.delete(req_name)
        except PixverseError as exc:
            log.error("生成失敗 %s: %s", image_name, exc)
            store.move(image_name,
                     f"{queue.IMAGES_FAILED}{posixpath.basename(image_name)}")
            store.delete(req_name)
        except Exception:
            log.exception("想定外のエラー %s", image_name)
            store.delete(req_name)

    # 2. 在庫の自動補充
    images = [n for n in queue.pending_images()]
    if auto_need > 0 and not images:
        log.warning("images/pending/ に元画像がありません。写真を追加してください")

    for image_name in images[:auto_need]:
        entry = prompts.pick(pool, exclude=used_prompt_ids)
        try:
            item_id = _generate_one(client, image_name, entry)
            if entry:
                prompts.mark_used(pool, entry["id"])
                used_prompt_ids.add(entry["id"])
            log.info("生成完了（自動） %s ← %s", item_id, image_name)
            ok += 1
        except PixverseError as exc:
            log.error("生成失敗 %s: %s", image_name, exc)
            store.move(image_name,
                     f"{queue.IMAGES_FAILED}{posixpath.basename(image_name)}")
        except Exception:
            log.exception("想定外のエラー %s", image_name)

    if used_prompt_ids:
        prompts.save(pool)

    remaining = len(queue.pending_images())
    log.info("生成 %d 件成功 / 未使用画像 残り %d 枚 / 投稿待ち %d 件",
             ok, remaining, queue.ready_count())
    if remaining == 0:
        log.warning("未使用の元画像が尽きました。写真を補充してください")
    return 0 if ok else 1
