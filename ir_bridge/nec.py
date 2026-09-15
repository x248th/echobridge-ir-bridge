"""nec: NEC 32bit フレームのデコーダ（pulse+space のペア合計で 0/1 を判定する方式）。

■ なぜカーネルのNECデコーダ（ir-keytable -p nec）を使わないか（人間が実機で判定済み・不変）
VS1838B の AGC でパルスが伸びスペースが縮み、カーネルの許容窓（NEC_UNIT=562.5us ± 281us）
を外れてデコードが落ちる。実測（SwitchBotから nec32:0x7d2e5b91 を撃った生波形）:

    リーダー   +9228 -4281
    ビット     +864 -298 / +810 -329 / +811 -1453 / +804 -1459 ...
    パルス     774〜898     （窓上限844を超えるものが複数ある）
    '0'スペース 271〜380     （窓下限281を下回るものがある）
    '1'スペース 1415〜1484
    終端       -20730

**個々の値は窓を外れるが、pulse+space の合計は 1125us / 2250us 付近に保たれる**
（+864-298=1162 / +810-329=1139 / +811-1453=2264）。AGCは配分をずらすだけで
ペアの長さは動かさないので、合計で判定すればずれを吸収できる。これが本モジュールの方式。

■ 出力するコード表記（ir-ctl / ir-keytable と同じ）
32bitのビット列は「先に来た8bitずつ・各バイトはLSB先行」で4バイトへ畳む
（NEC の送出順そのまま）。得た4バイト b0 b1 b2 b3 から表記を組む:
    b1 == ~b0 かつ b3 == ~b2 → "nec:0xAACC"     （通常NEC・アドレス8bit）
    b3 == ~b2               → "necx:0xAAAACC"   （拡張NEC・アドレス16bit）
    それ以外                 → "nec32:0x……"      （反転が成立しない32bit）

**nec32 のバイト並びは M2-B で実機確定した**（それまでは decoder/encoder で解釈が割れる
という理由で当方は「送出順そのまま」を主表記にしていたが、これは誤り）:

    ir-ctl -S nec32:0x7d2e5b91 で撃つ → 送出順の4バイトは 2e 7d 91 5b
    → ir-ctl 表記 = b1<<24 | b0<<16 | b3<<8 | b2

**ir-ctl 表記を正とする**（TX実装で ir-ctl と同じ土俵に立つため／製品の既定IRコードが
この表記で設計ドキュメントに記録済みで、表記が2つあると顧客も混乱するため）。
nec / necx は送信・受信で表記が一致することを実測で確認済み（nec:0x8877 を撃って
nec:0x8877 で受信）。曖昧さがあったのは nec32 だけである。

■ 照合はバイトで行う（M3-b）
表記文字列は**人に見せるためだけ**のものにした。対応表（learned）の照合・デバウンスの
キーは、すべて `(プロトコル名, 送出順4バイト)` で持つ。文字列で照合していると、
同じ波形が表記の違いで別物になりうる（M2-Bの旧表記併記はその症状への対症療法だった）。
設定ファイルに書かれた文字列は読み込み時に `code_to_bytes()` でバイトへ正規化する。
**M2-Bで残した旧表記（送出順そのまま）の後方互換ルックアップは削除した**
——照合がバイトになった時点で、表記違いを吸収する仕組み自体が要らなくなったため。
"""
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

# --- 判定窓（すべてマイクロ秒） ----------------------------------------------
# リーダー: 9000/4500（データ）・9000/2250（リピート）。実測 9228/4281 を含む幅を取る。
LEADER_PULSE_MIN, LEADER_PULSE_MAX = 7500, 11000
LEADER_SPACE_DATA_MIN, LEADER_SPACE_DATA_MAX = 3500, 5500
LEADER_SPACE_REPEAT_MIN, LEADER_SPACE_REPEAT_MAX = 1700, 3000

# ビット: pulse+space の合計で判定する（本モジュールの要）。
#   '0' = 1125us（2単位） / '1' = 2250us（4単位）。しきい値は中間の3単位=1687us。
# 実測の合計（M2-Dで受光素子2種・光源3系統まで広げた和集合）は
#   '0' 924〜1218us / '1' 2016〜2268us
# **下限800までの余裕は124usしかない**（'0'の最小924との差）。上限2800側は532us。
# ★M2以前のコメントは '0'≈1045〜1278 と書いていたが、これはVS1838B単独の範囲で、
#   余裕を2倍に見誤らせる値だった（M2-Dの棚卸しで是正）。窓を触るときはこの124usが基準。
#   実測値の内訳と出どころは tools/nec_selftest.py の GPIO / IRDROID 定義にある。
PAIR_SUM_MIN = 800
PAIR_SUM_MAX = 2800
PAIR_SUM_THRESHOLD = 1687

NEC_BITS = 32


class NecFrame(NamedTuple):
    """デコード結果。repeat=True のときコード類は None（リピートフレームは値を運ばない）。"""

    repeat: bool
    protocol: str | None = None
    # code=表記（ir-ctl互換）。**人に見せる用途だけ**に使う。照合は raw_bytes で行う。
    code: str | None = None
    raw_bytes: tuple[int, int, int, int] | None = None

    @property
    def key(self) -> tuple[str, tuple[int, int, int, int]] | None:
        """learned照合・デバウンスの共通キー。表記ではなくバイトで持つ（本モジュール冒頭）。"""
        if self.protocol is None or self.raw_bytes is None:
            return None
        return (self.protocol, self.raw_bytes)


def _fmt(protocol: str, scancode: int, width: int) -> str:
    return f"{protocol}:0x{scancode:0{width}x}"


def bytes_to_code(b0: int, b1: int, b2: int, b3: int) -> tuple[str, str]:
    """送出順の4バイトから (プロトコル名, 表記) を組む。"""
    if b0 ^ b1 == 0xFF and b2 ^ b3 == 0xFF:
        return "nec", _fmt("nec", (b0 << 8) | b2, 4)
    if b2 ^ b3 == 0xFF:
        return "necx", _fmt("necx", (b0 << 16) | (b1 << 8) | b2, 6)
    # ir-ctl 並び（M2-Bで実機確定。docstring参照）。
    return "nec32", _fmt("nec32", (b1 << 24) | (b0 << 16) | (b3 << 8) | b2, 8)


def code_to_bytes(code: str) -> tuple[str, tuple[int, int, int, int]] | None:
    """表記文字列を (プロトコル名, 送出順4バイト) へ戻す。読めなければ None。

    `bytes_to_code()` の逆。設定ファイル（learned.json）に書かれた文字列を、
    照合に使うバイトへ正規化するために要る。**表記の揺れはここで吸収して、
    以降はバイトだけで扱う**（照合系を1本に保つ）。
    nec / necx は反転バイトが表記に現れないので、ここで復元する。
    """
    proto, sep, hexpart = code.strip().partition(":")
    if not sep:
        return None
    proto = proto.strip().lower()
    try:
        value = int(hexpart.strip(), 16)
    except ValueError:
        return None
    if value < 0:
        return None
    if proto == "nec" and value <= 0xFFFF:
        b0, b2 = (value >> 8) & 0xFF, value & 0xFF
        return "nec", (b0, b0 ^ 0xFF, b2, b2 ^ 0xFF)
    if proto == "necx" and value <= 0xFFFFFF:
        b0, b1, b2 = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
        return "necx", (b0, b1, b2, b2 ^ 0xFF)
    if proto == "nec32" and value <= 0xFFFFFFFF:
        b1, b0 = (value >> 24) & 0xFF, (value >> 16) & 0xFF
        b3, b2 = (value >> 8) & 0xFF, value & 0xFF
        return "nec32", (b0, b1, b2, b3)
    return None


class NecDecoder:
    """MODE2イベントを1件ずつ食わせ、フレームが揃ったら NecFrame を返す状態機械。

    feed() は「フレームが確定した時だけ」NecFrame を返し、それ以外は None を返す。
    32bit揃った時点で確定させる＝末尾のストップパルスや終端スペースを待たない
    （待つと最後の1フレームが次の受信まで出てこない）。
    """

    _IDLE, _LEADER, _BIT_PULSE, _BIT_SPACE = 0, 1, 2, 3

    def __init__(self):
        self._state = self._IDLE
        self._bits: list[int] = []
        self._pulse = 0

    def reset(self) -> None:
        self._state = self._IDLE
        self._bits = []
        self._pulse = 0

    def feed(self, kind: str, usec: int) -> NecFrame | None:
        # タイムアウト・オーバーフローはフレームの切れ目。状態を捨てる。
        if kind in ("timeout", "overflow"):
            self.reset()
            return None

        if self._state == self._IDLE:
            if kind == "pulse" and LEADER_PULSE_MIN <= usec <= LEADER_PULSE_MAX:
                self._state = self._LEADER
            return None

        if self._state == self._LEADER:
            if kind != "space":
                # リーダーパルスの直後がパルス＝ありえない。取りこぼしとみなして捨てる。
                return self._restart_if_leader(kind, usec)
            if LEADER_SPACE_DATA_MIN <= usec <= LEADER_SPACE_DATA_MAX:
                self._state, self._bits = self._BIT_PULSE, []
                return None
            if LEADER_SPACE_REPEAT_MIN <= usec <= LEADER_SPACE_REPEAT_MAX:
                self.reset()
                return NecFrame(repeat=True)
            self.reset()
            return None

        if self._state == self._BIT_PULSE:
            if kind != "pulse":
                return self._restart_if_leader(kind, usec)
            if usec >= LEADER_PULSE_MIN:
                # ビットパルス（約560us）の位置にリーダー長のパルスが来た＝前のフレームが
                # 途中で切れ、次のフレームが始まっている。ここを拾い直さないと、
                # 直後の正常なフレームを丸ごと落とす（自己テストで検出）。
                self.reset()
                self._state = self._LEADER
                return None
            self._pulse = usec
            self._state = self._BIT_SPACE
            return None

        # _BIT_SPACE: ここで pulse+space の合計から 0/1 を決める（本方式の核心）。
        if kind != "space":
            return self._restart_if_leader(kind, usec)
        total = self._pulse + usec
        if not (PAIR_SUM_MIN <= total <= PAIR_SUM_MAX):
            self.reset()
            return None
        self._bits.append(1 if total >= PAIR_SUM_THRESHOLD else 0)
        self._state = self._BIT_PULSE
        if len(self._bits) < NEC_BITS:
            return None

        bits = self._bits
        self.reset()
        # 8bitずつ・各バイトはLSB先行（NECの送出順）で畳む。
        b = tuple(sum(bits[i * 8 + n] << n for n in range(8)) for i in range(4))
        protocol, code = bytes_to_code(*b)
        return NecFrame(repeat=False, protocol=protocol, code=code, raw_bytes=b)

    def _restart_if_leader(self, kind: str, usec: int) -> None:
        """崩れたフレームの途中で新しいリーダーが来ていたら、そこから拾い直す。"""
        self.reset()
        if kind == "pulse" and LEADER_PULSE_MIN <= usec <= LEADER_PULSE_MAX:
            self._state = self._LEADER
        return None
