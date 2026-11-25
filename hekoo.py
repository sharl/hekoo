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
#   - 配置は右起点 + 最大幅キャップ + 先住人への回避 (avoid) まで。
#     本格的な領土交渉は Stage 1 以降
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
import re
import signal
import sys
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont

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
BG_COLOR = (0, 0, 0, 255)       # 当面は黒プレート (場所の切り分け用。透明に戻すなら (0,0,0,0))
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
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}   # name -> (texts, images, expires_at) (挿入順)
        self._version = 0

    def upsert(self, name, texts=None, images=None, ttl=DEFAULT_TTL):
        expires = time.monotonic() + ttl
        with self._lock:
            self._items[name] = (texts, images, expires)
            self._version += 1

    def purge_expired(self):
        """期限切れのセグメントを落とし、落とした名前のリストを返す。"""
        now = time.monotonic()
        with self._lock:
            expired = [n for n, (_t, _i, exp) in self._items.items()
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
        """(version, [(name, texts, images), ...]) を返す。窓側はこれだけ読む。"""
        with self._lock:
            return self._version, [(n, ts, im) for n, (ts, im, _e) in self._items.items()]

    def public_dict(self):
        """GET /segments 用。画像は base64 で返さず寸法だけ出す。

        テキストも画像も、要素が1つなら単体、複数ならリストで返す
        (受け口の正規化と対称な形)。
        """
        with self._lock:
            out = {}
            for name, (texts, images, _exp) in self._items.items():
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
            if texts is None and images is None:
                return self._send_json(
                    400, {"ok": False, "error": "need \"text\" and/or \"image\""})
            if ttl is None:
                effective, source = DEFAULT_TTL, "default"
            else:
                effective, source = ttl, "declared"
            store.upsert(name, texts, images, ttl=effective)
            _log(f"post: {name} ttl={effective:g}s ({source})")
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


def render_strip(items, color=TEXT_COLOR, bg=BG_COLOR):
    """[(name, texts, images), ...] から帯の画像 (RGBA) を作る。

    1セグメントは、画像列とテキストを同封した1ユニットとして描く
    (画像 (リスト順) → PART_GAP → テキストの順)。MAX_WIDTH に入らない
    ときの縮退は、まずテキスト (断片の後ろから落とし、最後の1断片は
    「…」で切り詰める)、それも無理ならテキストを落として画像だけ残し
    (§6.1 の icon-only 縮退)、画像列だけでも入らなければ後ろの画像から
    落とし、それでも無理ならそのセグメントで打ち切る。
    セグメントが無ければ None。
    画像は保持しているオリジナルから、描画のたびに厚みへ再計算して貼る。
    """
    if not items:
        return None
    meas = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    kept = []  # (ims, fitted_text, text_w, text_bbox)
    width = PAD_X * 2
    for _name, texts, images in items:
        gap = GAP if kept else 0
        avail = MAX_WIDTH - width - gap
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
            # fitted が None ならテキストは落とす (画像があれば icon-only 側へ)
        while ims and _images_width(ims) + (PART_GAP if fitted is not None else 0) + tw > avail:
            ims.pop()  # 画像列だけでも入らなければ、後ろの画像から落とす
        if not ims and fitted is None:
            break  # このセグメントは収まらないので打ち切る
        unit_w = _images_width(ims) + (PART_GAP if (ims and fitted is not None) else 0) + tw
        kept.append((ims, fitted, tw, bbox))
        width += gap + unit_w
    if not kept:
        return None
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
    return img


def _describe(items):
    """ログ用にセグメントを短く表す。画像の実物は repr せず寸法だけ出す。"""
    out = []
    for name, texts, images in items:
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


# ---------------------------------------------------------------- Win32 ホスト
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

    def __init__(self, store):
        self.store = store
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
        self.plate_alpha = 255  # プレートの alpha。1 にすると見た目は透明だが
                                # 当たり判定は残る (0 だとクリックが素通りする)
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

    def _reposition(self, width, height):
        """右起点: アンカー (通常は TrayNotifyWnd の左端、先住人が居ればその左) に
        帯の右端を合わせる。"""
        if not self.notify:
            return False
        tx, ty, _tx2, ty2 = win32gui.GetWindowRect(self.tray)
        nx, _ny, _nx2, _ny2 = win32gui.GetWindowRect(self.notify)
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
    def _on_timer(self, hwnd, msg, wparam, lparam):
        if self.tray is None or not win32gui.IsWindow(self.tray):
            self.attach()
            if self.tray is None:
                return 0
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
        version, items = self.store.snapshot()
        if version != self._version:
            self._version = version
            _log(f"timer: version={version} items={_describe(items)}")
            img = render_strip(items, bg=(0, 0, 0, self.plate_alpha))
            if img is None and SHOW_PLACEHOLDER:
                # 空でも名札だけは出す: 「常駐したよ、ここに出るよ」が見える方が
                # 確認が楽 (本人の指摘)。本物のセグメントが来たらそちらに譲る
                img = render_strip([("hekoo", [PLACEHOLDER_TEXT], None)], color=PLACEHOLDER_COLOR, bg=(0, 0, 0, self.plate_alpha))
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
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_VERSION, f"hekoo {VERSION}")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        alpha_flags = win32con.MF_STRING
        if self.plate_alpha == 1:
            alpha_flags |= win32con.MF_CHECKED
        win32gui.AppendMenu(menu, alpha_flags, self.ID_ALPHA, "Transparent plate")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_EXIT, "Exit")
        x, y = win32api.GetCursorPos()
        cmd = win32gui.TrackPopupMenu(
            menu, win32con.TPM_LEFTALIGN | win32con.TPM_RETURNCMD, x, y, 0, self.hwnd, None
        )
        win32gui.DestroyMenu(menu)
        if cmd == self.ID_ALPHA:
            self.plate_alpha = 1 if self.plate_alpha == 255 else 255
            self._version = -1  # 次のタイマで描き直させる
            _log(f"plate: alpha={self.plate_alpha}")
        elif cmd == self.ID_EXIT:
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
