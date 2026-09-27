"""config: 設定（settings.json）と学習コード（learned.json）の読み書き。

■ 置き場所は ~/addon-data/ir-bridge/（W18・API_CONTRACT.md §6-1）
アンインストールしても残る場所。data/（アンインストールで消える）に置いていた v0.2.0 までは、
再インストールでプリセットが既定に戻り、学習済みのボタンが効かなくなった（DEV_LOG 2026-09-15 の
未決課題）。status.json・error_addon.log は data/ のまま（§6-1・§6-3）。
- ホームは Path.home() から組む（ユーザー名を書かない）。id は status.SERVICE（＝ディレクトリ名）。
- 作成と所有者は install.sh の責務（§6-1）。**起動時に無ければここでも作る**（誰かに消されても
  起動できるように。作れなければ WARNING で既定値のまま常駐する）。
- 自己テストは環境変数 IR_BRIDGE_CONFIG_DIR で置き場所ごと逃がす（tools/isolation.py）。

■ schema_version（W18）
外置きしたことで「v1.0.0 が書いた設定を将来の版が読む」ことが確定したので、両ファイルに
`schema_version`（整数）を入れる。**移行コードは持たない**（出荷台数 0 の時点で入れたため）。
1 以外（欠落を含む）は「読めなかったファイル」と同じ扱い＝既定値で起動し、元のファイルを保全する。
★v0.2.0 までの `format` キーは schema_version に置き換えた（書くだけで読んでいなかった。
  版番号のキーが2つあると、次に読む人がどちらが正か調べ直すことになる）。

■ 2ファイルに分ける理由
settings は人が滅多に触らない少数の値、learned は設定UIが頻繁に書き足す表で、
書き換え頻度も壊れたときの影響範囲も違う。learned が壊れても settings は生き残る。

■ 壊れていたときの方針（2段構え）
- **プログラムの誤り** → 起動を止める（codes.validate_presets の assert）
- **顧客のファイル編集が原因** → WARNING を出して既定値で常駐を続ける
  止めてしまうと、設定UI（:8100）ごと落ちて顧客が自力で直す手段を失う。
  learned は**1行の破損で全体を捨てない**（1行の書き損じで全ボタンが死ぬのは重すぎる）。

■ ★読めなかったファイルを黙って上書きしない（W18）
v0.2.0 までは、起動時こそ上書きしないが、**設定UIの最初の保存で**「既定値＋今回の変更」に
黙って置き換えていた（W18 第1段の9）。顧客の設定と、壊れた原因を調べる材料が同時に消える。
- 読めなかった（ファイルごと・schema_version 違い・項目の一部）ときは、起動時に元の中身を
  `<名前>.unreadable` へ**写して**から既定値（または読めた部分）で起動する。元のファイルは
  その場に残す（次の保存までは手で直せる）。世代は1つ（error_addon.log.old と同じ考え方）。
  同じ中身が保全済みなら書かない（壊れたまま再起動を繰り返しても SD を書かない）。
- 保全できなかった（元が読めない・書けない）ファイルは、**保存を拒否する**（SaveRefused）。
- WARNING は起動時にファイルごと1行（項目単位の WARNING は従来どおり項目ごと）。

■ 保持の仕方
読み込み結果は Config オブジェクト1つにまとめ、ロック付きの ConfigStore が抱える。
更新はフィールド単位で書き換えず、**新しい Config を作って参照を1回で差し替える**
＝読み手（受信スレッド）が半端に更新された設定を見る瞬間を作らない。
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import NamedTuple

from . import codes
from .codes import SceneTarget
from .nec import bytes_to_code, code_to_bytes
from .status import SERVICE, write_bytes_atomic

logger = logging.getLogger(__name__)

# 置き場所の差し替え口（**自己テスト専用**。unit にも data/env にも書かない）。
CONFIG_DIR_ENV = "IR_BRIDGE_CONFIG_DIR"
# §6-1: ~/addon-data/<id>/。id はディレクトリ名＝status.json の service（CLAUDE.md §14）。
CONFIG_DIR = Path(os.environ.get(CONFIG_DIR_ENV) or Path.home() / "addon-data" / SERVICE)
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LEARNED_FILE = CONFIG_DIR / "learned.json"
# 読めなかったファイルの写しの接尾辞（<名前>.unreadable）。
UNREADABLE_SUFFIX = ".unreadable"

# 互換性が壊れる変更のときだけ上げる。2ファイルは独立に採番する。
# ★1 以外（欠落を含む）は読めなかったファイルとして扱う。移行コードは持たない（モジュール冒頭）。
SETTINGS_SCHEMA_VERSION = 1
LEARNED_SCHEMA_VERSION = 1


class SaveRefused(OSError):
    """読めなかった元のファイルを保全できていないので、上書きを拒否した。"""


# 保存を拒否するファイル → 理由。load() が埋める（保全できなかったものだけ）。
_save_refused: dict[str, str] = {}

DEFAULT_DEBOUNCE_MS = 1000
# debounce_ms の下限（W18 R-6）。これより小さい値は下限に切り上げる（WARNING・元の中身は保全）。
# 根拠（実測）: NEC のフレーム周期は約 110ms（DEV_LOG 2026-09-12 M4-a 追補 E2: 押しっぱなしの
# データ1発＋リピート3発が 110ms 周期。データフレーム1発の送出は 67〜68ms＝M4-a 調査1）。
# 押している間フルフレームを送り直すリモコンは、同じコードを約 110ms ごとに送ってくる。
#   - 窓が 110ms 未満だと、1回の押下が複数回の実行になる（照明が連続で叩かれる）。
#   - 1フレーム落とすと次は 220ms 後に来る（Irdroid は 0.5〜1.2秒間隔の2発目で落とす実測がある。
#     M4-a 追補 E1）。1フレーム欠けても1回の押下を2回と数えないよう、2周期（220ms）より上に置く。
#   → 220ms に余裕を足して 250ms。人が同じボタンを意図して押し直す間隔の実測は無い（未測定）。
# 既定 1000ms はそのまま。下限を設ける目的は、0 などで上の畳み込みが効かなくなり、本体に繋がらない
# 間の WARNING 頻度（間引き前）がフレーム頻度（約9回/秒）まで上がる状態を作らないこと（第1段 2）。
MIN_DEBOUNCE_MS = 250
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


def _write_bytes(path, data: bytes) -> None:
    """→ status.write_bytes_atomic（W19-16 で共通化した。理由と流儀はそちらの docstring）。"""
    write_bytes_atomic(path, data)


def _write_json(path, payload: dict) -> None:
    refused = _save_refused.get(str(path))
    if refused is not None:
        raise SaveRefused(refused)
    _write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


class _Read(NamedTuple):
    raw: dict | None  # 使える中身。不在・読めないときは None
    problem: str | None  # 読めなかった理由。不在・正常なら None
    data: bytes | None  # 元の中身（保全用）。読めたバイト列が無ければ None


def _read_json(path, schema_version: int) -> _Read:
    """読むだけ（ログは出さない。load() がファイルごとに1行にまとめる）。"""
    try:
        data = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        # NotADirectoryError: 置き場所を作れなかった（途中がファイル）＝ファイルは無いのと同じ。
        return _Read(None, None, None)
    except OSError as e:
        return _Read(None, f"読めない（{e}）", None)
    try:
        raw = json.loads(data.decode("utf-8"))
    except ValueError as e:  # UnicodeDecodeError も ValueError
        return _Read(None, f"JSON として読めない（{e}）", data)
    if not isinstance(raw, dict):
        return _Read(None, "形式が不正（オブジェクトでない）", data)
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != schema_version:
        found = "無い" if "schema_version" not in raw else f"{version!r}"
        return _Read(None, f"schema_version が{found}（この版が読めるのは {schema_version}）", data)
    return _Read(raw, None, data)


def _drop(lost: list | None, msg: str, *args) -> None:
    """読めなかった項目を WARNING に出し、保全の要否の判定のために数える。"""
    logger.warning(msg, *args)
    if lost is not None:
        lost.append(msg % args)


def _preserve(path, data: bytes) -> str:
    """読めなかった元の中身を <名前>.unreadable へ写す。結果をログ用の短文で返す。

    同じ中身が既にあれば書かない。保全できなければ、その path の保存を拒否する。
    """
    dest = path.with_name(path.name + UNREADABLE_SUFFIX)
    replaced = False
    try:
        if dest.read_bytes() == data:
            return f"元の中身は {dest.name} に保全済み（同じ内容）"
        replaced = True
    except FileNotFoundError:
        pass
    except OSError:
        replaced = True
    try:
        _write_bytes(dest, data)
    except OSError as e:
        _save_refused[str(path)] = f"{path.name} を読めず、元の中身を保全できなかったため上書きしない"
        return f"元の中身を {dest.name} に保全できなかった（{e}）ため、{path.name} への保存は拒否する"
    return f"元の中身を {dest.name} に保全した" + ("（以前の保全は置き換えた）" if replaced else "")


def _report(path, read: _Read, lost: list, fallback: str) -> None:
    """ファイルごとに WARNING を1行にまとめる（起動時の1回）。"""
    if read.problem is None and not lost:
        return
    if read.data is not None:
        kept = _preserve(path, read.data)
    else:
        _save_refused[str(path)] = f"{path.name} を読めず、元の中身を保全できていないため上書きしない"
        kept = f"元の中身を読めないため保全していない（{path.name} への保存は拒否する）"
    if read.problem is not None:
        logger.warning("%s: %s。%sで起動する。%s", path, read.problem, fallback, kept)
    else:
        logger.warning("%s の一部（%d件・上の WARNING）を読めなかった。%s", path, len(lost), kept)


def _ensure_dir() -> None:
    """置き場所が無ければ作る（install.sh が作るのが正・§6-1。消されていても起動できるように）。"""
    for d in dict.fromkeys((SETTINGS_FILE.parent, LEARNED_FILE.parent)):
        if d.is_dir():
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
            logger.info("設定の置き場所が無いので作成: %s", d)
        except OSError as e:
            logger.warning("設定の置き場所を作れない（既定値で継続・変更は保存できない）: %s — %s", d, e)


# --- settings.json ------------------------------------------------------------
def _parse_base(raw, lost: list | None = None) -> int:
    """base は生値の文字列（"0x7d2e"）で持つ。読めない/構造条件違反なら既定へ落とす。

    ★assert で落とさない。原因は顧客のファイル編集なので、常駐を続けて WARNING で
    知らせるほうが復旧できる（プログラム埋め込みのプリセット表とは扱いを分ける）。
    """
    if raw is None:
        return codes.DEFAULT_BASE
    try:
        base = int(raw, 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        _drop(lost, "base を読めないので既定値 0x%04x を使う: %r", codes.DEFAULT_BASE, raw)
        return codes.DEFAULT_BASE
    if not 0 <= base <= 0xFFFF:
        _drop(lost, "base が2バイトに収まらないので既定値 0x%04x を使う: %r", codes.DEFAULT_BASE, raw)
        return codes.DEFAULT_BASE
    if not codes.is_separable_base(base):
        hi, lo = codes.base_bytes(base)
        _drop(
            lost,
            "base 0x%04x は通常NECの反転規則を破っていない（hi=0x%02x == ~lo=0x%02x）"
            "＝実在リモコンと衝突しうるので既定値 0x%04x を使う",
            base,
            hi,
            0xFF - lo,
            codes.DEFAULT_BASE,
        )
        return codes.DEFAULT_BASE
    return base


def _parse_brightness_lists(raw, lost: list | None = None) -> dict[str, list[int]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _drop(lost, "brightness_lists が不正（オブジェクトでない・既定を使う）: %r", raw)
        return {}
    out: dict[str, list[int]] = {}
    for key, values in raw.items():
        if not isinstance(values, list) or not values:
            _drop(lost, "brightness_lists の項目を無視（リストでない/空）: %r → %r", key, values)
            continue
        # ★真偽値を int として受けない（`isinstance(True, int)` は真・W19-11）。
        #   _parse_debounce は最初からこれを弾いていたのに、こちらだけ素通りしていた。
        clean = [
            v
            for v in values
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= codes.MAX_BRIGHTNESS
        ]
        if len(clean) != len(values):
            _drop(
                lost,
                "brightness_lists の範囲外の値を除いた: %r → %r（0〜%d のintのみ）",
                key,
                values,
                codes.MAX_BRIGHTNESS,
            )
        if clean:
            out[str(key)] = clean
    return out


def _parse_debounce(raw, lost: list | None = None) -> int:
    if raw is None:
        return DEFAULT_DEBOUNCE_MS
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        _drop(lost, "debounce_ms が不正なので既定値を使う: %r → %d", raw, DEFAULT_DEBOUNCE_MS)
        return DEFAULT_DEBOUNCE_MS
    if raw < MIN_DEBOUNCE_MS:
        # 既定（1000）ではなく下限へ切り上げる: 短くしたかった意図に近いほうへ寄せる。
        _drop(lost, "debounce_ms が下限 %dms より小さいので %dms を使う: %r", MIN_DEBOUNCE_MS, MIN_DEBOUNCE_MS, raw)
        return MIN_DEBOUNCE_MS
    return raw


# --- learned.json -------------------------------------------------------------
def _parse_learned(raw, lost: list | None = None) -> dict[tuple[str, codes.Bytes4], LearnedEntry]:
    """entries を (プロトコル名, バイト) キーの辞書へ正規化する。

    ★1行の破損で learned 全体を捨てない。捨てると顧客は**全ボタンを一度に失う**。
    読めなかった行だけ WARNING を出して飛ばし、残りは生かす。
    """
    entries = raw.get("entries") if raw else None
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        _drop(lost, "learned の entries が不正（オブジェクトでない・空として継続）")
        return {}

    out: dict[tuple[str, codes.Bytes4], LearnedEntry] = {}
    for code, body in entries.items():
        parsed = code_to_bytes(code) if isinstance(code, str) else None
        if parsed is None:
            _drop(lost, "learned の項目を無視（コード表記を読めない）: %r", code)
            continue
        if not isinstance(body, dict):
            _drop(lost, "learned の項目を無視（値がオブジェクトでない）: %r → %r", code, body)
            continue
        target_raw = body.get("target")
        if not isinstance(target_raw, dict) or not isinstance(target_raw.get("scene"), str):
            # 種別はキー名が兼ねる（kind フィールドは置かない）。いまは scene のみ。
            _drop(
                lost,
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
    """settings と learned を読む。無ければ既定で生成する。

    読めなかったもの（ファイルごと・schema_version 違い・項目の一部）は元の中身を保全してから
    既定値（読めた部分）で起動する。**読めなかったファイル自体は書き換えない**（モジュール冒頭）。
    """
    _save_refused.clear()
    _ensure_dir()

    settings_read = _read_json(SETTINGS_FILE, SETTINGS_SCHEMA_VERSION)
    if settings_read.data is None and settings_read.problem is None:  # 不在
        try:
            save_settings(default_config())
            logger.info("設定が無いので既定で作成: %s", SETTINGS_FILE)
        except OSError as e:
            logger.warning("設定の既定生成に失敗（既定値で継続）: %s", e)

    learned_read = _read_json(LEARNED_FILE, LEARNED_SCHEMA_VERSION)
    if learned_read.data is None and learned_read.problem is None:  # 不在
        try:
            _write_json(LEARNED_FILE, {"schema_version": LEARNED_SCHEMA_VERSION, "entries": {}})
            logger.info("学習コード表が無いので空で作成: %s", LEARNED_FILE)
        except OSError as e:
            logger.warning("学習コード表の生成に失敗（空で継続）: %s", e)

    settings = settings_read.raw or {}
    settings_lost: list[str] = []
    learned_lost: list[str] = []
    config = Config(
        base=_parse_base(settings.get("base"), settings_lost),
        debounce_ms=_parse_debounce(settings.get("debounce_ms"), settings_lost),
        brightness_lists=_parse_brightness_lists(settings.get("brightness_lists"), settings_lost),
        learned=_parse_learned(learned_read.raw, learned_lost),
    )
    _report(SETTINGS_FILE, settings_read, settings_lost, "既定値")
    _report(LEARNED_FILE, learned_read, learned_lost, "学習コード無し")
    return config


def save_settings(config: Config) -> None:
    _write_json(
        SETTINGS_FILE,
        {
            "schema_version": SETTINGS_SCHEMA_VERSION,
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
    _write_json(LEARNED_FILE, {"schema_version": LEARNED_SCHEMA_VERSION, "entries": entries})


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
