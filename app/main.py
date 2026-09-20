"""エントリポイント: python -m app.main <ingest|publish|research|generate|status|check>"""
from __future__ import annotations

import argparse
import logging
import sys


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)


def cmd_status() -> int:
    from . import config, queue
    log = logging.getLogger("status")
    counts = queue.ready_counts()
    past = queue.recent_origins(config.MIX_HISTORY)
    share = (past.count(queue.ORIGIN_LIVE) / len(past) * 100) if past else 0
    log.info("未使用の元画像   : %d 枚", len(queue.pending_images()))
    log.info("投稿待ち (ready) : %d 件（実写 %d / AI %d）",
             counts["total"], counts.get(queue.ORIGIN_LIVE, 0),
             counts.get(queue.ORIGIN_AI, 0))
    log.info("直近の実写率     : %.0f%%（目標 %.0f%%）",
             share, config.LIVE_RATIO * 100)
    log.info("処理中 (inflight): %d 件", len(queue.stale_inflight()))
    for name, item in queue.iter_ready_preview():
        log.info("  - %s %s", item["id"], (item.get("caption") or "")[:40])
    return 0


def cmd_check() -> int:
    """デプロイ前の疎通確認。どこで詰まっているかを切り分けるために使う。"""
    from . import config, instagram, prompts, queue, secrets, store
    from .pixverse import PixverseClient
    log = logging.getLogger("check")
    ok = True

    try:
        rel = store.release()
        assets = len(rel.get("assets", []))
        store.list_names(queue.READY)
        log.info("[OK] GitHub リポジトリ %s に接続できます（添付 %d 件）",
                 config.GITHUB_REPO, assets)
        log.info("     メディア置き場: %s", rel.get("html_url", ""))
    except Exception as exc:
        ok = False
        log.error("[NG] GitHub: %s", exc)
        return 1

    live_only = config.LIVE_RATIO >= 1.0
    try:
        key = secrets.get(config.SECRET_PIXVERSE_KEY,
                          env_fallback="PIXVERSE_API_KEY")
        balance = PixverseClient(key).credits()
        log.info("[OK] PixVerse API キーを取得しました（残高: %s）",
                 balance if balance else "取得できず・要ダッシュボード確認")
    except Exception as exc:
        # 実写のみ運用なら PixVerse は使わないので、欠けていても失敗にしない
        if live_only:
            log.info("[--] PixVerse 未設定（LIVE_RATIO=1.0 の実写のみ運用なので問題なし）")
        else:
            ok = False
            log.error("[NG] PixVerse: %s", exc)

    try:
        token = secrets.get(config.SECRET_IG_TOKEN, env_fallback="IG_ACCESS_TOKEN")
        client = instagram.InstagramClient(token, config.IG_USER_ID)
        me = client.me()
        log.info("[OK] Instagram: @%s (%s) user_id=%s",
                 me.get("username"), me.get("account_type"), client.user_id)
        log.info("     投稿枠: %s", client.publishing_limit().get("data"))
    except Exception as exc:
        ok = False
        log.error("[NG] Instagram: %s", exc)

    try:
        pool = prompts.load()
        untested = len([p for p in pool["prompts"]
                        if p.get("trials", 0) < prompts.MIN_TRIALS])
        log.info("[OK] 企画プール: %d 件（未検証 %d 件）",
                 len(pool["prompts"]), untested)
    except Exception as exc:
        ok = False
        log.error("[NG] 企画プール: %s", exc)

    counts = queue.ready_counts()
    log.info("[--] 投稿待ち: 実写 %d 件 / AI %d 件（実写の目標比率 %.0f%%）",
             counts.get(queue.ORIGIN_LIVE, 0), counts.get(queue.ORIGIN_AI, 0),
             config.LIVE_RATIO * 100)

    if config.PROMPT_PROVIDER == "gemini":
        try:
            secrets.get(config.SECRET_GEMINI_KEY, env_fallback="GEMINI_API_KEY")
            log.info("[OK] Gemini API キーを取得しました（model=%s）", config.GEMINI_MODEL)
        except Exception as exc:
            log.warning("[--] Gemini キー未設定。テンプレート生成にフォールバックします: %s", exc)

    unmanaged = store.unmanaged_assets()
    if unmanaged:
        log.info("[--] 未取り込みの添付が %d 件あります（ingest で取り込めます）",
                 len(unmanaged))

    if secrets.has("GH_ADMIN_TOKEN"):
        log.info("[OK] GH_ADMIN_TOKEN あり（Instagram トークンを自動延長できます）")
    else:
        log.warning("[--] GH_ADMIN_TOKEN 未設定。60 日ごとに手動でトークンを"
                    "取り直す必要があります")

    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="scheduled-movie-uploader")
    parser.add_argument(
        "command",
        choices=["ingest", "publish", "research", "generate", "status", "check"],
        help=("ingest: 添付された動画・写真を取り込む / "
              "publish: キューから 1 件 Instagram に投稿 / "
              "research: 実測値を集計し企画を補充 / "
              "generate: 写真から AI 動画を作る / "
              "status: キューの状況 / check: 疎通確認"),
    )
    args = parser.parse_args(argv)

    if args.command == "generate":
        from .jobs import generate
        return generate.run()
    if args.command == "publish":
        from .jobs import publish
        return publish.run()
    if args.command == "research":
        from .jobs import research
        return research.run()
    if args.command == "ingest":
        from .jobs import ingest
        return ingest.run()
    if args.command == "status":
        return cmd_status()
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
