"""動画解析の検証。ffmpeg で実ファイルを作って突き合わせる。

    python -m tests.test_video

ffmpeg が無い環境ではスキップする（CI やコンテナ内での実行を想定）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("GCS_BUCKET", "test-bucket")

from app import video  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                   check=True, capture_output=True)


def main() -> int:
    if not shutil.which("ffmpeg"):
        print("ffmpeg が無いためスキップします")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        src = "testsrc=size={size}:rate=30:duration={dur}"

        print("テスト用の動画を生成中…")
        ffmpeg("-f", "lavfi", "-i", src.format(size="1080x1920", dur=5),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "portrait.mp4"))
        ffmpeg("-f", "lavfi", "-i", src.format(size="1920x1080", dur=2),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "short.mp4"))
        ffmpeg("-i", str(d / "portrait.mp4"), "-c", "copy",
               "-movflags", "+faststart", str(d / "fast.mp4"))
        ffmpeg("-f", "lavfi", "-i", src.format(size="1920x1080", dur=6),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "land6.mp4"))
        # スマホの縦撮りを再現する（本体は横長 + 90 度の回転行列）
        ffmpeg("-display_rotation", "90", "-i", str(d / "land6.mp4"),
               "-c", "copy", str(d / "rotated.mov"))

        # (幅, 高さ, 尺, 回転, faststart, エラーが出るか)
        cases = {
            "portrait.mp4": (1080, 1920, 5.0, False, False, False),
            "fast.mp4":     (1080, 1920, 5.0, False, True, False),
            "short.mp4":    (1920, 1080, 2.0, False, False, True),
            "rotated.mov":  (1080, 1920, 6.0, True, False, False),
        }

        print()
        for name, (ew, eh, edur, erot, efast, has_err) in cases.items():
            data = (d / name).read_bytes()
            info = video.probe(data)
            check(f"{name}: 解像度 {ew}x{eh}",
                  (info.width, info.height) == (ew, eh))
            check(f"{name}: 尺 {edur}秒",
                  abs(info.duration_sec - edur) < 0.05)
            check(f"{name}: 回転判定 {erot}", info.rotated == erot)
            check(f"{name}: faststart 判定 {efast}", info.faststart == efast)
            check(f"{name}: 要件チェック {'NG' if has_err else 'OK'}",
                  bool(info.errors) == has_err)

            # 範囲読み（GCS 上の大きな動画を想定）でも同じ結果になること
            ranged = video.probe_remote(len(data), lambda a, b: data[a:b + 1])
            check(f"{name}: 範囲読みでも一致",
                  (ranged.width, ranged.height, ranged.rotated,
                   ranged.faststart, round(ranged.duration_sec, 2))
                  == (info.width, info.height, info.rotated,
                      info.faststart, round(info.duration_sec, 2)))

        print()
        check("横長には警告が出る",
              any("横長" in w for w in video.probe(
                  (d / "land6.mp4").read_bytes()).warnings))
        check("壊れたデータはエラーになる",
              not video.probe(b"not a video at all").ok)

    print()
    if failures:
        print(f"{len(failures)} 件失敗: {failures}")
        return 1
    print("すべて成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
