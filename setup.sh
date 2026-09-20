#!/usr/bin/env bash
# GitHub 側の初期構築。冪等なので何度実行してもよい。
#   REPO_NAME=dachs-reels bash setup.sh
#
# 前提: gh CLI が認証済みであること（gh auth status で確認）
set -euo pipefail

REPO_NAME="${REPO_NAME:-dachs-reels}"
PET_NAMES="${PET_NAMES:-うちの子}"
LIVE_RATIO="${LIVE_RATIO:-1.0}"
QUEUE_TARGET="${QUEUE_TARGET:-7}"
PROMPT_PROVIDER="${PROMPT_PROVIDER:-gemini}"
MEDIA_TAG="media"

OWNER="$(gh api user --jq .login)"
REPO="${OWNER}/${REPO_NAME}"
echo "==> 対象リポジトリ: ${REPO}"

# --- 1. リポジトリ -----------------------------------------------------
if gh repo view "${REPO}" >/dev/null 2>&1; then
  echo "    既に存在します"
else
  echo "==> リポジトリを作成（public）"
  gh repo create "${REPO}" --public \
    --description "ミニチュアダックス2匹のリールを毎朝自動投稿する" >/dev/null
fi

# --- 2. コードを push --------------------------------------------------
if [ ! -d .git ]; then
  git init -b main >/dev/null
fi
git remote get-url origin >/dev/null 2>&1 || \
  git remote add origin "https://github.com/${REPO}.git"

git add -A
git diff --cached --quiet || git commit -q -F - <<'MSG'
毎朝の自動投稿パイプライン

ミニチュアダックス2匹の動画を毎朝 8:00 JST に Instagram へリール投稿する。
GitHub Releases をメディア置き場、リポジトリ内の JSON を状態として使い、
クレジットカード不要で完結させる構成。

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
echo "==> push"
git push -u origin main

# --- 3. メディア置き場のリリース ---------------------------------------
if gh release view "${MEDIA_TAG}" --repo "${REPO}" >/dev/null 2>&1; then
  echo "==> リリース ${MEDIA_TAG} は作成済み"
else
  echo "==> リリース ${MEDIA_TAG} を作成"
  gh release create "${MEDIA_TAG}" --repo "${REPO}" --latest=false \
    --title "media" \
    --notes "動画と写真の置き場。ここにファイルを添付すると自動で取り込まれます。" >/dev/null
fi

# --- 4. 設定値（公開されても困らないもの）------------------------------
echo "==> Variables"
for kv in "PET_NAMES=${PET_NAMES}" "LIVE_RATIO=${LIVE_RATIO}" \
          "QUEUE_TARGET=${QUEUE_TARGET}" "PROMPT_PROVIDER=${PROMPT_PROVIDER}"; do
  gh variable set "${kv%%=*}" --repo "${REPO}" --body "${kv#*=}" >/dev/null
  echo "    ${kv%%=*} = ${kv#*=}"
done

# --- 5. GitHub Pages（スマホ管理画面）----------------------------------
echo "==> GitHub Pages"
if gh api "repos/${REPO}/pages" >/dev/null 2>&1; then
  echo "    設定済み"
else
  gh api -X POST "repos/${REPO}/pages" \
    -f "source[branch]=main" -f "source[path]=/docs" >/dev/null && \
    echo "    有効化しました" || \
    echo "    有効化に失敗（リポジトリの Settings → Pages で /docs を指定してください）"
fi

echo
echo "===================================================================="
echo " リポジトリ   : https://github.com/${REPO}"
echo " メディア置き場: https://github.com/${REPO}/releases/tag/${MEDIA_TAG}"
echo " 管理画面     : https://${OWNER}.github.io/${REPO_NAME}/"
echo "                （Pages の反映に数分かかります）"
echo "===================================================================="
echo
echo "次にシークレットを登録してください（値は画面に表示されません）:"
echo "  gh secret set IG_ACCESS_TOKEN --repo ${REPO}"
echo "  gh secret set GH_ADMIN_TOKEN  --repo ${REPO}   # Secrets:RW の PAT"
echo "  gh secret set GEMINI_API_KEY  --repo ${REPO}   # 任意"
echo "  gh variable set IG_USER_ID --repo ${REPO} --body '<数字のID>'"
