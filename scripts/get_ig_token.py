"""Instagram の長期アクセストークンを取得する対話スクリプト（初回のみ手動で実行）。

前提: Meta 開発者アプリで「Instagram API setup with Instagram login」を構成し、
      Instagram app ID / Instagram app secret / リダイレクト URI を控えておくこと。

使い方:
    python scripts/get_ig_token.py

取得したトークンは Secret Manager に登録する（README 参照）。
以後の延長は publish ジョブが自動で行う。
"""
from __future__ import annotations

import sys
import urllib.parse

import requests

SCOPES = "instagram_business_basic,instagram_business_content_publish"


def ask(label: str) -> str:
    value = input(f"{label}: ").strip()
    if not value:
        print("入力が空です。中止します。")
        sys.exit(1)
    return value


def main() -> int:
    app_id = ask("Instagram app ID")
    app_secret = ask("Instagram app secret")
    redirect_uri = ask("リダイレクト URI（Meta アプリに登録したものと完全一致）")

    auth_url = "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
    })
    print("\n--- 手順 1: 下の URL をブラウザで開き、対象の Instagram "
          "プロアカウントで承認してください ---")
    print(auth_url)
    print("\n承認後、リダイレクト先 URL の ?code=... の値をコピーします。")
    print("（末尾に #_ が付いていたら取り除いてください）\n")

    code = ask("code").split("#")[0]

    print("\n--- 手順 2: 短期トークンに交換 ---")
    resp = requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    payload = resp.json()
    if "access_token" not in payload:
        print(f"失敗しました: {payload}")
        return 1
    short_token = payload["access_token"]
    user_id = payload.get("user_id")
    print(f"短期トークンを取得しました（user_id={user_id}）")

    print("\n--- 手順 3: 長期トークン（60 日）に交換 ---")
    resp = requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    payload = resp.json()
    if "access_token" not in payload:
        print(f"失敗しました: {payload}")
        return 1

    long_token = payload["access_token"]
    days = int(payload.get("expires_in", 0)) / 86400

    print("\n================ 取得結果 ================")
    print(f"IG_USER_ID      : {user_id}")
    print(f"有効期間        : 約 {days:.0f} 日")
    print(f"長期アクセストークン:\n{long_token}")
    print("==========================================")
    print("\n次のコマンドで Secret Manager に登録してください:")
    print(f'  printf "%s" "{long_token}" | gcloud secrets versions add '
          f'ig-access-token --data-file=-')
    return 0


if __name__ == "__main__":
    sys.exit(main())
