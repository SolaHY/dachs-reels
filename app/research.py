"""市場調査: 自分の投稿の実測値を集めて、企画の良し悪しを数値化する。

Instagram のハッシュタグ人気投稿 API（ig_hashtag_search）は
Facebook Login 方式 + App Review + ビジネス認証が必要で個人では実用的でないため、
「自分のアカウントでの実測」を調査の主軸に据えている。

  投稿 → インサイト収集 → プロンプト/演出/ハッシュタグ別に集計 → 次の企画に反映

外部ソースを足したくなったら collect_external() を実装すれば
レポートにそのまま合流する。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import statistics
from typing import Any

from . import store, prompts, queue

log = logging.getLogger(__name__)

# 投稿から 48 時間はまだ数字が動くので、暫定値として扱う
SETTLE_HOURS = 48
# これより古い投稿は再取得しない（数字がほぼ固まるため）
REFRESH_WINDOW_DAYS = 30

# エンゲージメントの重み。保存とシェアはリールの拡散に効くので厚めに置く
WEIGHTS = {"likes": 1.0, "comments": 2.0, "saved": 3.0, "shares": 3.0}


def _age_hours(iso: str) -> float:
    published = dt.datetime.fromisoformat(iso)
    return (dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 3600


def collect_insights(client) -> list[dict[str, Any]]:
    """投稿済み項目のインサイトを取得し、queue/posted/ の JSON を更新する。

    Returns: 集計対象になったレコード一覧
    """
    records: list[dict[str, Any]] = []
    for name, item in store.iter_json(queue.POSTED):
        media_id = item.get("ig_media_id")
        published_at = item.get("published_at")
        if not media_id or not published_at:
            continue

        age = _age_hours(published_at)
        needs_fetch = (
            "insights" not in item
            or age < REFRESH_WINDOW_DAYS * 24
        )
        if needs_fetch:
            stats = client.media_insights(media_id)
            if stats:
                item["insights"] = stats
                item["insights_updated_at"] = queue.now_iso()
                item["settled"] = age >= SETTLE_HOURS
                store.write_json(name, item)
                log.info("インサイト取得 %s: %s", item["id"], stats)

        if item.get("insights"):
            records.append(item)
    return records


def engagement_rate(stats: dict[str, Any]) -> float:
    """リーチあたりの重み付き反応率。"""
    base = stats.get("reach") or stats.get("views") or 0
    if not base:
        return 0.0
    weighted = sum(WEIGHTS[k] * float(stats.get(k, 0) or 0) for k in WEIGHTS)
    return weighted / base


def _normalize(values: list[float]) -> list[float]:
    """0〜1 に正規化。全部同じ値なら一律 0.5。"""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """各投稿に score を付ける。反応率 6 割 + 到達 4 割。"""
    settled = [r for r in records if r.get("settled")]
    target = settled or records
    if not target:
        return []

    rates = [engagement_rate(r["insights"]) for r in target]
    reaches = [float(r["insights"].get("reach")
                     or r["insights"].get("views") or 0) for r in target]
    n_rates, n_reaches = _normalize(rates), _normalize(reaches)

    for r, rate, nr, nre in zip(target, rates, n_rates, n_reaches):
        r["engagement_rate"] = round(rate, 4)
        r["score"] = round(0.6 * nr + 0.4 * nre, 4)
    return target


def apply_to_pool(pool: dict[str, Any], scored: list[dict[str, Any]]) -> None:
    """投稿の score をプロンプト候補に還元する。"""
    by_prompt: dict[str, list[float]] = {}
    stats_by_prompt: dict[str, dict[str, float]] = {}
    for r in scored:
        pid = r.get("prompt_id")
        if not pid:
            continue
        by_prompt.setdefault(pid, []).append(r["score"])
        agg = stats_by_prompt.setdefault(pid, {})
        for k, v in r["insights"].items():
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0) + v

    for entry in pool["prompts"]:
        scores = by_prompt.get(entry["id"])
        if not scores:
            continue
        entry["trials"] = len(scores)
        entry["score"] = round(statistics.fmean(scores), 4)
        entry["stats"] = {k: round(v / len(scores), 1)
                          for k, v in stats_by_prompt[entry["id"]].items()}


def _group_scores(scored: list[dict[str, Any]], key: str) -> list[tuple[str, float, int]]:
    buckets: dict[str, list[float]] = {}
    for r in scored:
        value = r.get(key)
        if value:
            buckets.setdefault(str(value), []).append(r["score"])
    rows = [(k, statistics.fmean(v), len(v)) for k, v in buckets.items()]
    return sorted(rows, key=lambda t: t[1], reverse=True)


def _hashtag_scores(scored: list[dict[str, Any]]) -> list[tuple[str, float, int]]:
    """ハッシュタグ別の平均スコア。外部 API の代わりになる自前のタグ調査。"""
    buckets: dict[str, list[float]] = {}
    for r in scored:
        tags = r.get("hashtags") or re.findall(r"#\S+", r.get("caption", ""))
        for tag in set(tags):
            buckets.setdefault(tag, []).append(r["score"])
    rows = [(k, statistics.fmean(v), len(v)) for k, v in buckets.items() if len(v) >= 2]
    return sorted(rows, key=lambda t: t[1], reverse=True)


def build_report(scored: list[dict[str, Any]], pool: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": queue.now_iso(),
        "season": prompts.season_context(),
        "posts_analyzed": len(scored),
        "by_origin": _group_scores(scored, "origin"),
        "by_motion": _group_scores(scored, "motion"),
        "by_theme": _group_scores(scored, "theme"),
        "by_hashtag": _hashtag_scores(scored)[:20],
        "top_posts": [
            {
                "id": r["id"],
                "score": r["score"],
                "prompt": (r.get("prompt") or "")[:120],
                "permalink": r.get("ig_permalink", ""),
                "insights": r["insights"],
            }
            for r in sorted(scored, key=lambda r: r["score"], reverse=True)[:5]
        ],
        "pool_size": len(pool["prompts"]),
    }


def performance_summary(report: dict[str, Any]) -> str:
    """LLM に渡す、実測データの要約テキスト。"""
    if not report["posts_analyzed"]:
        return ""

    def fmt(rows, label):
        if not rows:
            return ""
        body = "\n".join(
            f"  - {name}: スコア {score:.2f}（{n}件）" for name, score, n in rows[:6]
        )
        return f"{label}:\n{body}\n"

    parts = [f"分析対象の投稿数: {report['posts_analyzed']}件\n"]
    parts.append(fmt(report.get("by_origin", []), "実写 / AI 生成 別の成績"))
    parts.append(fmt(report["by_motion"], "動きの種類別の成績"))
    parts.append(fmt(report["by_theme"], "テーマ別の成績"))
    parts.append(fmt(report["by_hashtag"], "ハッシュタグ別の成績"))
    if report["top_posts"]:
        best = report["top_posts"][0]
        parts.append(f"最も伸びた企画のプロンプト: {best['prompt']}")
    return "\n".join(p for p in parts if p)


def collect_external() -> dict[str, Any]:
    """外部トレンドの取り込み口（未実装）。

    ig_hashtag_search を使う場合はここに実装する。ただし
    Facebook Login 方式 + Instagram Public Content Access の審査が前提。
    """
    return {}
