"""GitHub をストレージとして使うバックエンド。

クレジットカード不要で完結させるため、クラウドストレージの代わりに
GitHub の 2 つの機能を使い分ける。

  状態（JSON）  → リポジトリ内の state/ 配下（Contents API）
  メディア（動画・写真）→ リリース `media` の添付ファイル（Releases API）

リリース添付は 1 ファイル 2GB までで、公開リポジトリなら
`https://github.com/<owner>/<repo>/releases/download/media/<name>` で
誰でも取得できる。Instagram はこの URL から動画を取りに来る。

パスの対応:
    videos/abc.mp4            → 添付ファイル名 videos__abc.mp4
    queue/ready/abc.json      → リポジトリの state/queue/ready/abc.json
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
STATE_DIR = "state"
MEDIA_TAG = os.environ.get("MEDIA_RELEASE_TAG", "media")
# これらで始まるパスは添付ファイル、それ以外はリポジトリ内の JSON として扱う
MEDIA_PREFIXES = ("videos/", "images/")
SEP = "__"

_session: requests.Session | None = None
_release: dict[str, Any] | None = None


class StoreError(RuntimeError):
    pass


# --- 接続 ---------------------------------------------------------------

def repo() -> str:
    # GITHUB_REPOSITORY は Actions が自動で入れる。ローカルでは GITHUB_REPO を使う。
    value = (os.environ.get("GITHUB_REPOSITORY")
             or os.environ.get("GITHUB_REPO", ""))
    if "/" not in value:
        raise StoreError(
            "リポジトリを特定できません。GITHUB_REPO を owner/repo の形式で"
            "設定してください（GitHub Actions では自動で入ります）")
    return value


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
    if not value:
        raise StoreError("環境変数 GITHUB_TOKEN が設定されていません")
    return value


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    return _session


def _call(method: str, url: str, *, ok404: bool = False, **kwargs):
    resp = session().request(method, url, timeout=120, **kwargs)
    if resp.status_code == 404 and ok404:
        return None
    if resp.status_code >= 300:
        raise StoreError(f"GitHub API {method} {url} → {resp.status_code}: "
                         f"{resp.text[:300]}")
    return resp


# --- メディア（リリース添付） -------------------------------------------

def _is_media(name: str) -> bool:
    return name.startswith(MEDIA_PREFIXES)


def _asset_name(path: str) -> str:
    return path.replace("/", SEP)


def _asset_path(asset_name: str) -> str:
    return asset_name.replace(SEP, "/")


def release(refresh: bool = False) -> dict[str, Any]:
    """メディア置き場のリリースを取得する。無ければ作る。"""
    global _release
    if _release is not None and not refresh:
        return _release

    resp = _call("GET", f"{API}/repos/{repo()}/releases/tags/{MEDIA_TAG}", ok404=True)
    if resp is None:
        log.info("メディア用のリリース %s を作成します", MEDIA_TAG)
        resp = _call("POST", f"{API}/repos/{repo()}/releases", json={
            "tag_name": MEDIA_TAG,
            "name": "media",
            "body": "動画と写真の置き場。手動で添付すると取り込まれます。",
            "make_latest": "false",
        })
    _release = resp.json()
    return _release


def _assets() -> list[dict[str, Any]]:
    return release().get("assets", [])


def _find_asset(path: str) -> dict[str, Any] | None:
    target = _asset_name(path)
    return next((a for a in _assets() if a["name"] == target), None)


def unmanaged_assets() -> list[dict[str, Any]]:
    """こちらの命名規則に従っていない添付＝手動で上げられた新規ファイル。"""
    return [a for a in release(refresh=True).get("assets", [])
            if not a["name"].startswith(tuple(p.replace("/", SEP)
                                               for p in MEDIA_PREFIXES))]


def public_url(path: str) -> str:
    asset = _find_asset(path)
    if asset:
        return asset["browser_download_url"]
    return (f"https://github.com/{repo()}/releases/download/"
            f"{MEDIA_TAG}/{_asset_name(path)}")


def rename_asset(asset_id: int, new_path: str) -> None:
    _call("PATCH", f"{API}/repos/{repo()}/releases/assets/{asset_id}",
          json={"name": _asset_name(new_path)})
    release(refresh=True)


# --- 状態（リポジトリ内の JSON）-----------------------------------------

def _state_path(name: str) -> str:
    return f"{STATE_DIR}/{name}"


def _contents_url(name: str) -> str:
    return f"{API}/repos/{repo()}/contents/{_state_path(name)}"


def _get_content(name: str) -> dict[str, Any] | None:
    resp = _call("GET", _contents_url(name), ok404=True)
    return resp.json() if resp else None


# --- 公開インターフェース（gcs.py と同じ形）-----------------------------

def list_names(prefix: str) -> list[str]:
    if _is_media(prefix):
        target = _asset_name(prefix)
        return sorted(_asset_path(a["name"]) for a in _assets()
                      if a["name"].startswith(target))

    url = f"{API}/repos/{repo()}/contents/{STATE_DIR}/{prefix.rstrip('/')}"
    resp = _call("GET", url, ok404=True)
    if resp is None:
        return []
    entries = resp.json()
    if isinstance(entries, dict):        # ファイルを直接指していた場合
        return [prefix.rstrip("/")]
    return sorted(f"{prefix}{e['name']}" for e in entries if e["type"] == "file")


def exists(name: str) -> bool:
    if _is_media(name):
        return _find_asset(name) is not None
    return _get_content(name) is not None


def read_bytes(name: str) -> bytes:
    if _is_media(name):
        asset = _find_asset(name)
        if not asset:
            raise StoreError(f"添付ファイルが見つかりません: {name}")
        resp = session().get(
            f"{API}/repos/{repo()}/releases/assets/{asset['id']}",
            headers={"Accept": "application/octet-stream"},
            timeout=600, allow_redirects=True)
        resp.raise_for_status()
        return resp.content

    data = _get_content(name)
    if data is None:
        raise StoreError(f"ファイルが見つかりません: {name}")
    return base64.b64decode(data["content"])


def read_json(name: str) -> dict[str, Any]:
    return json.loads(read_bytes(name).decode())


def write_json(name: str, data: dict[str, Any]) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode()
    existing = _get_content(name)
    payload = {
        "message": f"update {name}",
        "content": base64.b64encode(body).decode(),
    }
    if existing:
        payload["sha"] = existing["sha"]
    _call("PUT", _contents_url(name), json=payload)


def write_bytes(name: str, data: bytes, content_type: str) -> None:
    if not _is_media(name):
        raise StoreError(f"メディア以外をバイナリ保存しようとしています: {name}")
    old = _find_asset(name)
    if old:
        _call("DELETE", f"{API}/repos/{repo()}/releases/assets/{old['id']}")
    rel = release()
    url = (f"{UPLOADS}/repos/{repo()}/releases/{rel['id']}/assets"
           f"?name={_asset_name(name)}")
    resp = session().post(url, data=data,
                          headers={"Content-Type": content_type}, timeout=600)
    if resp.status_code >= 300:
        raise StoreError(f"添付のアップロードに失敗: {resp.status_code} "
                         f"{resp.text[:300]}")
    release(refresh=True)


def delete(name: str) -> None:
    if _is_media(name):
        asset = _find_asset(name)
        if asset:
            _call("DELETE", f"{API}/repos/{repo()}/releases/assets/{asset['id']}")
            release(refresh=True)
        return
    existing = _get_content(name)
    if existing:
        _call("DELETE", _contents_url(name),
              json={"message": f"delete {name}", "sha": existing["sha"]})


def move(src: str, dst: str) -> None:
    if _is_media(src) and _is_media(dst):
        asset = _find_asset(src)
        if not asset:
            raise StoreError(f"添付ファイルが見つかりません: {src}")
        rename_asset(asset["id"], dst)
        log.info("moved %s -> %s", src, dst)
        return
    data = read_bytes(src)
    if _is_media(dst):
        write_bytes(dst, data, "application/octet-stream")
    else:
        write_json(dst, json.loads(data.decode()))
    delete(src)
    log.info("moved %s -> %s", src, dst)


def size(name: str) -> int:
    asset = _find_asset(name)
    if asset:
        return int(asset.get("size") or 0)
    return len(read_bytes(name))


def read_range(name: str, start: int, end: int) -> bytes:
    """公開 URL に Range を投げて部分取得する（大きな動画の検査用）。"""
    resp = requests.get(public_url(name),
                        headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    resp.raise_for_status()
    return resp.content


def signed_url(name: str, minutes: int = 120) -> str:
    """公開リポジトリなので署名は不要。ダウンロード URL をそのまま返す。"""
    return public_url(name)


def iter_json(prefix: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for name in list_names(prefix):
        if name.endswith(".json"):
            yield name, read_json(name)
