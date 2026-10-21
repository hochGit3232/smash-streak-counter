# -*- coding: utf-8 -*-
"""
スマブラSP 連勝カウンター (OBS用)

OBS WebSocket で映像ソースのスクリーンショットを取得し、
- 対戦開始前画面: 左下ネームタグ(1P枠)から自分の名前を読み取る
- 結果画面: 自分の名前があるカードの順位が「1」(金色)なら +1、それ以外なら 0 にリセット
結果は streak.txt に書き出し、http://localhost:8765/ でオーバーレイ/操作パネルを配信します。

使い方:
    python streak_counter.py                 # OBSに接続して監視
    python streak_counter.py --test 画像.png  # 画像1枚で判定テスト
"""
import argparse
import asyncio
import base64
import difflib
import io
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOG_PATH = BASE / "log.txt"
# キャラアイコン (任意)。ゲームの画像なので配布物には含めず、各自でフォルダに置いたときだけ使う
ICON_DIR = BASE / "assets" / "fighters"
ICON_MAP_PATH = BASE / "fighter_icons.json"


SOUND_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}
# 操作パネルから切り替えられる設定 (これ以外は変更させない)
TOGGLES = ("show_recent", "show_nemesis", "play_reset_sound")


def sound_path(cfg):
    """鳴らす音のファイル。無ければ None"""
    name = str(cfg.get("reset_sound_file", "") or "")
    if not name:
        return None
    p = Path(name)
    p = p if p.is_absolute() else BASE / p
    return p if p.suffix.lower() in SOUND_TYPES and p.is_file() else None


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def load_icon_map():
    """日本語のキャラ名 → アイコンのURL。アイコンが無ければ空"""
    if not ICON_MAP_PATH.is_file():
        return {}
    try:
        raw = json.loads(ICON_MAP_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    icons = {}
    for name, rel in raw.items():
        file = Path(str(rel)).name
        if re.fullmatch(r"[a-z0-9_]+\.png", file) and (ICON_DIR / file).is_file():
            icons[name] = f"/icons/{file}"
    return icons


def log(*args):
    """画面に出すと同時に log.txt にも追記する (画面を閉じても後から見返せる)"""
    line = f"[{time.strftime('%m/%d %H:%M:%S')}] " + " ".join(str(a) for a in args)
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

DEFAULT_CONFIG = {
    "obs_host": "localhost",
    "obs_port": 4455,
    "obs_password": "",
    "source_name": "映像キャプチャデバイス",  # スイッチを映しているOBSのソース名
    "player_name": "",  # 空なら対戦開始前画面のネームタグから自動取得
    "auto_learn_name": True,
    "interval_sec": 0.5,
    "output_text": "streak.txt",
    "text_format": "{streak}連勝",  # 使える値: {streak} {best} {wins} {losses} {games} {nemesis}
    "recent_count": 30,  # 直近何戦の勝敗を数えるか
    "show_recent": True,  # 配信画面に「直近〇戦 〇勝〇敗」と勝敗の並びを出すか
    "show_nemesis": True,  # 配信画面に「よく負けるキャラ」を出すか
    "play_reset_sound": False,  # 連勝が途切れて 0 になったときに音を鳴らすか
    "reset_sound_file": "sounds/reset.mp3",  # 鳴らす音 (このフォルダからの場所。mp3 / wav / ogg / m4a)
    "reset_sound_volume": 0.8,  # 音量 (0.0〜1.0)
    "vs_threshold": 0.5,  # VS画面とみなす似ている度合い
    "http_port": 8765,
    "gold_ratio_threshold": 0.03,  # 順位エリア内の金色ピクセル割合がこれ以上なら1位
    "name_match_threshold": 0.6,
    "debug": False,
}


# ---------------------------------------------------------------- 設定/状態
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        # メモ帳などで BOM 付きで保存されても読めるように utf-8-sig で読む
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


class State:
    HISTORY_MAX = 200  # 保存しておく試合数の上限

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.streak = 0
        self.best = 0
        self.last_event = ""
        # 1試合ごとの記録: {"r": "W"/"L", "t": 時刻, "prev": 記録前の連勝数, "o": 相手キャラ}
        self.history = []
        # 音を鳴らす合図: 増えたら配信画面 (overlay.html) が音を鳴らす
        self.reset_seq = 0  # 連勝が途切れた回数
        self.test_seq = 0  # 操作パネルの「音を試す」を押した回数
        self.icons = load_icon_map()
        if self.icons:
            log(f"キャラアイコンを読み込みました ({len(self.icons)}体)")
        if STATE_PATH.exists():
            d = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
            self.streak, self.best = d.get("streak", 0), d.get("best", 0)
            self.history = d.get("history", [])
        self.save()

    def _changed(self, event):
        self.best = max(self.best, self.streak)
        self.last_event = event
        self.save()
        w, l, n = self.recent()
        log(f"{event} -> {self.streak}連勝 / 直近{n}戦 {w}勝{l}敗")

    def record(self, win, event, opponent=None):
        """1試合の結果を記録 (勝ち: 連勝+1 / 負け: 連勝0)。opponent は相手のキャラ名"""
        with self.lock:
            entry = {"r": "W" if win else "L", "t": time.strftime("%m/%d %H:%M"), "prev": self.streak}
            if opponent:
                entry["o"] = opponent
            self.history.append(entry)
            del self.history[:-self.HISTORY_MAX]
            if not win and self.streak > 0:  # 連勝が途切れた (1以上 → 0)
                self.reset_seq += 1
            self.streak = self.streak + 1 if win else 0
            self._changed(event)

    def test_sound(self):
        with self.lock:
            self.test_seq += 1

    def undo(self):
        """最後の1試合の記録を取り消し、連勝数も元に戻す"""
        with self.lock:
            if not self.history:
                return
            last = self.history.pop()
            self.streak = last["prev"]
            self._changed(f"取消 ({'勝ち' if last['r'] == 'W' else '負け'})")

    def set(self, value, event):
        """連勝数だけを直接変更 (戦績には残さない)"""
        with self.lock:
            self.streak = max(0, value)
            self._changed(event)

    def reset_best(self):
        """最高連勝数をリセット (今の連勝数から数え直す)"""
        with self.lock:
            self.best = self.streak
            self._changed("最高連勝リセット")

    def clear_history(self):
        with self.lock:
            self.history = []
            self._changed("戦績クリア")

    def recent(self):
        """直近 recent_count 戦の (勝ち数, 負け数, 試合数)"""
        games = self.history[-int(self.cfg.get("recent_count", 30)):]
        wins = sum(1 for g in games if g["r"] == "W")
        return wins, len(games) - wins, len(games)

    def nemesis(self, top=3):
        """直近 recent_count 戦で負けた回数が多い相手キャラ [(名前, 回数), ...]"""
        counts = {}
        for g in self.history[-int(self.cfg.get("recent_count", 30)):]:
            if g["r"] == "L" and g.get("o"):
                counts[g["o"]] = counts.get(g["o"], 0) + 1
        # 回数が多い順、同じ回数なら最近負けた方を先に
        order = {g.get("o"): i for i, g in enumerate(self.history)}
        return sorted(counts.items(), key=lambda kv: (-kv[1], -order.get(kv[0], 0)))[:top]

    def save(self):
        STATE_PATH.write_text(
            json.dumps({"streak": self.streak, "best": self.best, "history": self.history}, ensure_ascii=False),
            encoding="utf-8",
        )
        w, l, n = self.recent()
        nem = " / ".join(f"{k}×{v}" for k, v in self.nemesis()) or "なし"
        text = self.cfg["text_format"].format(streak=self.streak, best=self.best, wins=w, losses=l, games=n,
                                              nemesis=nem)
        (BASE / self.cfg["output_text"]).write_text(text, encoding="utf-8")

    def snapshot(self):
        with self.lock:
            w, l, n = self.recent()
            return {
                "streak": self.streak,
                "best": self.best,
                "event": self.last_event,
                "name": self.cfg.get("player_name", ""),
                "recent": {"wins": w, "losses": l, "games": n, "size": int(self.cfg.get("recent_count", 30))},
                # 直近の並び (古い→新しい)
                "marks": "".join(g["r"] for g in self.history[-int(self.cfg.get("recent_count", 30)):]),
                "nemesis": [{"name": k, "count": v, "icon": self.icons.get(k)} for k, v in self.nemesis()],
                "show_nemesis": bool(self.cfg.get("show_nemesis", True)),
                "show_recent": bool(self.cfg.get("show_recent", True)),
                "sound": {
                    "enabled": bool(self.cfg.get("play_reset_sound", False)),
                    "available": sound_path(self.cfg) is not None,
                    "volume": max(0.0, min(1.0, float(self.cfg.get("reset_sound_volume", 0.8)))),
                    "reset_seq": self.reset_seq,
                    "test_seq": self.test_seq,
                },
            }


# ---------------------------------------------------------------- OCR
_engine = OcrEngine.try_create_from_language(Language("ja"))
if _engine is None:
    sys.exit(
        "Windowsの日本語OCRが見つかりません。\n"
        "README.txt の「日本語OCRの追加」の手順で追加してから、もう一度起動してください。\n"
        "（管理者の PowerShell で次の1行を実行しても追加できます）\n"
        '  Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"'
    )


def ocr(img: Image.Image, engine=None):
    """[(text, (x, y, w, h)), ...] を行単位で返す。座標は img のピクセル。"""
    engine = engine or _engine
    rgba = img.convert("RGBA")
    w, h = rgba.size
    writer = DataWriter()
    # SoftwareBitmap は BGRA を期待
    r, g, b, a = rgba.split()
    writer.write_bytes(Image.merge("RGBA", (b, g, r, a)).tobytes())
    bmp = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, w, h)

    async def run():
        return await engine.recognize_async(bmp)

    result = asyncio.run(run())
    lines = []
    for line in result.lines:
        rects = [wd.bounding_rect for wd in line.words]
        x0 = min(r.x for r in rects)
        y0 = min(r.y for r in rects)
        x1 = max(r.x + r.width for r in rects)
        y1 = max(r.y + r.height for r in rects)
        text = "".join(wd.text for wd in line.words)
        lines.append((text, (x0, y0, x1 - x0, y1 - y0)))
    return lines


def ocr_scaled(img: Image.Image, box, scale):
    """box を拡大してOCRし、座標を元画像に戻して返す"""
    x0, y0, x1, y1 = box
    crop = img.crop(box)
    crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    return [
        (t, (x0 + x / scale, y0 + y / scale, w / scale, h / scale))
        for t, (x, y, w, h) in ocr(crop)
    ]


def norm(s):
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", "", s).lower()


def similarity(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if b in a:
        return 1.0
    # 行に余計な文字(1P など)が付くことがあるので、名前と同じ長さの部分文字列でも比較
    best = difflib.SequenceMatcher(None, a, b).ratio()
    n = len(b)
    for i in range(0, max(1, len(a) - n + 1)):
        best = max(best, difflib.SequenceMatcher(None, a[i:i + n], b).ratio())
    return best


# ---------------------------------------------------------------- 対戦相手のキャラ名
# VS画面の読み取りは smash-match-timestamps と同じ方式 (上部帯の見本との比較 + 英語OCR + ファイター名リスト)
from roster import ROSTER  # noqa: E402

_engine_en = OcrEngine.try_create_from_language(Language("en-US")) or _engine
_VS_T = np.asarray(Image.open(BASE / "vs_template.png").convert("L"), np.float32)[:, 130:]
_VS_T -= _VS_T.mean()
# VS画面の名前の位置 (画面比率): 左=1P / 右=2P
VS_NAME_BOXES = {"left": (100 / 1920, 15 / 1080, 900 / 1920, 140 / 1080),
                 "right": (1060 / 1920, 15 / 1080, 1880 / 1920, 140 / 1080)}
# 結果画面カード左上のキャラ名の位置 (1on1)
CARD_NAME_BOXES = {"left": (0.13, 0.02, 0.36, 0.19), "right": (0.54, 0.02, 0.77, 0.19)}


def vs_score(img: Image.Image):
    """VS画面の上部帯との似ている度合い (1に近いほどVS画面)。左上は配信のテロップが乗りやすいので比較しない"""
    a = np.asarray(img.convert("L").resize((384, 216), Image.BILINEAR), np.float32)[:24, 130:]
    a -= a.mean()
    return float((_VS_T * a).sum() / np.sqrt((_VS_T * _VS_T).sum() * (a * a).sum() + 1e-9))


def ascii_norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    s = s.translate(str.maketrans("01", "OI"))
    return re.sub(r"[^A-Z&]", "", s)


_ALIASES = [(ascii_norm(alias), jp) for jp, names in ROSTER.items() for alias in names]


def match_fighter(text):
    """OCR文字列 → 日本語のファイター名 (見つからなければ None)"""
    words = text.split()
    spans = {ascii_norm(" ".join(words[i:j])) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}
    spans.discard("")
    if not spans:
        return None
    score, jp = max((difflib.SequenceMatcher(None, t, a).ratio(), jp) for t in spans for a, jp in _ALIASES)
    return jp if score >= 0.75 else None


def read_fighter(img: Image.Image, box):
    """box (画面比率) の白い大文字のキャラ名を読む"""
    W, H = img.size
    x0, y0, x1, y1 = box
    crop = img.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))).convert("L")
    if crop.height < 120:
        crop = crop.resize((round(crop.width * 120 / crop.height), 120), Image.BICUBIC)
    a = np.asarray(crop)
    # 白文字だけ抜き出して白地に黒文字にする
    bw = Image.fromarray(np.where(a > 170, 0, 255).astype(np.uint8)).convert("RGB")
    return match_fighter(" ".join(t for t, _ in ocr(bw, _engine_en)))


# ---------------------------------------------------------------- 画面判定


def is_result_screen(img: Image.Image, joined):
    """
    結果画面にしか出ないものだけで判断する。
    (「世界戦闘力」はオンラインメニューやキャラ選択にも出るので使わない)
      - 左カードの「全滅/撃墜/落下/自滅」の見出し
      - 右下の「リプレイを保存」
      - A を押した人のカードの「READY FOR THE NEXT BATTLE」
    """
    W, H = img.size
    labels = "".join(t for t, _ in ocr_scaled(img, (int(W * 0.13), int(H * 0.55), int(W * 0.24), int(H * 0.92)), 3))
    if len(set(labels) & set("全滅撃墜落下自")) >= 3:
        return True
    replay = "".join(t for t, _ in ocr_scaled(img, (int(W * 0.84), int(H * 0.92), W, H), 3))
    if any(k in replay for k in ("リプレイ", "プレイを", "保存")):
        return True
    n = norm(joined).upper()
    return "NEXTBATTLE" in n or "READYFOR" in n


def gold_ratio(img: Image.Image, box):
    """box 内の金色(1位の数字)ピクセル割合"""
    a = np.asarray(img.crop(box).convert("RGB")).astype(np.int16)
    if a.size == 0:
        return 0.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    gold = (r > 190) & (g > 150) & (b < 110) & (r - b > 110) & (g > r * 0.72)
    return float(gold.mean())


def analyze(img: Image.Image, cfg, debug_dir=None):
    """
    戻り値: dict(screen="result"|"lobby"|"other", ...)
      result: win=True/False/None(名前が見つからない), score
      lobby : name=読み取ったネームタグ
    """
    W, H = img.size
    # VS画面: 両者のキャラ名を読む (重いOCRの前に軽い比較で判定)
    if vs_score(img) > cfg.get("vs_threshold", 0.5):
        fighters = {side: read_fighter(img, box) for side, box in VS_NAME_BOXES.items()}
        if any(fighters.values()):
            return {"screen": "vs", "fighters": fighters}

    lines = ocr(img)
    # 小さい文字対策: 上半分を拡大してもう一度OCR
    top_lines = ocr_scaled(img, (0, 0, W, int(H * 0.45)), 2)
    texts = [t for t, _ in lines]
    joined = "".join(texts) + "".join(t for t, _ in top_lines)

    if cfg.get("debug"):
        log("OCR:", texts, "| 上半分x2:", [t for t, _ in top_lines])

    if is_result_screen(img, joined):
        name = cfg.get("player_name", "")
        if not name:
            return {"screen": "result", "win": None, "reason": "名前未設定"}

        # 上半分にある行から自分の名前を探す → 自分のカードが左右どちらか
        best, best_line = 0.0, None
        for t, (x, y, w, h) in top_lines + lines:
            if y > H * 0.45:
                continue
            s = similarity(t, name)
            if s > best:
                best, best_line = s, (t, (x, y, w, h))
        my_side = None
        if best >= cfg["name_match_threshold"] and best_line:
            _, (x, y, w, h) = best_line
            my_side = "left" if x + w / 2 < W / 2 else "right"

        # A を押したプレイヤーのカードは「READY FOR THE NEXT BATTLE」に変わる
        ready_side = None
        for t, (x, y, w, h) in top_lines + lines:
            if "READY" in t.upper() or "NEXTBATTLE" in norm(t).upper():
                ready_side = "left" if x + w / 2 < W / 2 else "right"

        # 左右カードの順位表示エリア (1on1 のレイアウト)
        bands = {
            "left": (int(W * 0.28), 0, int(W * 0.50), int(H * 0.20)),
            "right": (int(W * 0.70), 0, int(W * 0.92), int(H * 0.20)),
        }
        gold = {k: round(gold_ratio(img, b), 4) for k, b in bands.items()}
        if debug_dir:
            for k, b in bands.items():
                img.crop(b).save(Path(debug_dir) / f"rank_area_{k}.png")
        # カード左上のキャラ名 (VS画面を読み逃したときの予備)。READY になったカードは読めない
        fighters = {side: (None if side == ready_side else read_fighter(img, box))
                    for side, box in CARD_NAME_BOXES.items()}
        return {
            "screen": "result",
            "my_side": my_side,
            "ready_side": ready_side,
            "gold": gold,
            "match": round(best, 2),
            "fighters": fighters,
        }

    # 対戦相手を待っている画面 (「対戦相手募集中」)。キャラ選択画面にも ENTRY! が出るので ENTRY では判断しない
    if "募集中" in joined:
        # 左下の1P枠ネームタグ (アイコンの右側) を拡大してOCR
        box = (int(W * 0.108), int(H * 0.895), int(W * 0.255), int(H * 0.965))
        if debug_dir:
            img.crop(box).save(Path(debug_dir) / "nametag_area.png")
        cands = [
            (x, t) for t, (x, y, w, h) in ocr_scaled(img, box, 3)
            if "ENTRY" not in t.upper() and len(norm(t)) >= 2
        ]
        cands.sort()
        name = "".join(t for _, t in cands)
        if "?" in name or "？" in name:
            name = ""
        return {"screen": "lobby", "name": name}

    return {"screen": "other"}


# ---------------------------------------------------------------- 監視ループ
class Watcher:
    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.in_result = False
        self.away = 0
        # ネームタグは1P枠から読むので、自分のカードは基本的に左 (1P)
        self.last_side = cfg.get("default_side", "left")
        self.name_reads = []
        self.vs_fighters = {}  # 直前のVS画面で読んだキャラ名 {"left": ..., "right": ...}
        self.vs_used = True  # VS画面の結果をもう試合結果に使ったか
        self.reset_episode()

    def reset_episode(self):
        # 1回の結果画面の間に集めた手がかり
        self.side_votes = {"left": 0, "right": 0}  # 自分の名前が見えた側
        self.ready_votes = {"left": 0, "right": 0}  # READY カードが出た側
        self.gold_hits = {"left": 0, "right": 0}  # 金色の1が見えた側
        self.card_fighters = {"left": {}, "right": {}}  # カードから読んだキャラ名ごとの回数
        self.result_frames = 0

    def frames(self, sec):
        return max(2, int(sec / self.cfg["interval_sec"]))

    def decide(self, final=False):
        """勝ち=True / 負け=False / まだ分からない=None"""
        thr_hits = 1 if final else 2
        if any(self.side_votes.values()):
            me = max(self.side_votes, key=self.side_votes.get)
        elif final:
            # 名前が一度も読めないまま終わった: 前の試合で自分がいた側 (最初は1P=左) とみなす
            me = self.last_side
        else:
            return None
        other = "right" if me == "left" else "left"
        if self.gold_hits[me] >= 1:
            return True
        if self.gold_hits[other] >= thr_hits:
            return False
        # 相手がすぐAを押して相手の金色の1が消えた場合: 自分のカードに一度も金色が出なければ負け
        if final and self.side_votes[me] >= 3:
            return False
        return None

    def summary(self):
        return (
            f"結果画面{self.result_frames}回 自分={self.side_votes} READY={self.ready_votes} "
            f"金色={self.gold_hits} 前回の自分側={self.last_side}"
        )

    def opponent(self):
        """対戦相手のキャラ名: 結果画面のカードで読めた名前 → だめならVS画面で読んだ名前"""
        opp = "right" if self.last_side == "left" else "left"
        votes = self.card_fighters[opp]
        if votes:
            return max(votes, key=votes.get)
        if not self.vs_used:
            return self.vs_fighters.get(opp)
        return None

    def commit(self, win):
        self.in_result = True
        if any(self.side_votes.values()):
            self.last_side = max(self.side_votes, key=self.side_votes.get)
        opp = self.opponent()
        self.vs_used = True
        info = f"相手={opp or '不明'} " + self.summary()
        self.state.record(win, ("WIN " if win else "LOSE ") + info, opp)

    def feed(self, img):
        res = analyze(img, self.cfg)
        scr = res["screen"]

        if scr == "vs":
            if self.vs_used:  # 新しい試合のVS画面
                self.vs_fighters, self.vs_used = {}, False
                log("VS画面:", " vs ".join(f or "?" for f in res["fighters"].values()))
            self.vs_fighters.update({k: v for k, v in res["fighters"].items() if v})

        if scr == "lobby" and res.get("name") and self.cfg.get("auto_learn_name"):
            name = res["name"]
            # 読み間違い対策: 同じ名前を3回続けて読めたときだけ覚える
            self.name_reads = (self.name_reads + [name])[-3:]
            if len(self.name_reads) == 3 and len(set(self.name_reads)) == 1 and name != self.cfg.get("player_name"):
                self.cfg["player_name"] = name
                CONFIG_PATH.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"ネームタグを取得: {name}")

        if scr == "result":
            self.away = 0
            if self.in_result:
                return
            if "reason" in res:
                log("結果画面:", res["reason"])
                return
            self.result_frames += 1
            if res["my_side"]:
                self.side_votes[res["my_side"]] += 1
            if res["ready_side"]:
                self.ready_votes[res["ready_side"]] += 1
            thr = self.cfg["gold_ratio_threshold"]
            for side, ratio in res["gold"].items():
                if ratio >= thr:
                    self.gold_hits[side] += 1
            for side, name in res["fighters"].items():
                if name:
                    self.card_fighters[side][name] = self.card_fighters[side].get(name, 0) + 1
            if self.cfg.get("debug"):
                log(f"結果画面: 自分={res['my_side']}(一致{res['match']}) READY={res['ready_side']} 金色={res['gold']}")
            win = self.decide()
            if win is not None:
                self.commit(win)
        else:
            self.away += 1
            # 結果画面が数秒消えたら次の試合を受け付ける
            if self.away >= self.frames(5):
                if not self.in_result and self.result_frames:
                    # 結果画面中に決めきれなかった分を、集めた手がかりで最終判定
                    win = self.decide(final=True)
                    if win is None:
                        log("結果画面を見たが勝敗を判定できず", self.summary())
                    else:
                        self.commit(win)
                self.in_result = False
                self.reset_episode()


def run_obs(cfg, state):
    import obsws_python as obs

    watcher = Watcher(cfg, state)
    while True:
        try:
            cl = obs.ReqClient(host=cfg["obs_host"], port=cfg["obs_port"], password=cfg["obs_password"], timeout=5)
            log(f"OBSに接続しました。ソース「{cfg['source_name']}」を監視中...")
            while True:
                t0 = time.time()
                r = cl.get_source_screenshot(cfg["source_name"], "jpg", 1280, 720, 90)
                data = r.image_data.split(",", 1)[1]
                img = Image.open(io.BytesIO(base64.b64decode(data)))
                watcher.feed(img)
                time.sleep(max(0, cfg["interval_sec"] - (time.time() - t0)))
        except KeyboardInterrupt:
            return
        except Exception as e:
            log("OBS接続エラー:", e, "-> 5秒後に再接続")
            time.sleep(5)


# ---------------------------------------------------------------- HTTP (オーバーレイ / 操作パネル)
def start_http(cfg, state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json"):
            b = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p.startswith("/icons/"):
                # ファイル名だけを受け付ける (フォルダの外は読ませない)
                file = p[len("/icons/"):]
                path = ICON_DIR / file
                if not re.fullmatch(r"[a-z0-9_]+\.png", file) or not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            if p == "/sound":
                # 設定で指定した音のファイルだけを返す
                path = sound_path(cfg)
                if path is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", SOUND_TYPES[path.suffix.lower()])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if p == "/api/option":
                # 操作パネルのオン/オフ切り替え。決まった項目だけ受け付けて config.json に保存する
                q = dict(urllib.parse.parse_qsl(self.path.partition("?")[2]))
                key = q.get("key")
                if key not in TOGGLES:
                    self.send_response(400)
                    self.end_headers()
                    return
                cfg[key] = q.get("value") == "1"
                save_config(cfg)
                log(f"設定を変更: {key} = {cfg[key]}")
                return self._send(json.dumps(state.snapshot(), ensure_ascii=False))
            if p in ("/", "/overlay"):
                return self._send((BASE / "overlay.html").read_text(encoding="utf-8"), "text/html")
            if p == "/dock":
                return self._send((BASE / "dock.html").read_text(encoding="utf-8"), "text/html")
            if p == "/api/state":
                return self._send(json.dumps(state.snapshot(), ensure_ascii=False))
            if p == "/api/win":
                state.record(True, "手動で勝ちを記録")
            elif p == "/api/lose":
                state.record(False, "手動で負けを記録")
            elif p == "/api/undo":
                state.undo()
            elif p == "/api/inc":
                state.set(state.streak + 1, "手動 +1")
            elif p == "/api/dec":
                state.set(state.streak - 1, "手動 -1")
            elif p == "/api/reset":
                state.set(0, "手動リセット")
            elif p == "/api/reset_best":
                state.reset_best()
            elif p == "/api/clear_history":
                state.clear_history()
            elif p == "/api/test_sound":
                state.test_sound()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self._send(json.dumps(state.snapshot(), ensure_ascii=False))

    srv = ThreadingHTTPServer(("127.0.0.1", cfg["http_port"]), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"オーバーレイ: http://localhost:{cfg['http_port']}/   操作パネル: http://localhost:{cfg['http_port']}/dock")
    print("※ この黒い画面を閉じるとカウンターが止まります。配信中は開いたままにしてください。")
    print("※ OBS にカウンターが表示されないときは、ブラウザソースのプロパティで「現在のページのキャッシュを更新」を押してください。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", nargs="+", help="画像ファイルで判定テスト")
    ap.add_argument("--name", help="プレイヤー名を指定してテスト")
    args = ap.parse_args()

    cfg = load_config()
    if args.test:
        if args.name:
            cfg["player_name"] = args.name
        for f in args.test:
            img = Image.open(f).convert("RGB")
            res = analyze(img, cfg, debug_dir=BASE)
            print(f, "->", json.dumps(res, ensure_ascii=False))
            if res["screen"] == "lobby" and res.get("name") and not args.name:
                cfg["player_name"] = res["name"]
        return

    state = State(cfg)
    start_http(cfg, state)
    run_obs(cfg, state)


if __name__ == "__main__":
    main()
