"""プロンプトプール: 生成候補の保管・選択・スコアリング。

プールは research/prompt_pool.json に置く。各エントリは
「どんな動きの動画を作るか（prompt）」と「どう投稿するか（caption/hashtags）」、
そして実測成績（stats/score）を持つ。

選択は ε-greedy。未使用の候補を優先して試し（探索）、
実績が出たものは score 順に使う（活用）。これで
「伸びる演出」が自動的に生き残る。
"""
from __future__ import annotations

import datetime as dt
import logging
import random
from typing import Any

from . import store

log = logging.getLogger(__name__)

POOL_PATH = "research/prompt_pool.json"
REPORT_PATH = "research/latest_report.json"

# 探索率。この確率で「実績を無視して未知の候補を試す」
EPSILON = 0.25
# プールに保つ候補数の上限（これを超えたら score 下位を捨てる）
POOL_MAX = 40
# 実績がこの回数に満たない候補は「未検証」とみなし、探索枠で優先する
MIN_TRIALS = 2


def empty_pool() -> dict[str, Any]:
    return {"updated_at": None, "prompts": []}


def load() -> dict[str, Any]:
    if store.exists(POOL_PATH):
        return store.read_json(POOL_PATH)
    return empty_pool()


def save(pool: dict[str, Any]) -> None:
    pool["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    store.write_json(POOL_PATH, pool)


def new_entry(prompt: str, caption: str, hashtags: list[str],
              *, motion: str = "", theme: str = "", source: str = "") -> dict[str, Any]:
    return {
        "id": f"p-{dt.datetime.now(dt.timezone.utc):%Y%m%d}-{random.randbytes(3).hex()}",
        "prompt": prompt.strip(),
        "caption": caption.strip(),
        "hashtags": hashtags,
        "motion": motion,
        "theme": theme,
        "source": source,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "uses": 0,
        "trials": 0,
        "score": None,
        "stats": {},
    }


def pick(pool: dict[str, Any], *, exclude: set[str] | None = None) -> dict[str, Any] | None:
    """次に使う候補を 1 件選ぶ。"""
    exclude = exclude or set()
    candidates = [p for p in pool["prompts"] if p["id"] not in exclude]
    if not candidates:
        return None

    untested = [p for p in candidates if p.get("trials", 0) < MIN_TRIALS]
    scored = [p for p in candidates if p.get("score") is not None]

    # 未検証があるうちは優先して試す。実績持ちがまだ無い間も同様。
    if untested and (random.random() < EPSILON or not scored):
        chosen = min(untested, key=lambda p: (p.get("uses", 0), p["created_at"]))
        log.info("探索: 未検証の候補を使用 %s", chosen["id"])
        return chosen

    if scored:
        # 上位 3 件から重み付きで選ぶ（1 つに固定されて飽きられるのを避ける）
        top = sorted(scored, key=lambda p: p["score"], reverse=True)[:3]
        chosen = random.choices(top, weights=[max(p["score"], 0.01) for p in top])[0]
        log.info("活用: 実績上位の候補を使用 %s (score=%.3f)",
                 chosen["id"], chosen["score"])
        return chosen

    chosen = min(candidates, key=lambda p: (p.get("uses", 0), p["created_at"]))
    log.info("候補を使用 %s", chosen["id"])
    return chosen


def mark_used(pool: dict[str, Any], prompt_id: str) -> None:
    for p in pool["prompts"]:
        if p["id"] == prompt_id:
            p["uses"] = p.get("uses", 0) + 1
            return


def merge(pool: dict[str, Any], entries: list[dict[str, Any]]) -> int:
    """新候補を追加する。文面が重複するものは弾く。"""
    existing = {p["prompt"].lower() for p in pool["prompts"]}
    added = 0
    for entry in entries:
        if entry["prompt"].lower() in existing:
            continue
        pool["prompts"].append(entry)
        existing.add(entry["prompt"].lower())
        added += 1
    prune(pool)
    return added


def prune(pool: dict[str, Any]) -> None:
    """上限を超えたら、実績のある下位から捨てる（未検証は残す）。"""
    if len(pool["prompts"]) <= POOL_MAX:
        return
    untested = [p for p in pool["prompts"] if p.get("score") is None]
    scored = sorted(
        (p for p in pool["prompts"] if p.get("score") is not None),
        key=lambda p: p["score"], reverse=True,
    )
    keep = untested + scored
    dropped = keep[POOL_MAX:]
    if dropped:
        log.info("成績下位 %d 件をプールから除外", len(dropped))
    pool["prompts"] = keep[:POOL_MAX]


def get(pool: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
    return next((p for p in pool["prompts"] if p["id"] == prompt_id), None)


# ---------------------------------------------------------------------
# 候補の生成
# ---------------------------------------------------------------------

def season_context(today: dt.date | None = None) -> str:
    """日本の季節・行事の文脈。LLM に渡す材料であり、テンプレ版でも使う。"""
    today = today or dt.date.today()
    m, d = today.month, today.day
    seasons = {
        (3, 4, 5): "春（桜、新緑、花見、入学）",
        (6, 7, 8): "夏（梅雨、海、花火、夏バテ対策）",
        (9, 10, 11): "秋（紅葉、味覚の秋、散歩日和、ハロウィン）",
        (12, 1, 2): "冬（雪、こたつ、クリスマス、正月、バレンタイン）",
    }
    season = next(v for k, v in seasons.items() if m in k)
    events = []
    for (em, ed), name in [
        ((10, 31), "ハロウィン"), ((12, 25), "クリスマス"), ((1, 1), "お正月"),
        ((2, 14), "バレンタイン"), ((3, 3), "ひな祭り"), ((5, 5), "こどもの日"),
        ((11, 1), "犬の日(11/1)"), ((9, 20), "動物愛護週間"),
    ]:
        target = dt.date(today.year + (1 if em < m else 0), em, ed)
        delta = (target - today).days
        if 0 <= delta <= 21:
            events.append(f"{name}まで{delta}日")
    return season + ("／直近の行事: " + "、".join(events) if events else "")


# テンプレート版の構成要素（LLM を使わない場合、またはフォールバック）
MOTIONS = [
    ("tail_wag", "tail wagging happily, ears bouncing slightly"),
    ("head_tilt", "curious head tilt, ears perking up, blinking"),
    ("trot", "trotting toward the camera with short legs, gentle bounce"),
    ("sniff", "sniffing the ground, nose twitching, whiskers moving"),
    ("yawn", "slow yawn then settling down, eyes half closing"),
    ("look_up", "slowly looking up at the camera, soft eye contact"),
    ("snuggle", "the two dogs leaning into each other, slow breathing"),
    ("zoomies", "a short burst of playful running, fur fluttering"),
]

CAMERAS = [
    "slow dolly in", "gentle handheld sway", "slight low-angle push in",
    "static shot with shallow depth of field", "slow pan following the dogs",
]

LIGHTS = [
    "warm morning light", "soft window light", "golden hour glow",
    "cozy indoor lamp light", "bright overcast daylight",
]

BASE_HASHTAGS = [
    "#ミニチュアダックスフンド", "#ダックスフンド", "#犬のいる暮らし",
    "#愛犬", "#多頭飼い", "#dachshund", "#miniaturedachshund",
    "#いぬすたぐらむ", "#犬好きさんと繋がりたい", "#dogsofinstagram",
]

# 呼び名は実名（例「レオとおはな」）が入るので、「〜たち」を付け足さない。
# 「レオとおはなたち」のような不自然な日本語になるため。
CAPTION_PATTERNS = [
    "おはようございます☀️\n今日の{name}です",
    "朝いちばんの{name}🐾\n今日もいい一日を",
    "今朝の一枚から🎬\n{name}の朝",
    "おはよう🌿\n今日も元気な{name}",
    "{name}、今日もよろしくね🐾",
]


def template_entries(count: int, pet_names: str = "うちの子") -> list[dict[str, Any]]:
    """LLM を使わずに候補を組み立てる。完全無料・完全に予測可能。"""
    entries = []
    combos = [(m, c, l) for m in MOTIONS for c in CAMERAS for l in LIGHTS]
    random.shuffle(combos)
    for (motion_key, motion_desc), camera, light in combos[:count]:
        prompt = (
            f"two miniature dachshunds, {motion_desc}, {camera}, {light}, "
            "natural fur texture, shallow depth of field, photorealistic, "
            "no morphing, no extra limbs, stable anatomy"
        )
        caption = random.choice(CAPTION_PATTERNS).format(name=pet_names)
        entries.append(new_entry(
            prompt, caption, random.sample(BASE_HASHTAGS, 8),
            motion=motion_key, theme="daily", source="template",
        ))
    return entries


GEMINI_INSTRUCTION = """\
あなたは Instagram リールの企画者です。日本語話者の飼い主が運用する
「ミニチュアダックスフンド2匹」のアカウント向けに、静止画1枚から
AI 動画生成（PixVerse image-to-video, {duration}秒, 縦9:16）で作る
リールの企画を {count} 件つくってください。

## 出力する各項目
- prompt: 動画生成用の英語プロンプト。**カメラワーク・犬の動き・光**を
  具体的に書く。静止画の被写体を大きく変える指示（衣装追加・頭数変更・
  場所の移動）は禁止。破綻防止のため "no morphing, stable anatomy" を含める。
- caption: 日本語のキャプション本文（絵文字可、2〜4行、ハッシュタグは含めない）
  **このキャプションは実写動画にも流用します。** 「AI で動かした」前提の表現は避け、
  実際に撮った動画に付けても自然に読める文にしてください。
- hashtags: 日本語と英語を混ぜた 8〜12 個のハッシュタグ（# 付き）
- motion: 動きの種類を表す英小文字スネークケースの短いキー（例 tail_wag）
- theme: 企画のテーマを表す英小文字スネークケースの短いキー（例 autumn_walk）

## 今の文脈
- 2匹の呼び名: {pet_names}（キャプションではこの名前で呼ぶこと。
  実名なので「〜たち」を付け足さない）
- 季節・行事: {season}
- 現在のフォロワー規模: 小規模個人アカウント（保存とシェアを伸ばしたい）

## これまでの実測データ
{performance}

## すでにプールにある企画（重複させないこと）
{existing}

実測データで伸びている motion / theme の傾向は踏襲しつつ、
半分程度は新しい切り口を試してください。
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "motion": {"type": "string"},
                    "theme": {"type": "string"},
                },
                "required": ["prompt", "caption", "hashtags", "motion", "theme"],
            },
        }
    },
    "required": ["ideas"],
}


def gemini_entries(count: int, *, api_key: str, model: str, duration: int,
                   performance: str, existing: list[str],
                   pet_names: str = "うちの子") -> list[dict[str, Any]]:
    """Gemini で候補を生成する。失敗時は例外を投げる（呼び出し側でフォールバック）。"""
    from google import genai
    from google.genai import types

    instruction = GEMINI_INSTRUCTION.format(
        count=count,
        duration=duration,
        pet_names=pet_names,
        season=season_context(),
        performance=performance or "（まだ実測データがありません。定番から始めてください）",
        existing="\n".join(f"- {p}" for p in existing[:20]) or "（なし）",
    )

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=instruction,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            temperature=1.0,
        ),
    )

    import json
    ideas = json.loads(response.text)["ideas"]
    return [
        new_entry(
            i["prompt"], i["caption"], list(i["hashtags"]),
            motion=i.get("motion", ""), theme=i.get("theme", ""), source="gemini",
        )
        for i in ideas
    ]
