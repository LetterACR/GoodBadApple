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

# 终端字符格大约是 1:2（高是宽的两倍），Braille 的点阵也是按这个比例推的
# （2×4 点/字符 → 每个点正好是正方形）。所以"不变形"的行列关系是：
#     rows = cols * 原片高 / 原片宽 / 2
# 字体比例不是 1:2 的终端仍会拉伸，这属于终端渲染的固有限制，用 -H 手填即可。
DEFAULT_CELL_ASPECT = 2.0

# 缩放插值方式。nearest 保留原"最近邻"风格，area 缩小最平滑
INTERP_MAP = {
    "nearest": cv2.INTER_NEAREST,
    "area":    cv2.INTER_AREA,
    "linear":  cv2.INTER_LINEAR,
    "cubic":   cv2.INTER_CUBIC,
}
DEFAULT_INTERP = "nearest"


# ============ Braille 点阵模式 ============
#
# 一个 Braille 字符编码 2×4 个二值点，所以同样 90×30 的窗口能出
# 180×120 = 21600 个点，是 ASCII 字符数（2700）的 8 倍。代价是每个点
# 只有 1 bit，得靠抖动把灰阶"骗"回来。Braille 不是一张字符表而是一种
# 渲染模式，所以不放进 CHARSETS，由 charset_name == "braille" 触发。

BRAILLE_CHARSET = "braille"

# 点号排布：  1 4
#             2 5
#             3 6
#             7 8
# 每个点对应 Unicode 盲文 U+2800 + code 里的一个 bit。
BRAILLE_BIT_MAP = np.array([
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80],
], dtype=np.uint16)

# 8×8 Bayer 有序抖动矩阵，已归一化到 [0, 1)
BAYER8 = np.array([
    [0, 32,  8, 40,  2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44,  4, 36, 14, 46,  6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43,  1, 33,  9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47,  7, 39, 13, 45,  5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32) / 64.0

DITHER_MODES = ("bayer", "floyd", "none")
DEFAULT_DITHER = "bayer"
DEFAULT_GAMMA = 1.0


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


class BrailleRenderer:
    """Braille 点阵渲染器：一个字符格子编码 2×4 个二值点。

    实际输出点阵为 (width*2) × (height*4)，即 90×30 的窗口能出
    180×120 = 21600 个点。每个点只有 1 bit，配合抖动在黑白下换回灰阶。

    invert 的含义与 AsciiRenderer 保持一致：True = 深色终端（文字亮、
    背景暗），此时"点亮一个点"就等于"画一个亮像素"。
    """

    def __init__(self, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                 invert=False, dither=DEFAULT_DITHER, gamma=DEFAULT_GAMMA):
        if dither not in DITHER_MODES:
            dither = DEFAULT_DITHER
        self.width = width
        self.height = height
        self.invert = invert
        self.dither = dither
        try:
            self.gamma = float(gamma)
        except (TypeError, ValueError):
            self.gamma = DEFAULT_GAMMA
        if self.gamma <= 0:
            self.gamma = DEFAULT_GAMMA
        self.dot_w = width * 2
        self.dot_h = height * 4

    def spec(self):
        """把抖动参数编码成字符串，用于缓存 key 和 config.json。"""
        return f"{self.dither}|g{self.gamma:g}"

    @classmethod
    def from_spec(cls, spec, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                  invert=False):
        """从 spec 字符串还原渲染器，解析失败的字段退回默认值。"""
        dither = DEFAULT_DITHER
        gamma = DEFAULT_GAMMA
        for part in str(spec or "").split("|"):
            part = part.strip()
            if part in DITHER_MODES:
                dither = part
            elif part.startswith("g"):
                try:
                    gamma = float(part[1:])
                except ValueError:
                    pass
        return cls(width, height, invert, dither, gamma)

    @staticmethod
    def _bayer(gray):
        """有序抖动：和固定阈值矩阵逐点比较，纯向量化。"""
        h, w = gray.shape
        bh, bw = BAYER8.shape
        tile = np.tile(BAYER8, (h // bh + 1, w // bw + 1))[:h, :w]
        return (gray > tile).astype(np.uint8)

    @staticmethod
    def _floyd(gray):
        """Floyd-Steinberg 误差扩散，把量化误差按 7/3/5/1 分给邻域。

        误差扩散天然串行、无法向量化。这里把图像放进 Python list 逐元素
        推进，比在 numpy 数组上做标量索引快 3~5 倍（弱机器上差别很明显）。
        """
        h, w = gray.shape
        img = gray.ravel().tolist()          # 行优先的一维缓冲
        bits = np.zeros(h * w, dtype=np.uint8)
        for y in range(h):
            base = y * w
            nxt = base + w
            for x in range(w):
                i = base + x
                old = img[i]
                if old > 0.5:
                    img[i] = 1.0
                    bits[i] = 1
                    err = old - 1.0
                else:
                    img[i] = 0.0
                    err = old
                if err:
                    if x + 1 < w:
                        img[i + 1] += err * 0.4375              # 7/16
                    if y + 1 < h:
                        if x > 0:
                            img[nxt + x - 1] += err * 0.1875    # 3/16
                        img[nxt + x] += err * 0.3125            # 5/16
                        if x + 1 < w:
                            img[nxt + x + 1] += err * 0.0625    # 1/16
        return bits.reshape(h, w)

    def _pack(self, bits):
        """(dot_h, dot_w) 的 0/1 矩阵 → Braille 字符行列表。"""
        blocks = bits.reshape(self.height, 4, self.width, 2)
        blocks = blocks.transpose(0, 2, 1, 3)          # (h, w, 4, 2)
        codes = (blocks.astype(np.uint16) * BRAILLE_BIT_MAP).sum(
            axis=(2, 3), dtype=np.uint32)
        codes += 0x2800
        return ["".join(chr(int(c)) for c in row) for row in codes.tolist()]

    def frame(self, bgr):
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        # 点阵比字符格密得多，缩小一律用 INTER_AREA，避免 nearest 丢点
        small = cv2.resize(gray, (self.dot_w, self.dot_h),
                           interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0

        if self.gamma != 1.0:
            small = np.power(small, 1.0 / self.gamma)

        if self.dither == "floyd":
            bits = self._floyd(small)
        elif self.dither == "none":
            bits = (small > 0.5).astype(np.uint8)
        else:
            bits = self._bayer(small)

        # 注意这里是 not invert，看着和直觉相反，但和 AsciiRenderer 对齐：
        #   invert=True （深色终端，文字亮）→ 亮像素应该"点出来" → 不翻转；
        #   invert=False（浅色终端，文字暗）→ 亮像素应该"留白"  → 才翻转。
        # 写反了就会在深色终端上播出负片效果。
        if not self.invert:
            bits = 1 - bits

        return self._pack(bits)


def _make_renderer(charset_name, chars, width, height, invert, interp):
    """按字符集名构造渲染器，返回 (renderer, 存入缓存的 chars 串, tag)。"""
    if charset_name == BRAILLE_CHARSET:
        renderer = BrailleRenderer.from_spec(chars, width, height, invert)
        tag = (f"| 字符集: braille ({renderer.dither}, "
               f"gamma {renderer.gamma:g}, 点阵 "
               f"{renderer.dot_w}×{renderer.dot_h})")
        return renderer, renderer.spec(), tag

    renderer = AsciiRenderer(chars, width, height, invert, interp)
    return renderer, "".join(chars), f"| 字符集: {charset_name} ({len(chars)} 档)"


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

    renderer, chars_str, tag = _make_renderer(
        charset_name, chars, width, height, invert, interp)

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
        save_frames(cache_path, video_data, {
            "source": str(video_path),
            "width": width,
            "height": height,
            "invert": invert,
            "charset": charset_name,
            "chars": chars_str,
            "frame_step": frame_step,
            "interp": interp,
        }, playback_fps)

    print(f"完成: {len(video_data)} 帧, {playback_fps:.3f} fps")
    return video_data, playback_fps


def save_frames(cache_path, video_data, settings, video_fps):
    """写缓存文件（video_to_ascii 和"复用反明暗缓存"共用同一套 payload 格式）。"""
    payload = {
        "settings": dict(settings),
        "video_fps": video_fps,
        "video_data": video_data,
    }
    with open(cache_path, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)


def video_size(video_path):
    """读视频的原始像素尺寸，返回 (宽, 高)；读不到返回 None（不抛异常）。"""
    vc = cv2.VideoCapture(str(video_path))
    try:
        if not vc.isOpened():
            return None
        w = int(vc.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(vc.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if w > 0 and h > 0:
            return w, h
        ok, frame = vc.read()            # 有些容器 CAP_PROP 给 0，读一帧再问
        if ok and frame is not None:
            h, w = frame.shape[:2]
            return (w, h) if w > 0 and h > 0 else None
        return None
    except Exception:
        return None
    finally:
        vc.release()


def fit_size(width, video_w, video_h, term_cols=0, term_rows=0,
             cell_aspect=DEFAULT_CELL_ASPECT):
    """按原片比例把字符画尺寸算成不变形的，返回 (cols, rows)。

    行数由列数和原片比例推出（`rows = cols * h / w / cell_aspect`）；给了终端
    尺寸时先按列宽收窄，再在高度放不下时**整体等比缩小**——不然竖屏视频会算出
    比终端还高的尺寸，播放时被从下面裁掉一截。原片尺寸未知时退回
    `(width, DEFAULT_HEIGHT)`，也就是老行为。
    """
    width = max(1, int(width))
    if not video_w or not video_h:
        return width, DEFAULT_HEIGHT

    cols = max(1, min(width, term_cols - 1)) if term_cols > 1 else width
    rows = max(1, round(cols * video_h / video_w / cell_aspect))
    if term_rows > 1 and rows > term_rows - 1:
        rows = max(1, term_rows - 1)
        cols = max(1, round(rows * video_w / video_h * cell_aspect))
    return cols, rows


_BRAILLE_FLIP = str.maketrans(
    {chr(0x2800 + i): chr(0x2800 + (i ^ 0xFF)) for i in range(256)})


def invert_frames(video_data, charset_name, chars):
    """把整批帧"取反"，用来复用相反明暗的同名缓存。

    渲染器里 invert 恰好是**最后一步整体取反**，所以取反得到的帧和"换个明暗
    重新渲染一遍"完全一致，不必重新解码视频：

    - ASCII：`idx = n-1-idx`，就是字符表镜像（[AsciiRenderer.frame]）；
    - Braille：`bits = 1-bits`，就是 8 个点逐位取反，打包后等价于
      `0x2800 + ((cp - 0x2800) ^ 0xFF)`（BRAILLE_BIT_MAP 覆盖 0x01..0x80，
      所以点阵总和是 0xFF）。

    返回新的帧数据；**自定义字符表里有重复字符时返回 None** —— 那样字符到档位
    的映射不唯一，镜像只能靠猜，调用方应该老老实实重新转换。
    """
    if charset_name == BRAILLE_CHARSET:
        return [[row.translate(_BRAILLE_FLIP) for row in frame]
                for frame in video_data]

    chars = list(chars or [])
    if len(chars) < 2 or len(set(chars)) != len(chars):
        return None
    n = len(chars)
    table = str.maketrans({c: chars[n - 1 - i] for i, c in enumerate(chars)})
    return [[row.translate(table) for row in frame] for frame in video_data]
