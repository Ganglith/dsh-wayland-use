#!/usr/bin/env python3
"""
dsh-wayland-use 的常驻后端。

只对 Wayland（GNOME/Mutter）生效，全部能力来自合成器自己的两个 D-Bus 接口：

  org.gnome.Mutter.RemoteDesktop  —— 注入指针/键盘/剪贴板
  org.gnome.Mutter.ScreenCast     —— 取屏幕像素（PipeWire → GStreamer → JPEG）

两个会话是「联动」的：ScreenCast 会话要带着 RemoteDesktop 的 SessionId 创建，
由 RemoteDesktop 启动，之后才拿得到 PipeWire 节点。协议是 stdin/stdout 上的
一行一个 JSON，二进制走 base64。父进程一关 stdin 就退出。

之所以做成常驻进程：建会话 + 建管道要 ~300ms，之后每次操作只要 1–15ms。
"""

import base64
import json
import os
import select
import sys
import time
import traceback

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

Gst.init(None)

RDP = "org.gnome.Mutter.RemoteDesktop"
RDS = RDP + ".Session"
SCP = "org.gnome.Mutter.ScreenCast"
SCS = SCP + ".Session"
SCST = SCP + ".Stream"

# ── evdev 键码（Mutter 的 NotifyKeyboardKeycode 收的是 evdev 码；下面统一存 evdev，
#    真正发送时再 +8 转成它期望的 X11 键码）────────────────────────────────────
EVDEV = {
    "esc": 1, "escape": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8,
    "8": 9, "9": 10, "0": 11, "minus": 12, "-": 12, "equal": 13, "=": 13,
    "backspace": 14, "tab": 15,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23,
    "o": 24, "p": 25, "[": 26, "leftbrace": 26, "]": 27, "rightbrace": 27,
    "enter": 28, "return": 28, "ctrl": 29, "control": 29, "leftctrl": 29,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37,
    "l": 38, ";": 39, "semicolon": 39, "'": 40, "apostrophe": 40, "`": 41,
    "grave": 41, "shift": 42, "leftshift": 42, "\\": 43, "backslash": 43,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50, ",": 51,
    "comma": 51, ".": 52, "period": 52, "/": 53, "slash": 53, "rightshift": 54,
    "kpasterisk": 55, "alt": 56, "leftalt": 56, "space": 57, " ": 57,
    "capslock": 58,
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64, "f7": 65,
    "f8": 66, "f9": 67, "f10": 68, "numlock": 69, "scrolllock": 70,
    "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111,
    "del": 111, "f11": 87, "f12": 88,
    "rightctrl": 97, "rightalt": 100, "meta": 125, "super": 125, "win": 125,
    "leftmeta": 125, "rightmeta": 126, "print": 99, "printscreen": 99,
    "pause": 119, "menu": 127,
}
MODS = {29, 42, 56, 125, 97, 54, 100, 126}
BTN = {"left": 0x110, "middle": 0x112, "right": 0x111, "back": 0x113,
       "forward": 0x114, "1": 0x110, "2": 0x112, "3": 0x111}


def keycode(name):
    k = str(name).strip().lower()
    if k in EVDEV:
        return EVDEV[k]
    raise ValueError("不认识的按键: %s" % name)


def pump(ms=0.0):
    """把 GLib 默认上下文里的待办跑掉（D-Bus 信号就是在这一步分发的）。"""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + ms / 1000.0
    while True:
        while ctx.pending():
            ctx.iteration(False)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.002)


def num_sim(a, b):
    """数字相似度：先看数值是否相等（前导零不算差别），再退到字符比例。

    「LINE 88」在屏幕上写成「088」、「LINE 7」写成「007」—— 字符比例只有
    0.8，会把真行挡掉；而「1590」与「150」字符比例 0.857 又该挡住（那是
    第 156 行的误读）。所以先用 int() 比一次，相等就算 1.0。
    """
    import difflib as _dl
    a, b = str(a), str(b)
    try:
        if int(a) == int(b):
            return 1.0
    except (TypeError, ValueError):
        pass
    return _dl.SequenceMatcher(None, a, b).ratio()


def np_sign(v):
    return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)


class Failure(RuntimeError):
    pass


class Session:
    def __init__(self):
        self.bus = None
        self.rd = None
        self.rd_sid = None
        self.sc = None
        self.stream = None
        self.node = None
        self.pipeline = None
        self.logical = None      # 逻辑尺寸（对外坐标空间）
        self.physical = None     # 物理尺寸（送给 Mutter 的坐标空间）
        self.frame = None        # 最新一帧 JPEG 字节
        self.frame_seq = 0
        self.frame_at = 0.0
        self.clip_data = b""
        self.clip_owner = False
        self.clip_ready = False
        self.alive = False
        self.action_at = 0.0
        self._subs = []

    # ── D-Bus 小工具 ────────────────────────────────────────────────────────
    def call(self, dest, path, iface, method, params=None, sig=None, timeout=10000):
        try:
            reply = self.bus.call_sync(
                dest, path, iface, method, params,
                GLib.VariantType.new(sig) if sig else None,
                Gio.DBusCallFlags.NONE, timeout, None)
        except GLib.Error as e:
            raise Failure("%s.%s: %s" % (iface.rsplit(".", 1)[-1], method, e.message))
        return reply.unpack() if sig else None

    def call_fd(self, dest, path, iface, method, params, timeout=8000):
        """带 out-fd 的方法必须走这条路，否则拿到的 fd 是无效数字。"""
        try:
            reply, fdl = self.bus.call_with_unix_fd_list_sync(
                dest, path, iface, method, params, GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE, timeout, None, None)
        except GLib.Error as e:
            raise Failure("%s.%s: %s" % (iface.rsplit(".", 1)[-1], method, e.message))
        idx = reply.unpack()[0]
        return fdl.get(idx)

    def prop(self, dest, path, iface, name):
        # Get 返回 (v)；unpack 之后外层是 tuple，里面那个值已经是原生类型了
        v = self.call(dest, path, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (iface, name)), "(v)")
        return v[0]

    def sub(self, iface, member, path, cb):
        sid = self.bus.signal_subscribe(None, iface, member, path, None,
                                        Gio.DBusSignalFlags.NONE, cb)
        self._subs.append(sid)
        return sid

    # ── 建会话 ──────────────────────────────────────────────────────────────
    def start(self):
        self.teardown()
        self.bus = self.bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)

        self.rd = self.call(RDP, "/org/gnome/Mutter/RemoteDesktop", RDP,
                            "CreateSession", None, "(o)")[0]
        self.rd_sid = self.prop(RDP, self.rd, RDS, "SessionId")

        self.sub(RDS, "SelectionOwnerChanged", self.rd, self._on_owner_changed)
        self.sub(RDS, "SelectionTransfer", self.rd, self._on_transfer)
        self.sub(RDS, "Closed", self.rd, self._on_closed)

        # ScreenCast 会话必须带着 RemoteDesktop 的会话 id 建，才拿得到绝对坐标
        # 指针定位需要的 stream 路径。
        self.sc = self.call(
            SCP, "/org/gnome/Mutter/ScreenCast", SCP, "CreateSession",
            GLib.Variant("(a{sv})", ({"remote-desktop-session-id":
                                      GLib.Variant("s", self.rd_sid)},)), "(o)")[0]

        node = {}

        def on_node(_c, _s, _p, _i, _sig, params):
            node.setdefault("id", params.unpack()[0])

        self.sub(SCST, "PipeWireStreamAdded", None, on_node)

        # RecordMonitor 必须在 Start 之前
        self.stream = self.call(
            SCP, self.sc, SCS, "RecordMonitor",
            GLib.Variant("(sa{sv})", ("", {"cursor-mode": GLib.Variant("u", 1)})),
            "(o)")[0]

        # 只启动 RemoteDesktop；由它隐式启动联动的 ScreenCast 会话
        self.call(RDP, self.rd, RDS, "Start")

        deadline = time.monotonic() + 6.0
        while "id" not in node and time.monotonic() < deadline:
            pump(20)
        if "id" not in node:
            raise Failure("等不到 PipeWire 流（会话没起来？）")
        self.node = node["id"]

        try:
            p = self.prop(SCP, self.stream, SCST, "Parameters")
            if isinstance(p, dict) and "size" in p:
                self.logical = tuple(int(v) for v in p["size"])
        except Failure:
            pass

        self._start_pipeline()
        # PipeWire 只在画面有变化时出帧，刚建好的会话不一定马上有首帧：
        # 轻轻抖一下指针制造 damage，让第一帧立刻到。
        deadline = time.monotonic() + 8.0
        nudge = 0
        while self.frame is None and time.monotonic() < deadline:
            if nudge < 12:
                try:
                    self.move_rel(1 if nudge % 2 == 0 else -1,
                                  1 if nudge % 2 == 0 else -1)
                except Failure:
                    pass
                nudge += 1
            pump(180)
        if self.frame is None:
            raise Failure("流已建立但拿不到画面（显示器在锁屏或没有输出？）")
        if nudge:
            try:
                self.move_rel(-1 if nudge % 2 == 0 else 1,
                              -1 if nudge % 2 == 0 else 1)
            except Failure:
                pass
        if self.logical is None:
            self.logical = self.physical
        self.alive = True

    def _start_pipeline(self):
        # 让 GStreamer 直接编码成 JPEG 再交给我们：探针拿到的就是成品字节，
        # 不用把 12MB 的 RGB 每帧拷一遍。
        desc = ("pipewiresrc path=%d do-timestamp=true ! videoconvert name=vc ! "
                "video/x-raw,format=RGB ! jpegenc quality=%d ! "
                "fakesink name=s sync=false async=false" % (self.node, QUALITY))
        self.pipeline = Gst.parse_launch(desc)
        self.sink = self.pipeline.get_by_name("s")
        self.sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_buffer)
        vc = self.pipeline.get_by_name("vc")
        if vc is not None:
            vc.get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER, self._on_raw)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_raw(self, pad, info):
        caps = pad.get_current_caps()
        if caps is not None and caps.get_size():
            st = caps.get_structure(0)
            try:
                w, h = st.get_value("width"), st.get_value("height")
                if w and h:
                    self.physical = (w, h)
            except Exception:
                pass
        return Gst.PadProbeReturn.OK

    def _on_buffer(self, _pad, info):
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        try:
            data = buf.extract_dup(0, buf.get_size())
            # 尺寸还没探到时先从 JPEG 头里读（SOF0 段）
            if self.physical is None and len(data) > 4:
                i = 2
                while i + 9 < len(data):
                    if data[i] != 0xFF:
                        break
                    mk = data[i + 1]
                    seg = (data[i + 2] << 8) | data[i + 3]
                    if mk in (0xC0, 0xC1, 0xC2):
                        self.physical = ((data[i + 7] << 8) | data[i + 8],
                                         (data[i + 5] << 8) | data[i + 6])
                        break
                    i += 2 + seg
            self.frame = data
            self.frame_seq += 1
            self.frame_at = time.monotonic()
        except Exception:
            pass
        return Gst.PadProbeReturn.OK

    def wait_frame(self, timeout_ms, after=0):
        """等到一帧比 after 更新的画面；屏上没变化就等到超时。"""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if self.frame is not None and self.frame_seq > after:
                return True
            pump(4)
        return self.frame is not None and self.frame_seq > after

    def teardown(self):
        for sid in self._subs:
            try:
                self.bus.signal_unsubscribe(sid)
            except Exception:
                pass
        self._subs = []
        if self.pipeline is not None:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        self.pipeline = None
        self.sink = None
        self.alive = False
        self.frame = None
        self.frame_seq = 0

    def _on_closed(self, *_a):
        self.alive = False

    def ensure(self):
        if not self.alive:
            self.start()

    # ── 剪贴板（文本输入的唯一可靠通道：键盘 keysym 会被输入法吃掉）──────────
    def _on_owner_changed(self, _c, _s, _p, _i, _sig, params):
        opts = params.unpack()[0] if params is not None else {}
        self.clip_owner = bool(opts.get("session-is-owner"))

    def _on_transfer(self, _c, _s, _p, _i, _sig, params):
        mime, serial = params.unpack()
        if not self.clip_owner:
            return
        try:
            fd = self.call_fd(RDP, self.rd, RDS, "SelectionWrite",
                              GLib.Variant("(u)", (serial,)))
            data = self.clip_data or b""
            off = 0
            while off < len(data):
                off += os.write(fd, data[off:off + 65536])
            os.close(fd)
            self.call(RDP, self.rd, RDS, "SelectionWriteDone",
                      GLib.Variant("(ub)", (serial, True)))
        except Exception:
            pass

    def set_clipboard(self, text):
        if not self.clip_owner:
            try:
                self.call(RDP, self.rd, RDS, "EnableClipboard",
                          GLib.Variant("(a{sv})", ({},)))
            except Failure:
                pass
        self.clip_data = text.encode("utf-8")
        self.call(RDP, self.rd, RDS, "SetSelection",
                  GLib.Variant("(a{sv})", ({"mime-types": GLib.Variant(
                      "as", ["text/plain;charset=utf-8", "UTF8_STRING",
                             "text/plain"])},)))
        deadline = time.monotonic() + 2.0
        while not self.clip_owner and time.monotonic() < deadline:
            pump(10)
        if not self.clip_owner:
            raise Failure("拿不到选区所有权（剪贴板被别的程序占着？）")

    # ── 指针 ────────────────────────────────────────────────────────────────
    def scale(self):
        if not self.logical or not self.physical:
            return 1.0
        return self.physical[0] / float(self.logical[0])

    def to_physical(self, x, y):
        s = self.scale()
        return float(x) * s, float(y) * s

    def _inject(self, method, variant):
        """往 RemoteDesktop 会话里塞一个输入事件，会话没了就重建再塞一次。

        为什么要这个兜底：会话被合成器回收时，D-Bus 上的 `Closed` 信号只有
        在 main() 的 select 里顺带 pump GLib 时才处理得到。一次请求内部连做
        几十步（OCR、翻页、点击）时没人 pump，`alive` 就还停在 True，接着
        每个点击都撞在已销毁的对象上，报出来是
        `Session/u474 上没有 org.gnome.Mutter.RemoteDesktop.Session 接口` ——
        看着像「接口不存在」，其实是「这个会话已经死了」。

        重建会话要 ~300ms（新建 ScreenCast 会话 + 管道），所以只在真的撞上
        未知对象时才付这个代价，正常路径不变。
        """
        try:
            self.call(RDP, self.rd, RDS, method, variant)
        except Failure as e:
            msg = str(e)
            if "UnknownMethod" not in msg and "UnknownObject" not in msg:
                raise
            self.start()
            self.call(RDP, self.rd, RDS, method, variant)

    def move(self, x, y):
        px, py = self.to_physical(x, y)
        self._inject("NotifyPointerMotionAbsolute",
                     GLib.Variant("(sdd)", (self.stream, px, py)))

    def move_rel(self, dx, dy):
        self._inject("NotifyPointerMotionRelative",
                     GLib.Variant("(dd)", (float(dx), float(dy))))

    def button(self, name, down):
        self._inject("NotifyPointerButton",
                     GLib.Variant("(ib)", (BTN.get(str(name).lower(), 0x110), down)))

    def scroll(self, dx, dy):
        self._inject("NotifyPointerAxis",
                     GLib.Variant("(ddu)", (float(dx), float(dy), 0)))

    # ── 键盘 ────────────────────────────────────────────────────────────────
    def keycode_down(self, code, down):
        # Mutter 收的就是裸 evdev 码（30 = KEY_A），不用做 X11 的 +8 换算。
        self._inject("NotifyKeyboardKeycode", GLib.Variant("(ub)", (code, down)))

    def key(self, combo):
        names = [n for n in str(combo).replace(" ", "").split("+") if n]
        if not names:
            raise ValueError("空按键")
        codes = [keycode(n) for n in names]
        mods = [c for c in codes[:-1]]
        main = codes[-1]
        ordered = [c for c in mods if c in MODS] + [c for c in mods if c not in MODS]
        for c in ordered:
            self.keycode_down(c, True)
        self.keycode_down(main, True)
        self.keycode_down(main, False)
        for c in reversed(ordered):
            self.keycode_down(c, False)

    def type_text(self, text):
        self.set_clipboard(text)
        # 有些应用要选区真的“有主”一会儿才会去取数据
        pump(30)
        self.key("ctrl+v")

    # ── 截图 ────────────────────────────────────────────────────────────────
    def touch(self):
        """记下「刚刚动过手」的时刻，截图据此判断缓存帧是否已经反映了这次操作。"""
        self.action_at = time.monotonic()

    def shot(self, fresh_ms=150):
        # 画面静止时 PipeWire 不出帧，死等只是白等：缓存帧只要晚于最后一次操作
        # 就是最新的。
        if self.frame is not None and self.frame_at >= self.action_at:
            return self.frame, (time.monotonic() - self.frame_at) * 1000.0
        seq = self.frame_seq
        self.wait_frame(fresh_ms, after=seq)
        if self.frame is None:
            raise Failure("还没有画面")
        return self.frame, (time.monotonic() - self.frame_at) * 1000.0

    # ── OCR（把截图变成可点的文字坐标，替代「猜坐标」）──────────────────────
    def _ocr_image(self, im, lang, f, tiles=None, psm=None):
        """把一张图放大 f 倍后切成网格并行识别。

        切法用实测换来的，别想着改回去：
        - **横切 8 条是最差的切法**。2560 宽的条里塞着好几个窗口的内容，
          tesseract 的版面分析会把大片纯色背景当成图块，整行吞掉：实测同一帧
          整屏单跑认得 0 行、横切 8 条只认出 2 行，而 4×3 网格认出 10 行、
          4×4 网格 11 行（真值 11）。
        - `--psm 6` 比 3 稳。网格块本来就是「一块连续文字」，psm 6 正是为这
          个假设设计的；整屏用 psm 11 会把窗口那 1px 边框认成一行横跨全宽的
          假字（"WILL"），psm 3 又慢 20%。
        - 块之间留 24px 重叠，避免正好压线的一行被切断 —— 重叠区的字只归
          它中心所在的那一块，不会重复。

        tiles / psm 可显式覆盖环境变量：**小块图（比如一行按钮带）必须传
        tiles="1x1"**。实测同一块按钮带，1x1 读出 `Cancel` + `Share`，
        4x3 只读出 `Shar Share`（Cancel 那块被切碎），2x1 也行 —— 图小的时候
        再切纯属自找麻烦。
        """
        import subprocess
        import tempfile
        from concurrent.futures import ThreadPoolExecutor

        from PIL import Image

        if f and f != 1:
            im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        W, H = im.width, im.height
        spec = (tiles or os.environ.get("DSH_WAYLAND_OCR_TILES", "4x3")).lower()
        try:
            nx, ny = [max(1, int(v)) for v in spec.split("x", 1)]
        except ValueError:
            nx, ny = 4, 3
        # 小图切了反而更慢（进程启动开销占比高），也更容易把一行切碎
        if W < 640 or H < 480:
            nx = ny = 1
        psm = psm or os.environ.get("DSH_WAYLAND_OCR_PSM", "6")
        ov = int(24 * max(1, f))

        jobs = []
        for gy in range(ny):
            for gx in range(nx):
                x0 = gx * W // nx
                x1 = W if gx == nx - 1 else (gx + 1) * W // nx
                y0 = gy * H // ny
                y1 = H if gy == ny - 1 else (gy + 1) * H // ny
                jobs.append((gx, gy, x0, y0, x1, y1))

        def run(job):
            gx, gy, x0, y0, x1, y1 = job
            cx0, cy0 = max(0, x0 - ov), max(0, y0 - ov)
            cx1, cy1 = min(W, x1 + ov), min(H, y1 + ov)
            part = im.crop((cx0, cy0, cx1, cy1))
            with tempfile.TemporaryDirectory() as td:
                png = os.path.join(td, "s.png")
                part.save(png, format="PNG", compress_level=1)
                try:
                    out = subprocess.run(
                        ["tesseract", png, "stdout", "-l", lang,
                         "--psm", psm, "tsv"],
                        capture_output=True, text=True, timeout=40)
                except FileNotFoundError:
                    raise Failure("没有 tesseract，装一下：sudo apt install "
                                  "tesseract-ocr tesseract-ocr-chi-sim")
                except subprocess.TimeoutExpired:
                    raise Failure("OCR 超时")
            rows = []
            for line in out.stdout.splitlines()[1:]:
                c = line.split("\t")
                if len(c) < 12:
                    continue
                text = c[11].strip()
                if not text:
                    continue
                try:
                    l, t, w, h = int(c[6]), int(c[7]), int(c[8]), int(c[9])
                except ValueError:
                    continue
                if w <= 0 or h <= 0:
                    continue
                gl = l + cx0
                gt = t + cy0
                ccx = gl + w / 2.0
                ccy = gt + h / 2.0
                # 重叠区里的字只归中心落在本块「负责区间」的那一份，避免重复
                if not (x0 <= ccx < x1 and y0 <= ccy < y1):
                    continue
                rows.append((text, gl, gt, w, h))
            return rows

        if nx * ny == 1:
            rows = run(jobs[0])
        else:
            with ThreadPoolExecutor(max_workers=min(12, nx * ny)) as ex:
                rows = [r for part in ex.map(run, jobs) for r in part]
        # 坐标一律按「放大后的图」原样返回，除以 f 和 k 的换算只在 ocr() 里
        # 做一次 —— 两边各除一次 f 会让结果缩成一半（f=2 时正好看起来「勉强
        # 对」），是最难发现的那种错。
        return rows

    @staticmethod
    def _cjk_count(t):
        return sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")

    @staticmethod
    def _is_cjk_line(t):
        """这一行主要是中文吗。

        不能看「有没有汉字」：chi_sim 模型读拉丁单词时会顺手吐一个汉字
        （"the button" → "有 be button"），据此判成中文行，就会让它顶掉 eng
        模型本已读对的 "the button" —— 一条正确的英文被一条错的中文挤掉，
        模型后面怎么试都找不回来。所以按占比判，1 个汉字 / 10 个字母不算中文行。
        """
        n = sum(1 for ch in t if ch.isalnum())
        return n > 0 and Session._cjk_count(t) * 2 >= n

    @staticmethod
    def _merge_rows(a, b):
        """合并两套语言模型按行并好之后的条目：重叠处只留一份，且要留对的那份。

        两条规则，都是实测撞出来的：

        1. **同一块区域里，中文读法胜出，拉丁乱码让位。** eng 模型看中文会吐
           一串等长甚至更长的乱码（"滚动与拖拽测试" → "Aah Shae Mal"），只比
           长度会让乱码赢。
        2. **两边都是中文时一律全留，绝不互相顶掉。** tesseract 给中文是逐字
           报框的，相邻汉字的框天然重叠（"滚动" 的 171px 宽框盖住 "与"）。早
           先按重叠面积做「整格替换」，正是这一条把整行中文删得精光。
        """
        def _sig(i):
            return (i["text"].strip().lower(), round(i["x"] / 6), round(i["y"] / 6))

        out = list(a)
        for r in b:
            drop = []
            keep = True
            for i, q in enumerate(out):
                oy = min(r["y"] + r["h"], q["y"] + q["h"]) - max(r["y"], q["y"])
                ox = min(r["x"] + r["w"], q["x"] + q["w"]) - max(r["x"], q["x"])
                if oy <= 0.4 * min(r["h"], q["h"]):
                    continue
                if ox <= 0.25 * min(r["w"], q["w"]):
                    continue
                # 巨型框守卫。chi_sim 偶尔会把整片正文吐成一个几百像素高的
                # 「中文行」（实测 (201,290,928,261) 盖住 15 行英文正文），它
                # 跟每一行都算重叠，接着「中文读法胜出」就把整片拉丁正文全顶掉
                # —— 屏幕上明明白白写着 8 行 LINE，OCR 交回来一个字都没有，
                # 翻页判据因此以为「没变化」，一路翻到超时。这种框比它要顶掉的
                # 行高十几倍，一眼能认出来，直接让位。
                if q["h"] > max(30.0, 3.0 * r["h"]) or \
                        q["w"] > max(600.0, 4.0 * r["w"]):
                    continue
                if r["text"].strip().lower() == q["text"].strip().lower():
                    keep = False                     # 同一段文字，留一份
                    break
                cr = Session._is_cjk_line(r["text"])
                cq = Session._is_cjk_line(q["text"])
                if cr == cq:
                    if cr:
                        continue                     # 中文 vs 中文：都要
                    # 拉丁 vs 拉丁：同一处被认了两遍（"the button" / "有 be
                    # button"），留**纯拉丁字母更多**的那个。不能比总长度：
                    # chi_sim 读英文单词时会顺手塞一个汉字，总长度反而更长
                    # （"有 be button" 11 字符 > "the button" 10），比长度会让
                    # 一条读对的英文被一条读错的顶掉。
                    def weight(t):
                        return (sum(1 for ch in t
                                    if ch.isascii() and ch.isalnum()), len(t))
                    if weight(r["text"]) > weight(q["text"]):
                        drop.append(i)
                    else:
                        # 这条拉丁行读得不如已有的好，整条丢掉 —— 但**已经
                        # 攒下的 drop 必须撤销**：那些是「本来要被 r 顶掉」的
                        # 行，r 不来了，它们就该留着。不清空的话两边一起消失，
                        # 整行正文凭空蒸发（实测：翻页后 8 行 LINE 全没了，
                        # 屏幕上明明白白写着，OCR 却说一个字都没有）。
                        keep = False
                        drop = []
                        break
                elif cq:
                    keep = False                     # 已有的中文读法更好
                    break
                else:
                    drop.append(i)                   # 中文读法顶掉拉丁乱码
            if drop:
                out = [q for i, q in enumerate(out) if i not in drop]
            if keep:
                out.append(r)
        # 两套模型报的框常常只差几个像素：同一行字，eng 报 (719,537,51,11)、
        # chi_sim 报 (719,536,51,13)，重叠判定可能双双放行，合并后一行变两行，
        # 行数虚涨。所以最后再按「文本 + 量化坐标」去重一次，只留第一份 ——
        # 实测 61 条里有 5 条是这种重影，翻页判「有没有变化」时全都算作差异。
        seen = set()
        dedup = []
        for it in out:
            k = _sig(it)
            if k in seen:
                continue
            seen.add(k)
            dedup.append(it)
        return dedup

    def _rows_to_items(self, rows, off, k, f):
        """原始词框 → 对外条目：换算坐标，并把同一行的词并成一条。

        模型看到的「一段文字」才是一次可点的目标；不并的话 "the button" 会变成
        两条孤立结果，按文字找就永远差半个词。

        但合并会把落点带偏：一行 "常规 设置 关于" 并成一条后，cx 落在整行正中
        间的空隙上，点「设置」实际点到的是空白。所以每条都额外留着每个词自己
        的框（words，带它在合并文本里的字符偏移 off），find_text 命中后再用
        _anchor() 把落点收回到 needle 真正覆盖的那几个词上。
        """
        items = []
        for text, l, t, w, h in rows:
            items.append({
                "text": text,
                # 行坐标是「裁剪 + 放大」后图里的像素；加裁剪原点再除 k 回逻辑
                "x": round((l + off[0] * f) / (k * f), 1),
                "y": round((t + off[1] * f) / (k * f), 1),
                "w": round(w / (k * f), 1),
                "h": round(h / (k * f), 1),
            })
        items.sort(key=lambda i: (round(i["y"] / 12), i["x"]))
        merged = []
        for it in items:
            last = merged[-1] if merged else None
            if (last and abs(last["y"] - it["y"]) <= max(6, it["h"] * 0.6)
                    and it["x"] - (last["x"] + last["w"]) <= it["h"] * 1.2):
                # 中文逐字报框，用空格拼起来会变成 "滚动 与 拖 拽" 这种
                # 搜不到的形式；汉字之间直接相连
                if Session._cjk_count(last["text"]) and Session._cjk_count(it["text"]):
                    sep = ""
                else:
                    sep = " "
                at = len(last["text"]) + len(sep)
                last["text"] += sep + it["text"]
                last["words"].append({
                    "text": it["text"], "off": at,
                    "x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"],
                })
                left = min(last["x"], it["x"])
                right = max(last["x"] + last["w"], it["x"] + it["w"])
                top = min(last["y"], it["y"])
                bot = max(last["y"] + last["h"], it["y"] + it["h"])
                last["x"], last["y"] = left, top
                last["w"], last["h"] = round(right - left, 1), round(bot - top, 1)
            else:
                merged.append({
                    "text": it["text"],
                    "x": it["x"], "y": it["y"], "w": it["w"], "h": it["h"],
                    "words": [{"text": it["text"], "off": 0,
                               "x": it["x"], "y": it["y"],
                               "w": it["w"], "h": it["h"]}],
                })
        for m in merged:
            m["cx"] = round(m["x"] + m["w"] / 2, 1)
            m["cy"] = round(m["y"] + m["h"] / 2, 1)
        return merged

    @staticmethod
    def _anchor(item, needle):
        """把落点从「整行中心」收回到 needle 真正覆盖的那几个词上。

        精确子串命中直接按字符偏移取；模糊命中（OCR 把 alpha 认成 aLpha 这类）
        按词比相似度取最像的那一两个词。都对不上才退回整条中心。
        """
        import difflib
        words = item.get("words") or []
        n = str(needle).strip().lower()
        hay = item.get("text", "").lower()
        if not words or not n:
            return item["cx"], item["cy"]
        if item.get("_word"):
            # _match 的词级模糊已经指出是哪个词了，直接用
            w = item["_word"]
            return round(w["x"] + w["w"] / 2, 1), round(w["y"] + w["h"] / 2, 1)
        pos = hay.find(n)
        if pos >= 0:
            end = pos + len(n)
            picked = [w for w in words
                      if w["off"] < end and w["off"] + len(w["text"]) > pos]
        else:
            # 模糊：单看最像的一个词，再看相邻两词连起来（"echo19" 这类）
            best, picked = 0.0, []
            for i, w in enumerate(words):
                cands = [(w["text"], [w])]
                if i + 1 < len(words):
                    cands.append((w["text"] + " " + words[i + 1]["text"],
                                  [w, words[i + 1]]))
                for text, ws in cands:
                    r = difflib.SequenceMatcher(None, n, text.lower()).ratio()
                    if r > best:
                        best, picked = r, ws
            if best < 0.6:
                return item["cx"], item["cy"]
        if not picked:
            return item["cx"], item["cy"]
        left = min(w["x"] for w in picked)
        right = max(w["x"] + w["w"] for w in picked)
        top = min(w["y"] for w in picked)
        bot = max(w["y"] + w["h"] for w in picked)
        return round((left + right) / 2, 1), round((top + bot) / 2, 1)

    def _raise_by_minimizing(self, want, limit=12):
        """反复 super+h 最小化最上层窗口，直到标题含 want 的窗口拿到焦点。

        返回 (目标窗口 or None, 被最小化的窗口标题列表)。
        为什么要这么干：Wayland 下客户端不知道自己摆在哪（AT-SPI 对非最大化
        窗口恒报 0,0），也没有任何「激活某个窗口」的协议；但 super+h 是逐层
        最小化 —— 每按一次焦点落到下面一层，指到目标为止目标就一定在最上层。
        Alt+Tab 不行：单次按只在最近两个窗口之间来回跳（实测八次全在 DSH 与
        桌面上打转），长按虽能走进切换器但焦点不跟着移动，读不到走到了哪。
        """
        hidden = []
        for _ in range(max(1, limit)):
            cur = self._active_title() or ""
            if want in cur:
                return self._find_win(want), hidden
            if cur:
                hidden.append(cur[:24])
            self.key("super+h")
            pump(620)
        cur = self._active_title() or ""
        if want in cur:
            return self._find_win(want), hidden
        return None, hidden

    def _find_win(self, want):
        """按标题子串取面积最大的那个窗口（同标题常有一堆残影小窗）。"""
        try:
            cands = [w for w in windows()
                     if w.get("width", 0) > 80 and w.get("height", 0) > 60
                     and want in (w.get("title") or "").lower()]
        except Exception:
            return None
        if not cands:
            return None
        cands.sort(key=lambda w: -(w["width"] * w["height"]))
        return cands[0]

    def focus_window(self, title=None, nth=0):
        """按标题子串把某个窗口提到最前，返回它的位置尺寸（拿不到返回 None）。

        翻页前必须做这件事：滚轮只喂给指针底下那一层，窗口互相盖着的时候
        在错误的窗口上翻页，OCR 读到的全是别人的界面 —— 用户看到的就是
        「明明在屏幕上，它却说没找到」。
        """
        want = str(title or "").strip().lower()
        if not want:
            return None, None
        try:
            cands = [w for w in windows()
                     if w.get("width", 0) > 80 and w.get("height", 0) > 60
                     and want in (w.get("title") or "").lower()]
        except Exception:
            return None, "读不到窗口列表"
        if not cands:
            return None, "没有标题含「%s」的窗口" % want
        # 已经在最前的优先，否则挑面积最大的那个（同标题常有一堆残影小窗）
        cands.sort(key=lambda w: (not w.get("active"),
                                  -(w["width"] * w["height"])))
        w = cands[min(nth, len(cands) - 1)]
        if w.get("active"):
            return w, None
        # 一、把盖在上面的窗口逐个最小化，直到目标露出来。
        # Wayland 下没有「按标题提窗」的接口（AT-SPI 的 extents 对非最大化
        # 窗口恒报 0,0，也没有 activate），但 super+h 是**逐层**最小化当前
        # 焦点窗口的：实测一串 super+h 依次走过 DSH → 面向对象编程实验 →
        # 微信 → long.txt 编辑器 → QQ → 桌面，每按一次焦点就往下落一层。
        # 所以一直按到目标拿到焦点为止，目标自然就在最上层了。
        # 副作用是把上面的窗口收进了 dock，所以要把收起来的标题带回去。
        hit, hidden = self._raise_by_minimizing(want, limit=max(14, len(cands) + 10))
        if hit is not None:
            self._hidden_windows = hidden
            return hit, None
        lw, lh = self.logical or (0, 0)
        if (w["x"] <= 0 and w["y"] <= 0 and lw and lh
                and (w["width"] < lw - 8 or w["height"] < lh - 8)):
            # Wayland 下客户端不知道自己摆在哪，AT-SPI 对**没最大化的窗口**
            # 一律报 (0,0,宽,高)。按这个坐标点标题栏只会点到别人身上，
            # 而标题栏的小灰字 OCR 也读不出来（实测整屏 OCR 里没有它），
            # 所以这里不假装能提窗，直接如实交代 —— 调用方会改用「在内容区
            # 点一下拿焦点」的路子，那条路验证有效。
            return None, ("按了一串 super+h 也没能把「%s」提到前台（最小化了 %d 个"
                          "窗口：%s），而且 AT-SPI 说它在 (0,0) %d×%d —— Wayland 下"
                          "非最大化窗口的坐标一律报 0,0、不可信，点标题栏无从下手"
                          % ((w.get("title") or "")[:30], len(hidden),
                             "、".join(hidden[-6:]) or "无",
                             w["width"], w["height"]))
        # 点标题栏提窗：横向取 40% 躲开左侧图标与右侧窗控按钮，纵向 12px 处。
        # 被别的窗口整个盖住时这一下会打到上层窗口上，所以点完必须复核；
        # 复核不过就如实返回 None —— 调用方会退回原区域继续搜，报错里也说得清。
        for dy in (12, 6, 20):
            self.move(w["x"] + int(w["width"] * 0.4), w["y"] + dy)
            pump(60)
            self.button("left", True)
            pump(20)
            self.button("left", False)
            pump(200)
            try:
                now = [x for x in windows() if x.get("active")]
            except Exception:
                now = []
            if any(abs(x["x"] - w["x"]) < 4 and abs(x["y"] - w["y"]) < 4
                   and abs(x["width"] - w["width"]) < 8 for x in now):
                return w, None
        return None, "「%s」被别的窗口盖着，提不到前台（滚轮会翻到上层窗口）" % (
            (w.get("title") or "")[:40])

    @staticmethod
    def _window_region(win, region):
        """把搜索/滚动区域收到窗口内部；两部分都给了就取交集。"""
        if not win:
            return region
        inner = {
            "x": win["x"] + int(win["width"] * 0.03),
            "y": win["y"] + int(win["height"] * 0.10),
            "w": max(40, int(win["width"] * 0.94)),
            "h": max(40, int(win["height"] * 0.82)),
        }
        if not region:
            return inner
        region = region if isinstance(region, dict) else dict(
            zip(("x", "y", "w", "h"), list(region)[:4]))
        x1 = max(inner["x"], int(float(region["x"])))
        y1 = max(inner["y"], int(float(region["y"])))
        x2 = min(inner["x"] + inner["w"], int(float(region["x"])) + int(float(region["w"])))
        y2 = min(inner["y"] + inner["h"], int(float(region["y"])) + int(float(region["h"])))
        if x2 <= x1 or y2 <= y1:
            return inner
        return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}

    def _norm_region(self, region):
        """region 统一成 dict 并给缺省值：搜索/滚动都以屏幕中部的可视区为准。

        整屏搜索会把顶栏、dock、壁纸上的浮层也算进去，滚动时更会被这些不可滚
        的东西占掉指针位置，所以缺省区域收进来一圈。
        """
        lw, lh = self.logical or self.physical or (1280, 800)
        if not region:
            return {"x": int(lw * 0.08), "y": int(lh * 0.10),
                    "w": int(lw * 0.84), "h": int(lh * 0.80)}
        if not isinstance(region, dict):
            region = dict(zip(("x", "y", "w", "h"), list(region)[:4]))
        return {"x": int(float(region["x"])), "y": int(float(region["y"])),
                "w": max(1, int(float(region["w"]))),
                "h": max(1, int(float(region["h"])))}

    @staticmethod
    def _text_clusters(items):
        """把条目按行距聚成若干文字块，按行数从多到少返回 [(x,y,w,h,lh,rows)]。

        屏幕上同时开着编辑器、IDE、浏览器时，只取「最长的一簇」会挑错：翻页
        要点的那一簇必须是真正装着目标内容的那一簇，而哪一簇才是它、在点之前
        并不知道 —— 所以全都给出来，由一个一个试、用窗口焦点来裁决。
        """
        rows = [i for i in items if i["text"].strip()]
        if not rows:
            return []
        rows.sort(key=lambda i: (i["cy"], i["x"]))
        hs = sorted(i["h"] for i in rows)
        lh = hs[len(hs) // 2] or 12.0
        groups, cur = [], [rows[0]]
        for it in rows[1:]:
            if abs(it["cy"] - cur[-1]["cy"]) <= lh * 1.6:
                cur.append(it)
            else:
                groups.append(cur)
                cur = [it]
        groups.append(cur)
        out = []
        for g in groups:
            if len(g) < 3:
                continue
            bx = min(i["x"] for i in g)
            by = min(i["y"] for i in g)
            bw = max(i["x"] + i["w"] for i in g) - bx
            bh = max(i["y"] + i["h"] for i in g) - by
            if bw < 80 or bh < 40:
                continue
            nrow = len({round(i["cy"] / max(4.0, lh * 0.5)) for i in g})
            if nrow < 3:
                continue
            out.append((bx, by, bw, bh, lh, nrow, len(g)))
        # 按「不同行数」排，不按条目数：标题栏、标签页、工具条常常是一行里
        # 挤十几个词块（items 不少但只有一两行），按条目数排会把它们排到正文
        # 前面 —— 然后点击落在标签栏上、把菜单点开，PageDown 就再也到不了
        # 正文（实测：点 (559,249) 点在标签栏上，此后滚轮和 PageDown 双双失效）。
        out.sort(key=lambda t: (-t[5], -t[6]))
        return out

    def _active_title(self):
        """当前有焦点窗口的标题（小写）；读不到返回 None。"""
        try:
            for w in windows():
                if w.get("active"):
                    return (w.get("title") or "").lower()
        except Exception:
            return None
        return None

    def _grab_focus(self, items, window=None):
        """在正文文字上点一下，把焦点交给目标窗口。

        Wayland 下没有任何「激活这个窗口」的协议，AT-SPI 对非最大化窗口又
        一律报 (0,0)，所以只能靠点。点哪儿要试：
        - 候选落点取「行数最多的那几簇文字」，每簇上下试三个位置；
        - 判据是点完 `windows()` 的 active 标题里含 window —— 不看像素，
          因为点开一个菜单画面同样会变；
        - 点歪了多半是点开了菜单（标签栏的「打开(o)」、日历、右键菜单），
          之后的按键全被菜单吃掉，所以每次点完按 escape 收摊再换下一个点。
        实测点 (559,249) 正中标签栏，弹出「打开」下拉，之后滚轮和 PageDown
        双双失效、翻三十屏原地不动 —— 就是这么来的。
        """
        want = (window or "").strip().lower()
        # 焦点已经对上了就别再点：每翻一页都点一下，等于把「点歪」的机会
        # 乘上页数 —— 翻十页就多十次点开菜单的可能。
        if want and want in (self._active_title() or "").lower():
            return True
        clusters = self._text_clusters(items)
        if not clusters:
            return False
        for (bx, by, bw, bh, _lh, _n, _c) in clusters[:5]:
            for fy in (0.55, 0.8, 0.3):
                x, y = int(bx + bw * 0.5), int(by + bh * fy)
                self.move(x, y)
                pump(50)
                self.button("left", True)
                pump(14)
                self.button("left", False)
                pump(160)
                cur = self._active_title() or ""
                if not want or want in cur:
                    return True
                self.key("escape")
                pump(150)
        return False

    def scroll_point(self, items):
        """在 OCR 结果里挑一个「一定落在正文上」的点当滚动落点。

        不取文字簇的包围盒中心 —— 多窗口同屏时那一簇会横跨两三个窗口，
        盒心可能落在窗口边框、dock 甚至桌面上（实测从 (586,484) 漂到
        (772,593)，滚半天画面纹丝不动）。改成**取这一屏最长的几行里、
        行数最密的那个纵向带的中心**：最长的行几乎必然在正文里。
        """
        rows = [i for i in items if len(i["text"].strip()) >= 12]
        if not rows:
            rows = [i for i in items if len(i["text"].strip()) >= 6]
        if not rows:
            return None
        rows.sort(key=lambda i: i["y"])
        # 按 24px 的纵向带分组，取行数最多、且其中位行最长的那个带
        bands = {}
        for i in rows:
            bands.setdefault(int(i["y"] // 24), []).append(i)
        best = max(bands.values(),
                   key=lambda g: (len(g), max(len(i["text"]) for i in g)))
        xs = [i["cx"] for i in best]
        ys = [i["cy"] for i in best]
        xs.sort()
        return int(xs[len(xs) // 2]), int(sum(ys) / len(ys))

    def _press(self, items, down=True, settle=300, window=None, grab=True,
               step=None, pt=None):
        """翻一页，返回翻完那一屏的条目；屏幕一帧没动（真到头）返回 None。

        主路径是**滚轮**，并且必须先把指针挪到正文上：

        - 滚轮只喂给指针底下那层可滚区域。Wayland 下拿不到窗口位置
          （AT-SPI 对非最大化窗口一律报 0,0），所以不猜窗口矩形，直接拿
          `_text_cluster` 从 OCR 结果里找「最长的一簇连续文字」—— 那就是
          当前正在看的正文，指针放它正中，滚的就是它。
        - 实测这个组合是**稳定推进**的：dy=90 一格约 10 行，连续十几页
          依次是 11~20、22~31、33~42、43~52… 没有跳行也没有倒退。
        - 不用 PageUp/PageDown：文本编辑器里按键会连带移动插入点，而 GTK
          会保证插入点可见，于是视图被拽回光标那一屏 —— 实测按十几次
          页码在 049→038→032→021→011→002 之间一路倒退，翻二十屏还在开头。
          键盘只在滚轮完全没反应时兜底（指针被别的窗口整块盖住的情况）。

        返回 None 表示「按完没有任何新画面」，调用方据此判到头。
        """
        # 步长必须让**相邻两屏有重叠**，否则正好骑在缝上的那一行永远看不到
        # —— 实测 dy=90 时一屏 8~9 行、跳 10 行，第 13 屏 140~149、第 14 屏
        # 151~160，目标「LINE 150」就这么从缝里漏过去，翻到末尾也找不到。
        # dy=45 时每屏约 5~6 行、跳 5 行，稳稳重叠。
        st = float(step) if step else 45.0
        sign = 1.0 if down else -1.0
        seq0 = self.frame_seq
        if pt is None:
            pt = self.scroll_point(items)
        if pt is not None:
            self.move(int(pt[0]), int(pt[1]))
            pump(50)
            if grab:
                self.button("left", True)
                pump(14)
                self.button("left", False)
                pump(220)
        self.scroll(0, sign * st)
        self.touch()
        pump(settle)
        self.touch()
        t0 = time.monotonic()
        while self.frame_seq <= seq0 and time.monotonic() - t0 < 0.7:
            pump(40)
        if self.frame_seq > seq0:
            # 12 次 × 60ms 上限 0.72 秒：24 次那版每屏多花 0.5 秒，
            # 五十几屏就是二十几秒，全砸在等一个几乎不会发生的残影上。
            self._still(tries=12, wait=60)
            got = self.ocr(upscale=1)
            if len([i for i in got if i["text"].strip()]) >= 3:
                return got
            # 12 次 × 60ms 上限 0.72 秒：24 次那版每屏多花 0.5 秒，
            # 五十几屏就是二十几秒，全砸在等一个几乎不会发生的残影上。
            self._still(tries=12, wait=60)
            got = self.ocr(upscale=1)
            return got
        # 滚轮没反应：可能是指针不在可滚区，也可能是窗口压根没焦点。
        # 用键盘试一次（键盘只认焦点，不认指针）。
        seq1 = self.frame_seq
        self.key("pagedown" if down else "pageup")
        self.touch()
        pump(settle)
        self.touch()
        t0 = time.monotonic()
        while self.frame_seq <= seq1 and time.monotonic() - t0 < 0.8:
            pump(40)
        if self.frame_seq <= seq1:
            return None                             # 画面纹丝不动：到头
        self._still(tries=12, wait=60)
        return self.ocr(upscale=1)

    def _probe_scroll(self, items, reg, step=90.0):
        """找一个「滚得动、而且滚的就是目标内容」的落点。

        滚轮只喂给指针下面的可滚区域，而窗口在屏幕上的位置拿不到可靠数据
        （Wayland 下 AT-SPI 一律报 0,0）。只能试 —— 但**判据不能是「全屏
        签名变了」**：屏幕上同时开着编辑器、IDE、浏览器时，滚 IDE 的侧边栏
        同样会让全屏像素变化，探滚就会把热点定到 IDE 上，之后整轮都在滚
        别人，翻五十屏也找不到本就在屏幕上的东西（实测过）。

        所以判据改成「**最长那一簇文字**本身变了没」：一屏文字里行距连续的
        最长一簇，十有八九就是当前主窗口的正文；它滚动了才算滚对了。
        落点也只在这簇的范围内试，不去碰屏幕别处。

        返回 (热点区域 or None, 探滚后读到的那一屏 or None)。
        """
        cluster = self._text_cluster(items)
        if cluster:
            bx, by, bw, bh, _lh = cluster
            cands = [(bx + bw // 2, by + bh // 2),
                     (bx + bw // 2, by + bh // 4),
                     (bx + bw // 2, by + bh * 3 // 4),
                     (bx + bw // 4, by + bh // 2),
                     (bx + bw * 3 // 4, by + bh // 2)]
        else:
            cx, cy = reg["x"] + reg["w"] // 2, reg["y"] + reg["h"] // 2
            cands = [(cx, cy)]
        base = self._cluster_sig(items, cluster)
        for (x, y) in cands:
            self.move(int(x), int(y))
            pump(50)
            self.scroll(0.0, step)
            self.touch()
            pump(240)
            self.touch()
            got = self._stable_ocr()
            if self._cluster_sig(got, self._text_cluster(got)) != base:
                return {"x": max(0, int(x) - 60), "y": max(0, int(y) - 60),
                        "w": 120, "h": 120}, got
        return None, None

    @staticmethod
    def _cluster_sig(items, cluster):
        """一簇文字的指纹：簇内的行文本按 y 排序拼起来。

        用它判断「这一簇滚了没」比全屏签名准 —— 屏幕别处发生变化的干扰被
        排除在外，只剩这一簇自己的内容。
        """
        if not items:
            return ""
        if cluster:
            bx, by, bw, bh, _lh = cluster
            sel = [i for i in items
                   if bx <= i["cx"] <= bx + bw and by <= i["cy"] <= by + bh]
        else:
            sel = list(items)
        sel.sort(key=lambda i: (round(i["y"] / 8), i["x"]))
        return "|".join(i["text"].strip().lower() for i in sel[:60])

    @staticmethod
    def _text_cluster(items):
        """找屏幕上「行距连续」的最大那一簇文字，返回 (x, y, w, h, 行高)。

        整屏 OCR 的结果里混着顶栏时钟、dock 图标、窗口标题、菜单 —— 它们的
        外接框覆盖整个屏幕，拿它当「内容区」等于没算。一个文本块内部行距是
        均匀的，跨到顶栏/dock 处会有一个明显的大空隙，那就是簇的边界。
        """
        rows = [i for i in items if i["text"].strip()]
        if len(rows) < 3:
            return None
        rows.sort(key=lambda i: i["y"])
        hs = sorted(float(i["h"]) for i in rows)
        lh = hs[len(hs) // 2]              # 中位行高，别用最小值（顶栏小字带偏）
        best = cur = [rows[0]]
        for prev, nxt in zip(rows, rows[1:]):
            if nxt["y"] - (prev["y"] + prev["h"]) <= lh * 1.6:
                cur.append(nxt)
                continue
            if len(cur) > len(best):
                best = cur
            cur = [nxt]
        if len(cur) > len(best):
            best = cur
        if len(best) < 3:
            return None
        x1 = min(i["x"] for i in best)
        y1 = min(i["y"] for i in best)
        x2 = max(i["x"] + i["w"] for i in best)
        y2 = max(i["y"] + i["h"] for i in best)
        if (x2 - x1) < 80 or (y2 - y1) < 40:
            return None
        return (x1, y1, x2 - x1, y2 - y1, lh)

    def _hold(self, reg, delta, settle, click=False):
        """把指针按到内容区正中滚一格，等画面稳定后再抓一帧。

        滚轮只喂给「有焦点的那个窗口」：实测从没点过的窗口滚多少格画面都纹丝
        不动。所以滚不动的时候要在区域正中点一下拿焦点 —— 这是滚动搜索必须
        付的代价，但只在真的滚不动时才付，平时不点。
        """
        reg = self._norm_region(reg)
        cx, cy = reg["x"] + reg["w"] // 2, reg["y"] + reg["h"] // 2
        self.move(cx, cy)
        pump(60)
        if click:
            self.button("left", True)
            pump(14)
            self.button("left", False)
            pump(140)
        if delta:
            self.scroll(0.0, delta)
        self.touch()
        pump(settle)
        self.touch()
        return cx, cy

    def ocr(self, lang=None, region=None, upscale=None, langs=None,
            tiles=None, psm=None):
        import io
        from PIL import Image

        data, _age = self.shot()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        lw, lh = self.logical or im.size
        k = im.width / float(lw)          # 逻辑 → 整帧图片像素
        off = (0, 0)                      # 裁剪区左上角，始终用「整帧图片像素」
        if region:
            # region 允许 dict / list / tuple 三种写法：模型经常顺手传成一个
            # [x,y,w,h] 数组，早先只认 dict，直接 TypeError 崩在 tuple index
            # 上，看上去像「OCR 坏了」。
            if not isinstance(region, dict):
                region = dict(zip(("x", "y", "w", "h"), list(region)[:4]))
            x = int(float(region["x"]) * k)
            y = int(float(region["y"]) * k)
            w = int(float(region["w"]) * k)
            h = int(float(region["h"]) * k)
            # 越界的 region 会让 crop 出空图，OCR 直接返回 0 条 —— 钳到画面内
            x = max(0, min(x, im.width - 1))
            y = max(0, min(y, im.height - 1))
            w = max(1, min(w, im.width - x))
            h = max(1, min(h, im.height - y))
            im = im.crop((x, y, x + w, y + h))
            off = (x, y)
        # 放大能救小字，但屏幕内容很稀疏时（比如一个全屏窗口里只有一行字）放大
        # 反而会让 tesseract 的版面分析彻底吐不出东西 —— 实测 f=2 时整屏返回
        # 空、f=1 时正常。所以这里不做「一刀切 2 倍」，而是让调用方按需升级：
        # find_text() 先用 1 倍跑一次，没找到再放大重跑。
        if upscale is None:
            upscale = os.environ.get("DSH_WAYLAND_OCR_UPSCALE", "1") or 1
        f = float(upscale)
        # 双通道并行：eng 与 chi_sim 各跑一份再合并。实测（同一帧、同一张图）
        # chi_sim+eng 会整行丢掉拉丁字母（"the button" 完全消失，只剩中文），
        # 而 eng 单跑 139ms 就命中、chi_sim 单跑 159ms 命中中文 —— 两条并行
        # 163ms，等于最慢那条的成本，却是唯一能同时拿到中英文的组合。
        if langs is None:
            if lang:
                langs = [lang]
            else:
                langs = list(filter(None, os.environ.get(
                    "DSH_WAYLAND_OCR_LANGS", "eng,chi_sim").split(",")))
        if len(langs) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(langs)) as ex:
                parts = list(ex.map(
                    lambda lg: self._ocr_image(im, lg, max(1.0, f),
                                               tiles=tiles, psm=psm), langs))
            items = self._rows_to_items(parts[0], off, k, f)
            for extra in parts[1:]:
                items = self._merge_rows(
                    items, self._rows_to_items(extra, off, k, f))
        else:
            items = self._rows_to_items(
                self._ocr_image(im, langs[0], max(1.0, f),
                                tiles=tiles, psm=psm), off, k, f)
        items.sort(key=lambda i: (round(i["y"] / 12), i["x"]))
        return items

    def find_text(self, needle, lang=None, region=None, nth=0):
        """按文字找东西，便宜的先来，找不到再逐步加码。

        三级升级，全都是「只有上一级没命中才做」：
          1. 1 倍图 + 精确/分词包含
          2. 1 倍图 + 模糊匹配（tesseract 常把 "button" 认成 "Dutton"）
          3. 2 倍图重跑一遍整个流程（小字号在 1 倍下认不出）
        屏幕内容稀疏时 2 倍反而会吐不出东西，所以它绝不能是默认值 —— 只有在
        1 倍两次都没找到时才值得付这个代价。
        """
        n = str(needle).strip().lower()
        items = self.ocr(lang=lang, region=region, upscale=1)
        if not n:
            return None, items
        hit = self._match(needle, n, items, nth)
        if hit:
            return self._with_anchor(hit, needle), items

        # 第三级：放大重跑。代价 ~1.5s，只有前面都落空才走。
        items2 = self.ocr(lang=lang, region=region, upscale=2)
        hit = self._match(needle, n, items2, nth)
        if hit:
            return self._with_anchor(hit, needle), items2
        # 还是没找到：回报「内容更丰富的」那一遍，好让报错信息列出屏幕上真正
        # 有什么 —— 稀疏画面下 2 倍会返回空，用它会得到一句毫无帮助的「没找到」。
        return None, (items2 if len(items2) >= len(items) else items)

    def _with_anchor(self, hit, needle):
        """给命中条目补上收紧后的落点 ax/ay（cli ck 用这两个，别看 cx/cy）。"""
        ax, ay = self._anchor(hit, needle)
        out = dict(hit)
        out["ax"], out["ay"] = ax, ay
        return out

    def _in_region(self, items, reg):
        """只留中心落在 reg 里的条目。

        翻页读的是整屏（裁图会丢行），但命中必须落在要搜的那块区域里 ——
        否则窗口外 dock 上、状态栏上的同名文字也会被当成结果点下去。
        """
        if not reg:
            return items
        x1, y1 = float(reg["x"]), float(reg["y"])
        x2, y2 = x1 + float(reg["w"]), y1 + float(reg["h"])
        out = [i for i in items
               if x1 <= i["cx"] <= x2 and y1 <= i["cy"] <= y2]
        return out

    def _signature(self, items, keep=40):
        return "|".join(i["text"] for i in items[:keep])

    def _still(self, tries=8, wait=90, thresh=0.0025):
        """等画面不动了再往下走 —— 判据是像素，不是再跑一遍 OCR。

        早先是「连跑两遍 OCR，文字签名一样就算稳」，稳是稳，但每屏要多付一次
        整屏 OCR（本机 5~12 秒），翻十屏光判稳就烧掉一分多钟。缩略图比对便宜
        三个数量级，效果一样：GTK 的平滑滚动只要还在动，缩略图就有差异。
        """
        import io
        from PIL import Image, ImageChops
        prev = None
        for _ in range(tries):
            data, _age = self.shot()
            im = Image.open(io.BytesIO(data)).convert("L").resize((96, 60))
            if prev is not None:
                diff = ImageChops.difference(im, prev)
                hist = diff.histogram()
                total = float(im.width * im.height)
                moved = sum(hist[12:]) / total      # 差异 >12 灰阶的像素占比
                if moved < thresh:
                    return True
            prev = im
            self.touch()
            pump(wait)
        return False

    def _stable_ocr(self, lang=None, region=None, tries=6, wait=140):
        """画面稳住之后读一次屏。

        判稳交给 _still()（缩略图像素比对），这里只负责读 —— 之前每屏要跑两
        遍整屏 OCR 才敢返回，翻十屏光判稳就一分多钟。
        """
        self._still()
        return self.ocr(lang=lang, region=region, upscale=1)

    def scroll_search(self, needle, lang=None, region=None, nth=0,
                      max_pages=24, delta=None, settle=260, window=None):
        import time as _time
        """当前视野找不到就自己翻页找，翻到底再翻回顶部，最后尽量滚回原处。

        屏幕上「没找到」十次里有九次是内容在下面看不见，不是真的没有 ——
        交给模型自己滚容易滚错方向、滚过头，也白烧几轮往返。

        翻页判据是「滚动后这片区域的文字指纹变了没有」：PipeWire 只在画面变化
        时出帧，静止画面拿到的永远是旧帧，靠帧号判断会误判到底。
        """
        win, pinned = None, None
        if window:
            win, pinned = self.focus_window(window)
        # 提窗失败不当致命错误：窗口多半就在屏幕中间，只是 AT-SPI 报不出它的
        # 真实位置。翻页时在内容区点一下就能拿到焦点（滚轮只喂焦点窗口），
        # 所以照常搜、照常翻，只是把原因带回去让模型知道可能翻在别人身上。
        loose_win = bool(window) and win is None
        # AT-SPI 在 GNOME Wayland 上给非最大化窗口的 extents 恒是 (0,0)，
        # 拿它当区域会框出一片空白（实测 700×305 报成 (0,0)，而窗口其实在
        # 屏幕正中）—— 这种假矩形一律不用。
        if win and (win["x"] <= 0 and win["y"] <= 0
                    and (win["width"] < self.logical[0]
                         or win["height"] < self.logical[1])):
            win = None
        region = self._window_region(win, region)
        if win and region:
            # AT-SPI 报的窗口矩形在 GNOME Wayland 上并不总跟真实位置一致
            # （见过 700×305 报成 (0,0) 而实际在屏幕中间）。区域裁错了会让
            # 首屏读到一片空白、翻页也滚在窗口外面，所以先探一次：读不到几条
            # 东西就说明框歪了，退回整屏。
            probe = self.ocr(region=region, upscale=1)
            if len([i for i in probe if i["text"].strip()]) < 4:
                region = None
        want = str(needle).strip().lower()
        if not want:
            return None, self.ocr(lang=lang, region=region), {
                "pages": 0, "moved": False, "window": pinned}
        # 首屏就用严格阈值：翻页时会连同别的近似行一起被越过，先放一份宽松命中
        # 进来等于永远不翻页（"alpha120" 会撞上 "alpha2"）。
        # 首屏按调用方给的范围（缺省整屏）找：顶栏、dock、状态栏上的字都在
        # 中部 84%×80% 之外，先收窄再搜会让它们永远找不到。收窄只用来决定
        # 「滚哪里」—— 滚动必须落在内容区，否则滚的是壁纸。
        items = self.ocr(lang=lang, region=region, upscale=1)
        hit = self._match(needle, want, items, nth, threshold=STRICT)
        if hit is None:
            items2 = self.ocr(lang=lang, region=region, upscale=2)
            hit = self._match(needle, want, items2, nth, threshold=STRICT)
            if hit is not None:
                items = items2
        if hit is not None:
            return self._with_anchor(hit, needle), items, {
                "pages": 0, "moved": False, "window": pinned}

        reg = self._norm_region(region)   # 只用来定滚动位置与步长
        # 步长必须按「真实内容高度」算，不能按整屏算。窗口只占屏幕一角时
        # （实测 700×305 的编辑器窗口放在 1536×960 的屏幕中间），按整屏高
        # 算出来的步长一步就跨过 1.5 个窗口，中间十几行直接跳过 —— 翻五十
        # 屏也命中不了本就在屏幕上的东西。所以用首屏 OCR 到的文字外接矩形
        # 当内容区估计：这些字在哪里，内容区就在哪里。
        keep_reg = self._norm_region(region)
        # 滚动落点不能瞎猜。窗口常常只占屏幕一角（实测 700×305 的编辑器
        # 飘在 1536×960 屏中间），指针落在窗口外时滚多少格画面都纹丝不动，
        # 会被误判成「到头了」直接放弃 —— 之前翻 50 屏全空就是这么来的。
        # 所以先「探滚」：在几个候选点各滚一格，第一个让画面变化的点就是
        # 真正的滚动热区，后面整轮都用它。
        # 不做「探滚」了：翻页已经改走键盘（PageDown/PageUp），只认焦点、
        # 不认指针位置，探滚那几下滚动反而是搅局 —— 它会把指针挪到别处、
        # 顺手滚乱别的窗口（IDE 的侧边栏、dock 的图标列表），之后翻页就在
        # 别人的窗口上翻，二十屏毫无进展。焦点统一交给 `_grab_focus`。
        # 一屏能看到的行数取决于窗口大小，步长按 reg 高度算再封顶 ——
        # 封顶是为了「一次别跨太多行」：本机 dy≈2.7 逻辑像素，140 约合 52 行，
        # 已经能盖住普通窗口一屏，再大会把中间整段跳过去。
        # 步长取「可视高度的 1/6 折成滚轮单位」，宁小勿大：两屏必须重叠，
        # 否则骑在缝上的那一行永远看不到（dy=90 时一屏 8~9 行、跳 10 行，
        # 「LINE 150」卡在第 13 屏 140~149 与第 14 屏 151~160 之间被跳过）。
        # 步长按「一屏三分之一」给，再靠 advance 里的两屏重叠校验兜住漏行：
        # 给大了重叠校验会发现跳行并退半步补，给小了页数不够走完文档。
        # 步长按「跨 5~6 行，而一屏能读 8~9 行」给，保证两屏稳叠：dy=90 实测
        # 跨 10 行，而编辑器一屏只有 8~9 行，骑在缝上的那一行必然漏掉。
        step = float(delta if delta is not None
                     else max(40.0, min(90.0, reg["h"] / 2.7 / 5.0)))
        net = [0.0]
        pages = 0
        moved = False
        recent = items
        grabbed = [False]          # 焦点已经拿过了没（只拿一次）
        dry = 0                    # 连续几页没出现新长行（判到头用）
        hot_pt = [None]            # 滚动落点，整轮只算一次
        if loose_win:
            # 窗口提不到前台，但焦点也许正好在它身上（用户刚点过、或它本来就
            # 在最上层，只是 AT-SPI 报不出坐标）。先滚一格看画面动没动：
            # 没动就是在翻别人，此时只要点一下就会把焦点送给上层窗口、
            # 之后一路滚错到底 —— 所以一格都不滚，如实交代。
            base = self._signature(self._in_region(recent, keep_reg))
            self._hold(reg, step, settle)
            probe_items = self._stable_ocr(lang=lang)
            if self._signature(self._in_region(probe_items, keep_reg)) == base:
                return None, probe_items, {"pages": 0, "moved": False,
                                           "window": pinned}
            recent = probe_items
            net = [0.0]
            net[0] = step
        # 总时间预算：翻页很便宜但整屏 OCR 不便宜（本机约 1.6 秒一屏）。
        # 用户要的是「找不到就自己翻翻看」，不是「卡在这儿翻两分钟」。
        # 150 秒是实测出来的下界：199 行的文档，从第 150 行找第 88 行要先翻到
        # 底再往回，53 屏 × 1.8 秒就是 95 秒，90 秒的预算正好卡在半路 ——
        # 用户看到的是「明明有却找不到」。宁可慢一点也要翻完。
        budget_s = float(os.environ.get("DSH_WAYLAND_SCROLL_BUDGET", "150"))
        deadline = _time.time() + budget_s
        # 有符号的净位移（单位是 scroll 的 dy），用来最后一格不差地滚回原位：
        # 正着翻 A 页、又倒着翻 B 页之后，位置是 A-B 而不是 A+B，
        # 按页数瞎滚会把用户的画面滚到别的倍数上去。


        # 滚动落点整轮只算一次，见 scroll_point 的 docstring
        hot_pt[0] = self.scroll_point(self._in_region(items, keep_reg)) \
            or self.scroll_point(items)

        def tex_set(it):
            return {i["text"].strip().lower() for i in it if i["text"].strip()}

        def _ov(a, b):
            return len(a & b) / float(len(a | b) or 1)

        def roll(sign, st, click=False):
            self._hold(reg, sign * st, settle, click=click)
            net[0] += sign * st
            # 抓帧一律读整屏，不读 region：同一帧裁成窗口那块再喂 tesseract，
            # 版面分析会把大片纯色背景当成一个图块，整行整行地丢字 ——
            # 实测整屏 45 条、裁成区域只剩 18 条，"LINE 150" 就是这么丢的。
            # region 只用来决定「滚哪里」，不限制「读哪里」。
            return self._stable_ocr(lang=lang)

        def longset(it):
            """够长、且在内容区里的行。

            两个条件都要：短条目（顶栏时钟、菜单残字）每帧都在变；屏幕边上
            那些 long 却跟正文无关的东西（通知横幅、dock 提示、状态栏）也在
            变 —— 不过滤掉它们，滚到底之后每帧都有「新长行」，dry 永远清零，
            干翻十几屏不停。
            """
            return {i["text"].strip().lower()
                    for i in self._in_region(it, keep_reg)
                    if len(i["text"].strip()) >= 12}

        def advance(sign):
            """翻一页，返回 (items, 是否到头)。

            「到头」判据：**连续三页都没出现新的长行**。

            试过、都不行的判据（别走回头路）：
            - 两屏文字集合重叠率 ≥0.85 就算没动 —— 一屏 16 行翻到下一屏
              11~20 行，重叠率本来就 >0.85，第二屏就误判掉头。
            - 「没有新帧就算到底」—— 编辑器滚到末尾仍会回弹出帧，实测一个
              不存在的词能翻 52 屏、烧掉 88 秒。
            - 全屏文字的新增占比 <0.20 —— 顶栏时钟和 dock 的残字每帧都变，
              分母又被整屏条目摊薄，抖动大到没法用。
            - 「新屏必须看得见上一屏末尾几行，否则回退半步」—— 回退那半步
              本身会让画面看着没前进，dry 计数被它带跑，八页就误判到底。
            只数「新出现的长行」（≥12 字符），脏数据全被过滤，信号最干净。
            漏行靠步长兜：dy 控制在一屏的 1/5 左右，两屏天然重叠。
            """
            nonlocal dry
            cur = self._press(recent, down=(sign > 0), window=window,
                              grab=False, step=step, pt=hot_pt[0])
            if cur is None:
                return recent, True                 # 一帧新画面都没有
            fresh = longset(cur) - longset(recent)
            if fresh:
                dry = 0
            else:
                dry += 1
            return cur, dry >= 3

        # 微调重读的预算：整轮最多折腾 4 次，每次约 2 秒（滚动 + 判稳 + OCR）。
        # 不能给多 —— 每屏都微调的话 90 秒预算只够翻十二屏。
        nudges = [0]
        import re as _re2
        want_groups = _re2.findall(r"\d+", want)

        def _suspect(it):
            """这一屏里有没有「像目标行、但被读歪了」的条目。

            命中它不代表找到了，只代表「这一屏大概就是目标所在的那一屏」，
            值得滚个半格重读一遍 —— OCR 的错认跟裁切边界强相关，同一行文字
            上下挪十几像素就换一种错法（实测同一行依次被读成
            "157 aLphal57 bravo157…"、"辆 LINH 157 alpha157 bravo157…"、
            "LINE 158 aLpha158…"），多换几个位置总有能读对的一次。

            数字必须**整组等值**才算疑似，不能用子串包含：needle 是
            "alpha88" 时用 "88" 做包含判断，屏幕上 089、188、288… 全都命中，
            于是每一屏都触发微调重读，八次 OCR 烧掉十六秒，90 秒预算翻十二屏
            就没了（实测 "alpha88 bravo88" 就是这样 139 秒翻空）。
            """
            if not want_groups:
                return False
            h = want_groups[0]
            for i in it:
                if len(i["text"].strip()) < 8:
                    continue
                for g in _re2.findall(r"\d+", i["text"]):
                    if num_sim(h, g) >= 0.85:
                        return True
            return False

        def retry(needle_, want_, items_, nth_, base_txt):
            """疑似目标屏被读歪：小步来回挪几格重读，直到命中或用完预算。

            挪动全部记进 net，最后按净位移复原时位置不会漂。
            """
            if nudges[0] >= 4 or _time.time() > deadline:
                return None
            for off in (-0.45, 0.3, -0.2, 0.5):
                if nudges[0] >= 4 or _time.time() > deadline:
                    return None
                delta_off = off * step
                self._hold(reg, delta_off, 200)
                net[0] += delta_off
                nudges[0] += 1
                got = self._stable_ocr(lang=lang)
                found_ = self._match(needle_, want_,
                                     self._in_region(got, keep_reg),
                                     nth_, threshold=STRICT)
                if found_:
                    return found_
                if not _suspect(got) and not _suspect(items_):
                    return None
                items_ = got
            return None

        def sweep(sign, budget):
            nonlocal pages, moved, recent
            for _ in range(budget):
                if _time.time() > deadline:
                    return None
                cur, end = advance(sign)
                pages += 1
                moved = True
                if end:
                    recent = cur
                    return None
                recent = cur
                # ⚠️ 过滤掉行号对不上 needle 的条目再匹配。
                #
                # `_in_region` 只按坐标过滤，挡不住「行号读歪」这一大类：
                # 实测第 150 行在屏幕上被 OCR 读成
                # "156 aLphal156 bravo156 chartLie156 detta sta150 echo150"、
                # "和 IN 1560 aLpha156 bravo150 charLie156 …"、
                # "辆 LINH 156 aLphal156 bravo156…" —— 三种读法都把行号读成
                # 156（含 150 只是巧合），于是「含 150 但不是第 150 行」，
                # 严档拒掉是**正确**的；而真正的第 150 行在同一帧里根本
                # 没被读出来。这种漏检没法靠放宽阈值救 —— 放宽就会点错行。
                # 只能靠步长兜：步长控制在一屏的 1/5，同一行会在相邻几屏
                # 里被反复重读，只要有一次读对就命中（实测 22~23 屏之间
                # 换个偏移就出现 "LINE 150 alpha150 bravo150"）。
                found = self._match(needle, want,
                                    self._in_region(cur, keep_reg),
                                    nth, threshold=STRICT)
                if found:
                    return found
                if _suspect(cur):
                    # 这一屏有像目标的行却不肯命中 —— 多半是行号被读歪了。
                    # 微调重读比继续往下翻便宜：继续翻要再走一整屏才可能
                    # 重新读到这行，而微调每次只挪半格。
                    got = retry(needle, want, cur, nth, found)
                    if got:
                        return got
            return None

        found = sweep(1.0, max_pages)
        if found is not None:
            return self._with_anchor(found, needle), recent, {
                "pages": pages, "moved": moved,
                "window": pinned}
        # 往下没有，从当前位置一路往回翻（会越过起点继续往上）。
        # 反向必须给足预算：正向常常几屏就到尾（起点本来就靠下），这时从尾巴
        # 往回找靠上的东西要走完**整个文档**。按 pages+4 算会半路截断 ——
        # 实测从 145 行翻到尾再往回，走到 90 行就没预算了，而目标在 88 行。
        # 放宽到 2×max_pages 不会失控：到底由「连续三页没新长行」判，
        # 总时长由 DSH_WAYLAND_SCROLL_BUDGET 兜底。
        found = sweep(-1.0, max_pages * 2)
        if found is not None:
            return self._with_anchor(found, needle), recent, {
                "pages": pages, "moved": moved,
                "window": pinned}
        # 两头都没找到：按净位移滚回原处，别把用户的画面丢在别处
        back_net = net[0]
        while abs(back_net) > 1.0:
            unit = min(abs(back_net), step)
            self._hold(reg, -np_sign(back_net) * unit, 120)
            back_net -= np_sign(back_net) * unit
        self.touch()
        back = self.ocr(lang=lang, region=region, upscale=1)
        loose = self._match(needle, want, self._in_region(back, keep_reg), nth,
                            threshold=0.72)
        if loose is not None:
            # 翻遍了也没有精确的，退回最像的一条，但如实标成模糊命中
            out = self._with_anchor(loose, needle)
            out["fuzzy"] = True
            return out, back, {"pages": pages, "moved": moved, "window": pinned}
        return None, back, {"pages": pages, "moved": moved, "window": pinned}

    # ── 已知会挡住操作的系统/第三方对话框 ──────────────────────────────────
    def dismiss_dialog(self, dry=False, accept=True):
        """认出「屏幕共享授权」这类模态框并处理掉，顺手报告是谁弹的。

        「接受远程控制」那个弹窗不是本插件弹的：它来自 xdg-desktop-portal-gnome
        的 RemoteDesktopDialog，由别的程序（HopToDesk / ToDesk 之类远控服务）
        调 portal.RemoteDesktop.Start 时触发。它一旦出现就压在画面最上层，
        后面所有按坐标的点击都会打到它身上，所以要先处理。

        accept=False（默认）按「取消/关闭」—— 会掐断正在等授权的那个远控连接。
        accept=True  按「共享/允许」—— 让远控拿到画面，等于默认同意共享。
        用户这边 HopToDesk 要常连，所以默认走 accept=True。

        dry=True 只认不动（给 wayland_dialogs 的查询分支用，避免「看一眼就点」）。
        """
        import difflib
        import re as _re
        items = self.ocr(upscale=2)
        texts = [i["text"] for i in items]
        blob = " ".join(texts).lower()
        # 去空格版本也要留一份：tesseract 经常把英文句子的词间空格吃掉
        # （实测 portal 原文读成 "Anapp wantsto shareyour rscreen."），
        # 拿带空格的原句去 in 就永远匹配不到，弹窗直接漏认。
        blob_ns = _re.sub(r"\s+", "", blob)

        def _hit(mark, txt, txt_ns):
            """这条弹窗特征在不在文本里。

            三级放宽，因为 tesseract 对英文句子的处理很不讲道理：
              ① 原句直接包含；
              ② 去掉所有空格再包含（它常把词间空格吃掉，也可能多吐一个字母：
                 "An app wants to share your screen" 实测读成
                 "Anapp wantsto shareyour rscreen."）；
              ③ 去空格后按最长公共块覆盖率算 —— 22 个字符的句子里多一个 r
                 不该算不同句子。覆盖率 ≥0.82 就算命中。
            """
            mm = mark.lower()
            if mm in txt:
                return True
            mns = _re.sub(r"\s+", "", mm)
            if not mns:
                return True
            if mns in txt_ns:
                return True
            # 漏斗只对**短行**开：拿 22 个字的句子去比整屏 6000 个字，
            # 匹配块会散落在各处、覆盖率轻松破 0.82，等于整屏文字都算
            # 「含这句原话」—— 实测点完共享、弹窗早没了，复查仍报有弹窗。
            # 逐条 OCR 结果都是单行（几十字），拿它比才是有意义的。
            if len(mns) < 8 or len(txt_ns) > 240:
                return False
            sm = difflib.SequenceMatcher(None, mns, txt_ns, autojunk=False)
            got = sum(b.size for b in sm.get_matching_blocks())
            return got / float(len(mns)) >= 0.82

        def _line_texts():
            for i in items:
                t = str(i.get("text", "")).lower()
                yield t, _re.sub(r"\s+", "", t)

        lines = list(_line_texts())

        def _any_hit(m):
            # 整屏拼串只走精确/去空格两档（模糊档在长文本上不可靠，见 _hit
            # 里的说明）；模糊兜底逐行比，单行才几十个字。
            if _hit(m, blob, blob_ns):
                return True
            return any(_hit(m, t, tns) for t, tns in lines)

        strong = [m for m in DIALOG_STRONG if _any_hit(m)]
        weak = [m for m in DIALOG_WEAK if _any_hit(m)]
        hit_mark = strong + weak
        deny, share = DIALOG_DENY, DIALOG_SHARE

        def _plain(t):
            return _re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(t).lower())

        def _btn_rows(items_, mark_y, voc=None, anchor=None):
            """找所有「以取消类词（或 voc 指定的词）开头」的行。

            返回 [(word, 行, 词块)]。deny 和 share 分开调：_btn_rows(items, y)
            找取消/关闭那一排，_btn_rows(items, y, DIALOG_SHARE) 找共享/允许那排。

            anchor 传弹窗文案那一行的 (cx, cy) 时再加两道位置约束 —— portal
            的布局是「文案在上、按钮在下、左右居中对齐」，实测按钮行在文案
            下方约 120~300px。没有这个约束，屏幕上任何一个以 cancel/share
            开头的行（聊天记录、代码、别的窗口的工具条）都会被当成按钮。
            """
            out_ = []
            for h in items_:
                ws = h.get("words") or []
                if not ws:
                    continue
                w0 = _plain(ws[0]["text"])
                if not w0 or len(w0) > 12:
                    continue
                if anchor is not None:
                    acx, acy = anchor
                    dy_ = h["cy"] - acy
                    if not (60.0 <= dy_ <= 400.0):
                        continue
                    if abs(h["cx"] - acx) > 520.0:
                        continue
                for word in (voc or deny):
                    pw_ = _plain(word)
                    if len(pw_) < 3:
                        continue
                    if not (w0.startswith(pw_) or pw_.startswith(w0)):
                        continue
                    if mark_y and min(abs(h["cy"] - y) for y in mark_y) > 300:
                        continue
                    out_.append((word, h, ws[0]))
                    break
            return out_

        def _share_near(items_, row):
            """同一行（或上下 60px 内）有没有「共享/允许」类词块。"""
            for h in items_:
                if abs(h["cy"] - row["cy"]) > 60:
                    continue
                for w in (h.get("words") or []):
                    low = str(w.get("text", "")).lower()
                    if any(s in low for s in share):
                        return True
            return False

        mark_y = [i["cy"] for i in items
                  if any(_hit(m, str(i.get("text", "")).lower(),
                              _re.sub(r"\s+", "", str(i.get("text", "")).lower()))
                         for m in hit_mark)]
        rows = _btn_rows(items, mark_y)

        def _buttons_below():
            """在弹窗文案下方那一条带重新 OCR，专门找按钮行。

            为什么必须这样：整屏 OCR 会漏掉这些按钮。实测一个 672×389 的授权框，
            整屏 4×3 切块只读出 "An app wants to share your screen…"（100 条
            结果里含 share 的仅此一条），"Share" / "Cancel" 两个按钮整块消失 ——
            它们落在某个块的边缘、又只有 18px 高，tesseract 直接吞了。而把
            「文案行下方 150~290px」裁出来单独读，1 倍就有：
                Share  (604, 621)  /  Cancel (892, 621)
            各种切块配置都稳定读到。所以按钮一律走这条定向通道，不指望全屏。
            """
            out_ = []
            for m in hit_mark_rows:
                reg = {"x": max(0.0, m["x"] - 60.0),
                       "y": m["cy"] + 120.0,
                       "w": min(self.logical[0] - max(0.0, m["x"] - 60.0), m["w"] + 120.0),
                       "h": 220.0}
                for tspec in ("1x1", "2x1"):
                    try:
                        got = self.ocr(upscale=2, region=reg, tiles=tspec)
                    except Exception:
                        continue
                    out_ += got
                    if _btn_rows(got, [m["cy"]], anchor=(m["cx"], m["cy"])):
                        break
            return out_

        hit_mark_rows = [i for i in items
                         if any(_hit(m, str(i.get("text", "")).lower(),
                                     _re.sub(r"\s+", "", str(i.get("text", "")).lower()))
                                for m in DIALOG_STRONG)]
        # 弹窗文案那一行拿来当锚点：按钮一定在它下面 60~400px、左右居中。
        # 光靠「以 cancel/share 开头」会在聊天正文、代码、别的窗口工具条上
        # 乱认（实测把「Cancel 馈默认工作区」当成了按钮）。
        anchor = None
        if hit_mark_rows:
            _a = max(hit_mark_rows, key=lambda i: i["w"])
            anchor = (_a["cx"], _a["cy"])
            rows = _btn_rows(items, mark_y, anchor=anchor)
        def _sweep_buttons():
            """不知道弹窗在哪时，横扫屏幕的中下部找按钮行。

            什么时候用：整屏拼串读到了 portal 原文（strong 非空），但逐行没有
            任何一行能当锚点 —— 全屏显示的对话框就是这样：tesseract 把 label
            拆成几段，每段都不完整，于是 _buttons_below 无处可挂。

            portal 的按钮总在中下部、横向居中（实测 1536×960 的屏，按钮行
            cy≈690~750）。所以扫三条带，每条都用 tiles="1x1"（小块必须 1x1，
            4x3 会把 "Cancel" 切成 "Shar Share"，见 _ocr_image 的说明）。
            """
            out_ = []
            L = self.logical or (1536, 960)
            # 条带要盖到 0.98 —— 实测全屏对话框的按钮贴在窗口最底部
            # （1536×960 上 Share 在 cy≈913），只扫到 0.92 就会整个漏掉。
            for y0, hh in ((0.34, 0.16), (0.46, 0.16), (0.58, 0.16),
                           (0.70, 0.16), (0.82, 0.16), (0.90, 0.10)):
                reg = {"x": 0.0, "y": L[1] * y0, "w": float(L[0]), "h": L[1] * hh}
                # 这条带子上有两个陷阱，都实测过，别改回去：
                #   ① **upscale 必须是 1**。同一块按钮带 up=1 读得出
                #      "Share" + "Cancel"，up=2 一个字都读不出（全屏窗口里
                #      只有两行灰字，放大后版面分析彻底吐空）。
                #   ② **psm 6 读不出**，psm 3 / 11 才行。psm 6 假设「一块连续
                #      文字」，而这里是左右两个按钮、中间大片空白。
                for tspec, psm_ in (("1x1", "3"), ("1x1", "11"),
                                    ("2x1", "3"), ("1x1", "6")):
                    try:
                        got = self.ocr(upscale=1, region=reg, tiles=tspec, psm=psm_)
                    except Exception:
                        continue
                    out_ += got
                    if _btn_rows(got, [], anchor=None):
                        return out_
            return out_

        # 强词命中却没有按钮行 → 定向重读下方那一条带（Portal 的按钮就长这样）。
        if hit_mark_rows and not rows:
            for _extra in range(2):
                more = _buttons_below()
                r2 = _btn_rows(more, mark_y or [m["cy"] for m in hit_mark_rows],
                               anchor=anchor)
                if r2:
                    seen = {(i["x"], i["y"], i["text"]) for i in items}
                    items = items + [g for g in more
                                     if (g["x"], g["y"], g["text"]) not in seen]
                    texts = [i["text"] for i in items]
                    rows = r2
                    break
                pump(200)
        # 还是没找到按钮（多半是全屏对话框，逐行读不到完整文案当锚点）→ 横扫。
        if strong and not rows:
            more = _sweep_buttons()
            r3 = _btn_rows(more, [], anchor=None) if more else []
            if r3:
                seen = {(i["x"], i["y"], i["text"]) for i in items}
                items = items + [g for g in more
                                 if (g["x"], g["y"], g["text"]) not in seen]
                texts = [i["text"] for i in items]
                rows = r3
                if anchor is None and hit_mark_rows:
                    _a = max(hit_mark_rows, key=lambda i: i["w"])
                    anchor = (_a["cx"], _a["cy"])
        # ── 「到底有没有弹窗」的判定（这是以前天天误报的地方）──────────────
        # 强词是 portal 对话框的原文特征，正常界面不会出现，见到就算。
        # 弱词（"远程控制"、"共享屏幕"…）单独出现**不算** —— 聊天正文、网页、
        # 编辑器里都可能写着这几个字，实测就因为对话里有一句「接受远程控制」
        # 而屏幕上根本没有弹窗，工具却报「认到弹窗」。
        # 弱词要升格成「有弹窗」，必须同时满足：真找到以取消类词开头的按钮行，
        # 且那个按钮旁边还有「共享/允许」类按钮 —— 这才是授权框的形状。
        found = bool(strong)
        if not found and weak and rows:
            found = any(_share_near(items, h) for _w, h, _bw in rows)
        if not found:
            return {"found": False, "mark": [], "marks": [], "weak": weak[:4],
                    "accept": bool(accept), "dry": bool(dry)}
        def _find_btn(items_, word, voc=None):
            """在 items 里找「以 word 开头」的按钮行，返回 (行, 词块) 或 None。

            voc 传哪套词表就按哪套找：默认取消/关闭那排，传 DIALOG_SHARE
            找共享/允许那排。
            """
            pw_ = _plain(word)
            if len(pw_) < 3:
                return None
            for word_, h, bw in _btn_rows(items_, mark_y, (voc or None), anchor):
                if _plain(word_) == pw_:
                    return h, bw
            # 严格模式没找到 → 宽松模式再试一次：不套 mark_y 和 anchor 的
            # 位置约束。为什么需要：全屏对话框的按钮贴在窗口最底部（实测
            # 文案 cy≈30、按钮 cy≈913），跟「按钮在文案下方 60~400px」
            # 的常规布局差了十万八千里，严格模式一定全过滤掉。
            # 宽松模式的代价是可能认到聊天正文里的 "cancel"，所以额外要求
            # 这一行很短（≤3 个词块）—— 按钮行就是这么一个孤零零的词。
            for h in items_:
                ws = h.get("words") or []
                if not ws or len(ws) > 3:
                    continue
                w0 = _plain(ws[0]["text"])
                if not w0 or len(w0) > 9 or len(w0) < 3:
                    continue
                if abs(len(w0) - len(pw_)) > 2:
                    continue
                if not (w0.startswith(pw_) or pw_.startswith(w0)):
                    continue
                return h, ws[0]
            # 按钮词本身必须是干净的一个短词：OCR 会把 "Share" 读成
            # "S creen" / "Sharc"，拿它模糊比对会匹配到标题行的 "Screen"，
            # 点下去等于点了窗口标题。所以第一词只认「长度差 ≤2 且有真前缀」。
            for h in items_:
                ws = h.get("words") or []
                if not ws:
                    continue
                w0 = _plain(ws[0]["text"])
                if not w0 or len(w0) > 9 or len(w0) < 3:
                    continue
                if abs(len(w0) - len(pw_)) > 2:
                    continue
                if not (w0.startswith(pw_) or pw_.startswith(w0)):
                    continue
                if mark_y and min(abs(h["cy"] - y) for y in mark_y) > 300:
                    continue
                if anchor is not None:
                    acx, acy = anchor
                    if not (60.0 <= h["cy"] - acy <= 400.0):
                        continue
                    if abs(h["cx"] - acx) > 520.0:
                        continue
                return h, ws[0]
            return None

        if dry:
            if rows:
                w, h, bw = rows[0]
                _pick = None
                _v = share if accept else deny
                for word in _v:
                    _g = _find_btn(items, word, _v)
                    if _g:
                        _pick = _g[0]["text"]
                        break
                return {"found": True, "dismissed": False, "dry": True,
                        "button": _pick or h["text"], "deny_word": w,
                        "accept": bool(accept),
                        "mark": hit_mark[:2], "marks": hit_mark[:2],
                        "texts": texts[:20]}
            return {"found": True, "dismissed": False, "dry": True,
                    "button": None, "deny_word": None,
                    "mark": hit_mark[:2], "marks": hit_mark[:2],
                    "texts": texts[:20]}

        tried = []
        want = share if accept else deny
        for word in want:
            pw = _plain(word)
            if len(pw) < 3:
                continue
            # 按钮的判据是「这行第一个词就是目标词」，不是「行里含目标词」。
            # 正文里出现 cancel/share（比如正在读代码 "you should cancel it"）
            # 时它是句中的第 N 个词，而 portal 的按钮行要么只有 "Cancel"，
            # 要么是 "Cancel   Share" 两词 —— 一律以它开头。
            picked = _find_btn(items, word, want)
            if picked is None:
                tried.append((word, "没找到以它开头的按钮行"))
                continue
            hit, bw = picked
            ax = bw["x"] + bw["w"] / 2.0
            ay = bw["y"] + bw["h"] / 2.0
            # 目标词必须就是落点那个词块本身，别踩到同一行另一个按钮
            _opp = deny if accept else share
            if any(m in str(bw["text"]).lower() for m in _opp):
                tried.append((word, "落点是「%s」，不点" % bw["text"]))
                continue
            # 移动和按下之间必须留出时间：指针刚跳过去就按，GTK 还在处理
            # 上次的 motion 事件，点击会落在旧位置上（实测 pump(20) 时按钮
            # 毫无反应，pump(120) 立刻关掉）。
            self.move(ax, ay)
            pump(140)
            self.button("left", True)
            pump(40)
            self.button("left", False)
            self.touch()
            pump(320)
            # 点完复查：重跑一遍**同一套判据**（dry=True 只认不动），确认弹窗
            # 真的没了。不复用「在 dlg_box 里找文字」的旧写法，有两个坑：
            #   ① 整屏 OCR 读不到这类按钮（实测 100 条结果里一个 "Cancel" 都
            #      没有），所以「按钮还在不在」永远判 False，复查形同虚设；
            #   ② 弱词（"远程控制"/"共享屏幕"）在屏幕别处的聊天正文、网页里
            #      一直写着，拿它当判据会把每次成功关闭都判成失败（实测点击
            #      回调都打出来了，复查仍说弹窗还在）。
            # 统一走 dismiss_dialog(dry=True)：强词是 portal 原文，正常界面不会
            # 出现；弱词必须配上真实按钮行才算数。这样「认」和「复查」永远同源。
            for _try in range(3):
                pump(300)
                if not self.dismiss_dialog(dry=True, accept=accept).get("found"):
                    return {"found": True, "dismissed": True,
                            "button": hit["text"], "mark": hit_mark[:2]}
            # 一次只能点一次：真弹窗点「取消」立刻就没了，没关掉说明认错了，
            # 再往下试另一个词只会越点越偏（实测把正文里的 cancel 逐行点了一遍）。
            return {"found": True, "dismissed": False, "mark": hit_mark[:2],
                    "tried": tried + [(word, "点了没关掉")], "texts": texts[:20]}
        return {"found": True, "dismissed": False, "mark": hit_mark[:2],
                "tried": tried, "texts": texts[:20]}

    def locate(self, needle, lang=None, region=None, nth=0, max_pages=24,
               delta=None, scroll=True, window=None, dismiss=True, accept=True):
        """找东西的统一入口：找不到就翻页，翻页也不行再看是不是被弹窗挡着。

        挡在最上层的通常是「屏幕共享授权」框（HopToDesk / ToDesk 这类远控
        调 portal.RemoteDesktop.Start 时弹的）。它压住整屏，后面按坐标的点击
        全打在它身上。

        默认 accept=True：认出就按「共享/允许」，让远控**拿到画面**、连接照常
        可用 —— 这是本机要的（HopToDesk 常连）。accept=False 才按「取消/关闭」，
        那会掐断正在等授权的连接。dismiss=False 则只 dry 查一眼、写进 meta
        提示调用方，自己不动手。
        """
        if scroll:
            hit, items, meta = self.scroll_search(
                needle, lang=lang, region=region, nth=nth,
                max_pages=max_pages, delta=delta, window=window)
        else:
            hit, items = self.find_text(needle, lang=lang, region=region, nth=nth)
            meta = {"pages": 0, "moved": False}
        if hit is not None:
            return hit, items, meta
        # ── 找不到时处理挡在最上层的授权框 ──────────────────────────────
        # 默认 accept=True 按「共享」：让远控拿到画面（本机 HopToDesk 要常连，
        # 点「取消」会把它的连接掐断，画面就黑给远端了）。
        if dismiss:
            closed = self.dismiss_dialog(accept=accept)
            if closed.get("found"):
                meta["handled"] = closed.get("button") or True
                meta["accepted"] = bool(accept)
                if closed.get("dismissed") or closed.get("button"):
                    if scroll:
                        hit, items2, meta2 = self.scroll_search(
                            needle, lang=lang, region=region, nth=nth,
                            max_pages=max_pages, delta=delta, window=window)
                        meta["pages"] += meta2.get("pages", 0)
                    else:
                        hit, items2 = self.find_text(needle, lang=lang, region=region, nth=nth)
                    if hit is not None:
                        return hit, items2, meta
                    items = items2
        else:
            chk = self.dismiss_dialog(dry=True, accept=accept)
            if chk.get("found"):
                meta["blockedBy"] = (chk.get("marks") or [])[:2] or True
        return None, items, meta

    def _match(self, needle, n, items, nth=0, threshold=0.72):
        """在一批 OCR 结果里找 needle：先精确/分词，再模糊。命中返回条目。

        threshold 管模糊那一档：单屏找可以用宽容一点（就这一屏，错了也看得见），
        翻页找必须收紧，不然「charlie60」会被「charlie5」这种近似行骗到，
        白翻十几页还点错东西。
        """
        if not items:
            return None

        import re as _re
        import difflib
        # 数字必须对得上：alpha120 与 alpha20 的相似度有 0.93，光看比例会点错行。
        # OCR 常把数字读歪（1→l、0→o），所以不比字面比比例，0.85 以上算同一个数。
        want_nums = "".join(_re.findall(r"\d+", n))

        want_groups = _re.findall(r"\d+", n)

        def nums_ok(text):
            """候选里的数字得跟 needle 对得上，否则「alpha7」会认成「alpha74」。

            做法是在候选的数字组序列里滑一个「和 needle 数字组数一样长」的窗口，
            逐组比相似度。这样「alpha7 bravo7」(7,7) 对不上「alpha77 bravo77」
            (77,77)，却认得出「alpha120」被读成「aLphal120」；单纯按字符比例比，
            这两件事区分不开 —— 而点错行正是按文字点击最伤人的错误。
            """
            if not want_groups:
                return True
            groups = _re.findall(r"\d+", text.lower())
            k = len(want_groups)
            if len(groups) < k:
                return False
            def same(w, h):
                # 数值相等优先：正文行号写成 "088"、"007"（三位补零）是常态，
                # 字符比例只有 0.8，按字符比会把真行整片挡掉。
                try:
                    if int(w) == int(h):
                        return True
                except ValueError:
                    pass
                return difflib.SequenceMatcher(None, w, h).ratio() >= 0.85

            for i in range(len(groups) - k + 1):
                win = groups[i:i + k]
                if all(same(w, h) for w, h in zip(want_groups, win)):
                    return True
            return False

        def lit_ok(text):
            """字面包含还不够：needle 以数字结尾时，后面不能再接数字。

            「alpha7」是「alpha74」的前缀，光判包含会把 74 行当成 7 行 —— 这正是
            按文字点击点错地方最常见的一种。
            """
            if not want_nums or not n[-1].isdigit():
                return n in text
            return any(not m.end() < len(text) or not text[m.end()].isdigit()
                       for m in _re.finditer(_re.escape(n), text))

        hits = [i for i in items if lit_ok(i["text"].lower())]
        if not hits:
            parts = [p for p in n.replace("　", " ").split() if p]
            if len(parts) > 1:
                hits = [i for i in items
                        if all(p in i["text"].lower() for p in parts)
                        and lit_ok(i["text"].lower())]
        if not hits:
            # 词级模糊：tesseract 的错认基本是「整个词少/多一个字符」
            # （alpha120 → aLpha1l20），在整行上滑窗会被长度差拖垮，
            # 逐词比就一枪中的，顺带能把落点直接收到那个词上。
            scored = []
            for i in items:
                words = i.get("words") or []
                best, bw = 0.0, None
                for k, w in enumerate(words):
                    cands = [(w["text"], w)]
                    if k + 1 < len(words):
                        cands.append((w["text"] + " " + words[k + 1]["text"], w))
                    for text, ww in cands:
                        low = text.lower()
                        if not nums_ok(low):
                            continue
                        r = difflib.SequenceMatcher(None, n, low).ratio()
                        if r > best:
                            best, bw = r, ww
                if best >= threshold and bw is not None:
                    it = dict(i)
                    it["_word"] = bw
                    scored.append((best, it))
            if scored:
                scored.sort(key=lambda x: (-x[0], x[1]["y"], x[1]["x"]))
                hits = [i for _r, i in scored]
        if not hits:
            # 「按词覆盖」：needle 拆成词，逐个在**同一行**的词表里找对应词。
            # OCR 常把行首的 "LINE" 吞掉或挪到上一行行尾（实测第 150 行被读成
            # "alpha150 bravo150 charlie150 ... L"，前面那行行尾多出一个
            # "LINE"），这时整行/整段的相似度都在 0.7 上下，永远过不了 0.88
            # 的严格档，而目标行明明就在屏幕上。逐词对得上就算命中 ——
            # 数字词额外容忍错认（"150" 对上 "alpha150" 里的数字）。
            toks = [t for t in n.replace("　", " ").split() if t]

            def _tok_best(tk, words, used, fn_first=None):
                """这一行里有没有词对得上 tk，返回 (相似度, 词, 下标)。"""
                best, bw, bi = 0.0, None, None
                for k, w in enumerate(words):
                    if k in used:
                        continue
                    low = w["text"].lower().strip()
                    if not low:
                        continue
                    # 带数字的词必须先过数字关，而且**必须整组对齐**：
                    # ``alpha150`` 对上 ``aLphal50`` 时，光看「有没有一组数字
                    # 相似度够」会放行（150 vs 150，因为 OCR 把 a 读成 l、
                    # 数字串都是 150），但那是**另一行**（真值 156 行里读到
                    # 的 alpha150 属于后一行）。要求候选词的数字组数与个数
                    # 都跟目标词一致，且每组都像 —— 行内错位就此堵死。
                    td = _re.findall(r"\d+", tk)
                    if td:
                        hd = _re.findall(r"\d+", low)
                        if len(hd) != len(td):
                            continue
                        if not all(difflib.SequenceMatcher(None, a, b).ratio() >= 0.90
                                   for a, b in zip(td, hd)):
                            continue
                        # 词里的数字前面那段（alpha / charlie）也得对得上，
                        # 否则 "charlie150" 会当成 "alpha150"
                        stem = _re.split(r"\d+", tk)[0]
                        if stem and not _re.split(r"\d+", low)[0].startswith(stem[:2]):
                            continue
                    if tk.isdigit():
                        r = difflib.SequenceMatcher(
                            None, tk, "".join(_re.findall(r"\d+", low))).ratio()
                        if tk in low:
                            r = max(r, 0.86)
                        # 行号就该锚在原处：行首那个数字块才是"这一行的号"，
                        # 后面内容里的 150（charLie150）只是数字巧合。
                        if fn_first is not None and k != fn_first and r >= 0.99:
                            r = 0.98
                    else:
                        r = difflib.SequenceMatcher(None, tk, low).ratio()
                        if tk in low:
                            r = max(r, 0.90)
                    if r > best:
                        best, bw, bi = r, w, k
                return best, bw, bi

            if len(toks) >= 2:
                # needle 里的第一个数字通常是**行号**，而 OCR 最常犯的错是
                # 把上一行行尾的残渣串到下一行开头，凑出
                # "156 aLphal156 bravo156 charLie150 ..." 这种脏行 —— 它同时
                # 含有 156 和 150，逐词匹配怎么看都像第 150 行，点上去就是
                # 点错行（实测真的点错了）。正文行的行号永远是**这一行第一个
                # 数字词块**，所以拿 needle 的第一个数字组去对它：对不上就
                # 整行作废，对上了再逐词比。这一步同时解决了「150 行被 OCR
                # 混读成内容带 156」的漏检 —— 行首那个 150 是真的。
                # 只有 needle 明显在指「某一行」时才启用行首硬闸：
                # "LINE 150"、"line 150"、"150" 这种，第一个数字就是行号。
                # "charlie5" 这类内容词不能套这条（它的 5 是内容不是行号）。
                _toks0 = [t for t in n.replace("　", " ").split() if t]
                _headish = bool(_toks0) and (
                    _toks0[0].lower().startswith("line")
                    or _re.fullmatch(r"\d+", _toks0[0]) is not None)
                head_num = want_groups[0] if (want_groups and _headish) else None
                head_strict = head_num is not None and n.find(head_num) <= 8

                def _sim(a, b):
                    return difflib.SequenceMatcher(None, a, b).ratio()

                _nsim = num_sim

                scored = []
                for i in items:
                    words = i.get("words") or []
                    if len(words) < 2:
                        continue
                    fn_k = next((k for k, w in enumerate(words)
                                 if _re.findall(r"\d+", w["text"].lower())), -1)
                    if head_strict and fn_k >= 0:
                        # 行首数字要像 needle 的首个数字：0.85 松到能认
                        # 「1590」→「150」（OCR 把 "LINE 150" 连读成一坨），
                        # 又紧到挡住「156」→「150」（0.67）。
                        fg = _re.findall(r"\d+", words[fn_k]["text"].lower())
                        if not (len(fg) == 1 and _nsim(head_num, fg[0]) >= 0.85):
                            continue
                        # 还得有旁证：正文格式是
                        # ``LINE N alphaN bravoN charlieN deltaN echoN``，所以
                        # **行首那个 N 后面跟着的一串数字本来就该全都是 N**。
                        # 要求行内其余数字块里「与行首数字相似度 ≥0.85」的
                        # 占**过半**，伪命中就基本没了：
                        #   - 真第 150 行   150 150 150 150 150 → 5/5 过
                        #   - OCR 读歪一格  150  50 150 150 150 → 4/5 过
                        #   - 脏行（156 混读）156 156 156 150 150 → 0/5 拒
                        # 最后那行是真值第 156 行，只是行尾串进了上一屏的
                        # 残渣 150/150；只按「行内有没有 150」判会把它当成
                        # 第 150 行点下去（实测真的点错了）。
                        others = [_re.findall(r"\d+", w["text"].lower())
                                  for k, w in enumerate(words) if k != fn_k]
                        others = [g[0] for g in others if g]
                        if others:
                            like = sum(1 for o in others
                                       if _nsim(head_num, o) >= 0.85)
                            if like * 2 < len(others):
                                continue
                    for allow_miss in (0, 1):
                        # allow_miss=1 只放过**非数字词**缺失：OCR 常把行首的
                        # "LINE" 整词吞掉，但数字读错就意味着另一行，不能通融。
                        used, anchor_w, miss, prev_bi = set(), None, 0, -9
                        anchor_bi = -1
                        for tk in toks:
                            best, bw, bi = _tok_best(tk, words, used,
                                                    fn_first=fn_k if fn_k >= 0 else None)
                            if best < 0.80 or bw is None:
                                if tk.isdigit() or allow_miss == 0:
                                    miss = -1
                                    break
                                miss += 1
                                continue
                            # 相邻的 needle 词必须落在**相邻的词块**上。
                            # 否则「LINE 150」会被 "LINE 156 aLphal50 bravo150"
                            # 这种 OCR 串行的行骗过：LINE 在第 0 块、数字在第
                            # 3 块，中间隔着 156 和 aLphal50 —— 那是第 156 行，
                            # 点上去就是点错行（实测真的点错了）。
                            # 词块顺序必须单调，且**带数字的词必须紧邻前一
                            # 个词**：OCR 常把行号读串（"LINE 150 …" 读成
                            # "LINE 156 aLpha150 bravo150"），放宽到隔两块就
                            # 会把 156 行当成 150 行 —— 点上去就是点错行。
                            # 非数字词（charlie/echo 之类）允许隔一块，OCR
                            # 偶尔把中间一个词读成碎块。
                            if prev_bi >= 0:
                                gap = bi - prev_bi
                                if gap <= 0 or gap > (1 if tk.isdigit() else 2):
                                    miss = -1
                                    break
                            prev_bi = bi
                            used.add(bi)
                            anchor_w = bw
                            anchor_bi = bi
                        if miss >= 0 and miss == allow_miss and anchor_w:
                            it = dict(i)
                            it["_word"] = anchor_w
                            scored.append((0.80 + 0.1 * (len(toks) - miss), it))
                            break
                if scored:
                    scored.sort(key=lambda x: (-x[0], x[1]["y"], x[1]["x"]))
                    hits = [i for _r, i in scored]
        if not hits:
            scored = []
            for i in items:
                hay = i["text"].lower()
                if not nums_ok(hay):
                    continue
                # 整段比一次，再按 needle 的字符数滑窗比一次（长行里的短目标）
                best = difflib.SequenceMatcher(None, n, hay).ratio()
                if len(hay) > len(n):
                    win = max(1, len(n))
                    for s in range(0, len(hay) - win + 1):
                        r = difflib.SequenceMatcher(None, n, hay[s:s + win]).ratio()
                        if r > best:
                            best = r
                if best >= threshold:
                    scored.append((best, i))
            if scored:
                scored.sort(key=lambda x: (-x[0], x[1]["y"], x[1]["x"]))
                hits = [i for _r, i in scored]
        if not hits:
            return None
        hits.sort(key=lambda i: (i["y"], i["x"]))
        return hits[min(nth, len(hits) - 1)]


QUALITY = int(os.environ.get("DSH_WAYLAND_JPEG_QUALITY", "75"))

# ── 「屏幕共享 / 远程控制」授权框的判据（dismiss_dialog 和 dialogs 共用一套）──
# 这类弹窗来自 xdg-desktop-portal-gnome 的 RemoteDesktopDialog，是别的远控程序
# （HopToDesk / ToDesk / RustDesk…）调 portal.RemoteDesktop.Start 时弹的。
# 本插件只走 org.gnome.Mutter.RemoteDesktop，从来不碰 portal，弹窗不是它弹的。
#
# 判据必须分强弱，不能「读到『远程控制』四个字就算有弹窗」—— 聊天正文、网页、
# 编辑器里都可能写着这几个字（实测就因为对话里有一句「接受远程控制」，工具
# 天天报「认到弹窗」，而屏幕上其实一个弹窗都没有）。
DIALOG_STRONG = (
    "wants to share your screen",       # portal 原文 "...wants to share your screen"
    "allow remote interaction",
    "allow remote input",
    "allow clipboard access",
    "share your screen",
    "共享您的屏幕", "共享你的屏幕",
)
# 弱词只用来在「已经确认有弹窗」之后帮忙定位，或者配合「真有取消按钮」才算数。
DIALOG_WEAK = (
    "allow remote", "remote interaction", "remote input", "clipboard access",
    "远程交互", "远程输入", "共享屏幕", "远程控制",
)
DIALOG_MARKS = DIALOG_STRONG + DIALOG_WEAK
DIALOG_DENY = ("cancel", "取消", "deny", "拒绝", "关闭", "close", "dismiss")
DIALOG_SHARE = ("share", "共享", "允许", "allow")

# 翻页搜索用的模糊阈值。单屏找可以松（0.72），翻页必须紧：宁可多翻一屏，
# 也不能把「alpha2」当成「alpha120」点下去。
STRICT = float(os.environ.get("DSH_WAYLAND_MATCH_STRICT", "0.88"))


# ── 窗口枚举（AT-SPI，只用来拿标题和矩形；GTK4 的控件树喂不出可点节点）──────
def windows():
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    out = []

    def walk(node, depth, limit):
        stack = [(node, 0)]
        while stack:
            n, d = stack.pop()
            if d > limit:
                continue
            try:
                role = n.get_role_name()
            except Exception:
                role = ""
            if role in ("frame", "dialog", "window", "alert"):
                try:
                    r = n.get_extents(Atspi.CoordType.SCREEN)
                    st = n.get_state_set()
                    out.append({
                        "title": n.get_name() or "",
                        "role": role,
                        "x": r.x, "y": r.y, "width": r.width, "height": r.height,
                        "active": bool(st.contains(Atspi.StateType.ACTIVE)),
                    })
                except Exception:
                    pass
                continue
            try:
                for i in range(n.get_child_count()):
                    c = n.get_child_at_index(i)
                    if c is not None:
                        stack.append((c, d + 1))
            except Exception:
                pass

    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        try:
            walk(app, 0, 2)
        except Exception:
            pass
    return out


# ── 谁在抢屏幕共享（弹窗归因用）──────────────────────────────────────────
REMOTE_AGENTS = ("hoptodesk", "todesk", "rustdesk", "anydesk", "sunlogin",
                 "teamviewer", "gnome-remote-desktop", "x11vnc")


def remote_agents():
    """找出本机上正在跑、会去要 portal 屏幕共享授权的远控程序。

    「接受远程控制」弹窗不是本插件弹的 —— 本插件全程只用
    org.gnome.Mutter.RemoteDesktop，不走 portal。真正弹它的是这些远控服务：
    它们每次重启都会重新调 portal.RemoteDesktop.Start，于是又弹一次。
    这个函数只读 /proc，不碰它们的进程。
    """
    out = []
    me = os.getpid()
    try:
        pids = os.listdir("/proc")
    except OSError:
        return out
    for pid in pids:
        if not pid.isdigit() or int(pid) == me:
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        low = raw.lower()
        for name in REMOTE_AGENTS:
            if name in low:
                out.append({"pid": int(pid), "agent": name, "cmd": raw[:160]})
                break
    return out


# ── 请求处理 ────────────────────────────────────────────────────────────────
S = Session()


def b64(meta, data):
    meta["bytes"] = len(data)
    meta["data"] = base64.b64encode(data).decode("ascii")
    return meta


def handle(req):
    m = req.get("method")
    a = req.get("params") or {}

    if m == "ping":
        return {"ok": True, "result": {"pid": os.getpid()}}

    if m == "status":
        return {"ok": True, "result": {
            "alive": S.alive,
            "logical": S.logical,
            "physical": S.physical,
            "scale": S.scale(),
            "frames": S.frame_seq,
            "node": S.node,
            "quality": QUALITY,
            "remoteAgents": remote_agents(),
        }}

    if m == "dialogs":
        S.ensure()
        # accept 决定按哪个按钮：True=共享/允许（让远控拿到画面，默认），
        # False=取消/关闭（会掐断正在等授权的那个远控连接）。
        acc = bool(a.get("accept", True))
        if a.get("close"):
            return {"ok": True, "result": S.dismiss_dialog(accept=acc)}
        # 查询分支必须走和 dismiss_dialog 同一套判据（dry=True 只认不动）：
        # 以前这里另有一套更短的词表、且 found 只看「读到词没有」，于是屏幕上
        # 只要出现「远程控制」四个字（聊天正文、网页、代码注释）就报有弹窗。
        res = S.dismiss_dialog(dry=True, accept=acc)
        return {"ok": True, "result": {
            "found": bool(res.get("found")),
            "marks": res.get("marks") or [],
            "mark": res.get("marks") or [],
            "weak": res.get("weak") or [],
            "button": res.get("button"),
            "accept": acc,
            "agents": remote_agents(),
            "texts": (res.get("texts") or [])[:20],
        }}

    if m == "reset":
        S.teardown()
        S.start()
        return {"ok": True, "result": {"alive": True, "logical": S.logical}}

    if m == "shot":
        S.ensure()
        data, age = S.shot(int(a.get("freshMs", 180)))
        return {"ok": True, "result": b64({
            "logical": S.logical, "physical": S.physical,
            "cursor": True, "ageMs": round(age, 1),
        }, data)}

    if m == "move":
        S.ensure()
        S.move(a["x"], a["y"]); S.touch()
        return {"ok": True, "result": {"x": a["x"], "y": a["y"]}}

    if m == "click":
        S.ensure()
        if a.get("x") is not None and a.get("y") is not None:
            S.move(a["x"], a["y"])
            pump(12)
        btn = a.get("button", "left")
        n = int(a.get("count", 1))
        for i in range(n):
            S.button(btn, True)
            pump(14)
            S.button(btn, False)
            if i + 1 < n:
                pump(70)
        S.touch()
        return {"ok": True, "result": {"button": btn, "count": n}}

    if m == "drag":
        S.ensure()
        S.move(a["fromX"], a["fromY"])
        pump(20)
        S.button("left", True)
        pump(30)
        steps = max(4, int(a.get("steps", 12)))
        for i in range(1, steps + 1):
            t = i / float(steps)
            S.move(a["fromX"] + (a["toX"] - a["fromX"]) * t,
                   a["fromY"] + (a["toY"] - a["fromY"]) * t)
            pump(12)
        pump(40)
        S.button("left", False)
        S.touch()
        return {"ok": True, "result": {"steps": steps}}

    if m == "scroll":
        S.ensure()
        dx = float(a.get("dx", 0)); dy = float(a.get("dy", 0))
        if a.get("x") is not None and a.get("y") is not None:
            S.move(a["x"], a["y"]); pump(10)
        # 一次送太多会被应用当成一坨，切成小步更接近真人滚轮
        steps = max(1, int(a.get("steps", 1)))
        for i in range(steps):
            S.scroll(dx / steps, dy / steps)
            pump(12)
        S.touch()
        return {"ok": True, "result": {"dx": dx, "dy": dy, "steps": steps}}

    if m == "key":
        S.ensure()
        S.key(a["combo"]); S.touch()
        return {"ok": True, "result": {"combo": a["combo"]}}

    if m == "type":
        S.ensure()
        text = a["text"]
        S.type_text(text); S.touch()
        return {"ok": True, "result": {"chars": len(text)}}

    if m == "clipboard":
        S.ensure()
        S.set_clipboard(a["text"])
        return {"ok": True, "result": {"chars": len(a["text"])}}

    if m == "windows":
        S.ensure()
        t0 = time.monotonic()
        ws = windows()
        return {"ok": True, "result": {"windows": ws,
                                       "ms": round((time.monotonic() - t0) * 1000, 1)}}

    if m == "ocr":
        S.ensure()
        t0 = time.monotonic()
        items = S.ocr(lang=a.get("lang"), region=a.get("region"))
        return {"ok": True, "result": {
            "items": items, "count": len(items),
            "logical": S.logical,
            "ms": round((time.monotonic() - t0) * 1000, 1),
        }}

    if m == "click_text":
        S.ensure()
        t0 = time.monotonic()
        hit, items, meta = S.locate(
            a["text"], lang=a.get("lang"), region=a.get("region"),
            nth=int(a.get("nth", 0)),
            max_pages=int(a.get("maxPages", 24)),
            delta=a.get("delta"),
            scroll=a.get("scroll", True),
            window=a.get("window"),
            dismiss=bool(a.get("dismissDialog", True)),
            accept=bool(a.get("acceptShare", True)))
        if hit is None:
            near = [i["text"] for i in items[:min(25, len(items))]]
            raise Failure("屏幕上没找到「%s」%s%s%s%s%s" % (
                a["text"],
                "（已翻 %d 屏）" % meta["pages"] if meta["pages"] else "",
                "（%s）" % meta["window"] if meta.get("window") else "",
                "（顺手按了共享授权框的「%s」）" % meta["handled"]
                if meta.get("handled") else "",
                "（画面上有个授权弹窗挡着，我没动它：%s。要处理就把 dismissDialog 设成 true）"
                % "、".join(meta["blockedBy"]) if meta.get("blockedBy") else "",
                ("；看到的有：" + " | ".join(near)) if near else ""))
        sx, sy = hit.get("ax", hit["cx"]), hit.get("ay", hit["cy"])
        S.move(sx, sy)
        pump(16)
        btn = a.get("button", "left")
        n = int(a.get("count", 1))
        for i in range(n):
            S.button(btn, True)
            pump(14)
            S.button(btn, False)
            if i + 1 < n:
                pump(70)
        S.touch()
        return {"ok": True, "result": {
            "text": hit["text"], "x": sx, "y": sy,
            "box": [hit["x"], hit["y"], hit["w"], hit["h"]],
            "pages": meta.get("pages", 0),
            "fuzzy": bool(hit.get("fuzzy")),
            "ms": round((time.monotonic() - t0) * 1000, 1),
        }}

    if m == "expect_text":
        # 等某个文字出现/消失：把「等页面反应过来」从模型的重试循环里挪进来
        S.ensure()
        deadline = time.monotonic() + int(a.get("timeoutMs", 5000)) / 1000.0
        want = str(a["text"]).lower()
        gone = bool(a.get("gone"))
        seen = False
        last = None
        while True:
            hit, items = S.find_text(a["text"], lang=a.get("lang"),
                                     region=a.get("region"))
            seen = hit is not None
            last = hit
            if seen != gone:
                break
            if time.monotonic() >= deadline:
                break
            # 动一下指针催一帧，静止画面 PipeWire 不出帧
            S.move_rel(1, 1); pump(60); S.move_rel(-1, -1)
            pump(120)
        return {"ok": True, "result": {"found": seen, "gone": gone and not seen,
                                       "text": a["text"],
                                       "hit": last}}

    if m == "launch":
        import shlex
        import subprocess
        S.ensure()
        cmd = a.get("command")
        if not cmd:
            raise Failure("要 command")
        if isinstance(cmd, str):
            argv = shlex.split(cmd)
        else:
            argv = [str(c) for c in cmd]
        try:
            p = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 cwd=os.path.expanduser("~"))
        except FileNotFoundError:
            raise Failure("找不到命令: %s" % argv[0])
        S.touch()
        return {"ok": True, "result": {"pid": p.pid, "argv": argv}}

    if m == "act":
        # 一次调用干完一串动作，只回一张最终截图 —— 模型不用为每一步付一次往返
        S.ensure()
        trace = []
        t0 = time.monotonic()
        if a.get("window"):
            _w, why = S.focus_window(a["window"])
            if why:
                trace.append("窗口没拉到前台：%s" % why)
        for st in a.get("steps") or []:
            op = st.get("op")
            _before = len(trace)
            if op == "move":
                S.move(st["x"], st["y"])
            elif op == "click":
                if st.get("x") is not None:
                    S.move(st["x"], st["y"]); pump(14)
                btn = st.get("button", "left")
                for i in range(int(st.get("count", 1))):
                    S.button(btn, True); pump(14); S.button(btn, False)
                    if i + 1 < int(st.get("count", 1)):
                        pump(70)
            elif op == "type":
                S.type_text(st["text"])
            elif op == "key":
                S.key(st["combo"])
            elif op == "scroll":
                S.scroll(float(st.get("dx", 0)), float(st.get("dy", 0)))
            elif op == "wait":
                pump(float(st.get("ms", 300)))
            elif op == "waitText":
                deadline = time.monotonic() + float(st.get("timeoutMs", 4000)) / 1000.0
                while time.monotonic() < deadline:
                    hit, _items = S.find_text(st["text"], lang=st.get("lang"))
                    if (hit is not None) != bool(st.get("gone")):
                        break
                    S.move_rel(1, 1); pump(70); S.move_rel(-1, -1); pump(110)
            elif op == "clickText":
                hit, items, _meta = S.locate(
                    st["text"], lang=st.get("lang"),
                    nth=int(st.get("nth", 0)),
                    max_pages=int(st.get("maxPages", 24)),
                    delta=st.get("delta"),
                    scroll=st.get("scroll", True),
                    window=st.get("window") or a.get("window"),
                    dismiss=bool(st.get("dismissDialog",
                                        a.get("dismissDialog", True))),
                    accept=bool(st.get("acceptShare", a.get("acceptShare", True))))
                if hit is None:
                    why = "（已翻 %d 屏）" % _meta.get("pages", 0)
                    if _meta.get("window"):
                        why += "（%s）" % _meta["window"]
                    if _meta.get("handled"):
                        why += "（顺手按了共享授权框的「%s」）" % _meta["handled"]
                    if _meta.get("blockedBy"):
                        why += ("（画面上有个授权弹窗挡着，我没动它：%s。"
                                "要处理就把 dismissDialog 设成 true）"
                                % "、".join(_meta["blockedBy"]))
                    trace.append("找不到「%s」%s" % (st["text"], why))
                    raise Failure("第 %d 步找不到「%s」%s" % (len(trace), st["text"], why))
                _ax, _ay = hit.get("ax", hit["cx"]), hit.get("ay", hit["cy"])
                S.move(_ax, _ay)
                pump(16)
                for _i in range(int(st.get("count", 1))):
                    S.button(st.get("button", "left"), True); pump(14)
                    S.button(st.get("button", "left"), False)
                    if _i + 1 < int(st.get("count", 1)):
                        pump(70)
                _pg = _meta.get("pages", 0)
                trace.append("点了「%s」@ (%d, %d)%s" % (
                    st["text"], round(_ax), round(_ay),
                    "（翻了 %d 屏）" % _pg if _pg else ""))
            elif op == "launch":
                import shlex
                import subprocess
                argv = shlex.split(st["command"]) if isinstance(st["command"], str) else st["command"]
                subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                 start_new_session=True, cwd=os.path.expanduser("~"))
            elif op == "sleep":
                time.sleep(float(st.get("seconds", 0.5)))
            else:
                raise Failure("不认识的步骤: %s" % op)
            if len(trace) == _before:   # 步骤自己记了更详细的说明就别再补一条干巴巴的 op 名
                trace.append(op)
            pump(int(st.get("settleMs", 90)))
        S.touch()
        out = {"steps": trace, "ms": round((time.monotonic() - t0) * 1000, 1)}
        data, age = S.shot(int(a.get("freshMs", 250)))
        out["ageMs"] = round(age, 1)
        out.update({"logical": S.logical, "physical": S.physical})
        return {"ok": True, "result": b64(out, data)}

    if m == "shot_crop":
        S.ensure()
        data, age = S.shot(int(a.get("freshMs", 180)))
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(data))
        lw, lh = S.logical or im.size
        sx = im.width / float(lw)
        box = (int(a["x"] * sx), int(a["y"] * sx),
               int((a["x"] + a["w"]) * sx), int((a["y"] + a["h"]) * sx))
        im = im.crop(box)
        scale = float(a.get("scale", 1))
        if a.get("maxWidth") and im.width > a["maxWidth"]:
            scale = a["maxWidth"] / float(im.width)
        if scale != 1:
            im = im.resize((max(1, int(im.width * scale)),
                            max(1, int(im.height * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
        return {"ok": True, "result": b64({"logical": S.logical,
                                           "physical": S.physical,
                                           "ageMs": round(age, 1)}, buf.getvalue())}

    raise Failure("不认识的方法: %s" % m)


def main():
    out = sys.stdout
    while True:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
        except InterruptedError:
            continue
        if r:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = handle(req)
            except Failure as e:
                resp = {"ok": False, "error": str(e)}
            except Exception as e:
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                        "trace": traceback.format_exc()}
            resp["id"] = req.get("id") if isinstance(req, dict) else None
            out.write(json.dumps(resp) + "\n")
            out.flush()
        # 空闲时也把 D-Bus 信号跑掉：剪贴板选区要在整个进程生命里持续服务
        while GLib.MainContext.default().pending():
            GLib.MainContext.default().iteration(False)
    S.teardown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
