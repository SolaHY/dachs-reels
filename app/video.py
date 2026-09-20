"""実写動画の検査。MP4 / MOV のボックス構造を直接読む（外部依存なし）。

スマホで撮った動画をそのまま上げると、横向き・短すぎ・長すぎといった理由で
翌朝の投稿時に Instagram 側で弾かれる。アップロードした瞬間に気付けるよう、
ここで尺・解像度・向きを取り出して Reels の要件と照合する。

ffmpeg がイメージに入っていれば normalize() で moov 位置の是正まで行う
（任意。Dockerfile のコメント参照）。
"""
from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Instagram リールの要件
MIN_DURATION_SEC = 3
MAX_DURATION_SEC = 15 * 60
MAX_BYTES = 1024 * 1024 * 1024
# アスペクト比の許容範囲（幅 / 高さ）。推奨は 9:16 = 0.5625
MIN_ASPECT = 0.01
MAX_ASPECT = 10.0
# これより横長だと縦画面での見栄えが大きく落ちるため警告する
PORTRAIT_MAX_ASPECT = 0.9

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}


@dataclass
class VideoInfo:
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    rotated: bool = False
    faststart: bool = False
    size_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if not self.width:
            return f"{self.size_bytes / 1e6:.1f}MB"
        return (f"{self.width}x{self.height} / {self.duration_sec:.1f}秒 / "
                f"{self.size_bytes / 1e6:.1f}MB")


# --- MP4 / MOV ボックス解析 --------------------------------------------

def _boxes(data: bytes, start: int, end: int):
    """[start, end) の範囲のボックスを (type, payload_start, payload_end) で返す。"""
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        btype = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield btype, pos + header, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, path: tuple[bytes, ...]):
    """入れ子のボックスを辿る。最初に見つかったものを返す。"""
    if not path:
        return start, end
    for btype, ps, pe in _boxes(data, start, end):
        if btype == path[0]:
            found = _find(data, ps, pe, path[1:])
            if found:
                return found
    return None


def _parse_mvhd(data: bytes, start: int, end: int) -> float:
    version = data[start]
    if version == 1:
        timescale = struct.unpack(">I", data[start + 20:start + 24])[0]
        duration = struct.unpack(">Q", data[start + 24:start + 32])[0]
    else:
        timescale = struct.unpack(">I", data[start + 12:start + 16])[0]
        duration = struct.unpack(">I", data[start + 16:start + 20])[0]
    return duration / timescale if timescale else 0.0


def _parse_tkhd(data: bytes, start: int, end: int) -> tuple[int, int, bool]:
    """トラックの表示サイズと、90/270 度回転しているかを返す。"""
    version = data[start]
    # version+flags(4) + creation / modification / track_id / reserved / duration
    offset = start + (4 + 32 if version == 1 else 4 + 20)
    offset += 16                      # reserved / layer / alternate_group / volume
    matrix = struct.unpack(">9i", data[offset:offset + 36])
    offset += 36
    width = struct.unpack(">I", data[offset:offset + 4])[0] >> 16
    height = struct.unpack(">I", data[offset + 4:offset + 8])[0] >> 16
    # 変換行列の a=0 かつ b≠0 は 90/270 度回転（スマホの縦撮りでよくある）
    rotated = matrix[0] == 0 and matrix[1] != 0
    if rotated:
        width, height = height, width
    return width, height, rotated


def _fill_from_moov(info: VideoInfo, moov: bytes) -> None:
    """moov ボックスの中身から尺と表示サイズを取り出す。"""
    mvhd = _find(moov, 0, len(moov), (b"mvhd",))
    if mvhd:
        try:
            info.duration_sec = _parse_mvhd(moov, *mvhd)
        except (struct.error, IndexError, ZeroDivisionError):
            log.warning("mvhd を解析できませんでした")

    for btype, ps, pe in _boxes(moov, 0, len(moov)):
        if btype != b"trak":
            continue
        tkhd = _find(moov, ps, pe, (b"tkhd",))
        if not tkhd:
            continue
        try:
            w, h, rotated = _parse_tkhd(moov, *tkhd)
        except (struct.error, IndexError):
            continue
        if w and h and w * h > info.width * info.height:
            info.width, info.height, info.rotated = w, h, rotated


def probe(data: bytes) -> VideoInfo:
    """手元にある全データから解析する。"""
    info = VideoInfo(size_bytes=len(data))

    found = _find(data, 0, len(data), (b"moov",))
    if not found:
        info.errors.append("MP4 / MOV として解析できません")
        return info

    # moov が mdat より前にあるか（Instagram は前方の moov を推奨）
    order = [t for t, _, _ in _boxes(data, 0, len(data)) if t in (b"moov", b"mdat")]
    info.faststart = order[:1] == [b"moov"]

    _fill_from_moov(info, data[found[0]:found[1]])
    _validate(info)
    return info


def probe_remote(total_size: int, read) -> VideoInfo:
    """GCS 上の動画を、必要な範囲だけ読んで解析する。

    スマホの動画は数百 MB になることがあるため全体は読まない。
    ボックスのヘッダ（16 バイト）だけを辿って moov の位置を特定し、
    その部分だけを取得する。

    read(start, end) は end を含む範囲のバイト列を返す関数。
    """
    info = VideoInfo(size_bytes=total_size)

    pos, moov_range, first_big_box = 0, None, None
    while pos + 8 <= total_size:
        header = read(pos, min(pos + 15, total_size - 1))
        if len(header) < 8:
            break
        box_size = int.from_bytes(header[0:4], "big")
        btype = header[4:8]
        if box_size == 1:
            if len(header) < 16:
                break
            box_size = int.from_bytes(header[8:16], "big")
        elif box_size == 0:
            box_size = total_size - pos
        if box_size < 8:
            break

        if btype in (b"moov", b"mdat") and first_big_box is None:
            first_big_box = btype
        if btype == b"moov":
            moov_range = (pos, box_size)
            break
        pos += box_size

    if not moov_range:
        info.errors.append("MP4 / MOV として解析できません")
        return info

    info.faststart = first_big_box == b"moov"
    start, length = moov_range
    moov_bytes = read(start, min(start + length, total_size) - 1)
    # 呼び出し側に渡すのは moov ボックス全体なので、ヘッダを飛ばして中身を見る
    inner = _find(moov_bytes, 0, len(moov_bytes), (b"moov",))
    if inner:
        _fill_from_moov(info, moov_bytes[inner[0]:inner[1]])

    _validate(info)
    return info


def _validate(info: VideoInfo) -> None:
    if info.size_bytes > MAX_BYTES:
        info.errors.append(f"ファイルが大きすぎます（{info.size_bytes / 1e9:.2f}GB / 上限 1GB）")

    if info.duration_sec:
        if info.duration_sec < MIN_DURATION_SEC:
            info.errors.append(
                f"尺が短すぎます（{info.duration_sec:.1f}秒 / リールは 3 秒以上）")
        elif info.duration_sec > MAX_DURATION_SEC:
            info.errors.append(
                f"尺が長すぎます（{info.duration_sec / 60:.1f}分 / 上限 15 分）")
    else:
        info.warnings.append("尺を読み取れませんでした")

    if info.width and info.height:
        aspect = info.aspect
        if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
            info.errors.append(f"アスペクト比が範囲外です（{aspect:.2f}）")
        elif aspect > PORTRAIT_MAX_ASPECT:
            info.warnings.append(
                f"横長です（{info.width}x{info.height}）。"
                "リールでは上下に余白が入り、表示が小さくなります")
    else:
        info.warnings.append("解像度を読み取れませんでした")

    if not info.faststart:
        # スマホで撮った動画はほぼ全てこうなる。Instagram はサーバ側で
        # ダウンロードしてから変換するため通常は問題にならないが、
        # 公式仕様は前方 moov を求めているので情報として残す
        info.warnings.append(
            "moov が後方にあります（通常は問題ありませんが、"
            "取り込みエラーが続く場合は ffmpeg での正規化を検討してください）")


# --- 任意の正規化（ffmpeg があるときだけ動く）--------------------------

def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def normalize(data: bytes) -> tuple[bytes, bool]:
    """再エンコードせずに moov を前方へ移し、編集リストを落とす。

    ffmpeg が無い環境では何もせずに返す。
    Returns: (データ, 変換したか)
    """
    if not ffmpeg_available():
        return data, False

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.mp4"
        dst = Path(tmp) / "out.mp4"
        src.write_bytes(data)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(src),
            "-c", "copy",                      # 再エンコードしない（速い・劣化なし）
            "-movflags", "+faststart",         # moov を先頭へ
            "-ignore_editlist", "1",
            str(dst),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            log.warning("ffmpeg での正規化に失敗したため元データを使います: %s", exc)
            return data, False
        out = dst.read_bytes()
        log.info("動画を正規化しました（%.1fMB → %.1fMB）",
                 len(data) / 1e6, len(out) / 1e6)
        return out, True
