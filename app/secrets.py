"""シークレットの読み書き。

読み取りは環境変数（GitHub Actions Secrets がそのまま env に入る）。
書き込みは Instagram の長期トークン延長のためだけに使う。GitHub Actions の
Secrets API は値を libsodium の sealed box で暗号化して送る必要がある。

書き込みには Secrets: write 権限を持つ PAT（環境変数 GH_ADMIN_TOKEN）が要る。
既定の GITHUB_TOKEN では権限が足りない。
"""
from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"


class SecretError(RuntimeError):
    pass


def get(name: str, *, env_fallback: str | None = None) -> str:
    """環境変数から取得する。env_fallback は旧インターフェース互換のため。"""
    for key in (env_fallback, name):
        if key and os.environ.get(key):
            return os.environ[key].strip()
    raise SecretError(f"シークレット {name} が設定されていません")


def has(name: str, *, env_fallback: str | None = None) -> bool:
    try:
        get(name, env_fallback=env_fallback)
        return True
    except SecretError:
        return False


def _admin_token() -> str | None:
    return os.environ.get("GH_ADMIN_TOKEN") or None


def _repo() -> str:
    value = (os.environ.get("GITHUB_REPOSITORY")
             or os.environ.get("GITHUB_REPO", ""))
    if "/" not in value:
        raise SecretError("GITHUB_REPOSITORY が owner/repo の形式ではありません")
    return value


def update(name: str, value: str) -> bool:
    """リポジトリの Actions Secret を更新する。

    Returns: 更新できたか（権限不足などで失敗した場合は False）
    """
    token = _admin_token()
    if not token:
        log.warning(
            "GH_ADMIN_TOKEN が無いため %s を更新できません。"
            "Secrets: write 権限の PAT を設定すると自動更新できます", name)
        return False

    try:
        from nacl import encoding, public
    except ImportError:
        log.warning("PyNaCl が入っていないため %s を更新できません", name)
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    repo = _repo()

    resp = requests.get(f"{API}/repos/{repo}/actions/secrets/public-key",
                        headers=headers, timeout=30)
    if resp.status_code >= 300:
        log.warning("公開鍵を取得できません（%s）: %s",
                    resp.status_code, resp.text[:200])
        return False
    key = resp.json()

    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder)
    ).encrypt(value.encode())

    resp = requests.put(
        f"{API}/repos/{repo}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": base64.b64encode(sealed).decode(),
              "key_id": key["key_id"]},
        timeout=30,
    )
    if resp.status_code >= 300:
        log.warning("シークレット %s の更新に失敗（%s）: %s",
                    name, resp.status_code, resp.text[:200])
        return False
    log.info("シークレット %s を更新しました", name)
    return True
