# -*- coding: utf-8 -*-
#
# proof of concept with Muse inspired by the amazing invention by @KrisGfx
# https://github.com/krisgfx9/Wireless-Device-Battery-Monitor
#
# hekoo.py -- hekoo Stage 0 (タスクバー帯ホスト、必要最低限)
#
# 兵児帯 (hekoobi) から自称した名前。正式な帯 (DeskBand) でも
# SwiftBar でもない、半端な長さの柔らかい帯をタスクバーに巻く。
#
# Stage 0 の範囲 (BASIC_DESIGN.md §8 Stage 0):
#   - セグメントはテキストのみ。POST /segments/{name} {"text": "..."}
#   - 起動したら API スレッドが立ち、curl で流し込める
#   - オプションなし。設定は全部このファイル冒頭に埋め込み
#   - 描画は Pillow、窓は pywin32 のレイヤード子窓 (Tk レス)
#   - 配置は右起点 + 最大幅キャップだけ。領土交渉の本格版は Stage 1 以降
#
# お手本: krisgfx9/Wireless-Device-Battery-Monitor (Shell_TrayWnd の子窓方式)
# テーマ追従の作法の出典: https://github.com/moses-palmer/pystray/issues/130
#
# 使い方 (Windows):
#   python hekoo.py
#   jo text="9.8C" | curl -s --json @- localhost:19876/segments/weather

import ctypes
import json
import os
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # Windows 以外では API/描画部分だけ import 可能にする (検証用)
    win32api = win32con = win32gui = None

# ---------------------------------------------------------------- 設定 (埋め込み)
HOST = ""                # 既定は全インタフェース (WSL からそのまま叩ける)。このマシンだけに閉じるなら "127.0.0.1"
PORT = 19876
MAX_WIDTH = 480          # 帯の最大幅 px。これを超えた分は出さない
FONT_SIZE = 22           # 大きく読めることが前提
PAD_X = 10               # 帯の左右の余白
GAP = 18                 # セグメント間の余白
TEXT_COLOR = (255, 255, 255, 255)
BG_COLOR = (0, 0, 0, 255)  # 当面は黒プレート (場所の切り分け用。透明に戻すなら (0,0,0,0))
PLACEHOLDER_TEXT = "hekoo"        # 初期状態 (セグメント無し) の名乗り。本人と合意して正式採用
PLACEHOLDER_COLOR = (150, 150, 150, 255)
SHOW_PLACEHOLDER = True           # 空のとき完全に引っ込みたい場合は False
REPOSITION_MS = 1000     # 再配置と変化チェックの周期
MAX_BODY = 64 * 1024     # POST body の上限

NAME_RE = re.compile(r"^[a-z0-9_-]+$")


# ---------------------------------------------------------------- セグメント保持
class SegmentStore:
    """テキストセグメントの保持。HTTP スレッドと窓側の両方から触るので錠前つき。

    並びは「最初に届いた順」。同名の再 POST は中身だけ差し替えて位置は保つ。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}   # name -> text (挿入順)
        self._version = 0

    def upsert(self, name, text):
        with self._lock:
            self._items[name] = text
            self._version += 1

    def delete(self, name):
        with self._lock:
            if name in self._items:
                del self._items[name]
                self._version += 1
                return True
            return False

    def snapshot(self):
        """(version, [(name, text), ...]) を返す。窓側はこれだけ読む。"""
        with self._lock:
            return self._version, list(self._items.items())

    def public_dict(self):
        with self._lock:
            return dict(self._items)


# ---------------------------------------------------------------- HTTP 受け口
def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 常駐物なので黙る
            pass

        def _send_json(self, status, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _name(self):
            # /segments/{name} の name を取り出す。合わなければ None
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "segments" and NAME_RE.match(parts[1]):
                return parts[1]
            return None

        def do_POST(self):
            name = self._name()
            if name is None:
                return self._send_json(404, {"ok": False, "error": "no such path"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return self._send_json(400, {"ok": False, "error": "bad body size"})
            try:
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                text = obj["text"]
                if not isinstance(text, str) or text == "":
                    raise ValueError
            except Exception:
                return self._send_json(400, {"ok": False, "error": "need {\"text\": \"...\"}"})
            store.upsert(name, text)
            return self._send_json(200, {"ok": True, "name": name})

        def do_DELETE(self):
            name = self._name()
            if name is None:
                return self._send_json(404, {"ok": False, "error": "no such path"})
            if store.delete(name):
                return self._send_json(200, {"ok": True, "name": name})
            return self._send_json(404, {"ok": False, "error": "not found"})

        def do_GET(self):
            if self.path == "/health":
                return self._send_json(200, {"ok": True})
            if self.path == "/segments":
                return self._send_json(200, {"ok": True, "segments": store.public_dict()})
            return self._send_json(404, {"ok": False, "error": "no such path"})

    return Handler


def start_api(store, port=PORT):
    """API スレッドを立てる。戻り値はサーバ (テスト用に触れるように)。"""
    server = ThreadingHTTPServer((HOST, port), make_handler(store))
    t = threading.Thread(target=server.serve_forever, name="hekoo-api", daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------- Pillow 描画
def _load_font(size):
    """(font, 使った名前) を返す。

    日本語が化けないことが最優先: Segoe UI は日本語グリフを持たず、
    Pillow は GDI のようなフォントリンク (自動フォールバック) をしないので、
    Segoe UI で描くと日本語は全部豆腐 (□) になる (実機で確認)。
    だから Windows の日本語 UI フォント (Yu Gothic UI 系) から探す。
    """
    candidates = (
        "YuGothR.ttc",   # Yu Gothic UI (Win10/11 の日本語 UI フォント)
        "YuGothM.ttc",
        "meiryo.ttc",    # メイリオ
        "meiryob.ttc",
        "msgothic.ttc",  # MS ゴシック
        "segoeui.ttf",   # ここからは欧文のみ (日本語は豆腐になる)
        "arial.ttf",
        "DejaVuSans.ttf",
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size), name
        except Exception:
            continue
    return ImageFont.load_default(), "(default)"


_FONT, _FONT_NAME = _load_font(FONT_SIZE)


def _fit_text(meas, text, limit):
    """limit px に収まる形を返す。長い場合は末尾を「…」で切り詰める。

    「…」すら入らなければ None (そのセグメントは落とす)。
    手紙を読まずに食べないための最小の縮退: 落とす前に、まず縮める。
    """
    def w(s):
        b = meas.textbbox((0, 0), s, font=_FONT)
        return b[2] - b[0]

    if w(text) <= limit:
        return text
    if w("…") > limit:
        return None
    t = text
    while t and w(t + "…") > limit:
        t = t[:-1]
    return (t + "…") if t else None


def render_strip(items, color=TEXT_COLOR):
    """[(name, text), ...] から帯の画像 (RGBA) を作る。

    MAX_WIDTH に入らない分は、まず「…」で切り詰めて収め、それでも
    入らない分は右から落とす (Stage 0 の領土交渉はここまで)。
    セグメントが無ければ None。
    """
    if not items:
        return None
    meas = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    kept = []
    width = PAD_X * 2
    for _name, text in items:
        gap = GAP if kept else 0
        fitted = _fit_text(meas, text, MAX_WIDTH - width - gap)
        if fitted is None:
            break
        bbox = meas.textbbox((0, 0), fitted, font=_FONT)
        tw = bbox[2] - bbox[0]
        kept.append((fitted, tw, bbox))
        width += gap + tw
    if not kept:
        return None
    height = FONT_SIZE + 14
    img = Image.new("RGBA", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)
    x = PAD_X
    for text, tw, bbox in kept:
        # textbbox の上端オフセットを引いて、見た目で縦中央に寄せる
        y = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((x, y), text, font=_FONT, fill=color)
        x += tw + GAP
    return img


def _to_bgra_premultiplied(img):
    """UpdateLayeredWindow 用に BGRA + premultiplied alpha の bytes にする。"""
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def _log(msg):
    """切り分け用のログ。コンソール起動 (py hekoo.py) のときは標準出力にも出す。

    開発中はコンソールで起動するので画面で見える方が確認が楽 (本人の進め方)。
    ファイルにも残すのは、コンソールなしで常駐させる将来のため。
    """
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hekoo.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- テーマ追従の作法
def apply_theme_ritual():
    """右クリックメニューを OS テーマに追従させる (家の作法)。

    darkdetect で現在のテーマを拾い、uxtheme.dll の序数 135
    (SetPreferredAppMode、序数でしか export されない) に渡す。
    出典: https://github.com/moses-palmer/pystray/issues/130
    """
    try:
        import darkdetect

        mode = {"Light": 0, "Dark": 1}[darkdetect.theme()]
        ctypes.windll["uxtheme.dll"][135](mode)
    except Exception:
        pass  # 作法が効かなくても本体は止めない


# ---------------------------------------------------------------- Win32 ホスト
class StripHost:
    """Shell_TrayWnd のレイヤード子窓として帯を出す。お手本の Create/Reposition 相当。"""

    ID_EXIT = 1001

    def __init__(self, store):
        self.store = store
        self.hwnd = None
        self.tray = None
        self.notify = None
        self._version = -1
        self._size = (0, 0)
        self._client_xy = (0, 0)
        self._class_atom = None
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

    # --- 窓の生成と親子付け ---
    def attach(self):
        self.tray = win32gui.FindWindow("Shell_TrayWnd", None)
        if not self.tray:
            _log("attach: Shell_TrayWnd not found")
            return False
        self.notify = win32gui.FindWindowEx(self.tray, 0, "TrayNotifyWnd", None)
        if self.hwnd is None:
            self._create_window()
        else:
            win32gui.SetParent(self.hwnd, self.tray)
        _log(f"attach: tray={self.tray} notify={self.notify} hwnd={self.hwnd}")
        return True

    def _create_window(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "HekooStripWindow"
        wc.lpfnWndProc = {
            win32con.WM_TIMER: self._on_timer,
            win32con.WM_RBUTTONUP: self._on_rbuttonup,
            win32con.WM_DESTROY: self._on_destroy,
            self._taskbar_created: self._on_taskbar_created,
        }
        if self._class_atom is None:
            self._class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE,
            wc.lpszClassName,
            "hekoo",
            win32con.WS_CHILD | win32con.WS_CLIPSIBLINGS,
            0, 0, 1, 1,
            self.tray, 0, wc.hInstance, None,
        )
        # SetTimer は pywin32 の win32gui に露出していない (実機で確認)。
        # 家の二段構え通り、pywin32 に無いものは ctypes で直接叩く。
        # HWND はポインタ幅なので argtypes も付ける (blit 側と同じ理由)
        user32 = ctypes.windll.user32
        user32.SetTimer.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
        ]
        user32.SetTimer(self.hwnd, 1, REPOSITION_MS, None)

    # --- 配置と転送 ---
    def _reposition(self, width, height):
        """右起点: TrayNotifyWnd の左端に帯の右端を合わせる。"""
        if not self.notify:
            return False
        tx, ty, _tx2, ty2 = win32gui.GetWindowRect(self.tray)
        nx, _ny, _nx2, _ny2 = win32gui.GetWindowRect(self.notify)
        sx = nx - width
        sy = ty + ((ty2 - ty) - height) // 2
        cx, cy = win32gui.ScreenToClient(self.tray, (sx, sy))
        # HWND_TOP + SWP_SHOWWINDOW: 兄弟 (XAML アイランド等) の一番上に置いて見せ続ける。
        # お手本 widget.cpp の PositionMirror と同じ流儀 (SWP_NOZORDER だと埋もれる)
        win32gui.SetWindowPos(
            self.hwnd, win32con.HWND_TOP, cx, cy, width, height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        if (cx, cy) != self._client_xy or (width, height) != self._size:
            _log(f"place: {width}x{height} at client=({cx}, {cy})")
        self._client_xy = (cx, cy)
        self._size = (width, height)
        return True

    def _blit(self, img):
        """Pillow 画像を UpdateLayeredWindow で転送する (pywin32 に無いので ctypes)。"""
        width, height = img.size
        data = _to_bgra_premultiplied(img)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [
                ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
            ]

        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        # ハンドルはポインタ幅。ctypes の既定 (int) だと、受け側 (restype) は
        # 欠け、渡し側 (argtypes 未指定) は OverflowError になる (実機で確認)。
        # 両方を c_void_p で明示する
        user32.GetDC.restype = ctypes.c_void_p
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.CreateDIBSection.restype = ctypes.c_void_p
        gdi32.CreateDIBSection.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint32,
        ]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # 上向き (top-down)
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bits = ctypes.c_void_p()
        hdc_screen = user32.GetDC(0)
        hbitmap = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not hbitmap:
            user32.ReleaseDC(0, hdc_screen)
            return
        ctypes.memmove(bits, data, len(data))
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        old = gdi32.SelectObject(hdc_mem, hbitmap)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

        # pptDst は渡さない (NULL = 位置は変えない)。位置は SetWindowPos が持ち、
        # ULW は表面とサイズだけを更新する。お手本 widget.cpp の PaintStrip と同じ。
        # (座標を渡す方式はスクリーン/クライアント両方を試して実機で不発だった)
        pt_src = POINT(0, 0)
        size = SIZE(width, height)
        user32.UpdateLayeredWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(POINT), ctypes.POINTER(SIZE), ctypes.c_void_p,
            ctypes.POINTER(POINT), ctypes.c_uint32,
            ctypes.POINTER(BLENDFUNCTION), ctypes.c_uint32,
        ]
        ok = user32.UpdateLayeredWindow(
            self.hwnd, hdc_screen, None, ctypes.byref(size),
            hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(blend), 2,  # ULW_ALPHA
        )
        # GetLastError は成功しても消えない (前に失敗した誰かの残り) ので、
        # 失敗したときだけ読む。座標は place ログの担当 (blit 時点では前回の値になる)
        err = ctypes.windll.kernel32.GetLastError() if not ok else 0
        _log(f"blit: size={width}x{height} ok={ok} err={err}")
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbitmap)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)

    # --- メッセージ処理 ---
    def _on_timer(self, hwnd, msg, wparam, lparam):
        if self.tray is None or not win32gui.IsWindow(self.tray):
            self.attach()
            if self.tray is None:
                return 0
        version, items = self.store.snapshot()
        if version != self._version:
            self._version = version
            _log(f"timer: version={version} items={items}")
            img = render_strip(items)
            if img is None and SHOW_PLACEHOLDER:
                # 空でも名札だけは出す: 「常駐したよ、ここに出るよ」が見える方が
                # 確認が楽 (本人の指摘)。本物のセグメントが来たらそちらに譲る
                img = render_strip([("hekoo", PLACEHOLDER_TEXT)], color=PLACEHOLDER_COLOR)
            if img is None:
                win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
                self._size = (0, 0)
            else:
                # 先に表面を更新してから配置して見せる (お手本の Paint → Reposition の順)
                self._blit(img)
                if not self._reposition(img.size[0], img.size[1]):
                    win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        elif self._size != (0, 0):
            self._reposition(*self._size)  # トレイ幅の変化に追随
        return 0

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        # Explorer 再起動。親を探し直して付け直す。セグメントは保持したまま
        self.tray = None
        self._version = -1  # 次のタイマで描き直させる
        self.attach()
        return 0

    def _on_rbuttonup(self, hwnd, msg, wparam, lparam):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_EXIT, "Exit")
        x, y = win32api.GetCursorPos()
        cmd = win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD, x, y, 0, self.hwnd, None
        )
        win32gui.DestroyMenu(menu)
        if cmd == self.ID_EXIT:
            win32gui.DestroyWindow(self.hwnd)
        return 0

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        win32gui.PostQuitMessage(0)
        return 0


# ---------------------------------------------------------------- 入口
def main():
    if win32gui is None:
        raise SystemExit("hekoo のホストは Windows (pywin32) 専用です")
    apply_theme_ritual()
    _log(f"font: {_FONT_NAME}")
    store = SegmentStore()
    start_api(store)
    host = StripHost(store)
    host.attach()

    # Ctrl+C の既定動作 (KeyboardInterrupt) は WNDPROC の中で受けると
    # pywin32 が "Python WNDPROC handler failed" と騒いで飲み込んでしまい、
    # ループが終わらない。SIGINT で PostQuitMessage するのが静かな終わり方
    def _on_sigint(signum, frame):
        win32gui.PostQuitMessage(0)

    signal.signal(signal.SIGINT, _on_sigint)
    win32gui.PumpMessages()


if __name__ == "__main__":
    main()
