# Bad Apple!! 终端字符画播放器

在终端里播放《Bad Apple!!》以及任意视频的 ASCII 字符画版本，支持背景音乐同步、自动缓存、终端背景适配和自定义字符集。

最新版本更新：2026.9.30

---

## 简介

本项目基于 [CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple) 的原始实现，并参考了 [CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple) 的性能优化思路，进行了一系列工程化改进。

核心流程：

1. 用 OpenCV 逐帧读取视频，将每帧灰度值映射到 ASCII 字符表；
2. 在终端用 `curses` 逐帧刷新显示；
3. 用 `pygame.mixer` 同步播放背景音乐；
4. 转换结果缓存到本地，下次播放直接读取，无需重复转换。

本项目改编内容大部分使用 AI 编写

---

## 特性

- **任意视频**：把视频和音频放进 `data/作品名/`，运行时自动扫描并让你选择。
- **音频可选**：视频没音频也能播，自动静音；同名音频自动配对，不会串到别的作品的音乐。
- **终端背景自动适配**：通过 `COLORFGBG` 和 OSC 11 查询检测终端背景色，自动决定是否反转字符映射，白底黑底都能正常显示。
- **智能缓存**：每个视频独立缓存目录，按分辨率、背景方向、字符集分开保存；多个缓存时列出菜单让你选，换回旧配置秒开。
- **自定义字符集**：内置 `classic` / `simple` / `block` / `minimal` 四套预设，支持任意自定义字符表。
- **简单/高级设置**：交互式向导分两档，新手只调分辨率字符集，进阶可调抽帧、插值、线程。
- **弱机器友好**：向量化转换、抽帧加速、OpenCV 线程控制，机房老电脑也能跑。
- **终端保护**：信号处理和 `atexit` 兜底，异常退出也不会把终端留在 raw 模式。
- **精确帧计时**：自写 `FpsTimer`，基于 `perf_counter()` 补偿漂移，长时间播放不累积误差。
- **进度条**：转换时显示百分比、柱状进度、已处理帧数。
- **跨平台**：Windows / Linux / macOS 均可运行，任意键继续、`q` / `ESC` 退出。

---

## 安装依赖

```bash
pip install pygame opencv-python numpy
```

---

## 快速开始

### 目录结构

`data/` 下每个子文件夹 = 一个作品，里面放该作品的视频和音频：

```
data/
├── badapple/
│   ├── badapple.flv
│   └── badapple.mp3
├── rickroll/
│   ├── rickroll.mp4
│   └── rickroll.mp3
└── touhou/
    ├── stage1.mp4      ← 可以共用下面那个音频
    ├── stage2.mp4
    └── bgm.mp3
```

也兼容**平铺**布局（文件直接放在 `data/` 根目录），会自动归为"根目录"一组。

支持的视频格式：`.flv .mp4 .avi .mkv .mov .webm .wmv`  
支持的音频格式：`.mp3 .wav .ogg .m4a .flac .aac`

> **提示**：`pygame.mixer` 只保证支持 wav / ogg / mp3。m4a / flac / aac 等格式视 SDL_mixer 编译情况而定，加载失败时程序会给出提示并自动静音，不会崩溃。想要稳定播放，建议把音频统一转成 mp3。

### 运行

```bash
python "Bad Apple.py"
```

按提示选择作品、视频，首次转换时输入分辨率、帧率和字符集；转换完成后按任意键播放。

---

## 目录结构

```
Bad-Apple/
├── Bad Apple.py          # 主程序：扫描、缓存、播放
├── convert.py            # 视频转 ASCII、进度条、pickle 写出
├── data/                 # 放视频和音频
│   ├── badapple/
│   │   ├── badapple.flv
│   │   └── badapple.mp3
│   └── rickroll/
│       ├── rickroll.mp4
│       └── rickroll.mp3
└── cache/                # 自动生成，不要手动改
    ├── badapple/
    │   ├── config.json
    │   ├── 90x30_dark.pkl
    │   ├── 90x30_light.pkl
    │   └── 90x30_dark_simple.pkl
    └── rickroll/
        ├── config.json
        └── 90x30_dark.pkl
```

---

## 命令行参数

### 简单选项

| 参数 | 说明 |
|---|---|
| `-v, --video PATH` | 视频路径，不填则自动扫描 `data/` |
| `-b, --bgm PATH` | 背景音乐路径，不填则自动扫描 |
| `-W, --width INT` | 字符画宽度（每行字符数） |
| `-H, --height INT` | 字符画高度（行数） |
| `-c, --charset NAME` | 选择预设字符集 |
| `--chars STR` | 直接指定字符表（从密到疏） |
| `-L, --list-charsets` | 列出所有预设字符集并退出 |

### 高级选项

| 参数 | 说明 |
|---|---|
| `-f, --fps FLOAT` | 播放帧率，不填则用视频自带 |
| `-d, --delay FLOAT` | 音乐开始后延迟多少秒才出画面（默认 0.4） |
| `-i, --invert` | 强制反转（浅色背景终端） |
| `--no-invert` | 强制不反转（深色背景终端） |
| `-r, --rebuild` | 忽略缓存，强制重新转换 |
| `--frame-step N` | 抽帧步长，N=2 表示隔一帧取一帧 |
| `--interp NAME` | 缩放插值：`nearest` / `area` / `linear` / `cubic` |
| `--threads INT` | OpenCV 线程数，弱 CPU 上设 1 |
| `--setup` | 强制启动交互式设置向导 |

### 常用示例

```bash
# 自动扫描 data/，交互选择
python "Bad Apple.py"

# 指定视频和音频
python "Bad Apple.py" -v data/other/other.mp4 -b data/other/other.mp3

# 换字符集
python "Bad Apple.py" --charset simple
python "Bad Apple.py" --chars "●◕◔○· "

# 弱机器快速转换
python "Bad Apple.py" -W 60 -H 20 -c simple --frame-step 2 --threads 1

# 强制重建缓存
python "Bad Apple.py" --rebuild

# 查看预设字符集
python "Bad Apple.py" --list-charsets
```

---

## 字符集

内置四套预设，按视觉密度从高到低排列：

| 名称 | 档数 | 字符表 |
|---|---|---|
| `classic` | 62 | `$@B%8&WM#*oahkbdpqwmZO0QLCJUYXzcvunxrjft/\|()1{}[]?-_+~<>i!lI;:,\"^`'. ` |
| `simple` | 10 | `@%#*+=-:. ` |
| `block` | 5 | `█▓▒░ ` |
| `minimal` | 4 | `@#. ` |

自定义字符表时，请按**从密到疏**的顺序排列：

```bash
python "Bad Apple.py" --chars "@#. "
python "Bad Apple.py" --chars "██▓▒░ "
```

首次转换时也会列出预设并允许你直接粘贴自定义字符表，之后自动沿用，不会重复询问。

---

## 设置向导

首次转换（或加 `--setup`）会启动交互式向导：

```
=== 设置向导 ===
  [1] 简单设置 —— 只调分辨率、字符集
  [2] 高级设置 —— 额外可调帧率、抽帧、插值、线程
选择 (回车默认 1): 2
字符画宽度 (默认 90 上次设置): 
字符画高度 (默认 30 上次设置): 
...
抽帧步长 (1=每帧都转，2=隔一帧，回车默认 1): 2
插值方式 ['nearest', 'area', 'linear', 'cubic'] (回车默认 nearest): 
OpenCV 线程数 (回车默认 自动): 1
```

- **简单模式**：只问分辨率、字符集，其他用默认。
- **高级模式**：追加帧率、抽帧、插值、线程。
- 任何命令行已给出的项不会再问。
- 已有配置的默认值会标注"上次设置"。

---

## 缓存机制

### 缓存结构

```
cache/
└── <视频相对 data/ 的路径>/
    ├── config.json             # 上次使用的设置
    └── <宽>x<高>_<dark|light>[_{字符集}].pkl
```

- 同名不同目录的视频通过**相对 `data/` 的路径**区分：  
  `data/set1/video.mp4` → `cache/set1__video/`  
  `data/set2/video.mp4` → `cache/set2__video/`
- `dark` / `light` 对应终端深色/浅色背景；
- 预设字符集 `classic` 不加后缀，其他预设用名字，自定义用字符表 MD5 前 6 位。

### 多缓存选择

同一视频存在多个缓存时，会列出菜单：

```
发现该视频的 3 个可用缓存:
  [1] 90x30 | custom [@#. ] 30fps  ← 上次使用
  [2] 90x30 | custom [●◕◔○· ] 30fps
  [3] 60x20 | simple 15fps
  [0] 重新设置
  (Ctrl+C 可随时中止)
选择 (0-3, 回车默认 1): 
```

- 列表按**最近使用时间倒序**，回车即用上次的。
- 显示分辨率、字符集预览、帧率。
- 输入 `0` 进设置向导。
- 如果当前终端背景与已有缓存不匹配，会明确提示。

### 缓存复用

- 向导结束会先按新设置查一遍缓存，**命中则直接复用**，不重新转换。
- 缓存路径完全由设置决定，**相同设置永远落到同一个 `.pkl`**，不会产生重复文件。

---

## 与原项目的关系

### 原项目

- 只支持固定的 `data/1.flv` 和 `data/bgm.mp3`；
- 缓存文件 `video_data.py` 只有一份，换视频要手动删除；
- 没有终端背景检测，白底终端显示反色；
- 播放计时用 `count * FRAME_RATE`，长时间播放会累积误差。

### 本改进版新增

**工程结构**

1. **data/ 目录自动扫描**：按扩展名识别视频和音频，多候选时列出菜单。
2. **一作品一文件夹**：每个作品独立配置，同名不同目录互不干扰。
3. **音频可选**：无音频静音播放；同名自动配对；加载失败有提示。

**缓存与管理**

4. **智能缓存管理**：每个视频独立子目录，多分辨率/背景/字符集互不覆盖。
5. **多缓存选择菜单**：列出所有缓存，显示分辨率、字符集预览、帧率、最近使用。
6. **缓存复用**：向导结束先查缓存，命中直接播放，不重新转换。
7. **pickle 缓存**：替代 `.py` 缓存，加载更快，文件更小。

**终端适配**

8. **终端背景自动适配**：`COLORFGBG` + OSC 11 查询，自动决定反转。
9. **终端恢复保护**：信号处理 + `atexit` + 启动自检，异常退出不留在 raw 模式。
10. **跨平台任意键继续**：Windows 用 `msvcrt.getch()`，Linux/macOS 用 `tty.setraw`。

**性能**

11. **向量化转换**：`AsciiRenderer` 用 numpy + OpenCV 替代逐像素 Python 循环，**10~30 倍加速**。
12. **抽帧加速**：`--frame-step 2` 隔一帧取一帧，转换时间减半。
13. **OpenCV 线程控制**：`--threads 1` 在弱 CPU 上减少调度开销。
14. **`FpsTimer` 精确计时**：基于 `perf_counter()` 的补偿式计时，长时间播放不漂移。
15. **进度条**：显示百分比、柱状进度、已处理帧数。

**字符集**

16. **四套内置预设** + **任意自定义字符表**，缓存自动区分。
17. **简单/高级设置向导**：交互式配置，默认值来自上次。

**播放细节**

18. **每帧 `erase()` + `ljust` 填满整行**，消除高速刷新时的灰色残留。
19. **`q` / `ESC` 退出**，随时中断。

---

## 常见问题

**Q：转换时终端卡住，回车只显示 `^M`？**  
A：这是 OSC 11 查询后终端状态未完全恢复导致的。代码中已在 `_query_osc11` 的 `finally` 中恢复终端属性并清空输入缓冲区，若仍有问题可执行 `stty sane` 手动恢复。

**Q：白底终端显示反色？**  
A：程序会自动检测并反转；如果检测失败，用 `--invert` 强制反转，`--no-invert` 强制不反转。

**Q：字符画错位或显示不全？**  
A：请先把终端窗口拉大到至少 `宽度 × 高度`（例如 90×30 就至少 90 列 30 行）。播放前会读取终端尺寸并裁剪，不会崩溃。

**Q：`block` 字符集显示错位？**  
A：部分终端把 `█▓▒░` 当作宽度 2 的字符。遇到这种情况请换回 `classic` 或 `simple`，或自定义时避开 CJK 宽字符。

**Q：m4a / flac 音频播放不了？**  
A：`pygame.mixer` 只保证支持 wav / ogg / mp3。程序会给出提示并自动静音。建议用 `ffmpeg -i bgm.m4a -c:a libmp3lame -q:a 2 bgm.mp3` 转成 mp3。

**Q：转换特别慢？**  
A：优先做这三件事：① 用 `-W 60 -H 20` 降分辨率；② 加 `--frame-step 2` 抽帧；③ 加 `--threads 1` 减少调度开销。或者在家用性能好的机器转好，把 `cache/` 一起拷到机房，直接读缓存播放。

**Q：想清空缓存重新转换？**  
A：删除 `cache/` 目录，或运行 `python "Bad Apple.py" -r` 强制重建当前配置。

---

## 致谢

- 原项目：[CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple)
- 优化参考：[CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple)
- 原曲：ZUN / Alstroemeria Records
- 字符画转换思路来自开源社区，感谢所有贡献者。

---

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。

本项目基于 [CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple) 和 [CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple) 改进，原项目未附带许可证，仅用于学习交流。视频、音频素材版权归各自作者所有，请勿用于商业用途。
