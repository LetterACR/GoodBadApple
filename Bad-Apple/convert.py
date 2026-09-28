"""把视频逐帧转换成 ASCII 字符画，可选写出 pickle 缓存。"""

import pickle
from pathlib import Path

import cv2

# 预设字符集：按视觉密度从高到低排列
# 索引 0 = 最密（表现暗像素），末尾 = 最疏（表现亮像素）
CHARSETS = {
    "classic": "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. ",
    "simple":  "@%#*+=-:. ",
    "block":   "█▓▒░ ",
    "minimal": "@#. ",
}
DEFAULT_CHARSET = "classic"

DEFAULT_WIDTH = 90
DEFAULT_HEIGHT = 30


def get_chars(name=None):
    """按名字取预设字符集，返回 (name, chars_list)。"""
    name = name or DEFAULT_CHARSET
    if name not in CHARSETS:
        raise ValueError(
            f"未知字符集 '{name}'，可用: {', '.join(CHARSETS)}"
        )
    return name, list(CHARSETS[name])


def frame_to_ascii(frame, chars, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                   invert=False):
    """把一帧 BGR 图像转成 ASCII 行列表。"""
    h, w, _ = frame.shape
    gray = 0.2126 * frame[:, :, 2] + 0.7152 * frame[:, :, 1] + 0.0722 * frame[:, :, 0]
    n = len(chars)

    lines = []
    for i in range(height):
        y = int(i * h / height)
        row = []
        for j in range(width):
            x = int(j * w / width)
            idx = int(gray[y][x] / 256 * n)
            if invert:
                idx = n - 1 - idx
            idx = max(0, min(n - 1, idx))
            row.append(chars[idx])
        lines.append("".join(row))
    return lines


def _progress(current, total, bar_len=30):
    if total > 0:
        ratio = min(current / total, 1.0)
        filled = int(bar_len * ratio)
        bar = "#" * filled + "-" * (bar_len - filled)
        print(f"\r[{bar}] {ratio * 100:5.1f}%  ({current}/{total})",
              end="", flush=True)
    else:
        print(f"\r已转换 {current} 帧", end="", flush=True)


def video_to_ascii(video_path, chars, charset_name="classic",
                   width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, invert=False,
                   cache_path=None):
    """读取视频，逐帧转 ASCII，可选写出 pickle 缓存。

    返回 (video_data, video_fps)。
    """
    vc = cv2.VideoCapture(str(video_path))
    if not vc.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    fps = vc.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:
        fps = 30.0
    total = int(vc.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    print(f"转换视频: {Path(video_path).name}  "
          f"| 字符集: {charset_name} ({len(chars)} 档)")
    video_data = []
    ok, frame = vc.read()
    while ok:
        video_data.append(frame_to_ascii(frame, chars, width, height, invert))
        _progress(len(video_data), total)
        ok, frame = vc.read()
    vc.release()
    print()

    if cache_path is not None:
        payload = {
            "settings": {
                "source": str(video_path),
                "width": width,
                "height": height,
                "invert": invert,
                "charset": charset_name,
                "chars": "".join(chars),
            },
            "video_fps": fps,
            "video_data": video_data,
        }
        with open(cache_path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    print(f"完成: {len(video_data)} 帧, {fps:.3f} fps")
    return video_data, fps