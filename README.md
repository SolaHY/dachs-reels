# scheduled-movie-uploader

ミニチュアダックス2匹の動画を、毎朝 8:00（JST）に Instagram へリール投稿する自動化。
実測データから「伸びる企画」を学習してキャプションとハッシュタグを自動生成し、
日々の操作はスマホだけで完結します。

**クレジットカード不要・完全無料。** GitHub だけで動きます。

| | |
|---|---|
| 管理画面（スマホ） | https://solahy.github.io/dachs-reels/ |
| 運用マニュアル（PDF） | https://solahy.github.io/dachs-reels/manual.pdf |
| 動画の投入口 | https://github.com/SolaHY/dachs-reels/releases/tag/media |
| 実行履歴 | https://github.com/SolaHY/dachs-reels/actions |

> スマホでの運用手順はマニュアル（PDF）にスクリーンショット付きでまとまっています。
> **投稿を始めるまでの残作業は [TASK.md](TASK.md) にあります。**

---

## 1. 構成

```
  スマホ
   ├─ 撮った動画を GitHub の Releases に添付  ──┐
   └─ 管理画面（GitHub Pages）で確認・編集 ──┐  │
                                             │  │
  ┌──────────────────────────────────────────▼──▼────────────┐
  │  GitHub リポジトリ（公開）                                │
  │    state/    投稿キュー・企画プール・成績レポート（JSON）  │
  │    Releases  動画と写真の実体（1ファイル2GBまで）          │
  │    Actions   ingest / publish / research / generate       │
  └───────────────────────┬───────────────────────────────────┘
                          │
   cron-job.org ──────────┘  毎朝 8:00 JST に repository_dispatch
   （無料・カード不要）        ※GitHub の cron は数時間遅れるため外部から叩く
                          │
                          ▼
                  Instagram Graph API
```

### 4 つのジョブ

| ジョブ | いつ走るか | 何をするか |
|---|---|---|
| **ingest** | リリース編集時／手動 | 添付された動画を検査し、企画を割り当ててキューへ |
| **publish** | 毎朝 8:00（外部トリガ） | キューから 1 件選んでリール投稿。トークンも自動延長 |
| **research** | 毎週月曜 2:00 | 実測値を集計し、次の企画を生成 |
| **generate** | 手動のみ | 写真から PixVerse で AI 動画を作る（`LIVE_RATIO=1.0` の間は何もしない） |

### なぜこの形なのか

- **GCS ではなく GitHub Releases** — Google Cloud は前払いを求められたため。
  Releases は公開リポジトリなら誰でも取得できる URL を持ち、Instagram はそこから動画を取りに来ます。
- **GitHub の cron を信用しない** — 2026 年は平均 4.5 時間遅延、実行されない日もあります。
  外部の無料 cron から `repository_dispatch` を叩き、GitHub の schedule は保険として残しています。
- **状態は JSON でリポジトリに置く** — 中身が全部見えるので、詰まったときに原因が追えます。

---

## 2. セットアップ

### 2-1. リポジトリを作る（PC で 1 回）

```bash
REPO_NAME=dachs-reels PET_NAMES="こむぎとあずき" bash setup.sh
```

リポジトリ作成・push・メディア用リリース作成・変数設定・Pages 有効化まで行います。

### 2-2. Instagram の準備（ここが所要時間の大半）

1. **プロアカウントに切り替える**（Instagram アプリ内・無料）
   設定 → アカウントの種類とツール → **クリエイター** または **ビジネス**
   → Facebook ページ連携は**不要**です
2. **Meta 開発者アプリを作る**
   <https://developers.facebook.com/apps/> → 作成 → ユースケース「その他」→ タイプ「ビジネス」
   → 製品に **Instagram** を追加 → 「**API setup with Instagram login**」
   → リダイレクト URI を登録（`https://localhost/` で可）
   → **Instagram app ID / app secret** を控える
3. **自分をテスターに追加する**（App Review 回避）
   App roles → Roles → **Instagram Testers** に自分のユーザー名を追加
   → Instagram アプリ側（設定 → ウェブサイトのアクセス許可）で承認
   > 自分のアカウントにだけ投稿するなら **App Review もビジネス認証も不要**です
4. **長期トークンを取得する**
   ```bash
   python scripts/get_ig_token.py
   ```
   60 日有効のトークンと `IG_USER_ID` が表示されます。

### 2-3. トークンと鍵を登録する

```bash
REPO=SolaHY/dachs-reels
gh secret set IG_ACCESS_TOKEN --repo $REPO          # 上で取得した長期トークン
gh variable set IG_USER_ID    --repo $REPO --body '178414...'
gh secret set GEMINI_API_KEY  --repo $REPO          # 任意（無いとテンプレ生成）
gh secret set GH_ADMIN_TOKEN  --repo $REPO          # トークン自動延長用
```

- **Gemini API キー**: <https://aistudio.google.com/apikey>（無料・カード不要）
- **GH_ADMIN_TOKEN**: <https://github.com/settings/personal-access-tokens/new> で
  このリポジトリのみ・**Secrets: Read and write** のファイングレード PAT。
  これが無いと 60 日ごとに手でトークンを取り直すことになります。

### 2-4. 毎朝 8 時のトリガを作る（cron-job.org）

<https://cron-job.org/en/signup/> で登録（無料・カード不要）し、ジョブを 1 つ作ります。

| 項目 | 値 |
|---|---|
| URL | `https://api.github.com/repos/SolaHY/dachs-reels/dispatches` |
| Schedule | 毎日 08:00 / タイムゾーン `Asia/Tokyo` |
| Method | `POST` |
| Header | `Authorization: Bearer <PAT>` |
| Header | `Accept: application/vnd.github+json` |
| Body | `{"event_type":"publish"}` |

PAT は **Contents: Read and write** 権限のファイングレード PAT
（`GH_ADMIN_TOKEN` を使い回しても構いません）。

### 2-5. 動作確認

```bash
cp .env.example .env    # 値を埋める
set -a && source .env && set +a
python -m app.main check
```

---

## 3. スマホでの日常運用

### 初回だけ

1. `https://<ユーザー名>.github.io/dachs-reels/` を開く
2. リポジトリ名と PAT（**Contents: RW / Actions: RW**）を入力
   → 端末内にのみ保存されます
3. **ホーム画面に追加**（iOS: 共有 → ホーム画面に追加）

### 毎日

| やること | 手順 |
|---|---|
| **動画を追加** | 管理画面の「リリースを開いて添付する」→ ファイルを選ぶ → 添付すると自動で取り込まれます |
| **キャプションを直す** | 投稿待ちの一覧でその場で編集 → 保存 |
| **順番を変える** | 「次に投稿」を押す |
| **今すぐ投稿** | 画面下のボタン |
| **成績を見る** | 「成績と企画」に実写 / AI 別、動き別、タグ別のスコア |

放っておいても毎朝 8:00 に 1 件投稿され、毎週月曜に企画が補充されます。

---

## 4. 学習ループ（市場調査）

ハッシュタグの人気投稿 API（`ig_hashtag_search`）は
[公式の比較表](https://developers.facebook.com/docs/instagram-platform/overview/)のとおり
**Instagram Login 方式では使えません**（Facebook Login + App Review + ビジネス認証が必要）。
代わりに**自分のアカウントでの実測**を調査の主軸にしています。汎用トレンドより、
自分のフォロワー層に最適化される分よく効きます。

1. **収集** — 投稿済みリールの `views / reach / likes / comments / saved / shares`
2. **採点** — `スコア = 0.6 × 反応率 + 0.4 × 到達数`（保存とシェアを 3 倍で重み付け）
3. **集計** — 実写 / AI 別、動き別、テーマ別、**ハッシュタグ別**の平均スコア
4. **生成** — 集計結果 + 季節・行事カレンダーを Gemini に渡して企画を 6 件生成
5. **選択** — ε-greedy（探索 25%）。未検証を試しつつ実績上位を活用

Gemini のキーが無ければテンプレート生成に自動フォールバックします。

---

## 5. 実写と AI のミックス

`LIVE_RATIO` が投稿に占める実写の目標比率です（既定 **1.0 = 実写のみ**）。

- publish は直近 10 件の実績を見て、実写が不足なら実写、足りていれば AI を選びます
- 狙った側の在庫が無ければもう一方で埋めるので、投稿は途切れません
- **`LIVE_RATIO=1.0` の間は PixVerse を一切呼びません**（キー未設定でも動きます）
- research が実写 / AI 別のスコアを比較し、差が出たら比率の見直しを提案します（自動では変えません）

AI 動画を混ぜたくなったら:

```bash
gh secret set PIXVERSE_API_KEY --repo $REPO
gh variable set LIVE_RATIO --repo $REPO --body 0.6
```

---

## 6. 動画の検査

MP4 / MOV のボックス構造を直接読み、取り込み時に要件と照合します。

| 判定 | 内容 |
|---|---|
| ✕ 拒否 | 3 秒未満 / 15 分超 / 1GB 超 / アスペクト比が範囲外 → `videos/rejected/` へ退避 |
| ⚠ 警告 | 横長（上下に余白が入り表示が小さくなる） |
| ⚠ 情報 | moov が後方（スマホ動画はほぼ全てこう。通常は問題なし） |

iPhone の縦撮り（本体 1920x1080 + 90 度の回転行列）も表示サイズ 1080x1920 として
正しく判定します。ffmpeg 生成の実ファイル 4 本で検証済み（`tests/test_video.py`）。

---

## 7. 費用

| サービス | 使用量 | 上限 | 判定 |
|---|---|---|---|
| GitHub Actions | 1 日 2〜3 実行 | 公開リポジトリは無制限 | 無料 |
| GitHub Releases | 動画 1 本 数十 MB | 1 ファイル 2GB | 無料 |
| GitHub Pages | 管理画面 | 1GB / 月 100GB 転送 | 無料 |
| cron-job.org | 1 日 1 回 | 無料枠 | 無料 |
| Gemini API | 週 1 回・数千トークン | 3.6 Flash 無料枠 | 無料 |
| PixVerse | `LIVE_RATIO=1.0` なら 0 | — | 無料 |

**クレジットカードの登録は一切不要です。**

---

## 8. トラブル対応

```bash
python -m app.main status    # 在庫と実写率
python -m app.main check     # 疎通確認
gh run list --repo $REPO     # 実行履歴
```

- **投稿に失敗した** → `state/queue/failed/<id>.json` の `error` に理由。
  直したら `state/queue/ready/` に移す
- **`state/queue/inflight/` に残っている** → **Instagram 側で投稿済みか必ず目視確認**
  してから移動（二重投稿を防ぐため自動では動かしません）
- **動画が取り込まれない** → 管理画面の「いま取り込む」を押す。
  それでもだめなら Actions の ingest のログを見る
- **8 時に投稿されない** → cron-job.org の実行履歴を確認。
  GitHub の schedule（8:20 JST）は保険なので遅れます
- **トークンが切れた** → `python scripts/get_ig_token.py` で取り直して
  `gh secret set IG_ACCESS_TOKEN`

---

## 9. 制約と注意点

- **リポジトリは公開です。** 投稿前の動画・写真も URL を知る人には見られます。
  Instagram が動画を取得するために必要な構成です。
- **動画は縦向き・3 秒以上**を推奨。横長は警告が出ます（投稿自体は可能）。
- **Instagram の投稿上限は 24 時間あたり 50 件。** 1 日 1 件なので問題ありません。
- **トークンは 60 日で失効。** `GH_ADMIN_TOKEN` があれば publish が自動延長します。
- **Gemini の無料枠は入力が学習に使われます。** 渡しているのは企画文と成績数値のみで、
  写真・動画・個人情報は送っていません。
- **Instagram の AI ラベル方針**（2026-08-31〜）は「AI で生成された**人物**」を掲げる
  プロフィールが対象です。実在するペットを撮った動画は該当しません。
- 管理画面の PAT は端末の localStorage にのみ保存されます。共有端末では使わないでください。

---

## 10. ローカル開発

```bash
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env && set -a && source .env && set +a

python -m app.main check      # 疎通確認
python -m app.main status     # 在庫
python -m app.main ingest     # 添付の取り込み
python -m app.main research   # 市場調査 + 企画生成
python -m app.main publish    # 投稿
python -m app.main generate   # AI 動画生成

python -m tests.test_flow     # 取り込み・投稿・ミックス・在庫（ネットワーク不要）
python -m tests.test_video    # 動画解析（ffmpeg があれば実ファイルで検証）
```

## 参考

- [Instagram Platform — Overview（Login 方式の比較表）](https://developers.facebook.com/docs/instagram-platform/overview/)
- [Instagram Platform — Content Publishing](https://developers.facebook.com/docs/instagram-platform/content-publishing/)
- [PixVerse Platform Docs](https://docs.platform.pixverse.ai/)
- [Gemini API pricing / 無料枠](https://ai.google.dev/gemini-api/docs/pricing)
