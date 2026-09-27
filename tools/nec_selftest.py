#!/usr/bin/env python3
"""nec_selftest: NECデコーダの自己テスト（実機・外部依存なしで走る）。

人間が実機で採った生波形の実測値をそのまま埋め込んである。**このファイルは
「なぜカーネルのNECデコーダを使わないか」の証拠でもある**: 下の PULSES/SPACE0 は
カーネルの許容窓（NEC_UNIT=562.5us ± 281us → パルス 281〜844us・'0'スペース同）を
外れる値を含み、それでも pulse+space のペア合計で判定すれば正しく復号できることを示す。

    python3 tools/nec_selftest.py     # 全件PASSなら rc=0
"""
import sys
from itertools import cycle
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/

import isolation  # noqa: E402  ← ir_bridge より先に（置き場所ごと一時ディレクトリへ逃がす・前後で本物を見張る）

from ir_bridge.nec import (  # noqa: E402
    LEADER_PULSE_MAX,
    LEADER_PULSE_MIN,
    LEADER_SPACE_DATA_MAX,
    LEADER_SPACE_DATA_MIN,
    PAIR_SUM_MAX,
    PAIR_SUM_MIN,
    PAIR_SUM_THRESHOLD,
    NecDecoder,
    code_to_bytes,
)

# --- 実測値（SwitchBotから nec32:0x7d2e5b91 を撃った生波形より） --------------
LEADER = (9228, 4281)
TERMINATOR_SPACE = 20730
# パルス 774〜898（窓上限844を超える値を含む）
PULSES = [774, 898, 810, 864, 811, 804]
# '0'スペース 271〜380（窓下限281を下回る値を含む）
SPACE0 = [271, 380, 298, 329, 350, 310]
# '1'スペース 1415〜1484
SPACE1 = [1415, 1484, 1453, 1459, 1430, 1470]
# 人間が貼った実測ペア（この並びは 0,0,1,1 と読める。先頭4ビットそのものではない＝M2-B）
MEASURED_HEAD = [(864, 298), (810, 329), (811, 1453), (804, 1459)]

KERNEL_UNIT, KERNEL_TOL = 562.5, 281.0  # ir-nec-decoder.c の許容窓


class Waveform(NamedTuple):
    """1つの受光素子から実際に出てくる波形の癖。テストはこれを差し替えて同じ判定を回す。"""

    name: str
    leader: tuple[int, int]
    # 終端は (種別, usec)。GPIO版は長いスペース、Irdroidは受信タイムアウト＝種別が違う。
    terminator: tuple[str, int]
    pulses: list
    space0: list
    space1: list


GPIO = Waveform(
    name="GPIO(VS1838B)",
    leader=LEADER,
    terminator=("space", TERMINATOR_SPACE),
    pulses=PULSES,
    space0=SPACE0,
    space1=SPACE1,
)

# --- Irdroid（USB・ir_toy ドライバ）の実測値 ---------------------------------
# VS1838B と違い AGC の偏りが小さく、理想値(562/562/1687)に近い。**2つの光源の実測を
# 合わせた範囲**を採る（片方だけだと、もう片方で窓を外れても気づけない）:
#
#   (a) 市販リモコン → Irdroid（人間が実測）
#       パルス 462〜588 / '0'スペース 525〜609 / '1'スペース 1575〜1659
#   (b) この機体のGPIO送信(lirc0) → Irdroid（M2-Dのループバックで実測）
#       パルス 588〜609 / '0'スペース 462〜483 / '1'スペース 1554〜1575
#       リーダー 8757〜8799 / 4305・ペア合計 '0'=1050〜1071 / '1'=2163
#
# (b) で分かるのは、Irdroidにも「パルスが伸び、スペースが縮む」偏りは残る（幅が小さいだけ）
# ということ。ペア合計方式はどちらの光源でも同じコードを返す＝方式を変える理由にはならない。
#
# **すべて 21us の倍数**であることに注意（ir-ctl -f が報告する Resolution 21 microseconds
# ＝ ir_toy は 21.3us 刻みでサンプリングする）。(a)(b) とも実測値が倍数になっていた。
IRDROID_RESOLUTION = 21
# 終端は「受信タイムアウト」として届く（GPIO版の長いスペースと種別が違う）。ir_toy の
# 受信タイムアウトは下限40000us・既定40000us（GPIO版は下限1us・既定125000us）。
# ★実測の timeout 値は 44287〜47196us で、40000us ちょうどでも21usの倍数でもなかった
#   （driver が実際の空き時間を載せてくる）。値に意味を持たせず、種別だけで捨てること。
IRDROID_RX_TIMEOUT = 44287
IRDROID = Waveform(
    name="Irdroid(ir_toy)",
    # (b)の実測。9000/4500 の公称より短いが、リーダー窓には十分入る。
    leader=(8757, 4305),
    terminator=("timeout", IRDROID_RX_TIMEOUT),
    # (a)と(b)の和集合。最悪の組み合わせ（最小パルス＋最小スペース等）は下で明示的に試す。
    pulses=[462, 483, 504, 546, 567, 588, 609],
    space0=[462, 483, 525, 546, 567, 588, 609],
    space1=[1554, 1575, 1596, 1617, 1638, 1659],
)
# リーダーの実測レンジ（(a)公称寄り 〜 (b)実測）。両端が窓に入ることを試験する。
IRDROID_LEADER_RANGE = ((8757, 9009), (4305, 4494))

# 送出順の4バイト → (プロトコル, 表記)。受光素子を替えても結果が変わらないことの基準表でもある。
PROTOCOL_CASES = [
    # M2-Bの実機確定: ir-ctl -S nec32:0x7d2e5b91 の送出順は 2e 7d 91 5b。
    ((0x2E, 0x7D, 0x91, 0x5B), "nec32", "nec32:0x7d2e5b91"),
    # 以下は実機で受信を確認済み（nec:0x8877 は送受で表記一致・他はテレビリモコン）。
    ((0x88, 0x77, 0x77, 0x88), "nec", "nec:0x8877"),
    ((0x40, 0xBF, 0x05, 0xFA), "nec", "nec:0x4005"),
    ((0x40, 0xBF, 0x08, 0xF7), "nec", "nec:0x4008"),
    ((0x40, 0xBF, 0x1E, 0xE1), "nec", "nec:0x401e"),
    ((0x00, 0xFF, 0x15, 0xEA), "nec", "nec:0x0015"),       # 反転が両方成立＝通常NEC
    ((0x86, 0x6B, 0x15, 0xEA), "necx", "necx:0x866b15"),   # アドレス16bit＝拡張NEC
    ((0xFF, 0xFF, 0xFF, 0xFF), "nec32", "nec32:0xffffffff"),
]

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


def build_frame(byte4, head_pairs=(), wave: Waveform = GPIO) -> list:
    """4バイト（送出順）から MODE2 イベント列を組む。head_pairs は先頭を実測値で置換する。"""
    events = [("pulse", wave.leader[0]), ("space", wave.leader[1])]
    bits = [(byte4[i] >> n) & 1 for i in range(4) for n in range(8)]
    p_it, s0_it, s1_it = cycle(wave.pulses), cycle(wave.space0), cycle(wave.space1)
    for i, bit in enumerate(bits):
        if i < len(head_pairs):
            pulse, space = head_pairs[i]
        else:
            pulse = next(p_it)
            space = next(s1_it) if bit else next(s0_it)
        events.append(("pulse", pulse))
        events.append(("space", space))
    events.append(("pulse", wave.pulses[0]))  # ストップパルス
    events.append(wave.terminator)
    return events


def decode(events):
    """イベント列を流し、確定したフレームを全部返す。"""
    dec = NecDecoder()
    return [f for f in (dec.feed(k, v) for k, v in events) if f is not None]


def main() -> int:
    print("■ 前提の確認（カーネル窓を外れる実測値が含まれること）")
    over = [p for p in PULSES if p > KERNEL_UNIT + KERNEL_TOL]
    under = [s for s in SPACE0 if s < KERNEL_UNIT - KERNEL_TOL]
    check("パルスに窓上限844usを超える値がある", bool(over), f"{over}")
    check("'0'スペースに窓下限281usを下回る値がある", bool(under), f"{under}")
    print("  → カーネルのNECデコーダが落ちる理由。ペア合計方式はこれを吸収する")

    print("■ 実測ペアのビット判定（人間が貼った生値そのまま）")
    # 先頭4ペアを実測値で置き換え、残り28ビットは0で埋める。
    # 実測ペアは 1162 / 1139 / 2264 / 2263 us ＝ 0,0,1,1 と読めるはず（byte0=0b00001100=0x0c）。
    frames = decode(build_frame((0x0C, 0x00, 0x00, 0x00), head_pairs=MEASURED_HEAD))
    ok = len(frames) == 1 and frames[0].raw_bytes is not None
    check("フレームが1つ確定する", ok, f"{len(frames)}件")
    if ok:
        b0 = frames[0].raw_bytes[0]
        check("実測4ペアが 0,0,1,1 と読める", b0 & 0x0F == 0x0C, f"byte0=0x{b0:02x}")
        check("残り28ビットは0のまま", frames[0].raw_bytes == (0x0C, 0, 0, 0), str(frames[0].raw_bytes))
    print(
        "  注: この4ペアは nec32:0x7d2e5b91 の先頭4ビットそのものではない（M2-Bで確定した\n"
        "      送出順は 2e 7d 91 5b で、先頭バイト0x2eの下位4ビットは 0,1,1,1）。\n"
        "      貼られた4ペアは '0'と'1'の例示だったと解釈できる。ここで試験しているのは\n"
        "      「その実測値がどちらのビットと読めるか」だけで、その点は有効。"
    )

    print("■ プロトコル判定（送出順の4バイト → 表記。ir-ctl / ir-keytable と同じ）")
    for byte4, proto, code in PROTOCOL_CASES:
        got = decode(build_frame(byte4))
        ok = len(got) == 1 and got[0].protocol == proto and got[0].code == code
        check(f"{bytes(byte4).hex()} → {code}", ok, "" if ok else f"got={got}")

    print("■ 表記 → バイトの逆変換（設定ファイルの文字列を照合用に正規化する経路）")
    # M3-b で照合をバイトに統一したので、旧表記（送出順そのまま）の後方互換は削除した。
    # 代わりに要るのが「文字列 → バイト」で、bytes_to_code と往復すること自体を試験する。
    for byte4, proto, code in PROTOCOL_CASES:
        got = code_to_bytes(code)
        ok = got == (proto, byte4)
        check(f"{code} → {bytes(byte4).hex()}", ok, "" if ok else f"got={got}")
    check("フレームの key はバイトで持つ", decode(build_frame((0x2E, 0x7D, 0x91, 0x5B)))[0].key
          == ("nec32", (0x2E, 0x7D, 0x91, 0x5B)))
    print("■ 読めない表記は None（learned の1行が壊れても全体を巻き添えにしないため）")
    for bad in ["", "nec32", "0x7d2e5b91", "sony:0x1234", "nec:0xzz", "nec:0x1ffff", "nec32:0x1ffffffff"]:
        check(f"{bad!r} → None", code_to_bytes(bad) is None, str(code_to_bytes(bad)))

    print("■ リピートフレーム（約9000/2250）")
    got = decode([("pulse", 9100), ("space", 2210), ("pulse", 800), ("space", TERMINATOR_SPACE)])
    check("リピートとして1件出る", len(got) == 1 and got[0].repeat, str(got))
    check("コードを持たない", bool(got) and got[0].code is None)

    print("■ ノイズ・欠落で誤検出しないこと")
    check("短いパルス列だけでは何も出ない", not decode([("pulse", 300), ("space", 400)] * 40))
    check("リーダーだけでは何も出ない", not decode([("pulse", 9228), ("space", 4281)]))
    truncated = build_frame((0x11, 0x22, 0x33, 0x44))[:20]  # 途中で切れたフレーム
    check("途中で切れたフレームは確定しない", not decode(truncated))
    check(
        "壊れたフレームの直後でも次を拾える（再同期）",
        [f.code for f in decode(truncated + build_frame((0x2E, 0x7D, 0x91, 0x5B)))] == ["nec32:0x7d2e5b91"],
    )
    check(
        "タイムアウトを挟んでも次を拾える",
        [f.code for f in decode([("timeout", 100000)] + build_frame((0x00, 0xFF, 0x15, 0xEA)))] == ["nec:0x0015"],
    )

    print("■ ペア合計の境界")
    # 合計 1169us（898+271）は '0'、2189us（774+1415）は '1'。どちらも実測の外れ値の組み合わせ。
    worst0 = decode(build_frame((0x00, 0xFF, 0x00, 0xFF), head_pairs=[(898, 271)] * 32))
    check("最悪配分でも全ビット0と読める", len(worst0) == 1 and worst0[0].raw_bytes == (0, 0, 0, 0), str(worst0))
    worst1 = decode(build_frame((0x00, 0xFF, 0x00, 0xFF), head_pairs=[(774, 1415)] * 32))
    check(
        "最悪配分でも全ビット1と読める",
        len(worst1) == 1 and worst1[0].raw_bytes == (0xFF, 0xFF, 0xFF, 0xFF),
        str(worst1),
    )

    print("■ Irdroid（USB・ir_toy）の実測波形 — 分解能21us / 受信タイムアウト40000us")
    # 前提: ir_toy は 21us 刻みでサンプリングする。ここで使う値がその倍数でなければ、
    # 「実機から出てくる値」を試験していないことになる。
    quantized = [
        v
        for v in list(IRDROID.pulses) + list(IRDROID.space0) + list(IRDROID.space1) + list(IRDROID.leader)
        # 終端の timeout は driver が実時間を載せるので倍数にならない＝ここでは見ない
        if v % IRDROID_RESOLUTION
    ]
    check(f"試験値がすべて{IRDROID_RESOLUTION}usの倍数", not quantized, f"倍数でない値={quantized}")
    (lp_lo, lp_hi), (ls_lo, ls_hi) = IRDROID_LEADER_RANGE
    check(
        "リーダーの実測レンジが両端とも判定窓に入る",
        LEADER_PULSE_MIN <= lp_lo and lp_hi <= LEADER_PULSE_MAX
        and LEADER_SPACE_DATA_MIN <= ls_lo and ls_hi <= LEADER_SPACE_DATA_MAX,
        f"pulse {lp_lo}〜{lp_hi}us / 窓{LEADER_PULSE_MIN}〜{LEADER_PULSE_MAX}us、"
        f"space {ls_lo}〜{ls_hi}us / 窓{LEADER_SPACE_DATA_MIN}〜{LEADER_SPACE_DATA_MAX}us",
    )
    check(
        "終端の timeout 値は分解能の倍数でない（＝値を当てにしない）",
        IRDROID_RX_TIMEOUT % IRDROID_RESOLUTION != 0,
        f"実測 {IRDROID_RX_TIMEOUT}us。デコーダは種別だけ見て状態を捨てる",
    )

    # ★本命: 受光素子を替えても対応表（data/ir_map.json）が書き換えにならないこと。
    # GPIO版とIrdroidで同じリモコンから違うコード文字列が出たら、顧客の設定が壊れる。
    for byte4, proto, code in PROTOCOL_CASES:
        got = decode(build_frame(byte4, wave=IRDROID))
        ok = len(got) == 1 and got[0].protocol == proto and got[0].code == code
        check(f"{bytes(byte4).hex()} → {code}（GPIO版と同一）", ok, "" if ok else f"got={got}")

    # ペア合計の余裕。量子化で1目盛(21us)ずれても判定が変わらないことを、数値で押さえる。
    sums0 = [p + s for p in IRDROID.pulses for s in IRDROID.space0]
    sums1 = [p + s for p in IRDROID.pulses for s in IRDROID.space1]
    check(
        "'0'のペア合計がしきい値より下（余裕 > 分解能）",
        max(sums0) < PAIR_SUM_THRESHOLD - IRDROID_RESOLUTION,
        f"最大{max(sums0)}us / しきい値{PAIR_SUM_THRESHOLD}us（余裕{PAIR_SUM_THRESHOLD - max(sums0)}us）",
    )
    check(
        "'1'のペア合計がしきい値より上（余裕 > 分解能）",
        min(sums1) > PAIR_SUM_THRESHOLD + IRDROID_RESOLUTION,
        f"最小{min(sums1)}us / しきい値{PAIR_SUM_THRESHOLD}us（余裕{min(sums1) - PAIR_SUM_THRESHOLD}us）",
    )
    check(
        "ペア合計が上下限の内側",
        PAIR_SUM_MIN <= min(sums0) and max(sums1) <= PAIR_SUM_MAX,
        f"{min(sums0)}〜{max(sums1)}us / 窓{PAIR_SUM_MIN}〜{PAIR_SUM_MAX}us",
    )
    worst = decode(
        build_frame((0, 0, 0, 0), head_pairs=[(min(IRDROID.pulses), min(IRDROID.space0))] * 32, wave=IRDROID)
    )
    check("最悪配分でも全ビット0と読める", len(worst) == 1 and worst[0].raw_bytes == (0, 0, 0, 0), str(worst))
    worst = decode(
        build_frame((0, 0, 0, 0), head_pairs=[(max(IRDROID.pulses), max(IRDROID.space1))] * 32, wave=IRDROID)
    )
    check(
        "最悪配分でも全ビット1と読める",
        len(worst) == 1 and worst[0].raw_bytes == (0xFF, 0xFF, 0xFF, 0xFF),
        str(worst),
    )

    print("■ 終端の扱い（Irdroidは長いスペースではなく timeout で終端が届く）")
    # 受信タイムアウトが 40000us（GPIO版の既定125000usより短く、下限も40000usで下げられない）
    # ことがデコーダに影響しないのは、**32bit揃った時点で確定して終端を待たない**ため。
    # ここでは「確定がどのイベントで起きたか」を位置で押さえる（終端を待っていたら位置がずれる）。
    events = build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID)
    dec = NecDecoder()
    fired_at = [i for i, (k, v) in enumerate(events) if dec.feed(k, v) is not None]
    last_bit_space = 2 + 64 - 1  # リーダー2件 + 32ビット×(pulse,space)の最後
    check(
        "32bit目のスペースで確定する（終端のtimeoutを待たない）",
        fired_at == [last_bit_space],
        f"確定位置={fired_at} 期待={[last_bit_space]}（全{len(events)}件・末尾2件が終端）",
    )
    check(
        "終端が timeout でもフレーム数は変わらない",
        len(decode(events)) == 1 and len(decode(build_frame((0x40, 0xBF, 0x08, 0xF7)))) == 1,
    )

    print("■ リピート列（フレーム間 42.5ms > 受信タイムアウト40ms ＝ timeout が挟まる）")
    # NECは110ms周期。データフレーム約67.5msの後、次のリピートまで約42.5ms空く。
    # Irdroidの受信タイムアウト(40ms)はこれより短いので、GPIO版なら「長いスペース」で
    # 届く隙間が timeout として届く。どちらでも拾えることを見る。
    repeat_frame = [
        ("pulse", IRDROID.leader[0]),
        ("space", 2247),  # 2250us を21usへ丸めた値（21*107）
        ("pulse", IRDROID.pulses[0]),
        ("timeout", IRDROID_RX_TIMEOUT),
    ]
    got = decode(build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID) + repeat_frame * 3)
    check(
        "データ1件＋リピート3件が出る",
        [f.repeat for f in got] == [False, True, True, True],
        f"{[(f.repeat, f.code) for f in got]}",
    )
    check("先頭のデータフレームのコードは変わらない", bool(got) and got[0].code == "nec:0x4008")
    # 押しっぱなしを離して押し直した想定＝データフレームの連投。
    two = decode(
        build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID)
        + build_frame((0x40, 0xBF, 0x1E, 0xE1), wave=IRDROID)
    )
    check(
        "timeoutを挟んだ連続フレームを両方拾える",
        [f.code for f in two] == ["nec:0x4008", "nec:0x401e"],
        str([f.code for f in two]),
    )

    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(isolation.run(main))
