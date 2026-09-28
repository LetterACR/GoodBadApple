# Bad Apple!! 终端字符画播放器

在终端里播放《Bad Apple!!》以及其他任意视频的 ASCII 字符画版本，支持背景音乐同步、自动缓存、终端背景适配和自定义字符集。

---

## 简介

本项目基于 [CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple) 的原始实现，并参考了 [CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple) 中的性能优化思路，进行了一系列工程化改进。

核心流程：

1. 用 OpenCV 逐帧读取视频，将每帧灰度值映射到 ASCII 字符表；
2. 在终端用 `curses` 逐帧刷新显示；
3. 用 `pygame.mixer` 同步播放背景音乐；
4. 转换结果缓存到本地，下次播放直接读取，无需重复转换。

本项目改编内容大部分使用 AI 编写

---

## 特性

- **任意视频**：把视频和音频丢进 `data/`，运行时自动扫描并让你选择。
- **智能缓存**：按视频、分辨率、背景方向、字符集分开保存，互不覆盖；换回旧配置秒开。
- **终端背景自动适配**：通过 `COLORFGBG` 和 OSC 11 查询检测终端背景色，自动决定是否反转字符映射，白底黑底都能正常显示。
- **自定义字符集**：内置 `classic` / `simple` / `block` / `minimal` 四套预设，支持任意自定义字符表。
- **进度条**：转换时显示百分比和柱状进度条。
- **精确帧计时**：自写 `FpsTimer`，基于 `perf_counter()` 补偿漂移，避免音画不同步。
- **缓存加速**：用 `pickle` 存储帧数据，加载速度比 `.py` 缓存快，文件也更小。
- **跨平台**：Windows / Linux / macOS 均可运行，任意键继续、`q` / `ESC` 退出。

---

## 安装依赖

```bash
pip install pygame opencv-python
```

> 原项目还需要 `matplotlib`，本改进版仅在 `convert.py` 的 `__main__` 调试分支中用到，正常播放不需要。

---

## 快速开始

1. 把视频和音频文件放入 `data/` 目录：

```
data/
├── badapple.flv
├── badapple.mp3
├── other.mp4
└── other.mp3
```

支持的视频格式：`.flv .mp4 .avi .mkv .mov .webm .wmv`  
支持的音频格式：`.mp3 .wav .ogg .m4a .flac .aac`

2. 运行：

```bash
python "Bad Apple.py"
```

3. 按提示选择视频、音频，首次转换时输入分辨率、帧率和字符集；转换完成后按任意键播放。

---

## 目录结构

```
Bad-Apple/
├── Bad Apple.py          # 主程序：扫描、缓存、播放
├── convert.py            # 视频转 ASCII、进度条、pickle 写出
├── data/                 # 放视频和音频
│   ├── badapple.flv
│   └── badapple.mp3
└── cache/                # 自动生成，不要手动改
    ├── badapple_90x30_dark.pkl
    ├── badapple_90x30_light.pkl
    ├── badapple_90x30_dark_simple.pkl
    └── badapple.config.json
```

---

## 命令行参数

```
python "Bad Apple.py" [选项]

选项:
  -v, --video PATH       视频路径，不填则自动扫描 data/
  -b, --bgm PATH         背景音乐路径，不填则自动扫描 data/
  -W, --width INT        字符画宽度（每行字符数）
  -H, --height INT       字符画高度（行数）
  -f, --fps FLOAT        播放帧率，不填则使用视频自带帧率
  -d, --delay FLOAT      音乐开始后延迟多少秒才出画面（默认 0.4）
  -i, --invert           强制反转（浅色背景终端）
      --no-invert        强制不反转（深色背景终端）
  -r, --rebuild          忽略缓存，强制重新转换
  -c, --charset NAME     选择预设字符集
      --chars STR        直接指定字符表（从密到疏）
  -L, --list-charsets    列出所有预设字符集并退出
```

### 常用示例

```bash
# 自动扫描 data/，交互选择
python "Bad Apple.py"

# 指定视频、音频、分辨率
python "Bad Apple.py" -v data/other.mp4 -b data/other.mp3 -W 120 -H 40

# 换字符集
python "Bad Apple.py" --charset simple
python "Bad Apple.py" --chars "●◕◔○· "

# 查看预设
python "Bad Apple.py" --list-charsets

# 强制重建当前配置的缓存
python "Bad Apple.py" --rebuild
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

## 缓存机制

缓存分为两层：

- **帧数据**：`cache/{视频名}_{宽}x{高}_{dark|light}[_{字符集}].pkl`
  - `dark` / `light` 对应终端深色/浅色背景；
  - 预设字符集 `classic` 不加后缀，其他预设用名字，自定义字符集用字符表 MD5 前 6 位。
- **视频配置**：`cache/{视频名}.config.json`
  - 记录上次使用的分辨率、帧率、音频文件名、反转设置、字符集。

命中条件：`settings` 中 `source` / `width` / `height` / `invert` / `chars` 全部一致。  
只要换了视频、分辨率、背景或字符集，就会自动重新转换，**不会覆盖其他配置的缓存**。

---

## 与原项目的关系

本项目基于 [CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple) 的原始代码，并参考了 [CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple) 的优化经验。

### 原项目

- 只支持固定的 `data/1.flv` 和 `data/bgm.mp3`；
- 缓存文件 `video_data.py` 只有一份，换视频要手动删除；
- 没有终端背景检测，白底终端显示反色；
- 播放计时用 `count * FRAME_RATE`，长时间播放会累积误差。

### 本改进版新增

1. **自动扫描 `data/`**：按扩展名识别视频和音频，多个候选时列出菜单让用户选择。
2. **智能缓存管理**：按视频、分辨率、背景方向、字符集分开保存，换回旧配置无需重复转换。
3. **终端背景自动适配**：`COLORFGBG` + OSC 11 查询，自动决定是否反转字符映射，支持 `--invert` / `--no-invert` 手动覆盖。
4. **自定义字符集**：四套内置预设 + 任意自定义字符表，缓存自动区分。
5. **转换进度条**：显示百分比、柱状进度、已处理帧数。
6. **pickle 缓存**：替代 `.py` 缓存，加载更快，文件更小。
7. **`FpsTimer` 精确计时**：基于 `perf_counter()` 的补偿式计时，长时间播放不会漂移。
8. **跨平台任意键继续**：Windows 用 `msvcrt.getch()`，Linux/macOS 用 `tty.setraw`，不再依赖回车。
9. **播放细节优化**：每帧 `erase()` + `ljust` 填满整行，消除高速刷新时的灰色残留；支持 `q` / `ESC` 退出。
10. **结构化重构**：`Cache` 类集中管理缓存，`resolve_*` 系列函数负责各项决策，`main()` 扁平清晰。

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

**Q：想清空缓存重新转换？**  
A：删除 `cache/` 目录，或运行 `python "Bad Apple.py" --rebuild` 强制重建当前配置。

---

## 致谢

- 原项目：[CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple)
- 优化参考：[CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple)
- 原曲：ZUN / Alstroemeria Records
- 字符画转换思路来自开源社区，感谢所有贡献者。

---

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。

本项目基于 [CallMeToProgram/Bad-Apple](https://github.com/CallMeToProgram/Bad-Apple)
和 [CalvinLoke/bad-apple](https://github.com/CalvinLoke/bad-apple) 改进，
原项目未附带许可证，仅用于学习交流。视频、音频素材版权归各自作者所有。