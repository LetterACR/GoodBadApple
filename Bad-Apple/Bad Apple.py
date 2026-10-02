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
import shutil
import signal
import unicodedata
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
    """停一下等按键（进入 curses 前的最后一句话，比如"按任意键开始播放…"）。

    非 TTY（管道、重定向、CI、测试）下拿不到 termios（`tcgetattr` 会直接抛
    `termios.error`），退化成读一行——不能让它把整条流程炸掉。
    """
    print(prompt, end="", flush=True)
    try:
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
    except Exception:
        try:
            input()
        except (EOFError, OSError, ValueError):
            pass
    print()


# ============ 交互界面 ============
#
# 所有选择都走同一个 _menu()：TTY 下是方向键菜单，非 TTY（管道、
# 重定向、测试）自动回退成编号输入，因此脚本里也能用。

def _is_tty():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def _dwidth(text):
    """终端显示宽度：中日韩全角字符占 2 列，len() 会算少。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def _dpad(text, width):
    return text + " " * max(0, width - _dwidth(text))


def _init_colors(stdscr):
    """把窗口背景钉成终端的默认前景/背景色，返回绘制用的基础属性。

    不少终端（尤其 IDE 内置的模拟终端）是 bce（背景色擦除）的：curses 画
    连续空格时会改用 ECH/EL，"填充色"取的是当前背景色。如果不显式把背景
    设成终端默认色，整片空格就可能被填成别的颜色 —— 表现就是播放时出现
    横贯画面的白条，而且只出现在"空格段"上，同行的字符却正常。

    这里的 except Exception 是有意的：要跨 ncurses / windows-curses 和
    不支持默认色的终端探测能力，拿不到就退回无色。
    """
    attr = curses.A_NORMAL
    try:
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, -1, -1)     # -1 = 终端默认色
            attr = curses.color_pair(1)
    except Exception:
        attr = curses.A_NORMAL

    for call in (lambda: stdscr.bkgd(" ", attr),      # 擦除/空格用这个背景
                 lambda: stdscr.attrset(attr)):
        try:
            call()
        except Exception:
            pass
    return attr


def _safe_addstr(win, y, x, text, attr=0):
    """写右下角会抛 curses.error，统一吞掉。"""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _fmt_size(path):
    try:
        return f"{path.stat().st_size / 1024 / 1024:.1f} MB"
    except OSError:
        return ""


def _charset_label(info, preview=""):
    """缓存里字符集的显示名：braille / custom 带参数预览。"""
    name = info.get("charset", "classic")
    if name in ("custom", convert.BRAILLE_CHARSET) and preview:
        return f"{name} [{preview}]"
    return name


def _menu_curses(stdscr, title, options, default, cancel, footer,
                 esc_value=None):
    try:
        curses.curs_set(0)
    except Exception:
        pass
    stdscr.keypad(True)
    base = _init_colors(stdscr)        # 空白格用终端默认背景
    idx = max(0, min(default, len(options) - 1))
    top = 0
    # 底部提示直接把 Esc 的落点写出来：有些屏的 cancel 不是"取消"而是
    # "重新设置"、"不使用音频"，笼统写 Esc 取消会骗人。
    hint = footer or ("↑↓ 选择   Enter 确认"
                      + (f"   Esc = {cancel}" if cancel else ""))
    # 说明列统一对齐到最长标签之后（按显示宽度，CJK 算 2 列）
    label_w = max(_dwidth(label) for label, _ in options)
    detail_col = 3 + label_w + 3

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        row = 0
        if title:
            _safe_addstr(stdscr, row, 0, title[:w - 1], base | curses.A_BOLD)
            row += 2
        avail = max(1, h - row - 2)

        if idx < top:
            top = idx
        elif idx >= top + avail:
            top = idx - avail + 1

        for n in range(top, min(len(options), top + avail)):
            label, detail = options[n]
            attr = base | (curses.A_REVERSE if n == idx else curses.A_NORMAL)
            prefix = " ▸ " if n == idx else "   "
            _safe_addstr(stdscr, row, 0, (prefix + label)[:w - 1], attr)
            if detail and detail_col < w - 1:
                _safe_addstr(stdscr, row, detail_col,
                             detail[:w - detail_col - 1], attr | curses.A_DIM)
            row += 1

        if len(options) > avail:      # 滚动指示
            _safe_addstr(stdscr, h - 2, 0,
                         f"   {idx + 1}/{len(options)}"[:w - 1],
                         base | curses.A_DIM)
        _safe_addstr(stdscr, h - 1, 0, hint[:w - 1], base | curses.A_DIM)
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            idx = (idx - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = (idx + 1) % len(options)
        elif ch == curses.KEY_HOME:
            idx = 0
        elif ch == curses.KEY_END:
            idx = len(options) - 1
        elif ch in (10, 13, curses.KEY_ENTER):
            return idx
        elif ch == 27:                       # Esc
            if cancel:
                return None
            if esc_value is not None:
                return esc_value
        elif ch in (ord("q"), ord("Q")):
            if cancel:
                return None
            if esc_value is not None:
                return esc_value
        elif curses.KEY_RESIZE == ch:
            continue
        elif ord("1") <= ch <= ord("9"):
            n = ch - ord("1")
            if n < len(options):
                return n


def _menu_plain(title, options, default, cancel, footer, esc_value=None):
    """非 TTY 回退：打印编号列表，读一行输入。

    esc_value 只对 Esc 有意义，编号菜单没有 Esc，忽略即可。
    """
    if title:
        print(f"\n{title}")
    for i, (label, detail) in enumerate(options, 1):
        suffix = f"   {detail}" if detail else ""
        print(f"  [{i}] {label}{suffix}")
    if cancel:
        print(f"  [0] {cancel}")

    lo = 0 if cancel else 1
    while True:
        s = input(f"序号 ({lo}-{len(options)}，回车默认 {default + 1}): ").strip()
        if not s:
            return default
        if s == "0" and cancel:
            return None
        if s.isdigit() and 1 <= int(s) <= len(options):
            return int(s) - 1
        print("输入无效，请重试。")


def _menu(title, options, default=0, cancel=None, footer=None, esc_value=None):
    """方向键单选菜单。

    options 是 [(标签, 说明), ...]；返回选中下标。
    cancel     非空时 Esc/q 返回 None，编号菜单里会多出一项 [0]。
    esc_value  Esc/q 直接返回这个下标，**不会**多出菜单项 —— 用于
               "Esc 等于确认某个动作"但又不想多一行的情况。
    只有一项且不可取消时不弹菜单，直接返回。
    """
    if not options:
        return None
    if len(options) == 1 and cancel is None:
        return 0
    if not _is_tty():
        return _menu_plain(title, options, default, cancel, footer, esc_value)

    # 菜单期间也置脏，这样 Ctrl+C 走信号处理器时能恢复终端；
    # 否则进程被直接杀掉，终端会卡在 curses 状态（无回显、备用屏）。
    global _terminal_dirty
    _terminal_dirty = True
    try:
        return curses.wrapper(_menu_curses, title, options, default,
                              cancel, footer, esc_value)
    except (curses.error, OSError):
        # 终端不支持 curses（TERM=dumb / 未设置）时退回编号菜单。
        # 故意只捕这两类：_menu_curses 自身的 bug 应该直接暴露出来。
        _restore_terminal()
        return _menu_plain(title, options, default, cancel, footer, esc_value)
    finally:
        _terminal_dirty = False
        _stty_sane()


def ask_text(prompt, default="", hint=""):
    """读一行文本；直接回车返回 default（调用方约定空串=保持不变）。

    只在 default 非空时才补 [回车=xxx]：否则会和 hint 里的
    "回车=视频自带" 之类打架，出现 "（回车=视频自带） [回车=空]" 这种怪提示。
    """
    suffix = f"（{hint}）" if hint else ""
    tail = f" [回车={default}]" if default != "" else ""
    s = input(f"{prompt}{suffix}{tail}: ").strip()
    return s if s else default


def ask_number(prompt, default, hint="", cast=int):
    s = ask_text(prompt, "", hint)
    if s == "":
        return default
    try:
        return cast(s)
    except (TypeError, ValueError):
        print(f"输入无效，保持 {default}")
        return default


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
        if charset_name in ("custom", convert.BRAILLE_CHARSET):
            # custom 的字符表、braille 的抖动参数（spec 串）都要进哈希，
            # 否则不同参数会撞进同一个缓存文件。
            h = hashlib.md5("".join(chars or []).encode("utf-8")).hexdigest()[:6]
            return f"_{charset_name}{h}"
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
        elif tag.startswith(convert.BRAILLE_CHARSET):
            charset_name = convert.BRAILLE_CHARSET
            charset_tag = tag                    # 如 braille_a1b2c3
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

    @staticmethod
    def _payload_mismatch(settings, chars, frame_step, interp):
        """缓存 payload 与当前设置的差异说明；完全一致返回 None。

        缓存文件名只编码 宽高 / 明暗 / 字符集，抽帧步长和插值方式**不在 key 里**
        （加进去会让现有 1.2 GB 缓存全部作废），所以命中之后必须回读 payload
        核对：不一致就当没命中，让调用方重新转换 —— 否则用户在设置屏把抽帧从
        1 改成 2、或把插值从 nearest 改成 area，会被一个同名的旧缓存静默顶掉，
        看起来像"设置没生效"。
        """
        if settings.get("chars") != "".join(chars or []):
            return "字符表不同"
        if frame_step is not None \
                and int(settings.get("frame_step", 1)) != int(frame_step):
            return (f"抽帧步长是 {settings.get('frame_step', 1)}，"
                    f"当前是 {frame_step}")
        if interp is not None and settings.get("interp", None) not in (None, interp):
            return f"插值方式是 {settings.get('interp')}，当前是 {interp}"
        return None

    def load_data(self, video_path, width, height, invert,
                  charset_name="classic", chars=None, fps_override=None,
                  frame_step=None, interp=None):
        """按设置精确查找缓存（用于向导结束后再查一次）→ (data, fps) 或 None。"""
        path = self.data_path(video_path, width, height, invert,
                              charset_name, chars)
        if not path.exists():
            return None
        result = self.load_by_path(path, fps_override)
        if result is None:
            return None
        data, fps, settings = result
        why = self._payload_mismatch(settings, chars, frame_step, interp)
        if why:
            print(f"已有缓存{why}，重新转换。")
            return None
        return data, fps

    def load_opposite(self, video_path, width, height, invert,
                      charset_name="classic", chars=None, fps_override=None,
                      frame_step=None, interp=None):
        """没命中时，试试**反明暗**的同名缓存，取反后直接用。

        深浅两种终端下渲染的唯一差别就是"最后整体取反"（见
        `convert.invert_frames`），所以把对面那份缓存逐字符翻过来，和重新解码
        渲染一遍得到的结果**完全相同**，但只需读一遍 pickle：

            180x60 深色缓存 --取反--> 180x60 浅色帧（不用碰视频）

        条件与 `load_data` 一致（字符表 / 抽帧 / 插值必须对得上）；不满足、
        ASCII 自定义字符表有重复字符、或对面压根没缓存时返回 None，由调用方
        走正常的转换流程。返回 (data, fps)。
        """
        path = self.data_path(video_path, width, height, not invert,
                              charset_name, chars)
        if not path.exists():
            return None
        result = self.load_by_path(path, fps_override)
        if result is None:
            return None
        data, fps, settings = result
        why = self._payload_mismatch(settings, chars, frame_step, interp)
        if why:
            print(f"反明暗的缓存{why}（缓存 key 里不含抽帧/插值），改为从视频转换。")
            return None
        flipped = convert.invert_frames(data, charset_name, chars)
        if flipped is None:
            print("自定义字符表里有重复字符，无法可靠取反，改为从视频转换。")
            return None
        print(f"复用反明暗缓存取反：{term_label(not invert)} → "
              f"{term_label(invert)}（{len(flipped)} 帧，不重新解码视频）")
        return flipped, fps

    def generate_inverted(self, src_path, video_path):
        """把某个缓存取反，另存成相反明暗的同名缓存。成功返回 True。

        给缓存菜单里的「⇄ 取反生成另一版」用：不需要知道抽帧/插值等设置，
        源缓存里怎么配的就照抄，只把明暗那一位翻过来。
        """
        info = self._parse_key(Path(src_path).stem)
        if info is None:
            return False
        try:
            with open(src_path, "rb") as f:
                payload = pickle.load(f)
        except Exception as e:
            print(f"缓存读取失败: {e}")
            return False

        settings = payload.get("settings", {})
        charset_name = settings.get("charset", info["charset"])
        chars = settings.get("chars", "")
        flipped = convert.invert_frames(payload["video_data"], charset_name, chars)
        if flipped is None:
            print(f"{Path(src_path).name}: 自定义字符表里有重复字符，跳过")
            return False

        target_invert = not info["invert"]
        dst = self.data_path(video_path, info["width"], info["height"],
                             target_invert, charset_name, chars)
        if not dst.exists():
            convert.save_frames(dst, flipped, dict(settings, invert=target_invert),
                                payload.get("video_fps", 30.0))
        print(f"{Path(src_path).name} → {dst.name}"
              f"（{len(flipped)} 帧，不重新解码视频）")
        return True


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


def _choose(items, kind, cancel=None):
    """从同类文件里选一个；只有一个时直接返回。

    cancel 非空时 Esc/q 返回 None，让调用方退回上一层菜单；只有一个候选时
    不弹菜单，也就没有"返回"可言（直接返回那一个）。
    """
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    opts = [(p.name, _fmt_size(p)) for p in items]
    i = _menu(f"选择{kind}", opts, cancel=cancel)
    return None if i is None else items[i]


def _choose_group(groups):
    """从作品列表里选一个，返回 (folder, videos, audios)。

    这是第一屏，没有上一层可退，所以不传 cancel：Esc/q 在这屏没有动作
    （想中止就直接 Ctrl+C，信号处理器会恢复终端）。
    """
    if len(groups) == 1:
        return groups[0]

    opts = []
    for folder, vids, auds in groups:
        label = "根目录" if folder == DATA_DIR else folder.name + "/"
        detail = f"{len(vids)} 视频 · {len(auds)} 音频"
        opts.append((label, detail))
    i = _menu("选择作品", opts, default=0)
    return groups[i if i is not None else 0]


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
        had_audio = bgm_path is not None      # None = 本来就没配音频，不必停顿
        bgm_path = None
        # 一定要停一下等回车：下面 curses 一清屏，上面那段失败原因就没了。
        # 早先只在 delay > 0 时才等，于是 -d 0 会把提示直接刷掉。
        if had_audio:
            try:
                input("按回车继续（静音播放）...")
            except (EOFError, OSError):
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
    normal = _init_colors(stdscr)      # 空白格必须用终端默认背景填充
    stdscr.clear()
    stdscr.refresh()

    def fit():
        """按当前窗口尺寸算可画的行列数。"""
        max_y, max_x = stdscr.getmaxyx()
        rows = min(len(video_data[0]) if video_data else 0, max(0, max_y - 1))
        return rows, max(0, max_x - 1)

    try:
        rows, cols = fit()

        if bgm_path is not None:
            pygame.mixer.music.play()
            time.sleep(delay)

        timer = FpsTimer(fps)
        for frame_data in video_data:
            stdscr.nodelay(True)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                break
            if ch == curses.KEY_RESIZE:
                # 窗口变了就重算行列并整屏重画：erase() 会把旧内容清干净
                rows, cols = fit()

            stdscr.erase()
            for i in range(rows):
                line = (frame_data[i][:cols].ljust(cols)
                        if i < len(frame_data) else " " * cols)
                try:
                    stdscr.addstr(i, 0, line, normal)
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
                        help=f"预设字符集，可选: {', '.join(convert.CHARSETS)}, "
                             f"{convert.BRAILLE_CHARSET}")
    simple.add_argument("--chars", metavar="STR",
                        help="自定义字符表（从密到疏），覆盖 --charset")
    simple.add_argument("--braille", action="store_true",
                        help="Braille 点阵模式（等价于 -c braille，"
                             "黑白下的极限分辨率）")
    simple.add_argument("-L", "--list-charsets", action="store_true",
                        help="列出预设字符集并退出")

    adv = p.add_argument_group("高级选项")
    adv.add_argument("-f", "--fps", type=float,
                     help="播放帧率，不填则用视频自带")
    adv.add_argument("-d", "--delay", type=float, default=0.4,
                     help="音乐开始后延迟多少秒才出画面")
    # 两个参数共用 dest="invert"，默认值 None = 自动检测终端背景。
    # --no-invert 必须显式写 SUPPRESS，否则 store_false 会自带
    # default=True，既污染帮助里的 "(default: True)"，也容易误导读者。
    adv.add_argument("-i", "--invert", dest="invert", action="store_true",
                     default=None, help="强制反转（深色终端，亮像素用密字符）")
    adv.add_argument("--no-invert", dest="invert", action="store_false",
                     default=argparse.SUPPRESS,
                     help="强制不反转（浅色终端）")
    adv.add_argument("-r", "--rebuild", action="store_true",
                     help="忽略缓存，强制重新转换")
    # frame_step / interp 的默认值用 None：这样才能区分"用户显式指定"和
    # "没写"，从而让设置屏回退到 config.json 里上次用的值（优先级见 README）。
    adv.add_argument("--frame-step", type=int, default=None, metavar="N",
                     help="抽帧步长，N=2 表示隔一帧取一帧，转换时间减半；"
                          "不填则沿用该视频上次的设置（默认 1）")
    adv.add_argument("--interp", choices=list(convert.INTERP_MAP),
                     default=None,
                     help="缩放插值方式；不填则沿用该视频上次的设置"
                          f"（默认 {convert.DEFAULT_INTERP}）")
    adv.add_argument("--braille-dither", choices=list(convert.DITHER_MODES),
                     help="Braille 抖动方式，仅在 Braille 模式下生效")
    adv.add_argument("--braille-gamma", type=float, metavar="G",
                     help="Braille gamma 校正（1.0 中性，>1 提亮暗部）")
    adv.add_argument("--threads", type=int,
                     help="OpenCV 线程数，弱 CPU 上设 1 可减少调度开销")
    adv.add_argument("--setup", action="store_true",
                     help="强制进入设置屏，跳过缓存菜单")

    return p.parse_args()


# ============ 决策函数 ============

def resolve_media(args):
    """返回 (video_path, audio_path)。audio_path 可能为 None。

    两层循环对应"作品 → 视频"两级菜单，每层的菜单都能 Esc 退回上一层：
    选视频时 Esc 回作品菜单；选音频时 Esc 回它真正的上一屏（视频菜单，若
    视频只有一个则回作品菜单；一层菜单都没弹过就没得退，Esc 仍是"静音"）。
    """
    if args.video:
        video = Path(args.video)
        if not video.exists():
            sys.exit(f"视频不存在: {video}")
        audio, _ = _resolve_audio_in(args, video.parent, video)
        return video, audio

    groups = scan_data_groups()
    valid = [g for g in groups if g[1]]
    if not valid:
        sys.exit(f"未在 {DATA_DIR} 找到视频文件，请把视频放入 data/作品名/")

    while True:                                   # 作品层
        folder, videos, audios = _choose_group(valid)

        while True:                               # 视频层
            video = _choose(videos, "视频",
                            cancel="← 返回选作品" if len(valid) > 1 else None)
            if video is None:                     # Esc：回作品菜单
                break

            # 音频菜单的"上一屏"到底是谁，取决于前面哪几个菜单真的弹过
            if len(videos) > 1:
                back_hint = "选视频"
            elif len(valid) > 1:
                back_hint = "选作品"
            else:
                back_hint = None              # 一层菜单都没弹过，没得退

            audio, back = _resolve_audio_in(args, folder, video, audios,
                                            back=back_hint)
            if not back:
                return video, audio
            if back_hint == "选视频":
                continue                      # 回视频菜单
            break                             # 回作品菜单


def _resolve_audio_in(args, folder, video, audios=None, back=None):
    """在指定文件夹里为视频找音频。返回 (audio_path, back)。

    优先级：--bgm > 同名 > 唯一 > 询问 > 无；audio_path 为 None 表示静音播放。
    back=True 表示用户在菜单里选了"返回上一步"，由调用方决定退回哪一屏。

    back 非空（有上一屏可退）时，"不使用音频"必须降级成普通菜单项，把 Esc
    让给"返回"；否则 Esc 同时意味着"静音"和"返回"，只能二选一。
    """
    if args.bgm:
        p = Path(args.bgm)
        if not p.exists():
            sys.exit(f"音乐不存在: {p}")
        return p, False

    if audios is None:
        audios = sorted(f for f in folder.iterdir()
                        if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    if not audios:
        return None, False

    # 1) 同名优先；2) 只有一个就直接用
    for a in audios:
        if a.stem == video.stem:
            return a, False
    if len(audios) == 1:
        return audios[0], False

    # 3) 多个音频让用户选，也可以跳过
    opts = [(a.name, _fmt_size(a)) for a in audios]
    if back:
        opts.append(("不使用音频（静音播放）", ""))
        i = _menu("选择音频", opts, cancel=f"← 返回{back}（不改动）")
        if i is None:
            return None, True
        return (None, False) if i == len(opts) - 1 else (audios[i], False)

    # 第一屏：没有上一层可退，Esc 就是"不要音频"
    i = _menu("选择音频", opts, cancel="不使用音频（静音播放）")
    return (None, False) if i is None else (audios[i], False)


def term_label(invert):
    return "深色终端" if invert else "浅色终端"


# 终端背景是「环境」属性而非「视频」属性，所以记在全局偏好里。
# 早先按每个视频的 config.json 回退是错的：一旦某个视频存错，就会
# 自我强化，换终端也永远回不来。
def _prefs_path():
    return CACHE_DIR / "_prefs.json"


def load_prefs():
    try:
        return json.loads(_prefs_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs(**fields):
    data = load_prefs()
    data.update(fields)
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _prefs_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def ask_terminal_bg():
    """探测不到背景时问一次，并记进全局偏好。返回 invert。"""
    i = _menu("探测不到终端背景，请选一个（只问这一次，之后可随时改）",
              [("深色终端", "黑底浅字，亮像素上墨"),
               ("浅色终端", "白底深字，亮像素留白")])
    invert = (i != 1)
    save_prefs(invert=invert, manual=True)
    print(f"已记住：{term_label(invert)}（改动：cache/_prefs.json）")
    return invert


def set_terminal_bg(invert, manual=True):
    """记下终端背景。manual=True 表示用户手动指定，之后不再被自动探测覆盖。"""
    save_prefs(invert=invert, manual=manual)
    return invert


def detect_invert(args):
    """决定 invert，返回 (invert, source)。

    source ∈ cli / manual / probe / saved / default。

    **必须在任何 curses 菜单之前调用。** 菜单会切到备用屏并改动 termios，
    之后再发 OSC 11 查询往往等不到终端应答，探测就会失败。
    """
    if args.invert is not None:
        return args.invert, "cli"

    prefs = load_prefs()

    # 用户手动指定过就以它为准。否则每次自动探测都会把它冲掉 ——
    # 在 IDE 内置的模拟终端上探测结果可能是错的，那样手动改就永远不生效。
    if prefs.get("manual") and isinstance(prefs.get("invert"), bool):
        return prefs["invert"], "manual"

    is_light = detect_light_bg()
    if is_light is None:
        # 有些终端首次应答慢，在干净的终端状态上重试一次
        is_light = detect_light_bg()
    _stty_sane()

    if is_light is not None:
        invert = not is_light
        save_prefs(invert=invert, manual=False)   # 记下来，下次探测失败也能用
        return invert, "probe"

    saved = prefs.get("invert")
    if isinstance(saved, bool):
        return saved, "saved"

    # 一无所知：交给调用方问一次（不要瞎猜然后写死）
    return None, "default"


def _parse_braille_spec(spec):
    """从 "dither|g<gamma>" 解析出 (dither, gamma)，坏值退回默认。"""
    dither, gamma = convert.DEFAULT_DITHER, convert.DEFAULT_GAMMA
    for part in str(spec or "").split("|"):
        part = part.strip()
        if part in convert.DITHER_MODES:
            dither = part
        elif part.startswith("g"):
            try:
                gamma = float(part[1:])
            except ValueError:
                pass
    return dither, gamma


def _braille_spec_from_args(args, ask=False, cur=None):
    """构造 Braille 的 spec 串 "dither|g<gamma>"。

    ask=True 表示用户刚刚在界面上显式选了 braille，此时用 cur（当前正在用的
    spec）当默认值，并且**不看命令行** —— 否则 `--braille` / `--braille-gamma`
    会把用户在设置屏里的选择一直压住，braille 参数等于改不动。

    抖动菜单里 Esc/q = **整套 braille 参数保持不动**（连 gamma 也不再追问），
    对应"返回上一层不改动"的统一语义。
    """
    cur_dither, cur_gamma = _parse_braille_spec(cur)

    if ask:
        modes = list(convert.DITHER_MODES)
        i = _menu("Braille 抖动方式",
                  [("bayer", "有序抖动，快，长视频首选"),
                   ("floyd", "误差扩散，层次最好，转换慢"),
                   ("none", "硬阈值，高对比度片源最锐利")],
                  default=modes.index(cur_dither),
                  cancel="保持当前（不改动）")
        if i is None:
            print(f"Braille 参数保持不变（抖动: {cur_dither}, "
                  f"gamma: {cur_gamma:g}）")
            return f"{cur_dither}|g{cur_gamma:g}"
        dither = modes[i]

        v = ask_text("Braille gamma", f"{cur_gamma:g}", ">1 提亮暗部")
        try:
            gamma = float(v)
        except (TypeError, ValueError):
            print(f"输入无效，保持 {cur_gamma:g}")
            gamma = cur_gamma
        if gamma <= 0:
            print(f"gamma 必须为正数，用默认 {convert.DEFAULT_GAMMA:g}")
            gamma = convert.DEFAULT_GAMMA
    else:
        dither = getattr(args, "braille_dither", None) or cur_dither
        gamma = getattr(args, "braille_gamma", None)
        if gamma is None:
            gamma = cur_gamma
        if not gamma or gamma <= 0:
            gamma = convert.DEFAULT_GAMMA

    print(f"使用 Braille 点阵（抖动: {dither}, gamma: {gamma:g}）")
    return f"{dither}|g{gamma:g}"


def pick_charset(cur_name=None, cur_chars=None):
    """交互式选字符集，返回 (name, chars)。

    cur_* 是当前设置：既决定默认高亮项，也是 Braille 参数的初值 ——
    否则在设置屏里反复进来，刚设好的抖动/gamma 会被忘掉，要重设一遍。
    Esc/q 走 cancel 分支，直接返回当前设置（"保持不动"），编号菜单里
    对应多出来的 [0] 项。注意不能用 esc_value=default：那会把 Esc 当成
    "选了默认项"，若当前正是 braille/custom 就会又追问一遍参数。
    """
    names = list(convert.CHARSETS)
    opts = []
    for nm in names:
        cs = convert.CHARSETS[nm]
        preview = cs if len(cs) <= 22 else cs[:22] + "…"
        opts.append((nm, f"{len(cs):2d} 档  {preview}"))
    opts.append((convert.BRAILLE_CHARSET, "点阵  2×4 点/字符，黑白极限分辨率"))
    opts.append(("自定义字符表…", "手动粘贴一串字符"))

    if cur_name in names:
        default = names.index(cur_name)
    elif cur_name == convert.BRAILLE_CHARSET:
        default = len(names)
    elif cur_name == "custom":
        default = len(names) + 1
    else:
        default = names.index(convert.DEFAULT_CHARSET)

    i = _menu("选择字符集", opts, default=default,
              cancel="保持当前（不改动）")
    if i is None:                             # Esc/q：保持当前设置
        if cur_name == convert.BRAILLE_CHARSET and cur_chars:
            # braille 的 cur_chars 是 spec 串 "dither|g<gamma>"，整串保留，
            # 不能 list() 拆成单个字符（那正是下面分支对 custom 的做法）
            return convert.BRAILLE_CHARSET, cur_chars
        if cur_name == "custom" and cur_chars and len(cur_chars) >= 2:
            return "custom", list(cur_chars)
        if cur_name in names:
            return convert.get_chars(cur_name)
        return convert.get_chars(convert.DEFAULT_CHARSET)

    if i == len(names):                       # braille
        spec = cur_chars if cur_name == convert.BRAILLE_CHARSET else None
        return convert.BRAILLE_CHARSET, _braille_spec_from_args(None, ask=True,
                                                                cur=spec)
    if i == len(names) + 1:                   # 自定义
        # 不 strip：字符表最后一位通常就是空格（最疏档），去掉会丢一档
        s = input("粘贴字符表（从密到疏，直接回车取消）: ")
        if len(s) >= 2:
            print(f"使用自定义字符集 ({len(s)} 档)")
            return "custom", list(s)
        print("输入太短，已取消自定义")
        if cur_name:
            return cur_name, cur_chars
        return convert.get_chars(convert.DEFAULT_CHARSET)
    return convert.get_chars(names[i])


def resolve_charset(args, video_path, cache):
    """按优先级决定用哪个字符集，返回 (charset_name, chars)。

    对 ASCII 字符集，chars 是 list[char]；对 braille，chars 是 spec 串
    "dither|g<gamma>"（渲染参数同样要进缓存 key，所以要跟着一起传）。

    优先级：--chars > --braille / --charset > 缓存里上次用的 > 默认。
    需要"问用户"的场合一律走 pick_charset()，这里不做交互。
    """
    if args.chars:
        chars = list(args.chars)
        if len(chars) < 2:
            sys.exit("自定义字符集至少需要 2 个字符")
        print(f"使用自定义字符集 ({len(chars)} 档)")
        return "custom", chars

    if args.braille or args.charset == convert.BRAILLE_CHARSET:
        return convert.BRAILLE_CHARSET, _braille_spec_from_args(args)

    if args.charset:
        try:
            return convert.get_chars(args.charset)
        except ValueError as e:
            sys.exit(str(e))

    cfg = cache.load_config(video_path)
    cached_name = cfg.get("charset")
    cached_chars = cfg.get("chars")
    if cached_name and cached_chars and len(cached_chars) >= 2:
        if cached_name == convert.BRAILLE_CHARSET:
            print(f"沿用上次字符集: braille ({cached_chars})")
            return convert.BRAILLE_CHARSET, cached_chars
        if cached_name != convert.DEFAULT_CHARSET:
            print(f"沿用上次字符集: {cached_name} ({len(cached_chars)} 档)")
        return cached_name, list(cached_chars)

    return convert.get_chars(convert.DEFAULT_CHARSET)


_VIDEO_SIZE_CACHE = {}


def _video_size(video_path):
    """原片像素尺寸，带记忆（设置屏可能来回进出好几轮）。失败返回 None。"""
    key = str(video_path)
    if key not in _VIDEO_SIZE_CACHE:
        _VIDEO_SIZE_CACHE[key] = convert.video_size(video_path)
    return _VIDEO_SIZE_CACHE[key]


def _auto_size(width, video_path):
    """按原片比例 + 当前终端大小算默认字符画尺寸，返回 (cols, rows)。

    不这么算的话，4:3 的片源套 90x30、16:9 的套 90x30，都会拉成"宽版普京"：
    字符格是 1:2 的，行列比必须跟着原片比例走才对得上。
    """
    size = _video_size(video_path)
    if not size:
        return width, convert.DEFAULT_HEIGHT
    try:
        term = shutil.get_terminal_size()
        term_cols, term_rows = term.columns, term.lines
    except Exception:
        term_cols = term_rows = 0
    return convert.fit_size(width, size[0], size[1], term_cols, term_rows)


def run_setup_wizard(args, video_path, cache, cfg, invert=False, back=None):
    """设置界面：一屏列出全部参数，回车直接开始，选一项即可改。

    只在真正需要转换时进入（没有可用缓存，或用户选了"重新设置"）。
    返回参数字典；**返回 None 表示用户选了 `← 返回…`**，调用方应该退回去。

    back 非空（例如 "选缓存"）时菜单底部多一行 `← 返回选缓存（不改动）`，
    Esc/q 也落在这一行——设置屏是从缓存菜单进来的，就得能退回去。
    back 为空（首次运行没缓存、`--setup`）时没有上一屏，Esc 仍是「开始转换」。

    每一项的初值都按同一套优先级取：**命令行 > 上次用的（config.json）> 默认**。
    早先抽帧/插值/线程只读命令行，于是从缓存菜单选"重新设置"进来时，这几项
    显示的是命令行默认值而不是上次用的值，和分辨率/字符集的体验不一致。

    帧率是唯一的例外：config.json 里的 `fps` 存的是**实际生效的帧率**（可能
    已经被抽帧除过），不能反推用户当时选的是"视频自带"还是某个数字，所以另存
    一个 `fps_override` 字段来记住意图（旧配置没有这个字段，退化为"视频自带"）。

    分辨率同理：高度是"按原片比例算出来的"还是"用户手填的"要分开记
    （`height_auto`），否则要么用户手填的尺寸被自动换算冲掉，要么老配置里
    那个不对比例的 90x30 永远修不回来。
    """
    width = args.width or cfg.get("width") or convert.DEFAULT_WIDTH
    height_auto = False
    if args.height:
        height = args.height
    elif cfg.get("height") and not cfg.get("height_auto", True):
        height = int(cfg["height"])          # 上次手填过，尊重它
    else:
        # 高度没被明确指定（或上次就是算出来的）→ 按原片比例算
        width, height = _auto_size(width, video_path)
        height_auto = True
    name, chars = resolve_charset(args, video_path, cache)
    fps = args.fps if args.fps is not None else cfg.get("fps_override")
    frame_step = args.frame_step if args.frame_step is not None \
        else cfg.get("frame_step") or 1
    interp = args.interp if args.interp is not None \
        else cfg.get("interp") or convert.DEFAULT_INTERP
    threads = args.threads if args.threads is not None else cfg.get("threads")
    # config.json 是用户可见、可手改的，读进来先做一次类型兜底
    if fps is not None:
        try:
            fps = float(fps)
        except (TypeError, ValueError):
            fps = None
    try:
        frame_step = max(1, int(frame_step))
    except (TypeError, ValueError):
        frame_step = 1
    if interp not in convert.INTERP_MAP:
        interp = convert.DEFAULT_INTERP
    if threads is not None:
        try:
            threads = int(threads)
        except (TypeError, ValueError):
            threads = None

    def charset_desc():
        if name == convert.BRAILLE_CHARSET:
            return f"braille  {chars}"
        return f"{name}  ({len(chars)} 档)" if name != "custom" \
            else f"custom  ({len(chars)} 档)"

    def fps_desc():
        return "视频自带" if fps is None else f"{fps:g} fps"

    def bg_desc():
        prefs = load_prefs()
        tag = "手动" if prefs.get("manual") else "自动"
        return f"{term_label(invert)}（{tag}）"

    while True:
        fields = [
            ("分辨率", f"{width} x {height}"),
            ("字符集", charset_desc()),
            ("终端背景", bg_desc()),
            ("帧率", fps_desc()),
            ("抽帧步长", str(frame_step)),
            ("插值方式", interp),
            ("OpenCV 线程", "自动" if threads is None else str(threads)),
        ]
        label_w = max(_dwidth(k) for k, _ in fields)
        opts = [(_dpad(k, label_w + 4) + v, "") for k, v in fields]
        start_at = len(opts)
        # 不带 ▸：高亮项自带 " ▸ " 前缀，写进标签会渲染成 " ▸ ▸ 开始转换"
        opts.append(("开始转换", ""))
        back_at = None
        if back:
            # 有上一屏（缓存菜单）可退时多加一行，Esc 让给它
            back_at = len(opts)
            opts.append((f"← 返回{back}（不改动）", ""))
        # esc_value 让 Esc/q 有个明确落点：能退就退，不能退就是「开始转换」。
        # 用 esc_value 而不是 cancel，是为了不在编号菜单里多出一行重复的 [0]。
        esc_at = back_at if back_at is not None else start_at
        footer = ("↑↓ 选择   Enter 修改/开始   Esc "
                  + (f"返回{back}" if back_at is not None else "直接开始"))
        i = _menu(f"设置  ·  {term_label(invert)}", opts,
                  default=start_at, esc_value=esc_at, footer=footer)

        if i is None or i == start_at:
            break
        if back_at is not None and i == back_at:
            return None                       # 返回上一屏（缓存菜单）

        if i == 0:
            v = ask_text("分辨率 (宽x高)", "", f"当前 {width}x{height}，回车不变")
            a, _, b = v.lower().partition("x")
            if a.strip().isdigit() and b.strip().isdigit():
                width, height = int(a), int(b)
                height_auto = False      # 用户手填了，之后不再自动按比例算
            elif v:
                print(f"输入无效，保持 {width}x{height}")
        elif i == 1:
            # 直接用 pick_charset：不走 resolve_charset 的命令行优先链，
            # 否则 --braille / -c 会把用户在这里的选择一直压住。
            name, chars = pick_charset(name, chars)
        elif i == 2:
            # 手动选择优先级高于自动探测，之后不会再被覆盖
            prefs = load_prefs()
            auto = not prefs.get("manual")
            j = _menu("终端背景",
                      [("自动探测", f"目前为{term_label(invert)}"),
                       ("深色终端", "黑底浅字，亮像素上墨"),
                       ("浅色终端", "白底深字，亮像素留白")],
                      default=0 if auto else (1 if invert else 2),
                      cancel="返回（不改动）")
            if j is None:
                continue
            if j == 0:
                invert, _ = detect_invert(args)
                if invert is None:
                    invert = prefs.get("invert", False)
                set_terminal_bg(invert, manual=False)
                print(f"终端背景改为自动探测：{term_label(invert)}")
            else:
                invert = set_terminal_bg(j == 1, manual=True)
                print(f"终端背景已固定为：{term_label(invert)}（手动）")
        elif i == 3:
            v = ask_text("帧率", "", "回车=视频自带")
            if v:
                try:
                    fps = float(v)
                except ValueError:
                    print(f"输入无效，保持 {fps_desc()}")
            else:
                fps = None
        elif i == 4:
            frame_step = max(1, ask_number("抽帧步长", frame_step,
                                           "1=每帧都转，回车不变"))
        elif i == 5:
            modes = list(convert.INTERP_MAP)
            j = _menu("插值方式", [(m, "") for m in modes],
                      default=modes.index(interp) if interp in modes else 0,
                      cancel="返回（不改动）")
            if j is not None:
                interp = modes[j]
        elif i == 6:
            v = ask_text("OpenCV 线程数", "", "回车=自动")
            if v.isdigit():
                threads = int(v)
            elif v == "":
                threads = None
            else:
                print(f"输入无效，保持 {threads if threads else '自动'}")

    return {
        "width": width, "height": height, "height_auto": height_auto,
        "charset_name": name, "chars": chars,
        "invert": invert,
        "fps": fps, "frame_step": frame_step,
        "interp": interp, "threads": threads,
    }

def _read_cache_meta(path):
    """从缓存文件读取 (字符集预览, 帧率, 完整字符表)。失败返回 ('', None, '')。

    完整字符表用来和 config.json 里的 chars 精确比对，判断哪条是"上次使用"：
    只比较 12 字符的预览会误判 —— 两个自定义字符表或两套 braille 参数，
    前 12 个字符相同就撞上了。
    """
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return "", None, ""

    s = payload.get("settings", {})
    chars = s.get("chars", "")
    preview = chars if len(chars) <= 12 else chars[:12] + "..."
    return preview, payload.get("video_fps"), chars

def _cache_menu_opts(matching, cfg):
    """构造缓存菜单项，返回 (opts, 默认下标)。"""
    last_w = cfg.get("width")
    last_h = cfg.get("height")
    last_chars = cfg.get("chars", "")
    opts, default_idx = [], 0

    for info, path in ((i, p) for _, i, p in matching):
        preview, fps, chars = _read_cache_meta(path)
        label = (f"{info['width']}x{info['height']}  "
                 f"{_charset_label(info, preview)}")
        is_last = (bool(last_chars)
                   and info["width"] == last_w and info["height"] == last_h
                   and chars == last_chars)
        if is_last:
            default_idx = len(opts)
        # 「上次使用」放进说明列，标签才能对齐成规整一列
        bits = [f"{fps:.0f} fps"] if fps else []
        if is_last:
            bits.append("← 上次使用")
        opts.append((label, "   ".join(bits)))
    return opts, default_idx


def ask_choose_cache(caches, cfg, invert, source="probe", cache=None,
                     video_path=None):
    """从缓存里挑一个，返回 (chosen, invert)；chosen 为 None 表示重新设置。

    菜单里**始终**带一个「切换终端背景」项：探测不准或探测不到时，用户在这里
    一键换到另一种背景，选择会存进全局偏好。当前背景一条缓存都没有时也照样
    显示——否则只有浅色缓存的视频会变成死路，用户被迫走一遍设置屏才能换背景。

    当前背景一条缓存都没有、而另一种背景有时，再给一行
    「⇄ 取反生成另一版」：把对面那些缓存逐字符取反存成当前明暗的缓存（无损，
    几秒，不重新解码视频）。传了 cache/video_path 才提供这一项。
    """
    if not caches:
        return None, invert

    can_generate = cache is not None and video_path is not None

    while True:
        matching = [(k, i, p) for k, i, p in caches if i["invert"] == invert]
        other = "浅色" if invert else "深色"
        mine = "深色" if invert else "浅色"
        opposite = [(k, i, p) for k, i, p in caches if i["invert"] != invert]

        opts, default_idx = _cache_menu_opts(matching, cfg)
        generate_at = None
        if not matching and opposite and can_generate:
            generate_at = len(opts)
            opts.append((f"⇄ 用{other}缓存取反生成{mine}版",
                         f"把 {len(opposite)} 个{other}缓存翻过来，秒级，"
                         f"不重新解码视频"))
            default_idx = generate_at      # 没有可选项时，高亮"能用的那个动作"

        flip_at = len(opts)
        if matching:
            detail = f"另一种背景有 {len(opposite)} 个缓存" if opposite \
                else "按另一种背景重新筛选缓存"
        else:
            detail = (f"切换到{other}终端后可用 {len(opposite)} 个缓存"
                      if opposite else "另一种背景也没有缓存")
            if generate_at is None:
                default_idx = flip_at
        opts.append((f"⇄ 切换为{other}终端", detail))

        title = f"选择缓存  ·  {term_label(invert)}"
        if source in ("saved", "default", "probe"):
            title += "（自动判断，可切换）" if source == "probe" \
                else "（探测不到，可切换）"
        elif source == "manual":
            title += "（手动指定）"
        if not matching:
            title += f"  ·  无{term_label(invert)}缓存"

        i = _menu(title, opts, default=default_idx, cancel="重新设置")
        if i is None:
            return None, invert
        if generate_at is not None and i == generate_at:
            done = sum(1 for _, _, p in opposite
                       if cache.generate_inverted(p, video_path))
            if not done:
                can_generate = False       # 一个都没成，别再来一遍
            caches = cache.list_caches(video_path)   # 重新扫描，新缓存才会出现
            continue
        if i == flip_at:
            # 手动切换后不再被自动探测覆盖
            invert = set_terminal_bg(not invert, manual=True)
            source = "manual"
            continue
        return (matching[i][0], matching[i][2]), invert

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
        print(f"  {convert.BRAILLE_CHARSET:8s} [点阵]   "
              f"Braille 点阵 + 抖动，2×4 点/字符，")
        print(f"  {'':8s}         需要终端字体支持 U+2800–U+28FF")
        return

    # 终端背景必须在这里探测：下面的 resolve_media 会开 curses 菜单，
    # 用过 curses 之后 OSC 11 查询就不可靠了（详见 detect_invert）。
    invert, invert_src = detect_invert(args)
    if invert is None:
        # 探测不到又没存过：问一次并记进全局偏好，不瞎猜
        invert = ask_terminal_bg()
        invert_src = "asked"

    cache = Cache(CACHE_DIR, DATA_DIR)
    video_path, bgm_path = resolve_media(args)
    cfg = cache.load_config(video_path)
    audio_name = bgm_path.name if bgm_path else ""

    # ---- 缓存菜单 ⇄ 设置屏：两边都能退回对方 ----
    all_caches = cache.list_caches(video_path)
    # --rebuild / --setup / 一条缓存都没有时不显示缓存菜单，设置屏也就没有上一屏
    menu_available = bool(all_caches) and not args.rebuild and not args.setup
    while True:
        chosen = None
        if menu_available:
            chosen, invert = ask_choose_cache(all_caches, cfg, invert, invert_src,
                                              cache=cache, video_path=video_path)

        if chosen is not None:
            key, data_path = chosen
            result = cache.load_by_path(data_path, args.fps)
            if result:
                video_data, fps, settings = result
                # fps_override 是"用户想用多少帧率"的意图，缓存里没有这个信息，
                # 只能沿用 config 里的旧值，别在这一步把它丢掉
                cache.save_config(
                    video_path,
                    width=settings.get("width"),
                    height=settings.get("height"),
                    height_auto=cfg.get("height_auto", True),
                    fps=fps,
                    fps_override=cfg.get("fps_override"),
                    invert=settings.get("invert", invert),
                    audio=audio_name,
                    charset=settings.get("charset", "classic"),
                    chars=settings.get("chars", ""),
                    frame_step=settings.get("frame_step", 1),
                    interp=settings.get("interp", convert.DEFAULT_INTERP),
                    threads=cfg.get("threads"),
                )
                play(video_data, fps, bgm_path, args.delay)
                return
            print("缓存损坏，重新转换")

        s = run_setup_wizard(args, video_path, cache, cfg, invert,
                             back="选缓存" if menu_available else None)
        if s is not None:
            break
        # 设置屏里选了「← 返回选缓存」：回到缓存菜单重新挑
    invert = s["invert"]           # 设置屏里可能刚切换过终端背景
    cache_path = cache.data_path(video_path, s["width"], s["height"],
                                 invert, s["charset_name"], s["chars"])

    # 新设置命中已有缓存 → 直接复用；
    # 只有反明暗的同名缓存 → 取反复用（不重新解码视频，见 Cache.load_opposite）；
    # 两个都没有 → 老老实实从视频转换
    # （抽帧/插值不在缓存 key 里，上面两个查询都会额外核对 payload）
    result = cache.load_data(video_path, s["width"], s["height"], invert,
                             s["charset_name"], s["chars"], s["fps"],
                             frame_step=s["frame_step"], interp=s["interp"])
    flipped = False
    if result is None and not args.rebuild:
        result = cache.load_opposite(video_path, s["width"], s["height"], invert,
                                     s["charset_name"], s["chars"], s["fps"],
                                     frame_step=s["frame_step"],
                                     interp=s["interp"])
        flipped = result is not None

    if result is not None and not args.rebuild:
        video_data, fps = result
        if flipped:
            # 取反结果存到当前明暗的 key 下，下次就直接命中，不用再翻一遍
            convert.save_frames(cache_path, video_data, {
                "source": str(video_path),
                "width": s["width"], "height": s["height"], "invert": invert,
                "charset": s["charset_name"], "chars": "".join(s["chars"]),
                "frame_step": s["frame_step"], "interp": s["interp"],
            }, fps)
            wait_any_key("\n按任意键开始播放…")
    else:
        video_data, video_fps = convert.video_to_ascii(
            video_path, s["chars"], s["charset_name"],
            s["width"], s["height"], invert, cache_path,
            frame_step=s["frame_step"], interp=s["interp"],
            threads=s["threads"])
        fps = s["fps"] or video_fps
        wait_any_key("\n按任意键开始播放…")

    cache.save_config(
        video_path,
        width=s["width"], height=s["height"], height_auto=s["height_auto"],
        fps=fps,
        fps_override=s["fps"],
        invert=invert, audio=audio_name,
        charset=s["charset_name"], chars="".join(s["chars"]),
        frame_step=s["frame_step"], interp=s["interp"],
        threads=s["threads"],
    )
    play(video_data, fps, bgm_path, args.delay)

if __name__ == "__main__":
    main()