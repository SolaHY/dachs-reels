"""環境変数から設定を読み込む。

GitHub Actions では workflow の env / secrets から渡る。
ローカルでは .env を読み込んで実行する。
"""
from __future__ import annotations

import os


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"環境変数 {name} が設定されていません")
    return value


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


# --- GitHub（ストレージ兼実行基盤）--------------------------------------
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("GITHUB_REPO", "")
MEDIA_RELEASE_TAG = os.environ.get("MEDIA_RELEASE_TAG", "media")

# --- PixVerse（LIVE_RATIO=1.0 なら未使用）-------------------------------
PIXVERSE_MODEL = os.environ.get("PIXVERSE_MODEL", "v5")
PIXVERSE_QUALITY = os.environ.get("PIXVERSE_QUALITY", "720p")
PIXVERSE_DURATION = _env_int("PIXVERSE_DURATION", 5)
PIXVERSE_TIMEOUT_SEC = _env_int("PIXVERSE_TIMEOUT_SEC", 900)

# --- Instagram ---------------------------------------------------------
IG_USER_ID = os.environ.get("IG_USER_ID", "")
IG_API_VERSION = os.environ.get("IG_API_VERSION", "v23.0")
IG_PUBLISH_TIMEOUT_SEC = _env_int("IG_PUBLISH_TIMEOUT_SEC", 600)

# --- 投稿キュー --------------------------------------------------------
QUEUE_TARGET = _env_int("QUEUE_TARGET", 7)
GENERATE_MAX_PER_RUN = _env_int("GENERATE_MAX_PER_RUN", 3)

# --- 実写と AI 生成のミックス ------------------------------------------
# 投稿全体に占める実写の目標比率。1.0 なら AI 生成を完全に止める
LIVE_RATIO = float(os.environ.get("LIVE_RATIO", "1.0"))
# 比率を判定するときに見る直近の投稿数
MIX_HISTORY = _env_int("MIX_HISTORY", 10)
# 在庫がこの件数を下回ったら、比率を無視してでも AI 生成で埋める
MIN_RUNWAY = _env_int("MIN_RUNWAY", 2)

# --- 投稿内容 ----------------------------------------------------------
DEFAULT_PROMPT = os.environ.get(
    "DEFAULT_PROMPT",
    "gentle natural camera movement, cinematic lighting, subtle motion",
)
DEFAULT_CAPTION = os.environ.get("DEFAULT_CAPTION", "")

# --- プロンプト生成 / 市場調査 ------------------------------------------
# gemini | template（gemini 失敗時は自動で template にフォールバック）
PROMPT_PROVIDER = os.environ.get("PROMPT_PROVIDER", "gemini")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
RESEARCH_NEW_PROMPTS = _env_int("RESEARCH_NEW_PROMPTS", 6)
POOL_MIN_UNTESTED = _env_int("POOL_MIN_UNTESTED", 4)
PET_NAMES = os.environ.get("PET_NAMES", "うちの子")

# --- シークレット（GitHub Actions Secrets 由来の環境変数名）-------------
SECRET_PIXVERSE_KEY = "PIXVERSE_API_KEY"
SECRET_IG_TOKEN = "IG_ACCESS_TOKEN"
SECRET_GEMINI_KEY = "GEMINI_API_KEY"
