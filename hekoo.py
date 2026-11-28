# -*- coding: utf-8 -*-
#
# proof of concept with Muse inspired by the amazing invention by @KrisGfx
# https://github.com/krisgfx9/Wireless-Device-Battery-Monitor
#
# hekoo.py -- hekoo Stage 0 + 画像セグメント (タスクバー帯ホスト、必要最低限)
#
# 兵児帯 (hekoobi) から自称した名前。正式な帯 (DeskBand) でも
# SwiftBar でもない、半端な長さの柔らかい帯をタスクバーに巻く。
#
# 範囲 (BASIC_DESIGN.md §8):
#   - セグメントはテキストと画像 (同封可)。POST /segments/{name} で
#     {"text": ...} / {"image": ...} / その両方を投げる。包みとして
#     {"segment": {...}} の形でもよい (受け口で吸収する)。
#     text も image も、str でも list でもよい (受け口でリストに正規化)。
#     text の断片は意味の塊として扱い、描画では途中では切らず、
#     後ろの断片から落として収める。並びは 画像 (リスト順) → テキスト。
#     POST は丸ごと差し替え (部分マージはしない。マージだと投げる側が
#     「今ホストに何が残っているか」を維持し続ける羽目になる)
#   - 画像は方向に無頓着 (縦帯でも矩形のまま住める)。受け口で1回だけ
#     デコードして実物で保持し、帯の厚みに合わせて縮小して貼る
#   - 起動したら API スレッドが立ち、curl で流し込める
#   - オプションなし。設定は全部このファイル冒頭に埋め込み
#   - 描画は Pillow、窓は pywin32 のレイヤード子窓 (Tk レス)
#   - 配置は右起点ゾーンと左起点ゾーンの2つ。セグメントは POST の
#     zone で本籍を申告する (無申告は右起点)。タスクボタン群の端は
#     UIA 観測スレッドが測り、帯はゾーンの最大幅に収めて被せない。
#     右起点は先住人への回避 (avoid) つき。左起点の開始オフセットは
#     POST /offset で申告する (右起点はトレイ端が観測できるので不要)
#
# お手本: krisgfx9/Wireless-Device-Battery-Monitor (Shell_TrayWnd の子窓方式)
# テーマ追従の作法の出典: https://github.com/moses-palmer/pystray/issues/130
#
# 使い方 (Windows):
#   python hekoo.py
#   jo text="9.8C" | curl -s --json @- localhost:19876/segments/weather
#   jo image="$(base64 -w0 plate.png)" | curl -s --json @- localhost:19876/segments/snow
#   jo text="2cm" image="$(base64 -w0 emoji_u2603.png)" | curl -s --json @- localhost:19876/segments/snow
#   jo segment=$(jo image="$(base64 -w0 emoji_u2603.png)" text=$(jo -a "\-10度" 2cm 1cm/h)) | curl -s --json @- localhost:19876/segments/snow

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import ctypes
import io
import json
import logging
import queue
import re
import signal
import sys
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import uiautomation as uia
except ImportError:     # 無い環境ではゾーン観測だけ止める (帯は従来動作で続く)
    uia = None

from getLog import getLog
from utils import resource_path

try:
    import win32api
    import win32con
    import win32gui
except ImportError:     # Windows 以外では API/描画部分だけ import 可能にする (検証用)
    win32api = win32con = win32gui = None


def getVersion():
    v = 'test'
    try:
        with open(resource_path('Assets/version.txt')) as fd:
            v = fd.read().strip().removeprefix('v')
    except Exception:
        pass
    return v


# ---------------------------------------------------------------- 設定 (埋め込み)
HOST = ""                       # 既定は全インタフェース (WSL からそのまま叩ける)。このマシンだけに閉じるなら "127.0.0.1"
PORT = 19876
VERSION = getVersion()
MAX_WIDTH = 480                 # 帯の最大幅 px。これを超えた分は出さない
FONT_SIZE = 22                  # 大きく読めることが前提
PAD_X = 10                      # 帯の左右の余白
GAP = 18                        # セグメント間の余白
PART_GAP = 6                    # セグメント内の画像と文字の間隔
TEXT_COLOR = (255, 255, 255, 255)
TEXT_STROKE = 1                 # 文字の縁取りの太さ px。プレートが透明でも輪郭が立つように
STROKE_COLOR = (0, 0, 0, 255)   # 縁取りの色。黒プレートの上では同化して見えない
BG_COLOR = (0, 0, 0, 255)       # プレートの色 (render_strip の既定値。ホストは下の alpha で上書きする)
PLATE_ALPHA_MIN = 1             # プレート alpha の下限。見た目は透明だが当たり判定は残る
PLATE_ALPHA_MAX = 255           # プレート alpha の上限 (不透明)
PLACEHOLDER_TEXT = "hekoo"      # 初期状態 (セグメント無し) の名乗り。本人と合意して正式採用
PLACEHOLDER_COLOR = (150, 150, 150, 255)
SHOW_PLACEHOLDER = True         # 空のとき完全に引っ込みたい場合は False
REPOSITION_MS = 1000            # 再配置と変化チェックの周期
ANCHOR_DEADBAND = 8             # アンカーの右戻りで無視する幅 px (これ以下は停泊)
ANCHOR_RESTORE_CYCLES = 60      # 右への大きな戻りを確定するまでの連続観測回数
                                # (タイマ周期 1 秒なので約60秒。先住人の一時的な
                                # 消失・再出現のバタつきを乗り切るための確認待ち)
MAX_BODY = 4 * 1024 * 1024      # POST body の上限 (base64 の画像が入る大きさ)
STRIP_HEIGHT = FONT_SIZE + 14   # 帯の厚み px。画像はこの厚みに合わせて縮小する (縦帯なら厚みは幅になる)
MAX_IMAGE_W = 960               # 受け付ける画像の最大寸法 (超えたら 400)
MAX_IMAGE_H = 256
DEFAULT_TTL = 900               # ttl 申告なしのときの既定値 (秒)。天気ポスターの周期5分の
                                # 3周期分を目安に置いた初期値 (2026-11-22、実測から)
OBSERVE_SEC = 1.0               # ゾーン観測スレッドの周期 (秒)。本体タイマ (1秒) と揃える
LEFT_OFFSET = 0                 # 左起点ゾーンの開始オフセット px の既定値。左隅の先住人は
                                # 多様で観測できないものもいるので、開始位置は POST /offset の
                                # 申告で受ける (この定数は申告が来るまでの初期値)
UIA_MAX_DEPTH = 12              # UIA ツリーを掘る深さの上限

NAME_RE = re.compile(r"^[a-z0-9_-]+$")


# ---------------------------------------------------------------- セグメント保持
class SegmentStore:
    """セグメントの保持。HTTP スレッドと窓側の両方から触るので錠前つき。

    1セグメントは「テキストと画像を同封した1通の手紙」: 中身は
    (texts, images) の組で、どちらか一方だけでも、両方でもよい。
    texts は断片 (意味の塊) のリスト、images はデコード済み画像の
    リストで、どちらも受け口で正規化済み。
    POST は丸ごと差し替えで、部分マージはしない。マージだと投げる側が
    「今ホストに何が残っているか」を維持し続ける羽目になり、隠れた状態は
    後で必ず驚きを呼ぶ (本人と合意した意味論)。
    画像は受け口で1回だけデコードした実物 (PIL Image, RGBA) で持つ。
    描画のたびに base64 をほどいたり PNG を開き直したりはしない
    (タスクトレイのアイコン生成と同じ作法: 作るのは変化したときだけ)。
    並びは「最初に届いた順」。同名の再 POST は中身だけ差し替えて位置は保つ。
    TTL: 各セグメントは失効時刻 (time.monotonic の期限) を持ち、期限を
    過ぎたら purge_expired() が落とす。再 POST のたびに期限は張り直
    される (定期投げが生存確認を兼ねる、という設計原則)。
    ゾーン: 各セグメントは本籍 (zone: "left"/"right") を持ち、帯は
    ゾーンごとに分かれて描かれる。無申告の既定は "right"。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}   # name -> (texts, images, expires_at, zone) (挿入順)
        self._version = 0

    def upsert(self, name, texts=None, images=None, ttl=DEFAULT_TTL, zone="right"):
        expires = time.monotonic() + ttl
        with self._lock:
            self._items[name] = (texts, images, expires, zone)
            self._version += 1

    def purge_expired(self):
        """期限切れのセグメントを落とし、落とした名前のリストを返す。"""
        now = time.monotonic()
        with self._lock:
            expired = [n for n, (_t, _i, exp, _z) in self._items.items()
                       if exp <= now]
            for n in expired:
                del self._items[n]
            if expired:
                self._version += 1
            return expired

    def delete(self, name):
        with self._lock:
            if name in self._items:
                del self._items[name]
                self._version += 1
                return True
            return False

    def snapshot(self):
        """(version, [(name, texts, images, zone), ...]) を返す。窓側はこれだけ読む。"""
        with self._lock:
            return self._version, [(n, ts, im, z) for n, (ts, im, _e, z) in self._items.items()]

    def public_dict(self):
        """GET /segments 用。画像は base64 で返さず寸法だけ出す。

        テキストも画像も、要素が1つなら単体、複数ならリストで返す
        (受け口の正規化と対称な形)。
        zone は既定 (right) でないときだけ付ける。
        """
        with self._lock:
            out = {}
            for name, (texts, images, _exp, zone) in self._items.items():
                t = None
                if texts is not None:
                    t = texts[0] if len(texts) == 1 else list(texts)
                dims = None
                if images is not None:
                    ds = [f"{im.size[0]}x{im.size[1]}" for im in images]
                    dims = ds[0] if len(ds) == 1 else ds
                if dims is not None:
                    out[name] = {"text": t, "image": dims} if t is not None else {"image": dims}
                else:
                    out[name] = t
                if zone != "right":
                    # 既定でないゾーンだけ形に出す (右起点は従来の形のまま)
                    if isinstance(out[name], dict):
                        out[name]["zone"] = zone
                    else:
                        out[name] = {"text": out[name], "zone": zone}
            return out


# ---------------------------------------------------------------- HTTP 受け口
def _normalize_texts(value):
    """text フィールドを断片リストに正規化する。

    str なら [str]、list ならそのまま (要素は全部、空でない str)、
    フィールド自が無ければ None、形が変なら False を返す。
    断片は投げる側が意味で刻んだ塊で、ホストは途中では切らない。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value] if value != "" else False
    if isinstance(value, list):
        if all(isinstance(t, str) and t != "" for t in value):
            return list(value) or None
        return False
    return False


def _normalize_images(value):
    """image フィールドをデコード済み画像のリストに正規化する。

    str なら [画像]、list なら各要素をデコード (全部 base64 文字列)、
    フィールド自が無ければ None、形が変だったり1つでもデコードに
    失敗したら False。デコードはここで1回だけ (SegmentStore 参照)。
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        if not value:
            return None
        out = []
        for v in value:
            if not isinstance(v, str):
                return False
            img = _decode_image(v)
            if img is None:
                return False
            out.append(img)
        return out
    return False


def _normalize_ttl(value):
    """ttl フィールド (秒) を正規化する。

    フィールド自が無ければ (null も同じ) None = 申告なし (既定値が効く)。
    数は寿命の秒数そのもの: デクリメントして 0 になったら期限切れ、
    という数え方をするので、0 はその場で失効する。負数や数でない
    ものは False。"never" のような無期限の特別形式は持たない
    (ホスト再起動で全セグメントが消える以上、無期限は実在しない。
    長く生かしたいものは大きな数を書く。2026-11-22 本人決定)。
    bool は int の顔をして紛れ込むので明示的に弾く。
    値付けの目安は投稿側の作法: 自分の更新周期より長く申告すること。
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value < 0:
        return False
    return value


def _normalize_zone(value):
    """zone フィールド (本籍) を正規化する。

    フィールド自が無ければ (null も同じ) None = 申告なし (既定の
    "right" が効く)。"left"/"right" 以外は False。再 POST で省略
    したら既定に戻る (丸ごと差し替えの一部、という作法は ttl と同じ)。
    """
    if value is None:
        return None
    if value in ("left", "right"):
        return value
    return False


def _normalize_offset(value):
    """offset フィールド (px) を正規化する。

    0 以上の数なら int にして返す。それ以外 (負数、数でないもの、
    フィールド自が無い None) は False。bool は int の顔をして
    紛れ込むので明示的に弾く (ttl と同じ)。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value < 0:
        return False
    return int(value)


def _decode_image(b64):
    """base64 の画像を PIL Image (RGBA) にして返す。変なものは None。

    ここで1回だけデコードして実物で保持するのが肝 (SegmentStore 参照)。
    寸法の上限を超えるものは受けない。
    """
    try:
        raw = base64.b64decode(b64, validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGBA")
    except Exception:
        return None
    if img.size[0] > MAX_IMAGE_W or img.size[1] > MAX_IMAGE_H:
        return None
    return img


def make_handler(store, offsets=None, territory=None):
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

        def _do_offset(self):
            # POST /offset: 左起点オフセットの申告を受ける (セグメントとは別の宛先)
            if offsets is None:
                return self._send_json(404, {"ok": False, "error": "no such path"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return self._send_json(400, {"ok": False, "error": "bad body size"})
            try:
                obj = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(obj, dict):
                    raise ValueError
            except Exception:
                return self._send_json(400, {"ok": False, "error": "need JSON body"})
            value = _normalize_offset(obj.get("offset"))
            if value is False:
                return self._send_json(400, {"ok": False, "error": "bad offset"})
            offsets.set(value)
            _log(f"post: offset={value}")
            return self._send_json(200, {"ok": True, "offset": value})

        def do_POST(self):
            if self.path == "/offset":
                return self._do_offset()
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
                if not isinstance(obj, dict):
                    raise ValueError
            except Exception:
                return self._send_json(400, {"ok": False, "error": "need JSON body"})
            fields = obj.get("segment")
            if not isinstance(fields, dict):
                fields = obj  # 包みが無ければトップレベル自体がフィールド
            texts = _normalize_texts(fields.get("text"))
            if texts is False:
                return self._send_json(400, {"ok": False, "error": "bad text"})
            images = _normalize_images(fields.get("image"))
            if images is False:
                return self._send_json(400, {"ok": False, "error": "bad image"})
            ttl = _normalize_ttl(fields.get("ttl"))
            if ttl is False:
                return self._send_json(400, {"ok": False, "error": "bad ttl"})
            zone = _normalize_zone(fields.get("zone"))
            if zone is False:
                return self._send_json(400, {"ok": False, "error": "bad zone"})
            if texts is None and images is None:
                return self._send_json(
                    400, {"ok": False, "error": "need \"text\" and/or \"image\""})
            if ttl is None:
                effective, source = DEFAULT_TTL, "default"
            else:
                effective, source = ttl, "declared"
            store.upsert(name, texts, images, ttl=effective, zone=zone or "right")
            _log(f"post: {name} ttl={effective:g}s ({source})"
                 + (f" zone={zone}" if zone else ""))
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
            if self.path == "/offset" and offsets is not None:
                return self._send_json(200, {"ok": True, "offset": offsets.get()})
            if self.path == "/territory" and territory is not None:
                return self._send_json(200, {"ok": True, **territory.snapshot()})
            if self.path == "/segments":
                return self._send_json(200, {"ok": True, "segments": store.public_dict()})
            return self._send_json(404, {"ok": False, "error": "no such path"})

    return Handler


def start_api(store, port=PORT, offsets=None, territory=None):
    """API スレッドを立てる。戻り値はサーバ (テスト用に触れるように)。"""
    server = ThreadingHTTPServer((HOST, port), make_handler(store, offsets, territory))
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
        b = meas.textbbox((0, 0), s, font=_FONT, stroke_width=TEXT_STROKE)
        return b[2] - b[0]

    if w(text) <= limit:
        return text
    if w("…") > limit:
        return None
    t = text
    while t and w(t + "…") > limit:
        t = t[:-1]
    return (t + "…") if t else None


def _fit_fragments(meas, fragments, limit):
    """断片リストを limit px に収まる形に繋いで返す。

    断片は投げる側が意味で刻んだ塊 (例: ["-10度", "2cm", "1cm/h"] の
    測定値ひとつひとつ)。途中では切らず、入る所までを半角スペースで
    繋ぎ、後ろの断片から落とす。最初の断片すら入らなければ、その断片
    だけ「…」で切り詰める (単一 str の従来動作)。何も入らなければ None。
    """
    def w(s):
        b = meas.textbbox((0, 0), s, font=_FONT, stroke_width=TEXT_STROKE)
        return b[2] - b[0]

    kept = []
    for frag in fragments:
        cand = " ".join(kept + [frag])
        if w(cand) <= limit:
            kept.append(frag)
        else:
            break
    if kept:
        return " ".join(kept)
    return _fit_text(meas, fragments[0], limit)


def _fit_image(img, thickness):
    """帯の厚みに合わせて画像を縮小する (拡大はしない)。

    横帯なら厚みは高さ、縦帯なら幅になる。画像は方向に無頓着なので、
    効くのはこの「厚みに合わせる」原則だけ。
    """
    w, h = img.size
    if h <= thickness:
        return img
    nw = max(1, round(w * thickness / h))
    return img.resize((nw, thickness), Image.LANCZOS)


def _images_width(ims):
    """画像列の合計幅 (間の PART_GAP 込み)。空なら 0。"""
    if not ims:
        return 0
    return sum(im.size[0] for im in ims) + PART_GAP * (len(ims) - 1)


def render_strip(items, color=TEXT_COLOR, bg=BG_COLOR, max_width=MAX_WIDTH):
    """_render_strip の薄い包み: 画像だけを返す (テストと名札用)。"""
    return _render_strip(items, color=color, bg=bg, max_width=max_width)[0]


def _render_strip(items, color=TEXT_COLOR, bg=BG_COLOR, max_width=MAX_WIDTH):
    """[(name, texts, images, zone), ...] から (帯の画像, 縮退したか) を作る。

    1セグメントは、画像列とテキストを同封した1ユニットとして描く
    (画像 (リスト順) → PART_GAP → テキストの順)。max_width (既定は
    MAX_WIDTH、ゾーンの最大幅でさらに絞れる) に入らない
    ときの縮退は、まずテキスト (断片の後ろから落とし、最後の1断片は
    「…」で切り詰める)、それも無理ならテキストを落として画像だけ残し
    (§6.1 の icon-only 縮退)、画像列だけでも入らなければ後ろの画像から
    落とし、それでも無理ならそのセグメントで打ち切る。
    何か落としたり切り詰めたりしたら、2つ目の返り値が True になる
    (「縮退した」のログ用)。
    セグメントが無ければ (None, False)。
    画像は保持しているオリジナルから、描画のたびに厚みへ再計算して貼る。
    """
    if not items:
        return None, False
    meas = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    kept = []  # (ims, fitted_text, text_w, text_bbox)
    width = PAD_X * 2
    degraded = False
    for _name, texts, images, _zone in items:
        gap = GAP if kept else 0
        avail = max_width - width - gap
        ims = [_fit_image(im, STRIP_HEIGHT) for im in images] if images else []
        fitted = None
        bbox = None
        tw = 0
        if texts is not None:
            t_avail = avail - _images_width(ims) - (PART_GAP if ims else 0)
            fitted = _fit_fragments(meas, texts, t_avail) if t_avail > 0 else None
            if fitted is not None:
                bbox = meas.textbbox((0, 0), fitted, font=_FONT, stroke_width=TEXT_STROKE)
                tw = bbox[2] - bbox[0]
                if fitted != " ".join(texts):
                    degraded = True  # 断片が落ちたか、切り詰められた
            else:
                degraded = True  # テキストは落とす (画像があれば icon-only 側へ)
        while ims and _images_width(ims) + (PART_GAP if fitted is not None else 0) + tw > avail:
            ims.pop()  # 画像列だけでも入らなければ、後ろの画像から落とす
            degraded = True
        if not ims and fitted is None:
            degraded = True
            break  # このセグメントは収まらないので打ち切る
        unit_w = _images_width(ims) + (PART_GAP if (ims and fitted is not None) else 0) + tw
        kept.append((ims, fitted, tw, bbox))
        width += gap + unit_w
    if not kept:
        return None, degraded
    height = STRIP_HEIGHT
    img = Image.new("RGBA", (width, height), bg)
    draw = ImageDraw.Draw(img)
    x = PAD_X
    for ims, fitted, tw, bbox in kept:
        for j, im in enumerate(ims):
            if j:
                x += PART_GAP
            img.alpha_composite(im, (x, (height - im.size[1]) // 2))
            x += im.size[0]
        if fitted is not None:
            if ims:
                x += PART_GAP
            # textbbox の上端オフセットを引いて、見た目で縦中央に寄せる
            y = (height - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text((x, y), fitted, font=_FONT, fill=color, stroke_width=TEXT_STROKE, stroke_fill=STROKE_COLOR)
            x += tw
        x += GAP
    return img, degraded


def _describe(items):
    """ログ用にセグメントを短く表す。画像の実物は repr せず寸法だけ出す。"""
    out = []
    for name, texts, images, _zone in items:
        parts = []
        if texts is not None:
            parts.append(" ".join(texts))
        if images:
            parts.extend(f"<image {im.size[0]}x{im.size[1]}>" for im in images)
        out.append((name, " ".join(parts)))
    return out


def _to_bgra_premultiplied(img):
    """UpdateLayeredWindow 用に BGRA + premultiplied alpha の bytes にする。"""
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


# ---------------------------------------------------------------- ログ
# ログの置き場所は家の決め (getLog.py): XDG_STATE_HOME があればそこ、
# なければ ~/.local/state の下にアプリ名で切る。実行ディレクトリや
# スクリプトの隣に生やすと、exe 化したときに置き場所が定まらない
# (2026-11-24 本人の決め)。
# 時刻付きで出すために logging を素直に使う (手書きの _log は廃止)。
# コンソール起動 (py hekoo.py) のときは標準出力にも出す (本人の進め方)。
# ファイルにも残すのは、コンソールなしで常駐させる将来のため。
# 時刻がないと「いつの投稿で状態が変わったか」が追えないので、
# フォーマットには必ず時刻を入れる (2026-11-22、画像消失の切り分けで痛感)。
logger = logging.getLogger("hekoo")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    _stream = logging.StreamHandler(sys.stdout)
    _stream.setFormatter(_formatter)
    logger.addHandler(_stream)
    try:
        _file = logging.FileHandler(
            getLog("hekoo", "hekoo.log"),
            encoding="utf-8", delay=True)
        _file.setFormatter(_formatter)
        logger.addHandler(_file)
    except Exception:
        pass


def _log(msg):
    """既存の呼び出し口をそのままに、logger へ流すだけの薄い包み。"""
    logger.info(msg)


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


# ---------------------------------------------------------------- ゾーン観測
def compute_zones(tray_left, rep_left, rep_right, notify_left):
    """ゾーン計算 (純粋関数)。((左起点, 左最大幅), (右起点, 右最大幅)) を返す。

    左起点ゾーンはトレイ左端からタスクボタン群 (TaskbarFrameRepeater)
    の左端まで、右起点ゾーンはボタン群の右端から TrayNotifyWnd の
    左端まで。帯はこの最大幅に収めることで、ボタンに被せない。
    """
    return ((tray_left, rep_left - tray_left),
            (notify_left, notify_left - rep_right))


def _pick_left_wall(rects, start):
    """左帯の通り道の壁を選ぶ (純粋関数)。

    rects は (left, right) の列。start より手前で終わっているもの
    (right <= start) は通り道にいないので除き、残りの左端の最小が
    壁。帯の開始位置を覆っているもの (left < start < right) は
    その左端が壁になり、予算は 0 に潰れて帯は引く。
    """
    walls = [l for (l, r) in rects if r > start]
    return min(walls) if walls else None


def _follow_start(rects, start, zone_end=None):
    """左帯の座り直し先を選ぶ (純粋関数)。

    領土 (start から先) に先住人がいたら、一番近い先住人の
    右端へ座り直す。座り直した先に重なる・連なる先住人が
    いれば連鎖して越える。誰もいなければ start のまま。
    申告した開始位置は「最も早い開始位置」であり、観測できる
    先住人への譲り方はホストの作法 (右帯のアンカー回避と同じ)。

    ゾーン端が分かっているときは、右端がゾーン端を越える
    先住人は座り直しの相手にしない。領土の外の住人を追うと
    帯まで領土の外へ出てしまう (2026-11-28、右ゾーンに
    住み始めた BatteryHub を追って左帯が消えた件)。そういう
    相手は壁 (_pick_left_wall) の担当で、帯は手前に残る。
    """
    ahead = sorted((l, r) for (l, r) in rects
                   if r > start and (zone_end is None or r <= zone_end))
    if not ahead:
        return start
    seat = ahead[0][1]
    for (l, r) in ahead[1:]:
        if l <= seat:
            seat = max(seat, r)
    return seat


def _choose_left_seat(rects, declared, zone_end=None):
    """左帯の座席を選ぶ (純粋関数)。(座席, 追わなかったか) を返す。

    座り直し (follow) した席と、申告位置に残る席 (先住人は壁に
    なる) の両方について描画幅の予算を数え、広い方に座る。
    遠くの先住人を追うと帯がゾーン端に潰れて縮退するので、
    そういう相手は追わずに壁で受ける (2026-11-28、タスクバー
    アプリの直前に移った BatteryHub を追って縮退した件)。
    同点なら申告位置。予算の数え方は _left_budget と同じ。
    """
    def budget(start, wall):
        b = MAX_WIDTH if zone_end is None \
            else min(MAX_WIDTH, zone_end - start)
        if wall is not None:
            b = min(b, wall - start)
        return max(0, b)

    followed = _follow_start(rects, declared, zone_end)
    if followed == declared:
        return declared, False
    b_follow = budget(followed, _pick_left_wall(rects, followed))
    b_stay = budget(declared, _pick_left_wall(rects, declared))
    if b_stay >= b_follow:
        return declared, True
    return followed, False


class ZoneState:
    """観測スレッドからメインスレッドへのゾーン情報の受け渡し口。

    観測スレッドは変化があったときだけ Queue にスナップショットを
    入れ、メインスレッド (右帯のタイマ) が取り出して current に
    反映する。左帯は current を読むだけ (取り出すのは1人だけ、
    という担当分け)。
    """

    def __init__(self):
        self.q = queue.Queue()
        self.current = None   # ((左起点, 左最大幅), (右起点, 右最大幅)) or None


class OffsetState:
    """左起点オフセットの申告の置き場所 (POST /offset で書かれる)。

    左隅の先住人は観測できないものがいるので、開始位置は申告で
    受ける (TTL や zone と同じ申告制)。HTTP スレッドが書き、
    左帯が読むので錠前つき。保存はメモリ上のみで、再起動したら
    既定値 (LEFT_OFFSET) に戻る (永続化は PoC 越えなので後段)。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._value = LEFT_OFFSET

    def get(self):
        with self._lock:
            return self._value

    def set(self, value):
        with self._lock:
            self._value = value


def _left_territory(zone, start, wall):
    """左の領土 [from, to] を合成する (純粋関数)。

    zone は観測値 (起点, 最大幅)。from は座り直し適用後の
    開始位置 (申告 + follow の結果)、to はゾーン端と座席の
    前方の壁の狭い方。空の領土は from > to のまま返す
    (潰すと「なぜ空か」が消える)。ゾーンが観測できて
    いなければ None (不明を 0 で誤魔化さない)。
    """
    if zone is None:
        return None
    origin, l_max = zone
    end = origin + l_max
    if wall is not None:
        end = min(end, wall)
    return [start, end]


def _right_territory(zone, anchor):
    """右の領土 [from, to] を合成する (純粋関数)。

    zone は観測値 ((左...), (右起点, 右最大幅))、anchor は追随
    ポリシー適用後の値 (生の測定値だと戻り待ちの間は嘘になる)。
    from はボタン群の右端、to はアンカー。
    """
    if zone is None or anchor is None:
        return None
    (r_org, r_max) = zone[1]
    return [r_org - r_max, anchor]


class TerritoryState:
    """GET /territory 用に、両帯が合成した領土と材料の置き場所。

    測って合成するのは各帯のタイマ (メインスレッド) で、API
    スレッドはここを読むだけ (窓を触るのはメインスレッド、
    という紀律のまま)。ログに出すのは領土だけでシンプルに、
    材料つきの詳細は GET /territory で返す (本人の決め)。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._left = {"zone": None, "offset": LEFT_OFFSET, "start": None,
                      "wall": None, "territory": None}
        self._right = {"zone": None, "anchor": None, "territory": None}

    def put_left(self, zone, offset, start, wall, territory):
        with self._lock:
            self._left = {"zone": zone, "offset": offset, "start": start,
                          "wall": wall, "territory": territory}

    def put_right(self, zone, anchor, territory):
        with self._lock:
            self._right = {"zone": zone, "anchor": anchor,
                           "territory": territory}

    def snapshot(self):
        with self._lock:
            return {"left": dict(self._left), "right": dict(self._right)}


def _find_tray_uia(root):
    """UIA ルート直下から Shell_TrayWnd (主タスクバー) を探す。"""
    for child in root.GetChildren():
        if child.ClassName == 'Shell_TrayWnd':
            return child
    return None


def _find_child_uia(elem, pred, depth=0):
    """UIA 部分木から pred に合う要素を最初に見つける。なければ None。"""
    if depth > UIA_MAX_DEPTH:
        return None
    if pred(elem):
        return elem
    for child in elem.GetChildren():
        found = _find_child_uia(child, pred, depth + 1)
        if found is not None:
            return found
    return None


def zone_observer(stop, state):
    """UIA 専用スレッド。ゾーンを観測して Queue に流すだけ。窓には触らない。

    UIA を UI スレッドから叩くと Explorer が固まることがあるので、
    観測は必ずこのスレッドの中でやる (BatteryHub の widget.cpp と
    同じ作法)。要素はキャッシュし、読みに失敗したら探し直す。
    実証は probe.py で済み (2026-11-26、probe ブランチ)。
    """
    with uia.UIAutomationInitializerInThread():
        root = uia.GetRootControl()
        tray = None
        repeater = None
        notify = None
        last = None           # 最後に Queue へ入れたスナップショット
        while not stop.is_set():
            try:
                if tray is None:
                    tray = _find_tray_uia(root)
                    repeater = None
                    notify = None
                if tray is not None and repeater is None:
                    repeater = _find_child_uia(
                        tray, lambda e: (e.AutomationId or '') == 'TaskbarFrameRepeater')
                if tray is not None and notify is None:
                    notify = _find_child_uia(
                        tray, lambda e: (e.ClassName or '') == 'TrayNotifyWnd')
                if repeater is None or notify is None:
                    snap = None
                else:
                    trect = tray.BoundingRectangle
                    rep = repeater.BoundingRectangle
                    ntf = notify.BoundingRectangle
                    snap = compute_zones(trect.left, rep.left, rep.right, ntf.left)
                if snap != last:
                    state.q.put(snap)
                    last = snap
                    if snap is None:
                        tray = None   # 次の周期で探し直す
            except Exception:
                if last is not None:
                    state.q.put(None)
                tray = None
                repeater = None
                notify = None
                last = None
            stop.wait(OBSERVE_SEC)


def start_zone_observer(state):
    """ゾーン観測スレッドを立てる。uiautomation が無ければ観測なし。"""
    if uia is None:
        _log("zone: uiautomation が無いので観測しない (帯は従来動作)")
        return None
    stop = threading.Event()
    t = threading.Thread(target=zone_observer, args=(stop, state),
                         name="hekoo-zone", daemon=True)
    t.start()
    return stop


# ---------------------------------------------------------------- Win32 ホスト
class StartFollower:
    """左帯の座り直しの非対称ポリシー (AnchorFollower の鏡像)。

    右 (譲って座り直す) は即時、左 (申告位置へ戻る) は慎重に:
    - 右への移動は領土の話 (被せない) なので即座に追う。
    - 左への移動は見た目の話でしかないので、ANCHOR_DEADBAND
      以下の戻りは無視して停泊し、それを超える戻りも
      ANCHOR_RESTORE_CYCLES 回連続で同じ値を観測してから
      確定する。先住人の幅がふらつくたびに帯が往復するのを
      防ぐ (右帯のアンカーで実証済みの型の左右反転)。
    """

    def __init__(self):
        self.applied = None      # 実際に使っている開始位置
        self._candidate = None   # 左戻りの候補
        self._count = 0          # 候補の連続観測回数

    def update(self, measured):
        """測定値を1回分食わせ、適用すべき開始位置を返す。"""
        if self.applied is None:
            self.applied = measured
            return self.applied
        if measured >= self.applied:
            # 右 (譲る) か不変。即時に追い、左戻りの数え直しは捨てる
            self.applied = measured
            self._candidate = None
            self._count = 0
            return self.applied
        # 左 (戻る)
        if self.applied - measured <= ANCHOR_DEADBAND:
            self._candidate = None
            self._count = 0
            return self.applied
        if measured == self._candidate:
            self._count += 1
        else:
            self._candidate = measured
            self._count = 1
        if self._count >= ANCHOR_RESTORE_CYCLES:
            self.applied = measured
            self._candidate = None
            self._count = 0
        return self.applied


class AnchorFollower:
    """アンカー追随の非対称ポリシー (純粋な状態機械、窓には触らない)。

    左 (譲る) は即時、右 (戻る) は慎重に:
    - 左への移動は領土の話 (被せない) なので即座に追う。
    - 右への移動は見た目の話でしかないので、ANCHOR_DEADBAND 以下の
      戻りは無視して停泊し、それを超える戻りも ANCHOR_RESTORE_CYCLES
      回連続で同じ値を観測してから確定する。
    発端の実測 (2026-11-22): 先住人の幅が 6px だけ約61秒間広がって
    戻る、を繰り返し、律儀に追う帯がそのたび往復してちらついた。
    右戻りを停泊させれば往復は消え、代償は先住人が細い間の
    最大 ANCHOR_DEADBAND px の隙間だけ。
    """

    def __init__(self):
        self.applied = None      # 実際に使っているアンカー
        self._candidate = None   # 右戻りの候補
        self._count = 0          # 候補の連続観測回数

    def update(self, measured):
        """測定値を1回分食わせ、適用すべきアンカーを返す。"""
        if self.applied is None:
            self.applied = measured
            return self.applied
        if measured <= self.applied:
            # 左 (譲る) か不変。即時に追い、右戻りの数え直しは捨てる
            self.applied = measured
            self._candidate = None
            self._count = 0
            return self.applied
        # 右 (戻る)
        if measured - self.applied <= ANCHOR_DEADBAND:
            self._candidate = None
            self._count = 0
            return self.applied
        if measured == self._candidate:
            self._count += 1
        else:
            self._candidate = measured
            self._count = 1
        if self._count >= ANCHOR_RESTORE_CYCLES:
            self.applied = measured
            self._candidate = None
            self._count = 0
        return self.applied


class StripHost:
    """Shell_TrayWnd のレイヤード子窓として帯を出す。お手本の Create/Reposition 相当。"""

    ID_EXIT = 1001
    ID_VERSION = 1002  # バージョン表記 (選んでも何もしない名乗り専用)
    ID_ALPHA = 1003    # プレートの alpha 切り替え (255 ⇄ 1)

    WINDOW_CLASS = "HekooStripWindow"
    WINDOW_TITLE = "hekoo"

    def __init__(self, store, zone_state=None, offset_state=None, territory_state=None):
        self.store = store
        self.zone_state = zone_state
        self.offset_state = offset_state
        self.territory_state = territory_state
        self._budget = None      # 現在の描画幅予算 (変わったら描き直す)
        self._zones_seen = None  # 最後にログへ出したゾーン
        self._wall_seen = None   # 左帯用: 最後にログへ出した先住人の壁
        self._noplace_seen = False  # 「居場所なかった」を出したか
        self._degraded_seen = False  # 「縮退した」を出したか
        self._territory_seen = None  # 最後にログへ出した自分の領土
        self.hwnd = None
        self.tray = None
        self.notify = None
        self._version = -1
        self._size = (0, 0)
        self._client_xy = (0, 0)
        self._follower = AnchorFollower()
        self._zstate = None
        self._tray_rect = None
        self._class_atom = None
        self.plate_alpha = PLATE_ALPHA_MIN  # プレートの alpha の既定値 (透明)。
                                # 0 だとクリックが素通りするので下限は 1

    # --- 窓の生成と親子付け ---
    def attach(self):
        self.tray = win32gui.FindWindow("Shell_TrayWnd", None)
        if not self.tray:
            _log("attach: Shell_TrayWnd not found")
            return False
        self.notify = win32gui.FindWindowEx(self.tray, 0, "TrayNotifyWnd", None)
        if self.hwnd is not None and not win32gui.IsWindow(self.hwnd):
            # Explorer 再起動で親ごと壊れた窓。ハンドルだけ残って実体がない
            _log("attach: strip window was destroyed, recreating")
            self.hwnd = None
            self._version = -1  # 作り直した窓には描き直させる
            self._size = (0, 0)
        if self.hwnd is None:
            self._create_window()
        else:
            win32gui.SetParent(self.hwnd, self.tray)
        _log(f"attach: tray={self.tray} notify={self.notify} hwnd={self.hwnd}")
        return True

    def _create_window(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self.WINDOW_CLASS
        wc.lpfnWndProc = {
            win32con.WM_RBUTTONUP: self._on_rbuttonup,
            win32con.WM_DESTROY: self._on_destroy,
        }
        if self._class_atom is None:
            self._class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE,
            wc.lpszClassName,
            self.WINDOW_TITLE,
            win32con.WS_CHILD | win32con.WS_CLIPSIBLINGS,
            0, 0, 1, 1,
            self.tray, 0, wc.hInstance, None,
        )
        # タイマーはこの窓には付けない。帯の窓は Shell_TrayWnd の子で、
        # Explorer 再起動のときに親ごと壊れてタイマーも道連れになる
        # (2026-11-26／27 に実機で確認)。刻むのは番人 (Watchdog) の
        # メッセージ専用窓の役目で、そちらから tick() が呼ばれる

    # --- 配置と転送 ---
    def _anchor_left(self, notify_left):
        """帯の右端を付けるスクリーン x を返す (「ここでーす」の検出)。

        既定は TrayNotifyWnd の左端。ただし、その左隣に先住人の帯 (タスクバーの
        子窓で、右端がうちのアンカーに接しているもの) が居たら、その左端まで
        ずれる。帯が連なっていれば連なりの一番左まで。先住人の名前は問わない
        (BatteryHub でも何でも、接して居れば隣に並ぶ)。
        同時起動で同じ場所を取り合うと、互いに HWND_TOP を主張し合って
        点滅する (実機で確認)。重ならないことが回避の第一歩。
        """
        anchor = notify_left
        try:
            children = []

            def _cb(hwnd, acc):
                if win32gui.GetParent(hwnd) == self.tray:
                    acc.append(hwnd)
                return True

            win32gui.EnumChildWindows(self.tray, _cb, children)
            _tx, tray_top, _tx2, tray_bottom = win32gui.GetWindowRect(self.tray)
        except Exception:
            return anchor
        for _ in range(8):  # 連なりをたどる上限
            found = None
            for h in children:
                if h in (self.hwnd, self.notify) or not win32gui.IsWindowVisible(h):
                    continue
                try:
                    l, t, r, b = win32gui.GetWindowRect(h)
                except Exception:
                    continue
                # 右端が今のアンカーに接していて、縦位置がタスクバーと重なるもの
                if anchor - 10 <= r <= anchor + 2 and l < r and t < tray_bottom and b > tray_top:
                    if found is None or l < found:
                        found = l
            if found is None:
                break
            anchor = found
        return anchor

    def _measure_anchor(self):
        """アンカーを測定し、追随ポリシーを適用した適用値を返す。

        描画幅の予算 (ゾーンの最大幅) を決めるのに適用値が要るので、
        測定と追随は配置より前にタイマ側でやる。ログは従来通り、
        適用値が変わったときだけ出す。
        """
        if not self.notify:
            return None
        try:
            nx, _ny, _nx2, _ny2 = win32gui.GetWindowRect(self.notify)
        except Exception:
            # Explorer の生まれ替わりの最中など、notify がまだ掴めない
            # (2026-11-28 の復帰検証で移行の 1 秒間に 1400 を観測)
            return None
        measured = self._anchor_left(nx)
        prev = self._follower.applied
        anchor = self._follower.update(measured)
        if anchor != prev:
            if prev is None or anchor < prev:
                if anchor < nx:
                    _log(f"anchor: 先住人がいるので左へずれる (notify left={nx} -> {anchor})")
            elif anchor >= nx:
                _log(f"anchor: 先住人がいなくなったので定位置へ (notify left={nx})")
            else:
                _log(f"anchor: 先住人が縮んだので右へ戻る (notify left={nx} -> {anchor})")
        return anchor

    def _drain_zones(self):
        """観測スレッドからのスナップショットを取り出し、current に反映する。

        取り出すのはこのタイマだけ (左帯は current を読むだけ)。
        ログはスナップショットが変わったときだけ出す。
        """
        if self.zone_state is None:
            return
        changed = False
        while True:
            try:
                snap = self.zone_state.q.get_nowait()
            except queue.Empty:
                break
            self.zone_state.current = snap
            changed = True
        if changed and self.zone_state.current != self._zones_seen:
            snap = self.zone_state.current
            self._zones_seen = snap
            if snap is None:
                _log("zone: 観測できない")
            else:
                (l_org, l_max), (r_org, r_max) = snap
                _log(f"zone: left origin={l_org} max={l_max} / right origin={r_org} max={r_max}")

    def _right_budget(self, anchor):
        """右帯の描画幅予算。ゾーンが分かれば最大幅で絞り、分からなければ MAX_WIDTH。"""
        if anchor is None or self.zone_state is None or self.zone_state.current is None:
            return MAX_WIDTH
        (_l_org, _l_max), (r_org, r_max) = self.zone_state.current
        rep_right = r_org - r_max   # ボタン群の右端 (右ゾーンの左の壁)
        return max(0, min(MAX_WIDTH, anchor - rep_right))

    def _watch_state(self, prefix, has_items, budget, degraded, noplace_detail):
        """「居場所なかった」と「縮退した」を、状態が変わったときだけ出す。

        どちらも「見た目が黙って変わる」隅の可視化 (本人の指示で
        左右対称に有効)。セグメントが無いときは黙って初期状態に
        戻す (失効で消えたのを「居場所ができた」「縮退が解消した」
        とは言わない)。
        """
        if not has_items:
            self._noplace_seen = False
            self._degraded_seen = False
            return
        if budget == 0:
            if not self._noplace_seen:
                _log(f"{prefix}: 居場所なかった ({noplace_detail})")
                self._noplace_seen = True
            self._degraded_seen = False
            return
        if self._noplace_seen:
            _log(f"{prefix}: 居場所ができた")
            self._noplace_seen = False
        if degraded != self._degraded_seen:
            _log(f"{prefix}: 縮退した" if degraded else f"{prefix}: 縮退が解消した")
            self._degraded_seen = degraded

    def _reposition(self, width, height, anchor):
        """右起点: アンカー (通常は TrayNotifyWnd の左端、先住人が居ればその左) に
        帯の右端を合わせる。"""
        if anchor is None:
            return False
        tx, ty, _tx2, ty2 = win32gui.GetWindowRect(self.tray)
        sx = anchor - width
        sy = ty + ((ty2 - ty) - height) // 2
        cx, cy = win32gui.ScreenToClient(self.tray, (sx, sy))
        if (cx, cy) == self._client_xy and (width, height) == self._size:
            # 場所もサイズも同じで、自分が兄弟の一番上なら触らない。
            # 毎秒 HWND_TOP を主張し直すと、先住人と主張し合って点滅の種になる。
            # 一番上でない状態は無言で主張し直していたので、遷移だけログに
            # 残す (2026-11-22、位置が落ち着いた後に残った不定期な
            # ちらつきの切り分け用)。
            try:
                visible = bool(win32gui.IsWindowVisible(self.hwnd))
                # GW_HWNDPREV が無い (= 一番上) ときの戻りは None のことも
                # 0 のこともある (実機で prev=0 を観測したので寄せておく)
                prev = win32gui.GetWindow(self.hwnd, win32con.GW_HWNDPREV) or None
                # 遷移判定は「実測した状態」だけで行う。SetWindowPos の後に
                # 「一番上になったはず」と仮の状態を書くと、実測と毎回
                # 食い違ってログが毎秒出るバグになる (2026-11-22 実機で踏んだ)
                state = (visible and prev is None, prev)
                if state != self._zstate:
                    if state[0]:
                        _log("zorder: 一番上を取り戻した")
                    else:
                        _log(f"zorder: 一番上ではない (visible={int(visible)} prev={prev})")
                    self._zstate = state
                if state[0]:
                    return True
            except Exception:
                pass
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
    def tick(self):
        # 番人 (Watchdog) のタイマーから毎秒呼ばれる。再配置と再描画の本体
        if self.tray is None or not win32gui.IsWindow(self.tray):
            self.attach()
            if self.tray is None:
                return
        # タスクバーの位置・大きさ・向きが変わったら描き直させる。
        # オリエンテーションの変更は再描画で吸収する (本人の見立て)
        try:
            rect = win32gui.GetWindowRect(self.tray)
        except Exception:
            rect = None
        if rect is not None:
            if self._tray_rect is not None and rect != self._tray_rect:
                _log(f"tray: rect changed {self._tray_rect} -> {rect}")
                self._version = -1
            self._tray_rect = rect
        for n in self.store.purge_expired():
            _log(f"expire: {n} (ttl)")
        self._drain_zones()
        anchor = self._measure_anchor()
        budget = self._right_budget(anchor)
        snap = self.zone_state.current if self.zone_state is not None else None
        terr = _right_territory(snap, anchor)
        if terr != self._territory_seen:
            _log(f"territory: right {terr[0]}..{terr[1]}" if terr else "territory: right -")
            self._territory_seen = terr
        if self.territory_state is not None:
            zone_pair = [snap[1][0] - snap[1][1], snap[1][0]] if snap is not None else None
            self.territory_state.put_right(zone_pair, anchor, terr)
        version, items = self.store.snapshot()
        right_items = [it for it in items if it[3] == "right"]
        if version != self._version or budget != self._budget:
            self._version = version
            self._budget = budget
            _log(f"timer: version={version} items={_describe(right_items)}")
            img = None
            degraded = False
            if budget > 0:
                img, degraded = _render_strip(right_items, bg=(0, 0, 0, self.plate_alpha), max_width=budget)
                if img is None and right_items:
                    degraded = True  # 入りきって消えたのも縮退の極限
            self._watch_state("right", bool(right_items), budget, degraded,
                              f"anchor={anchor}")
            if img is None and SHOW_PLACEHOLDER and budget > 0:
                # 空でも名札だけは出す: 「常駐したよ、ここに出るよ」が見える方が
                # 確認が楽 (本人の指摘)。本物のセグメントが来たらそちらに譲る
                img = render_strip([("hekoo", [PLACEHOLDER_TEXT], None, "right")], color=PLACEHOLDER_COLOR, bg=(0, 0, 0, self.plate_alpha), max_width=budget)
            if img is None:
                win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
                self._size = (0, 0)
            else:
                # 先に表面を更新してから配置して見せる (お手本の Paint → Reposition の順)
                self._blit(img)
                if not self._reposition(img.size[0], img.size[1], anchor):
                    win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
        elif self._size != (0, 0):
            self._reposition(*self._size, anchor)  # トレイ幅の変化に追随

    def on_taskbar_created(self):
        # Explorer 再起動 (番人経由で呼ばれる)。親を探し直して付け直す。
        # セグメントは保持したまま
        self.tray = None
        self._version = -1  # 次の tick で描き直させる
        self.attach()

    def _on_rbuttonup(self, hwnd, msg, wparam, lparam):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_VERSION, f"hekoo {VERSION}")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        alpha_flags = win32con.MF_STRING
        if self.plate_alpha == PLATE_ALPHA_MIN:
            alpha_flags |= win32con.MF_CHECKED
        win32gui.AppendMenu(menu, alpha_flags, self.ID_ALPHA, "Transparent plate")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_EXIT, "Exit")
        x, y = win32api.GetCursorPos()
        cmd = win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD, x, y, 0, self.hwnd, None
        )
        win32gui.DestroyMenu(menu)
        if cmd == self.ID_ALPHA:
            self.plate_alpha = PLATE_ALPHA_MIN if self.plate_alpha == PLATE_ALPHA_MAX else PLATE_ALPHA_MAX
            self._version = -1  # 次のタイマで描き直させる
            _log(f"plate: alpha={self.plate_alpha}")
        elif cmd == self.ID_EXIT:
            # 終了経路の可視化 (2026-11-28): メニュー Exit が空振りして
            # プロンプトに戻らなかった件の切り分け用。destroy の前後と
            # _on_destroy の quit 投稿にログを置き、どこまで進んで
            # どこで止まったかをログだけで追えるようにする
            _log(f"exit: destroy hwnd={self.hwnd}")
            win32gui.DestroyWindow(self.hwnd)
            _log("exit: destroy から戻った")
        return 0

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        _log("destroy: quit を投げる")
        win32gui.PostQuitMessage(0)
        return 0


class LeftStripHost(StripHost):
    """左起点ゾーンの帯。観測と受け渡しは右帯と共有し、こちらは
    左ゾーンのセグメントを申告した開始位置から右へ描く。
    領土内に HWND の先住人がいたら、その後ろに座り直す (follow、
    既定の作法)。先住人の後ろに次の先住人がいれば、その手前で
    止まる (被せない)。

    右クリックメニューは今のところ右帯の担当 (こちらは表示専用)。
    プレート alpha も当面は既定 (透明) のまま。
    セグメントの失効処理 (purge) は右帯のタイマが担当する。
    """

    WINDOW_CLASS = "HekooStripWindowLeft"
    WINDOW_TITLE = "hekoo-left"

    def __init__(self, store, zone_state=None, offset_state=None, territory_state=None):
        super().__init__(store, zone_state, offset_state, territory_state)
        self._start_follower = StartFollower()
        self._eff_start = None     # 座り直し適用後の開始位置
        self._follow_seen = None   # 最後にログへ出した開始位置

    def _on_rbuttonup(self, hwnd, msg, wparam, lparam):
        return 0

    def _left_offset(self):
        """現在の左起点オフセット (申告があればそれ、無ければ既定値)。"""
        if self.offset_state is not None:
            return self.offset_state.get()
        return LEFT_OFFSET

    def _left_origin(self):
        """左ゾーンの起点 (スクリーン x)。観測できていればそれ、無ければトレイ左端。"""
        if self.zone_state is not None and self.zone_state.current is not None:
            return self.zone_state.current[0][0]
        try:
            return win32gui.GetWindowRect(self.tray)[0]
        except Exception:
            return 0

    def _left_rects(self):
        """左帯の通り道にいる先住人の (left, right) の列を返す。

        右側の _anchor_left と同じ流儀で、トレイの子窓を列挙して
        「帯の通り道に立っている可視の子窓」を集める。XAML の土台
        (全幅に広がる) と自分たちの帯は数えない。ReBarWindow32 は
        タスクボタン群を内包する Explorer 自身の道具立てで、ボタン群の
        領土はゾーン観測の方が正しく測っているので、先住人には数えない。
        時計のようなオーバーレイはトレイの子窓ではないので、ここには
        映らない (そちらはオフセット申告の担当)。
        """
        if not self.tray:
            return []
        try:
            children = []

            def _cb(hwnd, acc):
                if win32gui.GetParent(hwnd) == self.tray:
                    acc.append(hwnd)
                return True

            win32gui.EnumChildWindows(self.tray, _cb, children)
            _tx, tray_top, _tx2, tray_bottom = win32gui.GetWindowRect(self.tray)
        except Exception:
            return []
        rects = []
        for h in children:
            if h == self.hwnd or not win32gui.IsWindowVisible(h):
                continue
            try:
                cls = win32gui.GetClassName(h)
                l, t, r, b = win32gui.GetWindowRect(h)
            except Exception:
                continue
            if cls.startswith("HekooStripWindow"):
                continue
            if cls in ("TrayNotifyWnd", "ReBarWindow32",
                       "Windows.UI.Composition.DesktopWindowContentBridge",
                       "Windows.UI.Input.InputSite.WindowClass"):
                continue
            if l < r and t < tray_bottom and b > tray_top:
                rects.append((l, r))
        return rects

    def _left_budget(self, start, wall):
        """左帯の描画幅予算。座席から見て、ゾーン端と前方の壁の狭い方で絞る。"""
        if self.zone_state is not None and self.zone_state.current is not None:
            (l_org, l_max), _r = self.zone_state.current
            budget = min(MAX_WIDTH, l_org + l_max - start)
        else:
            budget = MAX_WIDTH
        if wall is not None:
            budget = min(budget, wall - start)
        return max(0, budget)

    def _place_left(self, width, height):
        """左起点: 座り直し適用後の開始位置に帯の左端を合わせる。"""
        _tx, ty, _tx2, ty2 = win32gui.GetWindowRect(self.tray)
        sx = self._eff_start if self._eff_start is not None \
            else self._left_origin() + self._left_offset()
        sy = ty + ((ty2 - ty) - height) // 2
        cx, cy = win32gui.ScreenToClient(self.tray, (sx, sy))
        win32gui.SetWindowPos(
            self.hwnd, win32con.HWND_TOP, cx, cy, width, height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        if (cx, cy) != self._client_xy or (width, height) != self._size:
            _log(f"place-left: {width}x{height} at client=({cx}, {cy})")
        self._client_xy = (cx, cy)
        self._size = (width, height)
        return True

    def tick(self):
        # 右帯と同じく、番人のタイマーから毎秒呼ばれる
        if self.tray is None or not win32gui.IsWindow(self.tray):
            self.attach()
            if self.tray is None:
                return
        try:
            rect = win32gui.GetWindowRect(self.tray)
        except Exception:
            rect = None
        if rect is not None:
            if self._tray_rect is not None and rect != self._tray_rect:
                self._version = -1  # トレイの形が変わったら描き直す
            self._tray_rect = rect
        version, items = self.store.snapshot()
        left_items = [it for it in items if it[3] == "left"]
        rects = self._left_rects()
        declared = self._left_origin() + self._left_offset()
        snap = self.zone_state.current if self.zone_state is not None else None
        zone_end = snap[0][0] + snap[0][1] if snap is not None else None
        seat, declined = _choose_left_seat(rects, declared, zone_end)
        start = self._start_follower.update(seat)
        self._eff_start = start
        if start != self._follow_seen:
            # 座席が動いたら、そのとき見えていた先住人の rect を残す
            # (2026-11-28: BatteryHub の窓の本当の形を確認するため)
            _log(f"left-rects: {rects}")
            if self._follow_seen is not None:
                if start > self._follow_seen:
                    _log(f"left-follow: 先住人の後ろに座り直す (start={start})")
                elif start == declared and declined:
                    _log(f"left-follow: 追うより広いので申告位置に座る (start={start})")
                elif start == declared:
                    _log(f"left-follow: 先住人が退いたので申告位置へ戻る (start={start})")
                else:
                    _log(f"left-follow: 先住人に追随して戻る (start={start})")
            self._follow_seen = start
        wall = _pick_left_wall(rects, start)
        if wall != self._wall_seen:
            if wall is None:
                if start == declared:
                    _log("left-anchor: 先住人がいなくなった")
            else:
                _log(f"left-anchor: 先住人がいるので手前で止める (wall={wall})")
            self._wall_seen = wall
        budget = self._left_budget(start, wall)
        terr = _left_territory(snap[0] if snap is not None else None,
                               start, wall)
        if terr != self._territory_seen:
            _log(f"territory: left {terr[0]}..{terr[1]}" if terr else "territory: left -")
            self._territory_seen = terr
        if self.territory_state is not None:
            zone_pair = [snap[0][0], snap[0][0] + snap[0][1]] if snap is not None else None
            self.territory_state.put_left(zone_pair, self._left_offset(), start, wall, terr)
        if version != self._version or budget != self._budget:
            self._version = version
            self._budget = budget
            _log(f"timer-left: version={version} items={_describe(left_items)}")
            img = None
            degraded = False
            if budget > 0:
                img, degraded = _render_strip(left_items, bg=(0, 0, 0, self.plate_alpha), max_width=budget)
                if img is None and left_items:
                    degraded = True  # 入りきって消えたのも縮退の極限
            self._watch_state("left", bool(left_items), budget, degraded,
                              f"start={start} wall={wall} offset={self._left_offset()}")
            if img is None:
                win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)
                self._size = (0, 0)
            else:
                self._blit(img)
                self._place_left(img.size[0], img.size[1])
        elif self._size != (0, 0):
            self._place_left(*self._size)


class Watchdog:
    """帯の窓の番人。メッセージ専用窓 (親 = HWND_MESSAGE) でタイマーと
    TaskbarCreated を受ける。

    帯の窓は Shell_TrayWnd の子なので、Explorer が再起動すると親ごと
    壊れる。以前はタイマーも TaskbarCreated の受け先も帯の窓自身で、
    壊れた瞬間に復帰の経路まで一緒に消えた (2026-11-26／27 に実機で
    確認)。番人の窓はトレイの子ではないので殻の生死と無関係に残り、
    毎秒の tick と再起動の通知を帯へ届け続ける。

    HWND の操作はすべてメインスレッドのこの窓経由 (家の作法どおり)。
    片方の帯の tick が例外を出しても、もう片方は止めない。
    """

    WINDOW_CLASS = "HekooWatchdogWindow"
    WINDOW_TITLE = "hekoo-watchdog"

    def __init__(self, hosts):
        self.hosts = hosts
        self.hwnd = None
        self._class_atom = None
        self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")

    def start(self):
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self.WINDOW_CLASS
        wc.lpfnWndProc = {
            win32con.WM_TIMER: self._on_timer,
            win32con.WM_DESTROY: self._on_destroy,
            self._taskbar_created: self._on_taskbar_created,
        }
        if self._class_atom is None:
            self._class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindowEx(
            0,
            wc.lpszClassName,
            self.WINDOW_TITLE,
            0,
            0, 0, 0, 0,
            win32con.HWND_MESSAGE, 0, wc.hInstance, None,
        )
        # SetTimer は pywin32 の win32gui に露出していない (実機で確認)。
        # 家の二段構え通り、pywin32 に無いものは ctypes で直接叩く。
        # HWND はポインタ幅なので argtypes も付ける (blit 側と同じ理由)
        user32 = ctypes.windll.user32
        user32.SetTimer.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
        ]
        user32.SetTimer(self.hwnd, 1, REPOSITION_MS, None)
        _log(f"watchdog: hwnd={self.hwnd}")

    def _on_timer(self, hwnd, msg, wparam, lparam):
        for host in self.hosts:
            try:
                host.tick()
            except Exception as e:
                _log(f"watchdog: tick failed: {type(e).__name__}: {e}")
        return 0

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        _log("watchdog: TaskbarCreated")
        for host in self.hosts:
            try:
                host.on_taskbar_created()
            except Exception as e:
                _log(f"watchdog: reattach failed: {type(e).__name__}: {e}")
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
    offsets = OffsetState()
    territory = TerritoryState()
    start_api(store, offsets=offsets, territory=territory)
    zones = ZoneState()
    start_zone_observer(zones)
    host = StripHost(store, zones, territory_state=territory)
    left = LeftStripHost(store, zones, offsets, territory)
    watchdog = Watchdog([host, left])
    watchdog.start()
    host.attach()
    left.attach()

    # Ctrl+C の既定動作 (KeyboardInterrupt) は WNDPROC の中で受けると
    # pywin32 が "Python WNDPROC handler failed" と騒いで飲み込んでしまい、
    # ループが終わらない。SIGINT で PostQuitMessage するのが静かな終わり方
    def _on_sigint(signum, frame):
        _log("sigint: quit を投げる")
        win32gui.PostQuitMessage(0)

    signal.signal(signal.SIGINT, _on_sigint)
    win32gui.PumpMessages()


if __name__ == "__main__":
    main()
