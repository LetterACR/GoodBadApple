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
import atexit
import signal
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

_terminal_dirty = False          # 进入 curses 后置 True，恢复后置 False

def _restore_terminal():
    """尽最大努力把终端恢复成正常状态。可重复调用。"""
    global _terminal_dirty
    if not _terminal_dirty:
        return
    try:
        curses.endwin()
    except Exception:
        pass
    _stty_sane()
    _terminal_dirty = False


def _install_terminal_guards():
    """装信号处理和 atexit，确保异常退出时终端也能恢复。"""
    def _handler(signum, frame):
        _restore_terminal()
        # 恢复默认行为再重发信号，让进程按正常方式退出
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 某些环境（比如非主线程、Windows 部分信号）不支持，忽略
            pass

    atexit.register(_restore_terminal)


def _startup_terminal_check():
    """启动时先尝试清掉上一次残留的坏终端状态（非 Windows）。"""
    _stty_sane()

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
    """每个视频一个子目录，存放该视频的所有缓存和配置。

    同名不同目录的视频通过相对 data/ 的路径区分。
    """

    def __init__(self, root: Path, data_dir: Path = None):
        self.root = root
        self.data_dir = data_dir
        self.root.mkdir(exist_ok=True)

    def _video_stem(self, video_path: Path) -> str:
        """生成视频的缓存目录名。

        data/set1/video.mp4 → set1__video
        data/set2/video.mp4 → set2__video
        data/video.mp4      → video
        """
        if self.data_dir is not None:
            try:
                rel = video_path.relative_to(self.data_dir)
                parts = list(rel.with_suffix("").parts)
                stem = "__".join(parts)
            except ValueError:
                # 不在 data/ 下（比如用 --video 指定了外部路径）
                stem = video_path.stem
        else:
            stem = video_path.stem

        return "".join(
            c if c.isalnum() or c in "-_" else "_" for c in stem
        )

    @staticmethod
    def _charset_tag(charset_name, chars=None):
        if charset_name == "classic":
            return ""
        if charset_name == "custom":
            h = hashlib.md5("".join(chars or []).encode("utf-8")).hexdigest()[:6]
            return f"_custom{h}"
        return f"_{charset_name}"

    @classmethod
    def _make_key(cls, width, height, invert, charset_name, chars=None):
        bg = "dark" if invert else "light"
        return f"{width}x{height}_{bg}{cls._charset_tag(charset_name, chars)}"

    @staticmethod
    def _parse_key(key):
        """从缓存文件名反解设置。"""
        m = re.match(r"^(\d+)x(\d+)_(dark|light)(?:_(.+))?$", key)
        if not m:
            return None

        tag = m.group(4) or ""
        if tag.startswith("custom"):
            charset_name = "custom"
            charset_tag = tag                    # 保留完整 tag，如 custom_a1b2c3
        elif tag == "":
            charset_name = "classic"
            charset_tag = ""
        else:
            charset_name = tag
            charset_tag = tag

        return {
            "width": int(m.group(1)),
            "height": int(m.group(2)),
            "invert": m.group(3) == "dark",
            "charset": charset_name,
            "charset_tag": charset_tag,
        }

    def video_dir(self, video_path):
        d = self.root / self._video_stem(video_path)
        d.mkdir(exist_ok=True)
        return d

    def data_path(self, video_path, width, height, invert,
                  charset_name="classic", chars=None):
        key = self._make_key(width, height, invert, charset_name, chars)
        return self.video_dir(video_path) / f"{key}.pkl"

    def config_path(self, video_path):
        return self.video_dir(video_path) / "config.json"

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

    def list_caches(self, video_path):
        """返回 [(key, info, path), ...]，按修改时间倒序（最近用的在前）。"""
        d = self.video_dir(video_path)
        result = []
        for f in d.glob("*.pkl"):
            info = self._parse_key(f.stem)
            if info is None:
                continue
            result.append((f.stem, info, f))
        result.sort(key=lambda x: x[2].stat().st_mtime, reverse=True)
        return result

    def load_by_path(self, path, fps_override=None):
        """加载指定的缓存文件，返回 (video_data, fps, settings)。"""
        try:
            with open(path, "rb") as f:
                payload = pickle.load(f)
        except Exception as e:
            print(f"缓存读取失败: {e}")
            return None
        fps = fps_override or payload.get("video_fps", 30.0)
        return payload["video_data"], fps, payload.get("settings", {})

    def load_data(self, video_path, width, height, invert,
                  charset_name="classic", chars=None, fps_override=None):
        """按设置精确查找缓存（用于向导结束后再查一次）。"""
        path = self.data_path(video_path, width, height, invert,
                              charset_name, chars)
        if not path.exists():
            return None
        result = self.load_by_path(path, fps_override)
        if result is None:
            return None
        data, fps, settings = result
        expected_chars = "".join(chars or [])
        if settings.get("chars") != expected_chars:
            return None
        return data, fps


# ============ data/ 扫描 ============

def scan_data_groups():
    """扫描 data/，返回 [(group_dir, videos, audios), ...]。

    - 每个子文件夹自成一组；
    - data/ 根目录下的散文件归为"根目录"一组（兼容旧布局）。
    """
    DATA_DIR.mkdir(exist_ok=True)
    groups = []
    root_videos, root_audios = [], []

    for p in sorted(DATA_DIR.iterdir()):
        if p.is_dir():
            vids = sorted(f for f in p.iterdir()
                          if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
            auds = sorted(f for f in p.iterdir()
                          if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
            if vids or auds:
                groups.append((p, vids, auds))
        elif p.is_file():
            if p.suffix.lower() in VIDEO_EXTS:
                root_videos.append(p)
            elif p.suffix.lower() in AUDIO_EXTS:
                root_audios.append(p)

    if root_videos or root_audios:
        groups.append((DATA_DIR, sorted(root_videos), sorted(root_audios)))

    return groups


def _choose(items, kind):
    if not items:
        return None
    if len(items) == 1:
        print(f"自动选择{kind}: {items[0].name}")
        return items[0]

    print(f"\n发现多个{kind}，请选择:")
    for i, p in enumerate(items, 1):
        try:
            size_mb = p.stat().st_size / 1024 / 1024
            size_str = f"  ({size_mb:.1f} MB)"
        except OSError:
            size_str = ""
        print(f"  [{i}] {p.name}{size_str}")

    while True:
        s = input(f"序号 (1-{len(items)}, 回车默认 1): ").strip()
        if not s:
            return items[0]
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]
        print("输入无效，请重试。")

def _choose_group(groups):
    """从作品列表里选一个，返回 (folder, videos, audios)。"""
    if len(groups) == 1:
        folder, vids, auds = groups[0]
        print(f"自动选择作品: {folder.name}/")
        return groups[0]

    print("\n发现多个作品，请选择:")
    for i, (folder, vids, auds) in enumerate(groups, 1):
        if folder == DATA_DIR:
            label = "根目录"
        else:
            label = folder.name + "/"
        v_str = vids[0].name if vids else "无视频"
        a_str = auds[0].name if auds else "无音频"
        extra = ""
        if len(vids) > 1 or len(auds) > 1:
            extra = f"  [视频 {len(vids)}, 音频 {len(auds)}]"
        print(f"  [{i}] {label}  ({v_str} + {a_str}){extra}")

    while True:
        s = input(f"序号 (1-{len(groups)}, 回车默认 1): ").strip()
        if not s:
            return groups[0]
        if s.isdigit() and 1 <= int(s) <= len(groups):
            return groups[int(s) - 1]
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

def _load_audio_safe(audio_path):
    """尝试加载音频。成功返回 True，失败打印提示返回 False。

    pygame.mixer 只保证支持 wav / ogg / mp3，其他格式视 SDL_mixer
    的编译情况而定，常见 m4a / flac / aac / wma 都可能加载失败。
    """
    if audio_path is None:
        return False
    try:
        pygame.mixer.init()
        pygame.mixer.music.load(str(audio_path))
        return True
    except pygame.error as e:
        print(f"\n[音频加载失败] {audio_path.name}")
        print(f"  原因: {e}")
        print(f"  pygame 只保证支持 wav / ogg / mp3；"
              f"m4a / flac / aac / wma 等格式视 SDL_mixer 编译情况而定。")
        print(f"  将静音播放。如需音效，请把音频转为 mp3 或 ogg。")
        return False

def play(video_data, fps, bgm_path, delay=0.4):
    global _terminal_dirty

    # 在进入 curses 之前先试加载音频，失败能看到提示
    audio_ok = _load_audio_safe(bgm_path)
    if not audio_ok:
        bgm_path = None
        if delay > 0:
            # 给用户时间看提示
            try:
                input("按回车继续（静音播放）...")
            except EOFError:
                pass

    stdscr = curses.initscr()
    _terminal_dirty = True
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

        if bgm_path is not None:
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
        _restore_terminal()


# ============ 命令行 ============

def parse_args():
    p = argparse.ArgumentParser(
        description="在终端播放 ASCII 字符画视频",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    simple = p.add_argument_group("简单选项")
    simple.add_argument("-v", "--video", help="视频路径，不填则自动扫描 data/")
    simple.add_argument("-b", "--bgm", help="背景音乐路径，不填则自动扫描 data/")
    simple.add_argument("-W", "--width", type=int, help="字符画宽度")
    simple.add_argument("-H", "--height", type=int, help="字符画高度")
    simple.add_argument("-c", "--charset", metavar="NAME",
                        help=f"预设字符集，可选: {', '.join(convert.CHARSETS)}")
    simple.add_argument("--chars", metavar="STR",
                        help="自定义字符表（从密到疏），覆盖 --charset")
    simple.add_argument("-L", "--list-charsets", action="store_true",
                        help="列出预设字符集并退出")

    adv = p.add_argument_group("高级选项")
    adv.add_argument("-f", "--fps", type=float,
                     help="播放帧率，不填则用视频自带")
    adv.add_argument("-d", "--delay", type=float, default=0.4,
                     help="音乐开始后延迟多少秒才出画面")
    adv.add_argument("-i", "--invert", dest="invert", action="store_true",
                     default=None, help="强制反转（浅色终端）")
    adv.add_argument("--no-invert", dest="invert", action="store_false",
                     help="强制不反转（深色终端）")
    adv.add_argument("-r", "--rebuild", action="store_true",
                     help="忽略缓存，强制重新转换")
    adv.add_argument("--frame-step", type=int, default=1, metavar="N",
                     help="抽帧步长，N=2 表示隔一帧取一帧，转换时间减半")
    adv.add_argument("--interp", choices=list(convert.INTERP_MAP),
                     default=convert.DEFAULT_INTERP,
                     help="缩放插值方式")
    adv.add_argument("--threads", type=int,
                     help="OpenCV 线程数，弱 CPU 上设 1 可减少调度开销")
    adv.add_argument("--setup", action="store_true",
                     help="强制启动交互式设置向导")

    return p.parse_args()


# ============ 决策函数 ============

def ask_int(prompt, default, hint=""):
    suffix = f" {hint}" if hint else ""
    s = input(f"{prompt} (默认 {default}{suffix}): ").strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        print(f"输入无效，使用默认值 {default}")
        return default


def resolve_media(args):
    """返回 (video_path, audio_path)。audio_path 可能为 None。"""
    if args.video:
        video = Path(args.video)
        if not video.exists():
            sys.exit(f"视频不存在: {video}")
        audio = _resolve_audio_in(args, video.parent, video)
        return video, audio

    groups = scan_data_groups()
    valid = [g for g in groups if g[1]]
    if not valid:
        sys.exit(f"未在 {DATA_DIR} 找到视频文件，请把视频放入 data/作品名/")

    folder, videos, audios = _choose_group(valid)
    video = _choose(videos, "视频")
    audio = _resolve_audio_in(args, folder, video, audios)
    return video, audio


def _resolve_audio_in(args, folder, video, audios=None):
    """在指定文件夹里为视频找音频，找不到返回 None（静音播放）。

    优先级：--bgm > 同名 > 唯一 > 询问 > 无。
    """
    if args.bgm:
        p = Path(args.bgm)
        if not p.exists():
            sys.exit(f"音乐不存在: {p}")
        return p

    if audios is None:
        audios = sorted(f for f in folder.iterdir()
                        if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    if not audios:
        print("未找到音频，将静音播放")
        return None

    # 1) 同名优先
    for a in audios:
        if a.stem == video.stem:
            print(f"自动配对同名音频: {a.name}")
            return a

    # 2) 只有一个音频直接选
    if len(audios) == 1:
        print(f"自动选择音频: {audios[0].name}")
        return audios[0]

    # 3) 多个音频让用户选，也可以跳过
    print("\n发现多个音频：")
    for i, p in enumerate(audios, 1):
        print(f"  [{i}] {p.name}")
    print(f"  [0] 不使用音频（静音播放）")
    while True:
        s = input(f"序号 (0-{len(audios)}, 回车默认 1): ").strip()
        if not s:
            return audios[0]
        if s == "0":
            print("已选择静音播放")
            return None
        if s.isdigit() and 1 <= int(s) <= len(audios):
            return audios[int(s) - 1]
        print("输入无效，请重试。")


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


def resolve_charset(args, video_path, cache, need_interactive=False,
                    skip_cache=False):
    """返回 (charset_name, chars_list)。

    优先级：--chars > --charset > 缓存配置 > (交互询问) > 默认。
    need_interactive=True 时（首次转换）允许询问用户。
    skip_cache=True 时忽略缓存里的字符集（用于"重新设置"流程）。
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

    if not skip_cache:
        cfg = cache.load_config(video_path)
        cached_name = cfg.get("charset")
        cached_chars = cfg.get("chars")
        if cached_name and cached_chars and len(cached_chars) >= 2:
            if cached_name != convert.DEFAULT_CHARSET:
                print(f"沿用上次字符集: {cached_name} ({len(cached_chars)} 档)")
            return cached_name, list(cached_chars)

    if not need_interactive:
        return convert.get_chars(convert.DEFAULT_CHARSET)

    # 首次转换 / 重新设置：让用户选一次
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

def run_setup_wizard(args, video_path, cache, cfg):
    """交互式设置向导，返回 settings dict。"""
    print("\n=== 设置向导 ===")
    print("  [1] 简单设置 —— 只调分辨率、字符集")
    print("  [2] 高级设置 —— 额外可调帧率、抽帧、插值、线程")
    mode = input("选择 (回车默认 1): ").strip().lower()
    advanced = mode in ("2", "高级", "a", "advanced")

    s = {}

    # 分辨率：命令行 > 向导输入（默认填上次的值）
    default_w = cfg.get("width", convert.DEFAULT_WIDTH)
    default_h = cfg.get("height", convert.DEFAULT_HEIGHT)
    hint_w = "上次设置" if cfg.get("width") else "系统默认"
    hint_h = "上次设置" if cfg.get("height") else "系统默认"
    s["width"] = args.width or ask_int("字符画宽度", default_w, hint_w)
    s["height"] = args.height or ask_int("字符画高度", default_h, hint_h)

    # 字符集：跳过缓存，强制重新问
    name, chars = resolve_charset(args, video_path, cache,
                                  need_interactive=True, skip_cache=True)
    s["charset_name"] = name
    s["chars"] = chars

    # 帧率
    if args.fps is not None:
        s["fps"] = args.fps
    elif advanced:
        v = input("播放帧率 (回车使用视频自带): ").strip()
        s["fps"] = float(v) if v else None
    else:
        s["fps"] = None

    # 高级选项：默认沿用命令行/配置
    s["frame_step"] = args.frame_step
    s["interp"] = args.interp
    s["threads"] = args.threads

    if advanced:
        v = input(f"抽帧步长 (1=每帧都转，2=隔一帧，"
                  f"回车默认 {args.frame_step}): ").strip()
        if v.isdigit() and int(v) >= 1:
            s["frame_step"] = int(v)

        v = input(f"插值方式 {list(convert.INTERP_MAP)} "
                  f"(回车默认 {args.interp}): ").strip()
        if v in convert.INTERP_MAP:
            s["interp"] = v

        v = input(f"OpenCV 线程数 (回车默认 "
                  f"{args.threads if args.threads is not None else '自动'}): "
                  ).strip()
        if v.isdigit():
            s["threads"] = int(v)

    return s

def _read_cache_meta(path):
    """从缓存文件读取字符集预览和帧率。失败返回 ('', None)。"""
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return "", None

    s = payload.get("settings", {})
    chars = s.get("chars", "")
    if len(chars) <= 12:
        preview = chars
    else:
        preview = chars[:12] + "..."
    return preview, payload.get("video_fps")

def ask_choose_cache(caches, cfg, invert):
    matching = [(k, i, p) for k, i, p in caches if i["invert"] == invert]

    if not matching:
        other = [c for c in caches if c[1]["invert"] != invert]
        if other:
            want = "深色" if invert else "浅色"
            have = "浅色" if invert else "深色"
            print(f"\n当前是{want}终端，但该视频只有 {len(other)} 个"
                  f"{have}终端的缓存，无法直接沿用。")
            print(f"如需使用，请在{have}终端里运行，或输入 0 重新设置。")
        return None

    last_w = cfg.get("width")
    last_h = cfg.get("height")
    last_chars = cfg.get("chars", "")
    default_idx = 1

    print(f"\n发现该视频的 {len(matching)} 个可用缓存:")

    for i, (key, info, path) in enumerate(matching, 1):
        preview, fps = _read_cache_meta(path)
        is_last = (info["width"] == last_w
                   and info["height"] == last_h
                   and preview.startswith(last_chars[:12]) if last_chars else False)
        mark = "  ← 上次使用" if is_last else ""
        if is_last:
            default_idx = i

        # 字符集显示：custom 带预览
        if info["charset"] == "custom" and preview:
            charset_label = f"custom [{preview}]"
        elif info["charset"] == "custom":
            charset_label = "custom"
        else:
            charset_label = info["charset"]

        fps_str = f" {fps:.0f}fps" if fps else ""
        print(f"  [{i}] {info['width']}x{info['height']} | "
              f"{charset_label}{fps_str}{mark}")

    print(f"  [0] 重新设置")
    print("  (Ctrl+C 可随时中止)")

    while True:
        s = input(f"选择 (0-{len(matching)}, 回车默认 {default_idx}): ").strip()
        if not s:
            chosen = matching[default_idx - 1]
            return chosen[0], chosen[2]
        if s == "0":
            return None
        if s.isdigit() and 1 <= int(s) <= len(matching):
            chosen = matching[int(s) - 1]
            return chosen[0], chosen[2]
        print("输入无效，请重试。")

def ask_reuse_config(cfg):
    """检测到已有配置时询问是否沿用。返回 True=沿用，False=重新设置。"""
    print(f"\n检测到上次配置：{cfg['width']}x{cfg['height']} | "
          f"字符集 {cfg.get('charset', 'classic')} | "
          f"{cfg.get('fps', '?')} fps")
    ans = input("沿用上次设置？(回车=沿用，n=重新设置): ").strip().lower()
    return ans not in ("n", "no", "否")

# ============ 主流程 ============

def main():
    _startup_terminal_check()
    _install_terminal_guards()
    args = parse_args()

    if args.list_charsets:
        print("可用字符集:")
        for name, chars in convert.CHARSETS.items():
            preview = chars if len(chars) <= 40 else chars[:40] + "..."
            print(f"  {name:8s} [{len(chars):2d}档]  {preview}")
        return

    cache = Cache(CACHE_DIR, DATA_DIR)
    video_path, bgm_path = resolve_media(args)
    cfg = cache.load_config(video_path)
    invert = resolve_invert(args, video_path, cache)
    audio_name = bgm_path.name if bgm_path else ""

    # ---- 让用户从已有缓存里选一个 ----
    all_caches = cache.list_caches(video_path)
    chosen = None
    if all_caches and not args.rebuild and not args.setup:
        chosen = ask_choose_cache(all_caches, cfg, invert)

    if chosen is not None:
        key, data_path = chosen
        result = cache.load_by_path(data_path, args.fps)
        if result:
            video_data, fps, settings = result
            print(f"读取缓存: {data_path.name}")
            cache.save_config(
                video_path,
                width=settings.get("width"),
                height=settings.get("height"),
                fps=fps,
                invert=settings.get("invert", invert),
                audio=audio_name,
                charset=settings.get("charset", "classic"),
                chars=settings.get("chars", ""),
                frame_step=settings.get("frame_step", 1),
                interp=settings.get("interp", convert.DEFAULT_INTERP),
            )
            play(video_data, fps, bgm_path, args.delay)
            return
        print("缓存损坏，进入设置向导...")

    # ---- 重新设置 ----
    s = run_setup_wizard(args, video_path, cache, cfg)

    # 新设置恰好命中某个已有缓存 → 直接复用，不重新转换
    result = cache.load_data(video_path, s["width"], s["height"], invert,
                             s["charset_name"], s["chars"], s["fps"])
    if result is not None and not args.rebuild:
        video_data, fps = result
        print(f"新设置与已有缓存匹配，直接复用")
    else:
        cache_path = cache.data_path(video_path, s["width"], s["height"],
                                     invert, s["charset_name"], s["chars"])
        video_data, video_fps = convert.video_to_ascii(
            video_path, s["chars"], s["charset_name"],
            s["width"], s["height"], invert, cache_path,
            frame_step=s["frame_step"], interp=s["interp"],
            threads=s["threads"])
        fps = s["fps"] or video_fps
        wait_any_key("\n转换完成，按任意键继续...")

    cache.save_config(
        video_path,
        width=s["width"], height=s["height"], fps=fps,
        invert=invert, audio=audio_name,
        charset=s["charset_name"], chars="".join(s["chars"]),
        frame_step=s["frame_step"], interp=s["interp"],
    )
    play(video_data, fps, bgm_path, args.delay)

if __name__ == "__main__":
    main()