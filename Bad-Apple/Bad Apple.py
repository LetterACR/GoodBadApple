"""在终端播放 ASCII 字符画视频。"""

import argparse
import curses
import hashlib
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

import pygame

import convert

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CACHE_DIR = BASE_DIR / "cache"

VIDEO_EXTS = {".flv", ".mp4", ".avi", ".mkv", ".mov", ".webm", ".wmv"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}


# ============ 终端检测 ============

def _query_osc11(timeout=0.15):
    if os.name == "nt" or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import termios
        import tty
        import select
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    try:
        old_attr = termios.tcgetattr(fd)
    except termios.error:
        return None

    buf = b""
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b]11;?\x1b\\")
        sys.stdout.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], max(0, deadline - time.time()))
            if not r:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk
            if b"\x1b\\" in buf or b"\x07" in buf:
                break
    except Exception:
        return None
    finally:
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            pass
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_attr)
        except Exception:
            pass

    m = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", buf)
    if not m:
        return None

    def to8(s):
        v = int(s, 16)
        return v // 257 if len(s) == 4 else v

    return to8(m.group(1)), to8(m.group(2)), to8(m.group(3))


def _stty_sane():
    if os.name != "nt":
        try:
            os.system("stty sane 2>/dev/null")
        except Exception:
            pass


def detect_light_bg():
    colorfgbg = os.environ.get("COLORFGBG")
    if colorfgbg:
        try:
            return int(colorfgbg.split(";")[-1]) >= 7
        except ValueError:
            pass
    rgb = _query_osc11()
    if rgb is not None:
        r, g, b = rgb
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) > 128
    return None


def wait_any_key(prompt="按任意键继续..."):
    print(prompt, end="", flush=True)
    if os.name == "nt":
        import msvcrt
        msvcrt.getch()
    else:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old_attr = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attr)
    print()


# ============ 缓存管理 ============

class Cache:
    """管理 ASCII 数据缓存和每个视频的配置。"""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(exist_ok=True)

    @staticmethod
    def _stem(video_path: Path) -> str:
        return "".join(
            c if c.isalnum() or c in "-_" else "_" for c in video_path.stem
        )

    @staticmethod
    def _charset_tag(charset_name, chars):
        if charset_name == "classic":
            return ""
        if charset_name == "custom":
            h = hashlib.md5("".join(chars).encode("utf-8")).hexdigest()[:6]
            return f"_custom{h}"
        return f"_{charset_name}"

    def data_path(self, video_path, width, height, invert,
                  charset_name="classic", chars=None):
        tag = "dark" if invert else "light"
        suffix = self._charset_tag(charset_name, chars or [])
        return self.dir / f"{self._stem(video_path)}_{width}x{height}_{tag}{suffix}.pkl"

    def config_path(self, video_path):
        return self.dir / f"{self._stem(video_path)}.config.json"

    def load_config(self, video_path):
        p = self.config_path(video_path)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_config(self, video_path, **fields):
        self.config_path(video_path).write_text(
            json.dumps(fields, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_data(self, video_path, width, height, invert,
                  charset_name="classic", chars=None, fps_override=None):
        """命中返回 (video_data, fps)，未命中返回 None。"""
        if not (width and height):
            return None
        path = self.data_path(video_path, width, height, invert,
                              charset_name, chars)
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
        except Exception as e:
            print(f"缓存读取失败，将重新转换: {e}")
            return None

        s = payload.get("settings", {})
        if (s.get("width") == width
                and s.get("height") == height
                and s.get("source") == str(video_path)
                and s.get("invert", False) == invert
                and s.get("chars") == "".join(chars or [])):
            fps = fps_override or payload.get("video_fps", 30.0)
            print(f"读取缓存 {path.name} | {width}x{height} | "
                  f"{charset_name} | {fps:.3f} fps")
            return payload["video_data"], fps
        return None


# ============ data/ 扫描 ============

def scan_data_dir():
    DATA_DIR.mkdir(exist_ok=True)
    videos, audios = [], []
    for p in sorted(DATA_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() in VIDEO_EXTS:
            videos.append(p)
        elif p.suffix.lower() in AUDIO_EXTS:
            audios.append(p)
    return videos, audios


def choose(items, kind):
    if not items:
        return None
    if len(items) == 1:
        print(f"自动选择{kind}: {items[0].name}")
        return items[0]

    print(f"\n发现多个{kind}，请选择:")
    for i, p in enumerate(items, 1):
        size_mb = p.stat().st_size / 1024 / 1024
        print(f"  [{i}] {p.name}  ({size_mb:.1f} MB)")

    while True:
        s = input(f"序号 (1-{len(items)}, 回车默认 1): ").strip()
        if not s:
            return items[0]
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]
        print("输入无效，请重试。")


# ============ 播放 ============

class FpsTimer:
    """补偿式帧计时器，避免 time.sleep() 累积漂移。"""

    def __init__(self, fps):
        self.frame_time = 1.0 / max(fps, 1e-6)
        self.deadline = time.perf_counter()

    def sleep(self):
        self.deadline += self.frame_time
        wait = self.deadline - time.perf_counter()
        if wait > 0:
            time.sleep(wait)
        elif wait < -self.frame_time:
            self.deadline = time.perf_counter()


def play(video_data, fps, bgm_path, delay=0.4):
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.clear()
    stdscr.refresh()

    try:
        max_y, max_x = stdscr.getmaxyx()
        rows = min(len(video_data[0]) if video_data else 0, max_y - 1)
        cols = max_x - 1

        pygame.mixer.init()
        pygame.mixer.music.load(str(bgm_path))
        pygame.mixer.music.play()
        time.sleep(delay)

        timer = FpsTimer(fps)
        for frame_data in video_data:
            stdscr.nodelay(True)
            if stdscr.getch() in (ord("q"), 27):
                break

            stdscr.erase()
            for i in range(rows):
                line = (frame_data[i][:cols].ljust(cols)
                        if i < len(frame_data) else " " * cols)
                try:
                    stdscr.addstr(i, 0, line)
                except curses.error:
                    pass

            timer.sleep()
            stdscr.refresh()
    finally:
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        curses.endwin()


# ============ 命令行 ============

def parse_args():
    p = argparse.ArgumentParser(
        description="在终端播放 ASCII 字符画视频",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-v", "--video", help="视频路径，不填则自动扫描 data/")
    p.add_argument("-b", "--bgm", help="背景音乐路径，不填则自动扫描 data/")
    p.add_argument("-W", "--width", type=int, help="字符画宽度")
    p.add_argument("-H", "--height", type=int, help="字符画高度")
    p.add_argument("-f", "--fps", type=float, help="播放帧率，不填则用视频自带")
    p.add_argument("-d", "--delay", type=float, default=0.4,
                   help="音乐开始后延迟多少秒才出画面")
    p.add_argument("-i", "--invert", dest="invert", action="store_true",
                   default=None, help="强制反转（浅色终端）")
    p.add_argument("--no-invert", dest="invert", action="store_false",
                   help="强制不反转（深色终端）")
    p.add_argument("-r", "--rebuild", action="store_true",
                   help="忽略缓存，强制重新转换")

    g = p.add_argument_group("字符集")
    g.add_argument("-c", "--charset", metavar="NAME",
                   help=f"选择预设字符集，可选: {', '.join(convert.CHARSETS)}")
    g.add_argument("--chars", metavar="STR",
                   help="直接指定字符表（从密到疏），会覆盖 --charset")
    g.add_argument("-L", "--list-charsets", action="store_true",
                   help="列出所有预设字符集并退出")
    return p.parse_args()


# ============ 决策函数 ============

def ask_int(prompt, default):
    s = input(f"{prompt} (默认 {default}): ").strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        print(f"输入无效，使用默认值 {default}")
        return default


def resolve_video(args):
    if args.video:
        p = Path(args.video)
        if not p.exists():
            sys.exit(f"视频不存在: {p}")
        return p
    videos, _ = scan_data_dir()
    if not videos:
        sys.exit(f"未在 {DATA_DIR} 找到视频文件")
    return choose(videos, "视频")


def resolve_audio(args, video_path, cfg):
    if args.bgm:
        p = Path(args.bgm)
        if not p.exists():
            sys.exit(f"音乐不存在: {p}")
        return p
    _, audios = scan_data_dir()
    if not audios:
        sys.exit(f"未在 {DATA_DIR} 找到音频文件")

    preferred = cfg.get("audio")
    if preferred and (DATA_DIR / preferred).exists():
        print(f"沿用上次配对音乐: {preferred}")
        return DATA_DIR / preferred
    return choose(audios, "背景音乐")


def resolve_invert(args, video_path, cache):
    if args.invert is not None:
        return args.invert
    is_light = detect_light_bg()
    _stty_sane()
    if is_light is None:
        cached = cache.load_config(video_path).get("invert", False)
        print(f"无法检测终端背景，沿用上次设置: "
              f"{'反转' if cached else '不反转'}")
        return cached
    invert = not is_light
    print(f"检测到{'浅色' if is_light else '深色'}终端背景，"
          f"自动{'启用' if invert else '关闭'}反转")
    return invert


def resolve_size(args, cfg):
    width = args.width or cfg.get("width") or ask_int(
        "字符画宽度", convert.DEFAULT_WIDTH)
    height = args.height or cfg.get("height") or ask_int(
        "字符画高度", convert.DEFAULT_HEIGHT)
    return width, height


def resolve_charset(args, video_path, cache, need_interactive=False):
    """返回 (charset_name, chars_list)。

    优先级：--chars > --charset > 缓存配置 > (交互询问) > 默认。
    need_interactive=True 时（首次转换）允许询问用户。
    """
    if args.chars:
        chars = list(args.chars)
        if len(chars) < 2:
            sys.exit("自定义字符集至少需要 2 个字符")
        print(f"使用自定义字符集 ({len(chars)} 档)")
        return "custom", chars

    if args.charset:
        try:
            return convert.get_chars(args.charset)
        except ValueError as e:
            sys.exit(str(e))

    cfg = cache.load_config(video_path)
    cached_name = cfg.get("charset")
    cached_chars = cfg.get("chars")
    if cached_name and cached_chars and len(cached_chars) >= 2:
        if cached_name != convert.DEFAULT_CHARSET:
            print(f"沿用上次字符集: {cached_name} ({len(cached_chars)} 档)")
        return cached_name, list(cached_chars)

    if not need_interactive:
        return convert.get_chars(convert.DEFAULT_CHARSET)

    # 首次转换：让用户选一次
    print("\n可选字符集:")
    for name, chars in convert.CHARSETS.items():
        preview = chars if len(chars) <= 24 else chars[:24] + "..."
        mark = " (默认)" if name == convert.DEFAULT_CHARSET else ""
        print(f"  {name:8s} [{len(chars):2d}档]{mark}  {preview}")
    s = input(f"选择字符集名（回车默认 {convert.DEFAULT_CHARSET}，"
              f"或直接粘贴自定义字符表）: ").strip()

    if not s:
        return convert.get_chars(convert.DEFAULT_CHARSET)
    if s in convert.CHARSETS:
        return convert.get_chars(s)
    if len(s) >= 2:
        print(f"使用自定义字符集 ({len(s)} 档)")
        return "custom", list(s)
    print("输入太短，使用默认字符集")
    return convert.get_chars(convert.DEFAULT_CHARSET)


# ============ 主流程 ============

def main():
    args = parse_args()

    if args.list_charsets:
        print("可用字符集:")
        for name, chars in convert.CHARSETS.items():
            preview = chars if len(chars) <= 40 else chars[:40] + "..."
            print(f"  {name:8s} [{len(chars):2d}档]  {preview}")
        return

    cache = Cache(CACHE_DIR)

    video_path = resolve_video(args)
    cfg = cache.load_config(video_path)
    bgm_path = resolve_audio(args, video_path, cfg)
    invert = resolve_invert(args, video_path, cache)
    width, height = resolve_size(args, cfg)

    # 先看看缓存里有没有这套配置的字符集
    charset_name, chars = resolve_charset(args, video_path, cache)

    # 尝试读缓存
    result = None if args.rebuild else cache.load_data(
        video_path, width, height, invert, charset_name, chars, args.fps)

    if result is None:
        # 真需要转换时才交互询问字符集（覆盖前面的默认值）
        if (args.chars is None and args.charset is None
                and not cfg.get("chars")):
            charset_name, chars = resolve_charset(
                args, video_path, cache, need_interactive=True)

        fps_override = args.fps
        if fps_override is None:
            s = input("播放帧率 (回车使用视频自带): ").strip()
            fps_override = float(s) if s else None

        cache_path = cache.data_path(video_path, width, height, invert,
                                     charset_name, chars)
        video_data, video_fps = convert.video_to_ascii(
            video_path, chars, charset_name,
            width, height, invert, cache_path)
        fps = fps_override or video_fps
        wait_any_key("\n转换完成，按任意键继续...")
    else:
        video_data, fps = result

    cache.save_config(video_path,
                      width=width, height=height, fps=fps,
                      invert=invert, audio=bgm_path.name,
                      charset=charset_name, chars="".join(chars))
    play(video_data, fps, bgm_path, args.delay)


if __name__ == "__main__":
    main()