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

    def move(self, x, y):
        px, py = self.to_physical(x, y)
        self.call(RDP, self.rd, RDS, "NotifyPointerMotionAbsolute",
                  GLib.Variant("(sdd)", (self.stream, px, py)))

    def move_rel(self, dx, dy):
        self.call(RDP, self.rd, RDS, "NotifyPointerMotionRelative",
                  GLib.Variant("(dd)", (float(dx), float(dy))))

    def button(self, name, down):
        self.call(RDP, self.rd, RDS, "NotifyPointerButton",
                  GLib.Variant("(ib)", (BTN.get(str(name).lower(), 0x110), down)))

    def scroll(self, dx, dy):
        self.call(RDP, self.rd, RDS, "NotifyPointerAxis",
                  GLib.Variant("(ddu)", (float(dx), float(dy), 0)))

    # ── 键盘 ────────────────────────────────────────────────────────────────
    def keycode_down(self, code, down):
        # Mutter 收的就是裸 evdev 码（30 = KEY_A），不用做 X11 的 +8 换算。
        self.call(RDP, self.rd, RDS, "NotifyKeyboardKeycode",
                  GLib.Variant("(ub)", (code, down)))

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
    def _ocr_image(self, im, lang, f):
        """把一张（已裁剪的）图放大 f 倍后横切成几条并行识别。

        tesseract 是单线程的，整屏 2560 宽放大 2 倍一次要 3.2s，切成 8 条各跑
        一个进程只要 ~1s，识别结果一样 —— 这是 OCR 这条慢路径上最值得做的优化。
        条与条之间留一点重叠，避免正好压线的一行被切断。

        两个用实测换来的参数，别改回去：
        - `--psm 3` 而不是 11。整屏截图上 psm 11 会把窗口那 1px 边框当成一行
          字，吐出一个横跨全宽 5120px 的假行（识别成 "WILL"），真文字全被它
          吞掉；psm 3 同时又快 20%。
        - 放大要 2 倍。小字号在 1 倍下必然认错（"the button" → "the Pen"）。
        """
        import subprocess
        import tempfile
        from concurrent.futures import ThreadPoolExecutor

        from PIL import Image

        if f and f != 1:
            im = im.resize((int(im.width * f), int(im.height * f)), Image.LANCZOS)
        total_h = im.height
        bands = max(1, int(os.environ.get("DSH_WAYLAND_OCR_BANDS", "8")))
        # 太窄的图切了反而更慢（进程启动开销占比高）
        if total_h < 400 or im.width < 400:
            bands = 1
        ov = int(24 * max(1, f)) if bands > 1 else 0

        def run(args):
            top, bot = args
            part = im.crop((0, max(0, top - ov), im.width, min(total_h, bot + ov)))
            with tempfile.TemporaryDirectory() as td:
                png = os.path.join(td, "s.png")
                part.save(png, format="PNG", compress_level=1)
                try:
                    out = subprocess.run(
                        ["tesseract", png, "stdout", "-l", lang, "--psm", "3", "tsv"],
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
                # part 的 y=0 对应 im 的 max(0, top-ov)，换算回整图再判归属
                y0 = max(0, top - ov) + t
                cy = y0 + h / 2.0
                # 只在「本条的负责区间」内收，重叠区的字交给邻居，避免重复
                if bands > 1 and not (top <= cy < bot):
                    continue
                rows.append((text, l, y0, w, h))
            return rows

        spans = []
        for i in range(bands):
            top = (total_h * i) // bands
            bot = total_h if i == bands - 1 else (total_h * (i + 1)) // bands
            spans.append((top, bot))
        if bands == 1:
            rows = run(spans[0])
        else:
            with ThreadPoolExecutor(max_workers=bands) as ex:
                rows = [r for part in ex.map(run, spans) for r in part]
        # 坐标一律按「放大后的图」原样返回，除以 f 和 k 的换算只在 ocr() 里做
        # 一次 —— 两边各除一次 f 会让结果缩成一半（f=2 时正好看起来「勉强对」），
        # 是最难发现的那种错。
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
                        keep = False
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
        return out

    def _rows_to_items(self, rows, off, k, f):
        """原始词框 → 对外条目：换算坐标，并把同一行的词并成一条。

        模型看到的「一段文字」才是一次可点的目标；不并的话 "the button" 会变成
        两条孤立结果，按文字找就永远差半个词。
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
                    last["text"] += it["text"]
                else:
                    last["text"] += " " + it["text"]
                left = min(last["x"], it["x"])
                right = max(last["x"] + last["w"], it["x"] + it["w"])
                top = min(last["y"], it["y"])
                bot = max(last["y"] + last["h"], it["y"] + it["h"])
                last["x"], last["y"] = left, top
                last["w"], last["h"] = round(right - left, 1), round(bot - top, 1)
            else:
                merged.append(dict(it))
        for m in merged:
            m["cx"] = round(m["x"] + m["w"] / 2, 1)
            m["cy"] = round(m["y"] + m["h"] / 2, 1)
        return merged

    def ocr(self, lang=None, region=None, upscale=None, langs=None):
        import io
        from PIL import Image

        data, _age = self.shot()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        lw, lh = self.logical or im.size
        k = im.width / float(lw)          # 逻辑 → 整帧图片像素
        off = (0, 0)                      # 裁剪区左上角，始终用「整帧图片像素」
        if region:
            x, y, w, h = (int(region["x"] * k), int(region["y"] * k),
                          int(region["w"] * k), int(region["h"] * k))
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
                    lambda lg: self._ocr_image(im, lg, max(1.0, f)), langs))
            items = self._rows_to_items(parts[0], off, k, f)
            for extra in parts[1:]:
                items = self._merge_rows(
                    items, self._rows_to_items(extra, off, k, f))
        else:
            items = self._rows_to_items(
                self._ocr_image(im, langs[0], max(1.0, f)), off, k, f)
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
            return hit, items

        # 第三级：放大重跑。代价 ~1.5s，只有前面都落空才走。
        items2 = self.ocr(lang=lang, region=region, upscale=2)
        hit = self._match(needle, n, items2, nth)
        if hit:
            return hit, items2
        # 还是没找到：回报「内容更丰富的」那一遍，好让报错信息列出屏幕上真正
        # 有什么 —— 稀疏画面下 2 倍会返回空，用它会得到一句毫无帮助的「没找到」。
        return None, (items2 if len(items2) >= len(items) else items)

    def _match(self, needle, n, items, nth=0):
        """在一批 OCR 结果里找 needle：先精确/分词，再模糊。命中返回条目。"""
        if not items:
            return None

        hits = [i for i in items if n in i["text"].lower()]
        if not hits:
            parts = [p for p in n.replace("　", " ").split() if p]
            if len(parts) > 1:
                hits = [i for i in items
                        if all(p in i["text"].lower() for p in parts)]
        if not hits:
            import difflib
            scored = []
            for i in items:
                hay = i["text"].lower()
                # 整段比一次，再按 needle 的字符数滑窗比一次（长行里的短目标）
                best = difflib.SequenceMatcher(None, n, hay).ratio()
                if len(hay) > len(n):
                    win = max(1, len(n))
                    for s in range(0, len(hay) - win + 1):
                        r = difflib.SequenceMatcher(None, n, hay[s:s + win]).ratio()
                        if r > best:
                            best = r
                if best >= 0.72:
                    scored.append((best, i))
            if scored:
                scored.sort(key=lambda x: (-x[0], x[1]["y"], x[1]["x"]))
                hits = [i for _r, i in scored]
        if not hits:
            return None
        hits.sort(key=lambda i: (i["y"], i["x"]))
        return hits[min(nth, len(hits) - 1)]


QUALITY = int(os.environ.get("DSH_WAYLAND_JPEG_QUALITY", "75"))


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
        hit, items = S.find_text(a["text"], lang=a.get("lang"),
                                 region=a.get("region"),
                                 nth=int(a.get("nth", 0)))
        if hit is None:
            near = [i["text"] for i in items[:min(25, len(items))]]
            raise Failure("屏幕上没找到「%s」%s" % (
                a["text"], ("；看到的有：" + " | ".join(near)) if near else ""))
        sx, sy = hit["cx"], hit["cy"]
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
        for st in a.get("steps") or []:
            op = st.get("op")
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
                hit, items = S.find_text(st["text"], lang=st.get("lang"),
                                         nth=int(st.get("nth", 0)))
                if hit is None:
                    trace.append("找不到「%s」" % st["text"])
                    raise Failure("第 %d 步找不到「%s」" % (len(trace), st["text"]))
                S.move(hit["cx"], hit["cy"]); pump(16)
                S.button(st.get("button", "left"), True); pump(14)
                S.button(st.get("button", "left"), False)
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
