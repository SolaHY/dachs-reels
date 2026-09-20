"""GitHub ストレージをインメモリに差し替えた、主要フローのスモークテスト。

    python -m tests.test_flow

ネットワークには一切出ない。
"""
from __future__ import annotations

import json
import os
import struct
import sys

os.environ.setdefault("GITHUB_REPO", "owner/repo")
os.environ.setdefault("GITHUB_TOKEN", "dummy")
os.environ.setdefault("IG_ACCESS_TOKEN", "dummy")
os.environ.setdefault("PROMPT_PROVIDER", "template")

from app import store  # noqa: E402

# --- インメモリのストレージ --------------------------------------------
FILES: dict[str, bytes] = {}          # JSON など
ASSETS: dict[int, dict] = {}          # リリース添付（id -> {name, size, data}）
_next_id = [1]

MEDIA_PREFIXES = ("videos/", "images/")


def _is_media(name: str) -> bool:
    return name.startswith(MEDIA_PREFIXES)


def _asset_by_path(path: str):
    return next((a for a in ASSETS.values() if a["name"] == path), None)


def put_asset(name: str, data: bytes) -> int:
    aid = _next_id[0]
    _next_id[0] += 1
    ASSETS[aid] = {"id": aid, "name": name, "size": len(data), "data": data,
                   "browser_download_url": f"https://dl.example/{name}"}
    return aid


store.list_names = lambda prefix: sorted(
    [a["name"] for a in ASSETS.values() if a["name"].startswith(prefix)]
    if _is_media(prefix)
    else [k for k in FILES if k.startswith(prefix)])
store.exists = lambda n: (_asset_by_path(n) is not None) if _is_media(n) else n in FILES
store.read_bytes = lambda n: (_asset_by_path(n)["data"] if _is_media(n) else FILES[n])
store.read_json = lambda n: json.loads(FILES[n].decode())
store.write_json = lambda n, d: FILES.__setitem__(
    n, json.dumps(d, ensure_ascii=False).encode())
store.write_bytes = lambda n, d, ct: put_asset(n, d)
store.signed_url = lambda n, minutes=120: f"https://dl.example/{n}"
store.public_url = store.signed_url
store.size = lambda n: _asset_by_path(n)["size"]
store.read_range = lambda n, a, b: _asset_by_path(n)["data"][a:b + 1]
store.unmanaged_assets = lambda: [
    a for a in ASSETS.values() if not a["name"].startswith(MEDIA_PREFIXES)]
store.iter_json = lambda prefix: [
    (n, store.read_json(n)) for n in store.list_names(prefix) if n.endswith(".json")]


def _rename(asset_id: int, new_path: str) -> None:
    ASSETS[asset_id]["name"] = new_path
    ASSETS[asset_id]["browser_download_url"] = f"https://dl.example/{new_path}"


def _delete(name: str) -> None:
    if _is_media(name):
        asset = _asset_by_path(name)
        if asset:
            ASSETS.pop(asset["id"])
    else:
        FILES.pop(name, None)


def _move(src: str, dst: str) -> None:
    if _is_media(src) and _is_media(dst):
        _rename(_asset_by_path(src)["id"], dst)
    elif _is_media(src):
        FILES[dst] = _asset_by_path(src)["data"]
        _delete(src)
    else:
        FILES[dst] = FILES.pop(src)


store.rename_asset = _rename
store.delete = _delete
store.move = _move

from app import config, prompts, queue, video  # noqa: E402
from app.jobs import ingest, publish  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def fake_mp4(width: int, height: int, seconds: float) -> bytes:
    """解析に必要な箱だけを持つ最小の MP4。実データ検証は test_video.py。"""
    def box(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload) + 8) + tag + payload

    ts = 600
    mvhd = box(b"mvhd", struct.pack(">B3x", 0) + b"\x00" * 8
               + struct.pack(">II", ts, int(seconds * ts)) + b"\x00" * 80)
    matrix = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd = box(b"tkhd", struct.pack(">B3x", 0) + b"\x00" * 20 + b"\x00" * 16
               + matrix + struct.pack(">II", width << 16, height << 16))
    moov = box(b"moov", mvhd + box(b"trak", tkhd))
    return box(b"ftyp", b"isom" + b"\x00" * 8) + box(b"mdat", b"\x00" * 512) + moov


# 検査は Range 読みで行うので、添付の URL からではなくデータから読ませる
def _probe_asset(asset):
    data = ASSETS[asset["id"]]["data"]
    return video.probe_remote(len(data), lambda a, b: data[a:b + 1])


ingest._probe_asset = _probe_asset


# --- 1. 企画プールの用意 ------------------------------------------------
print("1. 企画プール")
pool = prompts.empty_pool()
prompts.merge(pool, prompts.template_entries(5, "こむぎとあずき"))
prompts.save(pool)
check("プールが保存される", store.exists(prompts.POOL_PATH))

# --- 2. 実写動画の取り込み ----------------------------------------------
print()
print("2. リリースに添付された実写動画の取り込み")
put_asset("IMG_1234.MOV", fake_mp4(1080, 1920, 6.0))
put_asset("IMG_5678.mp4", fake_mp4(1920, 1080, 2.0))     # 短すぎる
put_asset("memo.txt", b"not media")
put_asset("photo.jpg", b"\xff\xd8\xff\xe0fake-jpeg")

check("未取り込みが 4 件ある", len(store.unmanaged_assets()) == 4)
check("取り込みジョブが正常終了", ingest.run() == 0)

check("動画 1 件がキューに入る", queue.ready_count() == 1)
_, item = queue.next_ready()
check("由来が実写になる", item["origin"] == queue.ORIGIN_LIVE)
check("拡張子が保たれる", item["video_path"].endswith(".mov"))
check("解像度と尺が記録される",
      item["video_info"]["width"] == 1080 and item["video_info"]["duration_sec"] == 6.0)
check("企画のキャプションが載る", bool(item["caption"]))
check("ハッシュタグが載る", len(item["hashtags"]) > 0)

check("短すぎる動画は rejected に退避される",
      any(a["name"].startswith(queue.VIDEOS_REJECTED) for a in ASSETS.values()))
check("写真は images/pending/ に入る", len(queue.pending_images()) == 1)
check("扱えない形式は手つかずで残る",
      any(a["name"] == "memo.txt" for a in ASSETS.values()))
check("2 回目は何もしない（取り込み済みを再処理しない）",
      ingest.run() == 0 and queue.ready_count() == 1)

# ファイル名で出どころを見分ける（PixVerse アプリで作った動画を手で上げる運用）
print()
print("2b. ファイル名による実写 / AI の判別")
for fname, expected in [
    ("IMG_1234.MOV", queue.ORIGIN_LIVE),
    ("ai_walk.mp4", queue.ORIGIN_AI),
    ("AI-sunset.mov", queue.ORIGIN_AI),
    ("PixVerse_export.mp4", queue.ORIGIN_AI),
    ("airport.mp4", queue.ORIGIN_LIVE),        # 誤検出しないこと
    ("my_ai_dog.mp4", queue.ORIGIN_LIVE),      # 同上
]:
    got = ingest.origin_of(fname)
    check(f"{fname} → {expected}", got == expected)

put_asset("ai_autumn.mp4", fake_mp4(1080, 1920, 7.0))
ingest.run()
ai_items = [store.read_json(n) for n in store.list_names(queue.READY)
            if store.read_json(n)["origin"] == queue.ORIGIN_AI]
check("AI 名の動画が AI として積まれる", len(ai_items) == 1)
check("内訳に反映される", queue.ready_counts()[queue.ORIGIN_AI] == 1)
# 以降のテストのために取り除く
for n in store.list_names(queue.READY):
    if store.read_json(n)["origin"] == queue.ORIGIN_AI:
        store.delete(n)

# --- 3. 投稿 ------------------------------------------------------------
print()
print("3. Instagram への投稿")


class FakeIG:
    def __init__(self, *a, **kw):
        self.caption = None

    def publishing_limit(self):
        return {"data": [{"quota_usage": 0}]}

    def create_reel_container(self, video_url, caption, **kw):
        assert video_url.startswith("https://dl.example/videos/")
        self.caption = caption
        return "container-1"

    def wait_container(self, cid, **kw):
        return None

    def publish(self, cid):
        return "media-1"

    def permalink(self, mid):
        return "https://instagram.com/reel/xyz"


publish._ensure_fresh_token = lambda: "token"
publish.instagram.InstagramClient = FakeIG

check("投稿が成功する", publish.run() == 0)
check("ready が空になる", queue.ready_count() == 0)
check("inflight が残らない", not queue.stale_inflight())
posted = store.list_names(queue.POSTED)
check("posted に 1 件", len(posted) == 1)
check("permalink が記録される",
      store.read_json(posted[0])["ig_permalink"].startswith("https://instagram.com"))

print()
print("4. 投稿に失敗したとき")
put_asset("second.mp4", fake_mp4(1080, 1920, 5.0))
ingest.run()


class FailingIG(FakeIG):
    def create_reel_container(self, video_url, caption, **kw):
        raise publish.instagram.InstagramError("動画の形式が不正です")


publish.instagram.InstagramClient = FailingIG
check("終了コード 1", publish.run() == 1)
check("inflight に残らない", not queue.stale_inflight())
failed = store.list_names(queue.FAILED)
check("failed に 1 件", len(failed) == 1)
check("エラー理由が残る", "不正" in store.read_json(failed[0])["error"])

print()
print("5. キューが空のとき")
publish.instagram.InstagramClient = FakeIG
check("終了コード 1 で知らせる", publish.run() == 1)

# --- 6. 実写と AI のミックス --------------------------------------------
print()
print("6. 実写と AI を混ぜて投稿する順番")
for k in [n for n in list(FILES) if n.startswith((queue.READY, queue.POSTED))]:
    del FILES[k]


def seed(origin, n, offset=0):
    for i in range(n):
        item_id = f"2026010{offset + i}-000000-{origin[:3]}{i}"
        queue.enqueue_ready({"id": item_id, "origin": origin,
                             "video_path": f"videos/{item_id}.mp4",
                             "caption": "", "created_at": queue.now_iso()})


seed(queue.ORIGIN_LIVE, 3)
seed(queue.ORIGIN_AI, 3, offset=3)
counts = queue.ready_counts()
check("内訳が正しい",
      counts[queue.ORIGIN_LIVE] == 3 and counts[queue.ORIGIN_AI] == 3)
check("履歴が無いときは実写を選ぶ",
      queue.next_ready_mixed(0.6)[1]["origin"] == queue.ORIGIN_LIVE)

for i in range(8):
    store.write_json(f"{queue.POSTED}2026020{i}-past.json",
                     {"id": f"p{i}", "origin": queue.ORIGIN_LIVE})
for i in range(2):
    store.write_json(f"{queue.POSTED}2026021{i}-past.json",
                     {"id": f"q{i}", "origin": queue.ORIGIN_AI})
check("直近の実写率が算出できる",
      abs(queue.recent_origins(10).count(queue.ORIGIN_LIVE) / 10 - 0.8) < 1e-9)
check("実写が過剰なら AI を選ぶ",
      queue.next_ready_mixed(0.6)[1]["origin"] == queue.ORIGIN_AI)

for k in [n for n in store.list_names(queue.READY)
          if store.read_json(n)["origin"] == queue.ORIGIN_AI]:
    store.delete(k)
check("AI 在庫が無ければ実写で埋める",
      queue.next_ready_mixed(0.6)[1]["origin"] == queue.ORIGIN_LIVE)
check("LIVE_RATIO=1.0 なら常に実写",
      queue.next_ready_mixed(1.0)[1]["origin"] == queue.ORIGIN_LIVE)

# --- 7. AI 生成の抑制 ---------------------------------------------------
print()
print("7. PixVerse を呼ぶ条件")
from app.jobs import generate as gen  # noqa: E402

config.QUEUE_TARGET = 6
config.LIVE_RATIO = 1.0
check("LIVE_RATIO=1.0 なら在庫ゼロでも生成しない（キー不要で成立）",
      gen.run() == 0)

config.LIVE_RATIO = 0.6
for k in [n for n in list(FILES) if n.startswith(queue.READY)]:
    del FILES[k]
seed(queue.ORIGIN_LIVE, 6)
check("実写だけで満杯なら生成しない", gen.run() == 0)

print()
if failures:
    print(f"{len(failures)} 件失敗: {failures}")
    sys.exit(1)
print("すべて成功")
