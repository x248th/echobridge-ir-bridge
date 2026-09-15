"""config: 設定（data/settings.json）と学習コード（data/learned.json）の読み書き。

■ 2ファイルに分ける理由
settings は人が滅多に触らない少数の値、learned は設定UIが頻繁に書き足す表で、
書き換え頻度も壊れたときの影響範囲も違う。learned が壊れても settings は生き残る。

■ 壊れていたときの方針（2段構え）
- **プログラムの誤り** → 起動を止める（codes.validate_presets の assert）
- **顧客のファイル編集が原因** → WARNING を出して既定値で常駐を続ける
  止めてしまうと、設定UI（:8100）ごと落ちて顧客が自力で直す手段を失う。
  learned は**1行の破損で全体を捨てない**（1行の書き損じで全ボタンが死ぬのは重すぎる）。

■ 保持の仕方
読み込み結果は Config オブジェクト1つにまとめ、ロック付きの ConfigStore が抱える。
更新はフィールド単位で書き換えず、**新しい Config を作って参照を1回で差し替える**
＝読み手（受信スレッド）が半端に更新された設定を見る瞬間を作らない。
"""
import json
import logging
import os
import threading
from typing import NamedTuple

from . import codes
from .codes import SceneTarget
from .nec import bytes_to_code, code_to_bytes
from .status import DATA_DIR

logger = logging.getLogger(__name__)

SETTINGS_FILE = DATA_DIR / "settings.json"
LEARNED_FILE = DATA_DIR / "learned.json"

# 互換性が壊れる変更のときだけ上げる。2ファイルは独立に採番する。
SETTINGS_FORMAT = 1
LEARNED_FORMAT = 1

DEFAULT_DEBOUNCE_MS = 1000
# ★輝度リストの既定はここに置かない。**既定は webui.DEFAULT_UI_BRIGHTNESS（[0, 100]）の1つだけ**
#   （M3-b の DEFAULT_BRIGHTNESS_LIST = [0,25,50,75,100] は M3-d で削除した。値の違う定数が
#   2か所にあると、次に読む人がどちらが正か調べ直すことになる）。


class LearnedEntry(NamedTuple):
    target: SceneTarget
    label: str


class Config(NamedTuple):
    base: int
    debounce_ms: int
    # instance（文字列キー） -> 輝度リスト。**未記載の照明はここに現れない**
    # （設定UIが既定 webui.DEFAULT_UI_BRIGHTNESS を当てる）。
    brightness_lists: dict[str, list[int]]
    # (プロトコル名, 送出順4バイト) -> LearnedEntry。**照合はバイトで行う**（nec.py 冒頭）。
    learned: dict[tuple[str, codes.Bytes4], LearnedEntry]


def default_config() -> Config:
    return Config(codes.DEFAULT_BASE, DEFAULT_DEBOUNCE_MS, {}, {})


def _write_json(path, payload: dict) -> None:
    """tmp → fsync → rename。電源断で半端なJSONが残らないようにする。

    fsync まで行うのは、SDカードのRaspberry Piで**書いた直後に電源が落ちる**ことが
    現実に起きるため（顧客宅にUPSは無い）。rename 自体は原子的だが、fsync しないと
    中身がディスクに届く前に rename だけ先に永続化されうる。
    """
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)  # data/ の他の生成物（status.json）と権限を揃える
    tmp.replace(path)


def _read_json(path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("設定を読めない（既定値で継続）: %s — %s", path, e)
        return None
    if not isinstance(raw, dict):
        logger.warning("設定の形式が不正（オブジェクトでない・既定値で継続）: %s", path)
        return None
    return raw


# --- settings.json ------------------------------------------------------------
def _parse_base(raw) -> int:
    """base は生値の文字列（"0x7d2e"）で持つ。読めない/構造条件違反なら既定へ落とす。

    ★assert で落とさない。原因は顧客のファイル編集なので、常駐を続けて WARNING で
    知らせるほうが復旧できる（プログラム埋め込みのプリセット表とは扱いを分ける）。
    """
    if raw is None:
        return codes.DEFAULT_BASE
    try:
        base = int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "base を読めないので既定値 0x%04x を使う: %r", codes.DEFAULT_BASE, raw
        )
        return codes.DEFAULT_BASE
    if not 0 <= base <= 0xFFFF:
        logger.warning("base が2バイトに収まらないので既定値 0x%04x を使う: %r", codes.DEFAULT_BASE, raw)
        return codes.DEFAULT_BASE
    if not codes.is_separable_base(base):
        hi, lo = codes.base_bytes(base)
        logger.warning(
            "base 0x%04x は通常NECの反転規則を破っていない（hi=0x%02x == ~lo=0x%02x）"
            "＝実在リモコンと衝突しうるので既定値 0x%04x を使う",
            base,
            hi,
            0xFF - lo,
            codes.DEFAULT_BASE,
        )
        return codes.DEFAULT_BASE
    return base


def _parse_brightness_lists(raw) -> dict[str, list[int]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("brightness_lists が不正（オブジェクトでない・既定を使う）: %r", raw)
        return {}
    out: dict[str, list[int]] = {}
    for key, values in raw.items():
        if not isinstance(values, list) or not values:
            logger.warning("brightness_lists の項目を無視（リストでない/空）: %r → %r", key, values)
            continue
        clean = [v for v in values if isinstance(v, int) and 0 <= v <= codes.MAX_BRIGHTNESS]
        if len(clean) != len(values):
            logger.warning(
                "brightness_lists の範囲外の値を除いた: %r → %r（0〜%d のintのみ）",
                key,
                values,
                codes.MAX_BRIGHTNESS,
            )
        if clean:
            out[str(key)] = clean
    return out


def _parse_debounce(raw) -> int:
    if raw is None:
        return DEFAULT_DEBOUNCE_MS
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        logger.warning("debounce_ms が不正なので既定値を使う: %r → %d", raw, DEFAULT_DEBOUNCE_MS)
        return DEFAULT_DEBOUNCE_MS
    return raw


# --- learned.json -------------------------------------------------------------
def _parse_learned(raw) -> dict[tuple[str, codes.Bytes4], LearnedEntry]:
    """entries を (プロトコル名, バイト) キーの辞書へ正規化する。

    ★1行の破損で learned 全体を捨てない。捨てると顧客は**全ボタンを一度に失う**。
    読めなかった行だけ WARNING を出して飛ばし、残りは生かす。
    """
    entries = raw.get("entries") if raw else None
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        logger.warning("learned の entries が不正（オブジェクトでない・空として継続）")
        return {}

    out: dict[tuple[str, codes.Bytes4], LearnedEntry] = {}
    for code, body in entries.items():
        parsed = code_to_bytes(code) if isinstance(code, str) else None
        if parsed is None:
            logger.warning("learned の項目を無視（コード表記を読めない）: %r", code)
            continue
        if not isinstance(body, dict):
            logger.warning("learned の項目を無視（値がオブジェクトでない）: %r → %r", code, body)
            continue
        target_raw = body.get("target")
        if not isinstance(target_raw, dict) or not isinstance(target_raw.get("scene"), str):
            # 種別はキー名が兼ねる（kind フィールドは置かない）。いまは scene のみ。
            logger.warning(
                "learned の項目を無視（target が {\"scene\": \"...\"} でない）: %r → %r",
                code,
                target_raw,
            )
            continue
        label = body.get("label")
        out[parsed] = LearnedEntry(
            target=SceneTarget(target_raw["scene"]),
            label=label if isinstance(label, str) else "",
        )
    return out


# --- 読み書き -----------------------------------------------------------------
def load() -> Config:
    """settings と learned を読む。無ければ既定で生成する。"""
    settings = _read_json(SETTINGS_FILE)
    if settings is None and not SETTINGS_FILE.exists():
        try:
            save_settings(default_config())
            logger.info("設定が無いので既定で作成: %s", SETTINGS_FILE)
        except OSError as e:
            logger.warning("設定の既定生成に失敗（既定値で継続）: %s", e)
        settings = None

    learned_raw = _read_json(LEARNED_FILE)
    if learned_raw is None and not LEARNED_FILE.exists():
        try:
            _write_json(LEARNED_FILE, {"format": LEARNED_FORMAT, "entries": {}})
            logger.info("学習コード表が無いので空で作成: %s", LEARNED_FILE)
        except OSError as e:
            logger.warning("学習コード表の生成に失敗（空で継続）: %s", e)

    settings = settings or {}
    return Config(
        base=_parse_base(settings.get("base")),
        debounce_ms=_parse_debounce(settings.get("debounce_ms")),
        brightness_lists=_parse_brightness_lists(settings.get("brightness_lists")),
        learned=_parse_learned(learned_raw),
    )


def save_settings(config: Config) -> None:
    _write_json(
        SETTINGS_FILE,
        {
            "format": SETTINGS_FORMAT,
            "base": f"0x{config.base:04x}",
            "debounce_ms": config.debounce_ms,
            "brightness_lists": config.brightness_lists,
        },
    )


def save_learned(config: Config) -> None:
    entries = {}
    for (_proto, raw), entry in config.learned.items():
        _p, code = bytes_to_code(*raw)
        entries[code] = {"target": {"scene": entry.target.key}, "label": entry.label}
    _write_json(LEARNED_FILE, {"format": LEARNED_FORMAT, "entries": entries})


def base_label(base: int) -> str:
    """ログ用の base の呼び名。プリセット表に無い値でも読める形にする。"""
    n = codes.preset_number(base)
    return f"プリセット{n}（0x{base:04x}）" if n is not None else f"0x{base:04x}（プリセット表に無い値）"


class ConfigStore:
    """Config をロック付きで抱える。**差し替えは参照の1回の代入で行う。**

    M3の設定UI（:8100）が同一プロセスから書き換える前提。フィールドを1つずつ
    書き換えると、読み手（受信スレッド）が「新しい base ＋ 古い learned」という
    **存在しなかった組み合わせ**を読む瞬間が生まれる。丸ごと差し替えれば隙間が無い。

    共有状態はこれと RecentCodes の2つだけ（M3-cで1つ増えた）。
    読む側＝受信スレッド、書く側＝設定UIスレッド（将来）。
    """

    def __init__(self, config: Config):
        self._config = config
        self._lock = threading.Lock()

    def get(self) -> Config:
        with self._lock:
            return self._config

    def replace(self, config: Config) -> Config:
        """設定を差し替え、**差し替え前の Config を返す**。

        ベースが変わったときのログはここに置く。差し替え経路がいくつ増えても
        （UI・reload・将来の何か）必ず1行残るようにするため。
        ★ログはロックの外で出す。logging は I/O でブロックしうるので、
          ロックを握ったまま呼ぶと受信スレッドの get() を巻き添えに待たせる。
        """
        with self._lock:
            old = self._config
            self._config = config
        if old.base != config.base:
            # 顧客が既に困っている場面（衝突対応）なので、切り替わったことが
            # journal からはっきり読めるようにする。1回につき1行なので枠には影響しない。
            logger.info(
                "ベースを%sから%sへ変更しました。学習済みのボタンは再学習が必要です",
                base_label(old.base),
                base_label(config.base),
            )
        return old

    def reload(self) -> Config:
        """ファイルを読み直して差し替える。差し替え後の Config を返す。

        ★**検証は起動時とまったく同じ `load()` を通す。** 別の検証経路を書くと、
        「起動時は弾かれるが差し替えでは素通りする値」という非対称が生まれる。
        load() をそのまま呼ぶことで、非対称を作りようがなくしてある。

        ★mtimeポーリングは持たない（M3-cの方針）。ファイル直接編集の反映は
        `systemctl restart` が正で、それは仕様である:
          1. 直接編集するのは障害対応の場面で、明示的な再起動のほうが「効いた」ことが確実
          2. ポーリングは書き込み途中のファイルを読む故障モードを新設する
          3. 5秒ごとのRXデバイス監視ループ（受信を止めてはいけない）に責務を乗せない
        本メソッドの想定呼び出し元は M3(d) の設定UI——自分でファイルを書いた**直後**に
        呼ぶので、書き込み途中を読む問題が起きない。
        """
        new = load()
        self.replace(new)
        return new
