# dsh-wayland-use

给 DeepSeek Harness 用的 **Wayland 原生桌面控制插件**。不走 xdotool / uinput / X11，
直接用 Mutter 的 `RemoteDesktop` + `ScreenCast` 两个 D-Bus 会话读写桌面，所以点击、
按键、截图在纯 Wayland 会话下是真的生效，而不是只动 XWayland 的光标。

## 为什么它比 cua-driver-native 快

关键差别是**常驻**：后端是一个 Python 进程，Mutter 会话和 GStreamer 管道从头到尾
只建一次（约 300ms），之后每一步都在毫秒级。

| 操作 | 耗时 |
| --- | --- |
| `wayland_status` | 0.1ms |
| `wayland_screenshot`（首次，含建会话） | ~230ms |
| `wayland_screenshot`（之后，取缓存帧） | **2ms** |
| 操作后截图（等到新帧） | ~47ms |
| `wayland_key` | 1–2ms |
| `wayland_click_text`（含 OCR） | ~170–190ms |
| `wayland_act` 批量 3 步 + 截图 | ~600ms |

截图之所以能到 2ms：PipeWire 是**画面变化驱动**的，静止时不出帧，所以缓存帧就是
当前画面；插件给每次输入动作打时间戳，帧比动作新就直接回缓存，不用重抓。

## 截图

![桌面截图](assets/screenshot-desktop.png)

![OCR 带坐标读屏](assets/screenshot-read.png)

下面这张是 `wayland_read` 的产物：图上每个框都是 OCR 认出的文字，框左上角的
`文字 @x,y` 就是可以直接喂给 `wayland_act` 的 `click` 的逻辑坐标。

## 安装

```sh
dsh plugin add https://github.com/Ganglith/dsh-wayland-use
```

或手动装进 profile（`~/.dsh/profiles/<profile>/`）三处：

1. 把本包放进 `node_modules/dsh-wayland-use`（或做成符号链接，改源码立即生效）
2. `package.json` 的 `dependencies` 加 `"dsh-wayland-use"`，`dsh.profile.bundles` 也加一条
3. `cordis.patch.yml` 加 `- id: wayland-use` + `name: dsh-wayland-use`

DSH 的 HMR 会自动热载新的 patch，不用重启。**唯一的例外**：`lib/daemon.py` 是子进程，
改完要 `pkill -f "daemon[.]py"`，下次调用时它会自己重新拉起。

## 工具

| 工具 | 作用 |
| --- | --- |
| `wayland_screenshot` | 截图。`region` 局部、`scale` 缩放、`maxWidth` 限宽 |
| `wayland_read` | OCR 读出屏幕上的文字**带坐标**，`contains` 过滤 |
| `wayland_windows` | 列窗口标题 + 矩形 + 焦点标记（AT-SPI） |
| `wayland_act` | **主力**：一次调用跑一串动作，只回一张最终截图 |
| `wayland_click_text` | 按文字找并点击（不用自己猜坐标）；找不到自动翻页，`window` 指定翻哪个窗口，`acceptShare` 决定授权框按哪一边 |
| `wayland_dialogs` | 查/关挡在最前面的屏幕共享授权弹窗（`close: true`；`accept` 默认 true 按「共享」放行，设 false 才按「取消」） |
| `wayland_key` | 按组合键，如 `ctrl+a`、`super+Return` |
| `wayland_launch` | 启动程序 |
| `wayland_status` | 自检；`reset: true` 重建会话 |

`wayland_act` 的 step 支持：`click` `clickText` `type` `key` `move` `scroll`
`wait` `waitText` `launch` `sleep`，每步可带 `settleMs`。能用批量就别逐步来 ——
一次往返比十次往返快得多，这也是「笨」和「快」的真正差距。

### 按文字点击怎么定位

同一行被 OCR 读成一条之后，整条的中心常常落在行中间某个字上，点下去就是「点不准」。
所以命中之后还要在行内再定位一次：拿 needle 在这行的字块列表里做子串匹配（不中再用
difflib 比单词和相邻两词），把落点换成**目标词自己的中心**（返回里的 `x`/`y`，内部叫
`ax`/`ay`）。`"LINE 019 alpha19 bravo19 ..."` 整行中心 631.8，`alpha19` 的中心是 532.8 ——
差一百像素，正好点空。

匹配也不只看「包含」：needle 以数字结尾时，匹配位置后面不能再跟数字；needle 里的数字
组要和候选里的数字组逐组比相似度。否则 `alpha7 bravo7` 会撞上 `alpha74 bravo74`，
`alpha120` 会撞上 `alpha12`，翻页时一路「命中」在错的行上。数字比较**先比值再比字符**
（`088` 与 `88` 算同一个数，`1590` 与 `150` 只有 0.857 不算）。

needle 形如 `LINE 150`（首词是 `line` 或纯数字）时还有两道**行号硬闸** —— 这是
「点错行」唯一可靠的堵法：

- **行首数字必须是 needle 的首个数字**。OCR 最常犯的错是把上一行行尾的残渣串到下一行
  开头，凑出 `156 aLphal156 bravo156 charLie150 …` 这种脏行：它同时含 156 和 150，
  逐词匹配怎么看都像第 150 行，点下去就是点错行（实测真的点错了）。
- **行内其余数字要过半与行首一致**。正文格式是 `LINE N alphaN bravoN charlieN echoN`，
  真第 150 行是 `150 150 150 150 150`（5/5 过）；OCR 把 `alpha50` 读歪是 `150 150 50
  150 150`（4/5 过）；脏行 `156 156 156 150 150` 是 0/5，直接拒。

### 找不到就自己翻页

`wayland_click_text`（以及 `act` 的 `clickText`）默认 `scroll: true`：当前视野没有，
就自己往下滚、滚到底再往回滚，最多 `maxPages` 屏（默认 24）。没找到会按净位移滚回
原处，不把用户的画面丢在别处。

翻页的三条硬约束（都是踩出来的）：

- **落点只算一次**。滚到文档末尾时「最长那一簇文字」的包围盒会漂到窗口外（实测从
  (592,414) 漂到 (893,864)，那是下方 dock），指针搁在那里滚的是别人的东西。所以
  开局用 `scroll_point()` 挑一个点（取最长那几行里行数最密的纵向带的中位 x）并缓存。
- **不能翻页时顺手点一下**。编辑器里点正文会移动插入点，GTK 保证插入点可见，视图
  被拽回光标那一屏 —— 实测每页点一下，页码在 001~016 和 001~008 之间来回抖，七十屏
  还在开头。只在滚不动时内部点一次抢焦点。
- **步长必须让两屏重叠**。dy=90 时一屏 8~9 行却跳 10 行，「LINE 150」正好卡在第 13 屏
  140~149 与第 14 屏 151~160 之间，翻到末尾也找不到。现在按 `区域高/2.7/5` 取，本机约 57。

判到底只认一件事：**连续三页都没出现新的长行**（≥12 字符）。试过、都不行的判据：
两屏文字集合重叠率 ≥0.85（一屏 16 行翻到下一屏 11~20 行，重叠率本来就 >0.85，第二屏
就误判掉头）；「没有新帧就算到底」（滚到末尾仍会回弹出帧，不存在的词翻 52 屏烧 88 秒）；
全屏文字新增占比 <0.20（顶栏时钟和 dock 残字每帧都变，分母被整屏条目摊薄）。

**「疑似屏」会原地微调重读**。行号读歪是 OCR 的常态（同一行会依次被读成
`157 aLphal57 bravo157…`、`辆 LINH 157 alpha157 bravo157…`、`LINE 158 aLpha158…`），
而错认跟裁切边界强相关 —— 内容上下挪十几像素就换一种错法。所以某屏出现「含目标数字
但不肯命中」的行时，不急着往下翻，先上下挪半格重读几次（最多 8 次，挪动都记进净位移，
最后能滚回原位）。实测「LINE 150」就是靠它从 47 秒 22 屏命中，而不是翻满 90 秒仍落空。

要翻的是某个窗口里的内容，给它 `window: "标题片段"`：会先按 `super+h` 逐层最小化去
提窗。**AT-SPI 在 Wayland 下给非最大化窗口的 extents 恒是 (0,0)**（实测 700×305 报成
(0,0) 而窗口其实在屏幕正中），这种假矩形一律不用，改从 OCR 文字里推断内容区。窗口被
完全盖住、提不到前台时会直接把原因报回来，不硬翻。

## 坐标

对外一律是**逻辑像素**（本机 1536×960；你的机器按实际缩放），内部乘 scale 换成物理像素
给 Mutter。OCR 出来的坐标和 `wayland_windows` 的矩形都是同一套，可以直接互相喂。

## 几个坑（都踩过，别再踩）

- **不要用 `NotifyKeyboardKeysym` 打文本**。有 ibus/拼音输入法的机器上字母键会被
  输入法吃掉（打 `abc` 得到 `阿保持`）。文本一律走 `NotifyKeyboardKeycode` 敲 Ctrl+V，
  内容经 RemoteDesktop 自带的剪贴板送进去。
- **`SelectionWrite` 是带 out-fd 的方法**，必须用 `bus.call_with_unix_fd_list_sync`
  拿 `fdl.get(0)`；用普通 `call_sync` 拿到的 fd 数值是坏的（`[Errno 9] Bad fd`）。
- **剪贴板请求要一直服务**。GTK 是在按下 Ctrl+V 的那一刻才来要数据的，只服务第一次
  会粘不上。
- **键码是裸 evdev**（`NotifyKeyboardKeycode(30)` = A），不是「evdev+8」。
- **两个会话的启动顺序是硬约束**：建 RD → 读 SessionId → 建 SC 并带上
  `remote-desktop-session-id` → RecordMonitor → **只 Start RD**。手动 Start SC 会报
  `Must be started from remote desktop session`。
- **绝对指针定位要联动 screencast 会话**才能用 `NotifyPointerMotionAbsolute`，
  它收的**就是 PipeWire 流帧的物理像素，1:1，不做任何缩放**（实测传入 (1280,800)
  落点 (1278,800)）。所以逻辑坐标要先乘 `physical/logical` 再喂进去。
- **OCR 不能一刀切放大**。稀疏画面 2 倍反而吐不出东西（`--psm 3` 返回空），密集桌面
  2 倍才好。所以是「1 倍找不到才升级到 2 倍」。
- **OCR 不要用 `chi_sim+eng` 混合模型**，它会把整行拉丁字母丢掉。要并行跑 `eng` 和
  `chi_sim` 两份再合并，代价等于最慢那条（并行），却能同时拿到中英文。
- **滚轮只喂给有焦点的那个窗口**。从没点过的窗口，滚多少格画面都纹丝不动 —— 所以
  「滚了没变化」第一反应应该是先点它一下拿焦点，而不是判到底。
- **`NotifyPointerAxis` 的 dy 不是逻辑像素**。本机实测 1 个单位 ≈ 2.7 逻辑像素（正文
  行高约 19px），翻一屏取 `区域高/2.7/5`（本机约 57），保证两屏重叠不漏行。
- **OCR 横切条带是最差的切法**。8 条横切时整屏只能读出 0~4 行，改成 4×3 网格 + `--psm 6`
  后同一帧能读全 11 行（真值）。条带越宽，tesseract 越容易把大片纯色背景当图块整行
  吞掉。现在默认 `DSH_WAYLAND_OCR_TILES=4x3`。
- **`_merge_rows` 会整行吞字**。① 拉丁行「新读法不优于旧读法」时丢掉新行，却把已经
  攒下、「本来要被它顶掉」的行也一起丢了；② `chi_sim` 会把整片正文吐成一个几百像素高
  的巨型框，它跟每一行都算重叠，接着「中文读法胜出」就把整片拉丁正文全顶掉；
  ③ 同一行被两个模型各读一次、框差 1px，重叠判定双双放行，一行变两行。三处都已修
  （撤销 drop、巨型框让位、按文本+坐标去重）。
- **GNOME Wayland 下 alt+Tab 只会在系统认定的两个窗口间来回跳**，不是环形的；
  窗口之间互盖时没法靠它切到第三个窗口，老老实实报错比空转十屏强。
- **「接受远程控制」那个弹窗不是本插件弹的**。它是 xdg-desktop-portal-gnome 的
  `RemoteDesktopDialog`，由 HopToDesk / ToDesk / rustdesk 这类远控服务调
  `portal.RemoteDesktop.Start` 时触发。本插件全程走 `org.gnome.Mutter.RemoteDesktop`，
  不碰 portal，所以不会弹；但那个框会压在画面最上层，把所有按坐标的点击吃掉 ——
  `wayland_dialogs` 认它；`close: true` 会按 **`accept` 指定的那一边**（默认按「共享/允许」
  放行 —— 你要用远控就得让它拿到画面；只有想掐断连接时才传 `accept: false` 按「取消」）。
  想让它别再弹：在远控软件里打开「无人值守 / Wayland 免提示」，或停掉它的传入连接。
  另外 `~/.local/share/xdg-desktop-portal/` 下没有 restore token 时，每次服务重启
  都要重新要一次授权 —— 这就是「老是弹」的机制。
- **不能点整行中心**。portal 的按钮行是 `Cancel   Share` / `Share   Cancel` 挤在
  一条，行宽 400 时中心正好落在两个按钮中间 —— 想按共享会按成取消，想取消会把
  屏幕共享出去。落点必须是**那个词自己的中心**，并且「这行的第一个词就是它」才认
  （正文里出现 cancel/share 时它是句中第 N 个词）。
- **移动和按下之间要留 100ms 以上**。指针刚跳过去就按，GTK 还在处理 motion，
  点击会落在旧坐标上（实测 `pump(20)` 时按钮毫无反应、`pump(140)` 立刻生效）。
- **淡灰按钮上的字 OCR 时好时坏**，而且「放大能救小字」在这里**不成立**：同一块
  按钮带 up=1 读得出 `Share`+`Cancel`，up=2 一个字都读不出（全屏窗口里只有两行
  灰字，放大后版面分析彻底吐空）；同配置连跑两次也会一次读到一次读不到。所以找
  按钮要换 `psm`/`tiles` 重读几次，别指望放大。
- **「有弹窗」要分强弱词**。旧实现「OCR 读到『远程控制』四个字就算有弹窗」，于是
  屏幕上只要有聊天正文/网页/代码注释写着这几个字，工具就天天报「认到弹窗」，而画面
  里其实一个框都没有。现在强词（`wants to share your screen`、`allow remote
  interaction` 这类 portal 原文）见到就算；弱词（`远程控制`、`共享屏幕`…）必须同时
  看到「以取消类词开头的按钮行 + 旁边还有共享类按钮」这个形状才升格。
  `wayland_dialogs` 不带参数只查不动（走 `dismiss_dialog(dry=True)`）。
- **关闭后复查要和「认」用同一套判据**。别在弹窗矩形里另写一套找文案的逻辑 ——
  两套判据不一致时会出现「点击回调都打出来了、复查却说弹窗还在」。现在直接重跑
  `dismiss_dialog(dry=True)`，认和复查同源。实测 `dismissed=True` + 回调
  `CANCEL_CLICKED`。
- **整屏 OCR 读不到这类对话框的按钮**。实测一个 672×389 的授权框，整屏 100 条结果
  里含 share 的只有那句 label，`Share`/`Cancel` 整块消失（18px 高、落在切块边缘被
  tesseract 吞掉）。按钮必须走定向条带：取 label 行下方 `cy+120`、高 `220`、左右
  各外扩 `60`，并**强制 `tiles=1x1`** —— 同一块条带 1x1 读出 `Cancel`+`Share`，
  4x3 只读出 `Shar Share`。所以 `_ocr_image` 与 `ocr` 都加了 `tiles` / `psm` 参数。
- **全屏对话框连定向条带都挂不上**。全屏的授权框把 label 拆成几段，逐行都当不了
  锚点，于是「文案下方找按钮」整套失效。补了 `_sweep_buttons()`：横扫屏幕中下部
  （六条带，必须盖到 0.98 —— 按钮贴在窗口最底，实测 cy≈913），每块再换 `psm` 试
  （`3`/`11` 行、`6` 不行）。这条路径上有两个反直觉的坑，都是实测撞出来的：
  **`upscale` 必须是 1**（同一块按钮带 up=1 读出 `Share`+`Cancel`，up=2 一个字都
  没有），**`psm` 不能是默认的 6**（psm 6 假设「一块连续文字」，而这里是左右两个
  按钮夹一大片空白，读出来是空的）。
- **横扫扫到的按钮可能离文案很远**。全屏框的文案 cy≈30、按钮 cy≈913，用「按钮在
  文案下方 60~400px」的常规约束一定全被过滤掉。`_find_btn` 因此加了宽松兜底：不套
  位置约束，但要求那行**短得像个孤零零的按钮**（≤3 个词块、首词长度与目标词差 ≤2）。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DSH_WAYLAND_PYTHON` | `/usr/bin/python3` | 需要带 `gi` |
| `DSH_WAYLAND_JPEG_QUALITY` | `75` | 截图质量 |
| `DSH_WAYLAND_OCR_LANGS` | `eng,chi_sim` | 并行通道 |
| `DSH_WAYLAND_OCR_TILES` | `4x3` | OCR 网格切块（横切条带会整行吞字，别改回去） |
| `DSH_WAYLAND_OCR_PSM` | `6` | tesseract 版面模式（6=单块连续文字，整屏最稳） |
| `DSH_WAYLAND_MATCH_STRICT` | `0.88` | 翻页搜索时的严格相似度（低于它才去翻页） |
| `DSH_WAYLAND_SCROLL_BUDGET` | `150` | 一次滚屏搜索最多花几秒 |
| `DSH_WAYLAND_OCR_UPSCALE` | `1` | 默认放大倍数（找不到会自动升到 2） |

后端的 stderr 里带 ERROR/Traceback 的行会写到 `$TMPDIR/dsh-wayland-<uid>/daemon.log`。

## 依赖

系统：`python3-gi`（Gio/GLib/Gst/PIL）、`gstreamer1.0-plugins-{base,good}`（pipewiresrc /
videoconvert / jpegenc）、`tesseract-ocr` + `tesseract-ocr-chi-sim`（OCR，可选）。
无 Node 侧依赖。
