"""毎朝の投稿ジョブ: キューの先頭を Instagram にリール投稿する。"""
from __future__ import annotations

import datetime as dt
import logging

from .. import config, store, instagram, queue, secrets

log = logging.getLogger(__name__)

TOKEN_STATE = "state/ig_token.json"
# 有効期限 60 日。30 日を超えたら延長する（延長は発行 24 時間後から可能）
REFRESH_AFTER_DAYS = 30
# 署名付き URL の有効時間。Instagram 側の取り込みに十分な余裕を持たせる
SIGNED_URL_MINUTES = 180


def _ensure_fresh_token() -> str:
    """必要なら長期トークンを延長し、使えるトークンを返す。"""
    token = secrets.get(config.SECRET_IG_TOKEN, env_fallback="IG_ACCESS_TOKEN")

    if store.exists(TOKEN_STATE):
        state = store.read_json(TOKEN_STATE)
        refreshed_at = dt.datetime.fromisoformat(state["refreshed_at"])
        age = dt.datetime.now(dt.timezone.utc) - refreshed_at
        if age < dt.timedelta(days=REFRESH_AFTER_DAYS):
            log.info("トークンは %d 日前に更新済み。延長不要", age.days)
            return token
    else:
        log.info("トークン更新履歴がないため、まず延長を試みます")

    try:
        new_token, expires_in = instagram.refresh_long_lived_token(token)
    except instagram.InstagramError as exc:
        log.error("トークン延長に失敗しました（投稿は現行トークンで続行）: %s", exc)
        return token

    if new_token != token:
        if not secrets.update(config.SECRET_IG_TOKEN, new_token):
            # 保存できないなら延長の意味が無いので、状態も書かず次回も試す
            log.error("延長したトークンを保存できませんでした。"
                      "GH_ADMIN_TOKEN（Secrets: write）を設定してください")
            return token
        token = new_token

    now = dt.datetime.now(dt.timezone.utc)
    store.write_json(TOKEN_STATE, {
        "refreshed_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + dt.timedelta(seconds=expires_in)).isoformat(
            timespec="seconds"),
        "expires_in_days": round(expires_in / 86400, 1),
    })
    log.info("トークンを延長しました（残り約 %.0f 日）", expires_in / 86400)
    return token


def _caption(item: dict) -> str:
    """本文とハッシュタグを組み立てる。Instagram の上限は 2,200 文字。"""
    body = (item.get("caption") or "").strip()
    tags = [t for t in (item.get("hashtags") or []) if t.strip()]
    text = body + ("\n\n" + " ".join(tags) if tags else "")
    if len(text) > 2200:
        log.warning("キャプションが 2,200 文字を超えたため切り詰めます")
        text = text[:2199]
    return text


def run() -> int:
    # 直前に添付された動画も取りこぼさないよう、投稿前に必ず取り込む
    from . import ingest
    ingest.run()

    stale = queue.stale_inflight()
    if stale:
        log.error(
            "処理途中で終了した項目が残っています（投稿済みか確認のうえ "
            "queue/inflight/ から手動で移動してください）: %s", ", ".join(stale))

    counts = queue.ready_counts()
    log.info("投稿待ちの内訳: 実写 %d 件 / AI %d 件（実写の目標比率 %.0f%%）",
             counts.get(queue.ORIGIN_LIVE, 0), counts.get(queue.ORIGIN_AI, 0),
             config.LIVE_RATIO * 100)

    nxt = queue.next_ready_mixed(config.LIVE_RATIO, config.MIX_HISTORY)
    if nxt is None:
        log.error("投稿キューが空です。実写の追加か generate ジョブを確認してください")
        return 1
    ready_name, item = nxt
    origin = item.get("origin", "?")
    past = queue.recent_origins(config.MIX_HISTORY)
    log.info("投稿対象: %s（%s）／直近 %d 件の実写率 %.0f%%",
             item["id"],
             "実写" if origin == queue.ORIGIN_LIVE else "AI 生成",
             len(past),
             (past.count(queue.ORIGIN_LIVE) / len(past) * 100) if past else 0)

    token = _ensure_fresh_token()
    client = instagram.InstagramClient(token, config.IG_USER_ID)

    limit = client.publishing_limit()
    usage = (limit.get("data") or [{}])[0].get("quota_usage")
    log.info("24 時間の投稿枠 使用済み: %s / 50", usage)

    inflight_name = queue.to_inflight(ready_name)
    try:
        video_url = store.signed_url(item["video_path"], minutes=SIGNED_URL_MINUTES)
        container_id = client.create_reel_container(video_url, _caption(item))
        client.wait_container(container_id)
        media_id = client.publish(container_id)

        item.update({
            "published_at": queue.now_iso(),
            "ig_media_id": media_id,
            "ig_permalink": client.permalink(media_id),
        })
        queue.finish(inflight_name, item, prefix=queue.POSTED)
        log.info("投稿完了 %s → %s", item["id"], item.get("ig_permalink", media_id))
        return 0
    except instagram.InstagramError as exc:
        log.error("投稿失敗 %s: %s", item["id"], exc)
        item["failed_at"] = queue.now_iso()
        item["error"] = str(exc)
        queue.finish(inflight_name, item, prefix=queue.FAILED)
        return 1
    except Exception as exc:
        # 投稿済みかどうか判断できないため inflight に残し、手動確認に委ねる
        log.exception("想定外のエラー。%s は inflight に残します", inflight_name)
        raise exc
