"""市場調査ジョブ（週次）: 実測値を集計し、次の企画候補を補充する。"""
from __future__ import annotations

import logging

from .. import config, store, instagram, prompts, research, secrets

log = logging.getLogger(__name__)


def _refill(pool: dict, performance: str) -> int:
    """未検証の候補が減っていたら補充する。"""
    untested = [p for p in pool["prompts"] if p.get("trials", 0) < prompts.MIN_TRIALS]
    if len(untested) >= config.POOL_MIN_UNTESTED:
        log.info("未検証の候補が %d 件あるため補充しません", len(untested))
        return 0

    count = config.RESEARCH_NEW_PROMPTS
    existing = [p["prompt"] for p in pool["prompts"]]
    entries = []

    if config.PROMPT_PROVIDER == "gemini":
        try:
            api_key = secrets.get(config.SECRET_GEMINI_KEY,
                                  env_fallback="GEMINI_API_KEY")
            entries = prompts.gemini_entries(
                count,
                api_key=api_key,
                model=config.GEMINI_MODEL,
                duration=config.PIXVERSE_DURATION,
                performance=performance,
                existing=existing,
                pet_names=config.PET_NAMES,
            )
            log.info("Gemini で %d 件の企画を生成しました", len(entries))
        except Exception as exc:
            log.warning("Gemini での生成に失敗したためテンプレートに切り替えます: %s", exc)

    if not entries:
        entries = prompts.template_entries(count, config.PET_NAMES)
        log.info("テンプレートで %d 件の企画を生成しました", len(entries))

    added = prompts.merge(pool, entries)
    log.info("重複を除いて %d 件をプールに追加しました", added)
    return added


def _suggest_ratio(by_origin: list) -> None:
    """実写と AI のどちらが伸びているかを見て、比率の見直しを促す。

    自動では変えない。どちらを厚くするかは運用者が決めるべき判断のため。
    """
    scores = {name: score for name, score, n in by_origin if n >= 3}
    live, ai = scores.get("live"), scores.get("pixverse")
    if live is None or ai is None:
        return
    gap = live - ai
    if abs(gap) < 0.1:
        log.info("  実写と AI の成績はほぼ互角です（現在の LIVE_RATIO=%.2f を維持で可）",
                 config.LIVE_RATIO)
    elif gap > 0:
        log.info("  実写が %.2f ポイント上回っています。LIVE_RATIO を上げる価値があります"
                 "（現在 %.2f）", gap, config.LIVE_RATIO)
    else:
        log.info("  AI 生成が %.2f ポイント上回っています。LIVE_RATIO を下げても良さそうです"
                 "（現在 %.2f）", -gap, config.LIVE_RATIO)


def run() -> int:
    pool = prompts.load()

    # 1. 実測値を集める
    scored = []
    try:
        token = secrets.get(config.SECRET_IG_TOKEN, env_fallback="IG_ACCESS_TOKEN")
        client = instagram.InstagramClient(token, config.IG_USER_ID)
        records = research.collect_insights(client)
        scored = research.score_records(records)
        log.info("インサイトを取得した投稿: %d 件（うち集計対象 %d 件）",
                 len(records), len(scored))
    except Exception as exc:
        # 調査が失敗しても候補の補充だけは続ける（投稿を止めないため）
        log.warning("インサイトの収集に失敗しました: %s", exc)

    # 2. 実測値を企画候補に還元する
    if scored:
        research.apply_to_pool(pool, scored)

    report = research.build_report(scored, pool)
    store.write_json(prompts.REPORT_PATH, report)
    log.info("調査レポートを更新しました（分析対象 %d 件）", report["posts_analyzed"])

    labels = {"live": "実写", "pixverse": "AI 生成"}
    for name, score, n in report.get("by_origin", []):
        log.info("  %s: スコア %.2f（%d件）", labels.get(name, name), score, n)
    _suggest_ratio(report.get("by_origin", []))

    for name, score, n in report["by_motion"][:3]:
        log.info("  伸びている動き: %s (スコア %.2f / %d件)", name, score, n)
    for name, score, n in report["by_hashtag"][:5]:
        log.info("  効いているタグ: %s (スコア %.2f / %d件)", name, score, n)

    # 3. 次の企画を補充する
    _refill(pool, research.performance_summary(report))
    prompts.save(pool)

    log.info("プール内の企画: %d 件（未検証 %d 件）",
             len(pool["prompts"]),
             len([p for p in pool["prompts"]
                  if p.get("trials", 0) < prompts.MIN_TRIALS]))
    return 0
