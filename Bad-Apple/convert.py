"""把视频逐帧转换成 ASCII 字符画，可选写出 pickle 缓存。"""

import pickle
from pathlib import Path

import cv2
import numpy as np

CHARSETS = {
    "classic": "$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\\|()1{}[]?-_+~<>i!lI;:,\"^`'. ",
    "simple":  "@%#*+=-:. ",
    "block":   "█▓▒░ ",
    "minimal": "@#. ",
}
DEFAULT_CHARSET = "classic"

DEFAULT_WIDTH = 90
DEFAULT_HEIGHT = 30

# 缩放插值方式。nearest 保留原"最近邻"风格，area 缩小最平滑
INTERP_MAP = {
    "nearest": cv2.INTER_NEAREST,
    "area":    cv2.INTER_AREA,
    "linear":  cv2.INTER_LINEAR,
    "cubic":   cv2.INTER_CUBIC,
}
DEFAULT_INTERP = "nearest"


def get_chars(name=None):
    """按名字取预设字符集，返回 (name, chars_list)。"""
    name = name or DEFAULT_CHARSET
    if name not in CHARSETS:
        raise ValueError(
            f"未知字符集 '{name}'，可用: {', '.join(CHARSETS)}"
        )
    return name, list(CHARSETS[name])


class AsciiRenderer:
    """向量化的帧 → ASCII 转换器。"""

    def __init__(self, chars, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                 invert=False, interp=DEFAULT_INTERP):
        self.width = width
        self.height = height
        self.invert = invert
        self.n = len(chars)
        self.char_array = np.array(chars, dtype="U1")
        self.interp = INTERP_MAP.get(interp, cv2.INTER_NEAREST)

    def frame(self, bgr):
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (self.width, self.height),
                           interpolation=self.interp)
        idx = small.astype(np.int32) * self.n // 256
        if self.invert:
            idx = self.n - 1 - idx
        np.clip(idx, 0, self.n - 1, out=idx)
        mapped = self.char_array[idx]
        return ["".join(row) for row in mapped.tolist()]


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
                   cache_path=None, frame_step=1, interp=DEFAULT_INTERP,
                   threads=None):
    if threads is not None:
        cv2.setNumThreads(threads)

    vc = cv2.VideoCapture(str(video_path))
    if not vc.isOpened():
        print(f"\n[无法打开视频] {video_path}")
        print(f"  OpenCV 的可解码格式取决于其后端（ffmpeg / gstreamer）。")
        print(f"  常见可用的: mp4 / avi / mkv / flv / mov / webm")
        print(f"  若当前格式打不开，建议用 ffmpeg 转成 mp4 后重试。")
        raise SystemExit(1)

    fps = vc.get(cv2.CAP_PROP_FPS)
    if not fps or fps != fps or fps <= 0:
        fps = 30.0
    total = int(vc.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    frame_step = max(1, int(frame_step))
    playback_fps = fps / frame_step

    renderer = AsciiRenderer(chars, width, height, invert, interp)

    tag = f"| 字符集: {charset_name} ({len(chars)} 档)"
    if frame_step > 1:
        tag += f" | 抽帧 1/{frame_step}"
    if threads is not None:
        tag += f" | 线程 {threads}"
    print(f"转换视频: {Path(video_path).name}  {tag}")

    video_data = []
    step = 0
    ok, frame = vc.read()
    total_out = total // frame_step if total else 0
    while ok:
        if step % frame_step == 0:
            video_data.append(renderer.frame(frame))
            _progress(len(video_data), total_out)
        step += 1
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
                "frame_step": frame_step,
                "interp": interp,
            },
            "video_fps": playback_fps,
            "video_data": video_data,
        }
        with open(cache_path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    print(f"完成: {len(video_data)} 帧, {playback_fps:.3f} fps")
    return video_data, playback_fps