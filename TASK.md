# 残作業

最終更新: 2026-09-20

投稿が始まるまでに必要なのは **A-1 と A-2 だけ**です。所要 30〜40 分。
それ以外は「やっておくと楽になる」もので、後回しにできます。

---

## A. 投稿を始めるのに必須

### A-1. Instagram のトークンを取る 🔴

- [ ] **プロアカウントに切り替える**
  Instagram アプリ → 設定 → アカウントの種類とツール → プロアカウントに切り替える
  → **クリエイター** または **ビジネス** を選ぶ
  > Facebook ページとの連携は不要です

- [ ] **Meta 開発者アプリを作る** — <https://developers.facebook.com/apps/>
  作成 → ユースケース「その他」→ タイプ「ビジネス」
  → 製品に **Instagram** を追加 → 「**API setup with Instagram login**」
  → リダイレクト URI に `https://localhost/` を登録
  → **Instagram app ID** と **app secret** を控える

- [ ] **自分をテスターに追加する**（App Review 回避）
  App roles → Roles → **Instagram Testers** に自分のユーザー名を追加
  → Instagram アプリ側（設定 → ウェブサイトのアクセス許可）で承認
  > 自分のアカウントにだけ投稿するなら App Review もビジネス認証も不要

- [ ] **長期トークンを取得する**
  ```bash
  python scripts/get_ig_token.py
  ```
  → 60 日有効のトークンと `IG_USER_ID`（数字）が表示される

### A-2. GitHub に登録する 🔴

- [ ] **トークンを登録**（⚠️ **ターミナル**で実行すること）
  ```bash
  gh secret set IG_ACCESS_TOKEN --repo SolaHY/dachs-reels
  ```
  > Claude Code の `!` 経由だと対話入力が効かず、**空の値が登録されます**。
  > PowerShell か Git Bash を直接開いて実行してください。

- [ ] **ユーザー ID を登録**
  ```bash
  gh variable set IG_USER_ID --repo SolaHY/dachs-reels --body '17841400000000000'
  ```

- [ ] **疎通確認**
  ```bash
  cp .env.example .env    # 値を埋める
  set -a && source .env && set +a
  python -m app.main check
  ```
  Instagram の行が `[OK]` になれば投稿できます。

---

## B. 自動運転にするために必要

### B-1. 毎朝 8 時のトリガ（cron-job.org） 🟠

これを設定するまで、投稿は **GitHub の予備スケジュール（8:20 JST・数時間遅れることあり）**
か手動実行に頼ることになります。

- [ ] <https://cron-job.org/en/signup/> で登録（無料・カード不要・メールのみ）
- [ ] ジョブを 1 つ作成

  | 項目 | 値 |
  |---|---|
  | URL | `https://api.github.com/repos/SolaHY/dachs-reels/dispatches` |
  | 実行間隔 | 毎日 08:00 ／ タイムゾーン `Asia/Tokyo` |
  | メソッド | `POST` |
  | ヘッダー | `Authorization: Bearer <PAT>` |
  | ヘッダー | `Accept: application/vnd.github+json` |
  | 本文 | `{"event_type":"publish"}` |

  PAT は **Contents: Read and write** のファイングレード PAT
  （<https://github.com/settings/personal-access-tokens/new>）

- [ ] 作成後、cron-job.org の「Test run」で 1 回叩いて
      Actions に `publish` が現れることを確認

### B-2. トークンの自動延長 🟠

未設定だと **60 日ごとに A-1 をやり直す**ことになります。

- [ ] <https://github.com/settings/personal-access-tokens/new> で PAT を作る
      - Repository access: `SolaHY/dachs-reels` のみ
      - Permissions: **Secrets → Read and write**
- [ ] 登録（⚠️ ターミナルで）
  ```bash
  gh secret set GH_ADMIN_TOKEN --repo SolaHY/dachs-reels
  ```

---

## C. スマホのセットアップ 🟡

- [ ] PAT を作る（**Contents: RW** と **Actions: RW**）
- [ ] スマホで <https://solahy.github.io/dachs-reels/> を開く
- [ ] リポジトリ `SolaHY/dachs-reels` と PAT を入力 → 保存して接続
- [ ] **ホーム画面に追加**（iOS: 共有 → ホーム画面に追加）

---

## D. 運用開始

- [ ] 動画を 3〜7 本アップロードして在庫を作る
      管理画面 →「リリースを開いて添付する」→ Edit → 添付 → **Update release**
- [ ] 管理画面で取り込まれたことを確認（1〜2 分）
- [ ] キャプションを確認・必要なら編集
- [ ] 「今すぐ投稿」で 1 本試す
- [ ] Instagram で実際の投稿を確認

---

## E. あとで気が向いたら 🟢

- [ ] **AI 動画を混ぜる** — いまは実写のみ（`LIVE_RATIO=1.0`）
  1. <https://platform.pixverse.ai/> で **API 側の残高**を確認
     > アプリ／Web で買ったクレジットは Open API では使えない可能性が高いです
  2. ```bash
     gh secret set PIXVERSE_API_KEY --repo SolaHY/dachs-reels
     gh variable set LIVE_RATIO --repo SolaHY/dachs-reels --body '0.6'
     ```
  3. 写真を `images/pending/` に入れると AI 動画が作られます

- [ ] **投稿時刻の検証** — 2 か月分の成績が溜まったら、cron-job.org の時刻を
      夜（19〜21 時）に変えて比較する

- [ ] **`videos/rejected/` の掃除** — 弾かれた動画が溜まったら削除

- [ ] **`state/queue/posted/` の掃除** — 半年ほど経ったら古い記録を消す

---

## 現在の状態（2026-09-20 時点）

| 項目 | 状態 |
|---|---|
| リポジトリ・ワークフロー | ✅ 稼働中（ingest / publish / research / generate） |
| 管理画面（GitHub Pages） | ✅ 公開済み |
| 運用マニュアル | ✅ https://solahy.github.io/dachs-reels/manual.pdf |
| メディア置き場 | ✅ リリース `media` 作成済み・自動取り込み検証済み |
| 企画プール | ✅ 6 件（Gemini 生成・「レオとおはな」） |
| `GEMINI_API_KEY` | ✅ 登録済み（`gemini-3.6-flash`） |
| `IG_ACCESS_TOKEN` | ❌ **未登録** ← A-2 |
| `IG_USER_ID` | ❌ **未登録** ← A-2 |
| `GH_ADMIN_TOKEN` | ❌ 未登録 ← B-2 |
| `PIXVERSE_API_KEY` | ― 不要（実写のみ運用のため） |
| cron-job.org | ❌ 未設定 ← B-1 |
| 投稿待ちの在庫 | 0 件 ← D |

---

## 注意点（ハマりどころ）

- **`gh secret set` は必ずターミナルで。** Claude Code の `!` 経由では対話入力が
  効かず、空の値が登録されます（一度これで詰まりました）
- **コードを変更したら `bash setup.sh` を実行。** ワークフローの反映に加えて、
  添付時の自動取り込みが使う `media` タグを最新コミットに合わせます。
  タグが古いままだと、取り込みが古いワークフローで動いて失敗します
- **リポジトリは公開です。** 投稿前の動画も URL を知る人には見られます
- **`state/queue/inflight/` に項目が残ったら、必ず Instagram 側で投稿済みか
  目視確認してから動かす**（二重投稿を防ぐため自動では処理しません）
