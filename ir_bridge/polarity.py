"""polarity: pulse/space のラベルが入れ替わって届いたフレームを救う（M4-a）。

■ ★これは受信経路の一部である（送信機能の付随物ではない）
M4-a（送信）の作業中に見つかったが、**送信とは無関係に起きる**。赤外線が約1.4秒以内に
続けて2発来れば通常運用でいつでも起きる。デバウンス（既定1000ms）は**コード単位**なので、
**異なるコードの連続は吸収しない**——シーンAを押して0.5秒後にシーンBを押すと、Bが反転して
落ちる。**送信を使わない構成でもこのモジュールは外せない。**
これまでの実機試験（IR-8 / IR-9 / IR-13）が通っていたのは、十分に間隔を空けた単発しか
撃っていなかったからで、症状が無かったのではなく**潜在していた**（DEV_LOG 2026-09-11〜12 M4-a）。

■ 何が起きるか（2026-09-11 実測・Irdroid / ir_toy ドライバ・kernel 6.18）
ir_toy は USB で届く16bit値に「pulse, space, pulse, …」と**交互に**ラベルを付ける。
交互の位相はデバイスに1つのフラグ（drivers/media/rc/ir_toy.c の `irtoy->pulse`）で持ち、
揃い直すのはデバイスが無信号 約1.4秒（16bitカウンタ × 21.33us の桁あふれ）で送ってくる
0xffff を受けたときだけである。**その 0xffff が来る前に次の赤外線が始まると**、余分な値が
1つ挟まってラベルが1つずれ、以後のイベントの pulse/space が入れ替わる。
    実測（送信なし・2発の間隔）: 0.3〜1.0秒で2発目が化ける／1.3秒以上では15回中0回
送信（`irtoy_tx`）もこのフラグを初期化しないので、受信直後の送信も同じ窓に入りうる。

中身は無傷で、ラベルを入れ替えれば同じコードに復号できる（実測フレームで確認。
tools/tx_selftest.py に固定データとして入れてある）。

■ ★次に読む人へ: nec.py の判定窓を疑わないこと
「Irdroid で続けて押すと2発目が取れない」はデコーダのバグに見えるが、**窓の問題ではない**。
ペア合計は正しい値のまま届いていて、pulse と space の**名札**だけが入れ替わっている。
CLAUDE.md §15 の判定窓（実測から導いた閾値）はここでは一切動かさない。変えるのはペアの
組み方だけ——ペア合計方式が AGC の配分ずれに耐えるのと同じ方向の頑健さを1つ足す話であって、
窓を緩める話ではない。

■ 並走ではなくフォールバック
通常の復号を常に先に行う（遅延も結果も従来と同じ）。**1つのバースト（区切りから区切りまで）
で通常の復号が何も出さなかったときだけ**、同じバーストをラベルを入れ替えて1回だけ流し直す。
並走させると誤検出の面が2倍になり、通常復号で取れたフレームを反転側が別の値に読む余地も残る。

区切りは timeout / overflow イベント、または長さ BURST_GAP_US 以上のイベント（**ラベルは問わない**
——反転中は長い無信号が pulse として届く）。NEC のフレーム内部に 11ms を超える区間は無いので、
フレームの途中で切ることはない。救えたフレームはバーストの終わり（Irdroid なら受信タイムアウト
約40ms後）に出るので、その分だけ遅れる。

★救えないもの: 値そのものが欠けたフレーム（実測: 0.6秒間隔の2発目で pulse/space が1組欠け、
31ビットで終わる例がある）。反転ではないので入れ替えても復号できない。
"""
from collections import deque

from .nec import NecDecoder, NecFrame

# これ以上長いイベントはバーストの区切りとみなす。NEC リーダーの窓上限（11000us）より長く、
# GPIO版の終端スペース（実測 20730us）より短い。
BURST_GAP_US = 15000
# 区切りが来ないまま（連続ノイズ等）イベントが溜まり続けないための上限。NEC 1フレームは約68件。
BURST_MAX_EVENTS = 256

_SWAP = {"pulse": "space", "space": "pulse"}


class PolarityFallbackDecoder:
    """NecDecoder を包み、ラベル反転のフレームを後から救う。

    feed() は (NecFrame, recovered) のリストを返す。recovered=True はラベルを入れ替えて
    復号できたもの（通常の復号では取れなかったもの）。
    """

    def __init__(self):
        self._decoder = NecDecoder()
        self._burst: deque = deque(maxlen=BURST_MAX_EVENTS)
        # このバーストで通常の復号が何か（データでもリピートでも）出したか。
        self._decoded = False

    def reset(self) -> None:
        self._decoder.reset()
        self._burst.clear()
        self._decoded = False

    def feed(self, kind: str, usec: int) -> list[tuple[NecFrame, bool]]:
        out = []
        frame = self._decoder.feed(kind, usec)  # 通常の復号が常に先（従来と同じ）
        if frame is not None:
            self._decoded = True
            out.append((frame, False))
        if kind in ("timeout", "overflow") or usec >= BURST_GAP_US:
            out.extend(self._flush())
        else:
            self._burst.append((kind, usec))
        return out

    def _flush(self) -> list[tuple[NecFrame, bool]]:
        burst, decoded = list(self._burst), self._decoded
        self._burst.clear()
        self._decoded = False
        if decoded or not burst:
            return []
        swapped = NecDecoder()
        frames = (swapped.feed(_SWAP.get(k, k), u) for k, u in burst)
        return [(f, True) for f in frames if f is not None]
