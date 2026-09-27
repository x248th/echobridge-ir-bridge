"""codes: IRコードの導出（encode）と逆写像（decode）。M3-b の中核。

■ 思想（なぜ対応表を持たずに導出するか）
シーンも個別照明も、**コードそのものが宛先を表す**ようにする。対応表を持たなければ、
学習作業も、対応表とシーン一覧のずれも、対応表の永続化も要らなくなる。
本モジュールは「宛先 → 4バイト」と「4バイト → 宛先」の相互変換だけを担い、
**状態を一切持たない**（シーン一覧も照明一覧もキャッシュしない。理由は decode の注記）。

■ コードの構造（送出順の4バイト）
    b0 b1 = base（プリセット。下位・上位の順で送出される）
    b2    = シーンなら NN（シーン番号）／照明なら BB（輝度%）
    b3    = シーンなら 0x00（予約）／照明なら II（instance・1始まり）

    シーン    scene_NN      → 2e 7d NN 00   （既定プリセット 0x7d2e の場合）
    個別照明  instance, BB% → 2e 7d BB II

ir-ctl 表記に直すと `nec32:0x7d2e{II}{BB}` になる（nec.py の並び規則）。
例: 照明3 を 50% → 2e 7d 32 03 → `nec32:0x7d2e0332`。

■ 実在リモコンとの構造的分離（本方式が成立する条件）
NEC は `b1 == ~b0`（通常NEC）や `b3 == ~b2`（拡張NEC）という反転規則を持つ。
市販リモコンの大半はこの規則に**従う**ので、**規則を意図的に破ったコードを使えば
実在リモコンと衝突しない**。破り方は2箇所で担保する:

  1. base 側: `base_hi != 0xFF - base_lo` → 通常NECになりえない
  2. 宛先側: `II != 0xFF - BB`            → 拡張NECになりえない

2つが揃うと、導出コードは**必ず nec32 に分類される**（＝反転規則に従う実在リモコンの
コード空間と交わらない）。衝突しうるのは「反転規則を破る機器」だけで、そのために
プリセットを3つ用意してある。

**衝突しないことは保証ではない。** 構造的に確率を下げているだけで、
「反転規則を破るリモコン」が同じ base を使えば当たる。だからプリセット切替がある。
"""
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

# --- プリセット表（プログラム埋め込みの定数） --------------------------------
# 選定根拠は**構造条件のみ**（測定の裏付けは無い。当てずっぽうではないが実測でもない）:
#   - base_hi != 0xFF - base_lo（通常NECにならない）
#   - 0x00 / 0xFF を含まない
#   - 3つとも上位・下位の両バイトが互いに異なる
#     ← 片方のバイトしか見ない照合が世の中に存在しても、必ず逃げ場があるようにする保険
#
#   プリセット1（既定） 0x7d2e  送出 b0=2e b1=7d   ~2e=d1 != 7d
#   プリセット2         0x4b6a  送出 b0=6a b1=4b   ~6a=95 != 4b
#   プリセット3         0x2957  送出 b0=57 b1=29   ~57=a8 != 29
PRESETS = (0x7D2E, 0x4B6A, 0x2957)
DEFAULT_BASE = PRESETS[0]

# b3 == 0x00 をシーンに予約する。instance は1始まりなので照明と衝突しない。
SCENE_INSTANCE = 0x00
MAX_BRIGHTNESS = 100
MAX_SCENE_NUMBER = 0xFF
MAX_INSTANCE = 0xFF

Bytes4 = tuple[int, int, int, int]


# --- 宛先の型 -----------------------------------------------------------------
# summary は**動詞の連用形で終える**こと。ログはこれに接尾辞を足して3つの文を作る:
#     受信 <コード> → {summary}（実行を要求）
#     {summary}しました
#     {summary}できませんでした（<理由>）— <詳細>
# 名詞で終えると「シーン scene_15しました」のように崩れる（M3-bのレビューで実際に踏んだ）。
#
# label は**名詞で終える**（送信側 M4-a のログ用）: 「{label} のコードを送信しました（<コード>）」
class SceneTarget(NamedTuple):
    key: str

    @property
    def summary(self) -> str:
        return f"シーン {self.key} を発火"

    @property
    def label(self) -> str:
        return f"シーン {self.key}"


class LightTarget(NamedTuple):
    instance: int
    brightness: int

    @property
    def summary(self) -> str:
        return f"照明{self.instance} を {self.brightness}% に設定"

    @property
    def label(self) -> str:
        return f"照明{self.instance} を {self.brightness}%"


Target = SceneTarget | LightTarget


class Interpretation(NamedTuple):
    """受信コードの解釈結果。ログ・RecentCodes・発火の3者がこれ1つを見る。

    target が None なら発火しない（未登録・範囲外・別プリセット）。
    reason は**ログと診断レポート（開発側）**に出す短文。
    display は**設定UIの受信ログ（顧客）**に出す短文で、宛先が無い行だけが持つ。
    ★2つを使い回さない（宛先が違う・UI調整 ■I）。以前は reason を画面にもそのまま出していて、
    「未登録（learned にも導出にも一致せず）」のような開発者の語が顧客の画面に出ていた。
    display は「何もしなかった」ことが分かる形で終える（顧客は照明が動かなかった理由を探している）。
    """

    target: Target | None
    reason: str
    # source: learned / derived / other_preset / out_of_range / foreign_necx / unknown
    source: str
    display: str = ""


def base_bytes(base: int) -> tuple[int, int]:
    """base（0x7d2e 等）を (hi, lo) に割る。送出順は b0=lo, b1=hi。"""
    return (base >> 8) & 0xFF, base & 0xFF


def is_separable_base(base: int) -> bool:
    """base が通常NECの反転規則を破っているか（＝実在リモコンと構造的に分離できるか）。"""
    hi, lo = base_bytes(base)
    return hi != 0xFF - lo


def preset_number(base: int) -> int | None:
    """base がプリセット表の何番目か（1始まり）。表に無ければ None。"""
    return PRESETS.index(base) + 1 if base in PRESETS else None


def validate_presets() -> None:
    """プリセット表を起動時に一括検証する。破れていたら**起動を止める**。

    ★assert を使う。破れている状態で常駐を続けると、実在リモコンと衝突しうるコードを
    製品が出し続けることになり、顧客側では「たまに誤動作する」としか見えない。
    設定ファイル由来の異常（後述の fallback）と違い、**これはプログラムの誤りなので
    直ちに止めるのが正しい**。
    ※python を -O で起動すると assert は消える。unit は -O を使っていない（M3-b DEV_LOG）。
    """
    for base in PRESETS:
        hi, lo = base_bytes(base)
        assert is_separable_base(base), (
            f"プリセット 0x{base:04x} が通常NECの反転規則を破っていない"
            f"（hi=0x{hi:02x} == ~lo=0x{0xFF - lo:02x}）"
        )
        assert 0x00 not in (hi, lo) and 0xFF not in (hi, lo), (
            f"プリセット 0x{base:04x} が 0x00 / 0xFF を含む（hi=0x{hi:02x} lo=0x{lo:02x}）"
        )
    assert len(set(PRESETS)) == len(PRESETS), f"プリセットに重複がある: {PRESETS}"
    his = [base_bytes(b)[0] for b in PRESETS]
    los = [base_bytes(b)[1] for b in PRESETS]
    assert len(set(his)) == len(his), f"プリセットの上位バイトが重複している: {his}"
    assert len(set(los)) == len(los), f"プリセットの下位バイトが重複している: {los}"


# --- encode（宛先 → 4バイト） -------------------------------------------------
def scene_number(key: str) -> int:
    """"scene_15" → 15。`scene_` の後ろを int() で取る。取れなければ ValueError。"""
    prefix = "scene_"
    if not key.startswith(prefix):
        raise ValueError(f"シーンkeyが 'scene_' で始まらない: {key!r}")
    return int(key[len(prefix) :])


def is_foreign_necx(b2: int, b3: int) -> bool:
    """拡張NECの反転規則に合致するか（`b3 == ~b2`）＝**当機の導出コードではない**。

    導出コードはこの条件を絶対に満たさない（encode 側の `_assert_separable` が保証する）。
    したがって受信フレームがこれを満たすなら、それは実在の拡張NECリモコンである。
    """
    return b3 == 0xFF - b2


def _assert_separable(b2: int, b3: int) -> None:
    """拡張NECの反転規則を破っていることを保証する（構造的分離の後半）。

    ★**encode 側にだけ置く。** 「導出規則が衝突コードを生成しない」ことを守る条件で
    あって、受信フレームの妥当性チェックではない。decode 側に置くと、他社の拡張NEC
    リモコンのボタン1つで常駐が落ちる（受信するフレームの中身はこちらで制御できない）。

    ★instance <= 154 かつ BB <= 100 なら自動的に成立するので、いまは必ず通る。
    それでも明示的に置くのは、**将来 instance の上限が動いたときに黙って壊れる**箇所
    だからである（例: instance 155 を 100% にすると 155 == 0xFF-100 で拡張NECになり、
    実在リモコンのコード空間へ入り込む）。ここが落ちたら方式の前提が崩れている。
    """
    assert b3 != 0xFF - b2, (
        f"導出コードが拡張NECの反転規則に合致してしまう（b2=0x{b2:02x} b3=0x{b3:02x}）"
        "＝実在リモコンと構造的に分離できない。instance/輝度の上限を見直すこと"
    )


def encode_scene(base: int, key: str) -> Bytes4:
    """シーンkey → 送出順4バイト。NN が1バイトに収まらなければ ValueError。"""
    nn = scene_number(key)
    if not 0 <= nn <= MAX_SCENE_NUMBER:
        # 現実には起きない（EDT 24B×10スロット）。起きたら表現できないので呼び側が外す。
        raise ValueError(f"シーン番号が1バイトに収まらない: {key} (NN={nn})")
    hi, lo = base_bytes(base)
    _assert_separable(nn, SCENE_INSTANCE)
    return (lo, hi, nn, SCENE_INSTANCE)


def encode_light(base: int, instance: int, brightness: int) -> Bytes4:
    """照明 instance を brightness% にするコード → 送出順4バイト。"""
    if not 1 <= instance <= MAX_INSTANCE:
        raise ValueError(f"instance が範囲外: {instance}（1〜{MAX_INSTANCE}）")
    if not 0 <= brightness <= MAX_BRIGHTNESS:
        raise ValueError(f"輝度が範囲外: {brightness}（0〜{MAX_BRIGHTNESS}）")
    hi, lo = base_bytes(base)
    _assert_separable(brightness, instance)
    return (lo, hi, brightness, instance)


def target_info(target: Target | None) -> dict | None:
    """宛先を JSON にできる形へ（受信ログの表示用・M3-i）。

    ★渡すのは **key と instance/brightness という照合できる値だけ**で、表記文字列は渡さない。
    表示名の差し込みは受け手（設定UI）が行う。表記文字列を渡して受け手がそれを加工すると、
    **表示がログ文面に結合**し、文面を変えた瞬間に黙って劣化する（M3-b で照合を表記から
    バイトへ寄せたのと同じ判断）。
    """
    if isinstance(target, SceneTarget):
        return {"kind": "scene", "key": target.key}
    if isinstance(target, LightTarget):
        return {"kind": "light", "instance": target.instance, "brightness": target.brightness}
    return None


def encode(base: int, target: Target) -> Bytes4:
    """宛先 → 送出順4バイト（送信 M4-a の入口）。表現できなければ ValueError。

    ★存在しないシーン・照明を**ここでも検証しない**（decode の注記と同じ理由）。
    送ったコードを顧客のスマートリモコンが学習し、使ったときに本体が 404 を返す——それが唯一の真実。
    """
    if isinstance(target, SceneTarget):
        return encode_scene(base, target.key)
    if isinstance(target, LightTarget):
        return encode_light(base, target.instance, target.brightness)
    raise TypeError(f"未知の宛先型: {target!r}")


def encodable_scene_keys(keys) -> list[str]:
    """表現できるシーンkeyだけを残す。外したものは WARNING で理由を残す。

    学習UIの一覧を作るために要る。**黙って外すと「一覧に出ないシーン」の原因が
    追えなくなる**し、黙って別の値を出すのは最悪（違うシーンのコードを配ることになる）。
    """
    ok = []
    for key in keys:
        try:
            scene_number(key)
        except ValueError as e:
            logger.warning("シーンをコード化できないため一覧から外す: %s — %s", key, e)
            continue
        nn = scene_number(key)
        if nn > MAX_SCENE_NUMBER:
            logger.warning(
                "シーン番号が1バイト(0〜%d)に収まらないため一覧から外す: %s (NN=%d)",
                MAX_SCENE_NUMBER,
                key,
                nn,
            )
            continue
        ok.append(key)
    return ok


# --- decode（4バイト → 宛先） -------------------------------------------------
def decode(base: int, raw: Bytes4) -> Interpretation | None:
    """導出コードとして解釈する。現在の base にも他プリセットにも当たらなければ None。

    分類の順序（base 一致のとき）:
        II == 0xFF - BB  → 他社リモコンの拡張NECフレーム（発火しない）
        II == 0x00       → シーン
        BB > 100         → 範囲外として弾く（発火しない）
        それ以外          → 照明 II を BB%

    ★**この関数は assert しない。** 受信フレームの中身はこちらで制御できないので、
    どんな4バイトを食わされても例外を投げずに解釈を返すこと。構造条件の assert は
    encode 側の責務である（`_assert_separable` のdocstring）。

    ★存在しないシーン・照明を**ここで検証しない**。一覧を保持すると状態を持たない設計が
    崩れ、しかも古いキャッシュは「実在するのに弾かれる」という、より悪い故障になる。
    存在しない宛先は本体へ投げて 404 を WARNING に出す（それが唯一の真実）。
    """
    b0, b1, b2, b3 = raw
    hi, lo = base_bytes(base)
    if (b0, b1) != (lo, hi):
        # 他のプリセットなら、そう言う。顧客がプリセットを切り替えた直後に必ず起きるので、
        # 「未登録」と一緒くたにすると原因に辿り着けない。
        for other in PRESETS:
            if other == base:
                continue
            o_hi, o_lo = base_bytes(other)
            if (b0, b1) == (o_lo, o_hi):
                return Interpretation(
                    target=None,
                    reason=(
                        f"プリセット{preset_number(other)} のコードです。"
                        f"現在はプリセット{preset_number(base)} です"
                    ),
                    source="other_preset",
                    display=(
                        f"プリセット{preset_number(other)} のコードです"
                        f"（いまはプリセット{preset_number(base)} なので、何もしません）"
                    ),
                )
        return None

    # ★シーン判定より**先**に見る。導出コードはこの条件を絶対に満たさないので誤検出しない
    #   （満たすのは scene_255 に相当する形だけで、それは encode 側の assert が拒む）。
    #   これが出るのは「顧客の環境に base と同じ先頭2バイトを使う拡張NECリモコンが
    #   実在する」という意味で、**衝突調査で最も知りたい情報**である。
    #   分類せずに放っておくと II=155〜255 が「存在しない instance」として 404 になるだけで、
    #   顧客も当方も原因に辿り着けない（M3-b レビューでの指摘）。
    if is_foreign_necx(b2, b3):
        n = preset_number(base)
        return Interpretation(
            target=None,
            reason=(
                "他社リモコンの拡張NECフレーム（当機のコードではない）"
                f"— プリセット{n}(0x{base:04x}) と同じ先頭2バイトを使う機器が環境にある。"
                "誤動作が起きるならプリセットを切り替えること"
            ),
            source="foreign_necx",
            display="他の機器のリモコンです（何もしません）",
        )

    if b3 == SCENE_INSTANCE:
        # :02d は最小幅なので、3桁のシーン番号もそのまま通る。
        return Interpretation(SceneTarget(f"scene_{b2:02d}"), "", "derived")

    if b2 > MAX_BRIGHTNESS:
        # 導出コードの形はしているが輝度として読めない。**本体へは投げない**
        # （0-100 しか受け付けない契約なので 400 が返るだけ。叩くだけ無駄で、
        #   実体は実在リモコンとの衝突であることが多い）。
        return Interpretation(
            target=None,
            reason=f"導出コードだが輝度 {b2} が範囲外（0〜{MAX_BRIGHTNESS}）のため本体へ投げない",
            source="out_of_range",
            display="本機のコードとして読めませんでした（何もしません）",
        )

    return Interpretation(LightTarget(b3, b2), "", "derived")
