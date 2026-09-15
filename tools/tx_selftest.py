#!/usr/bin/env python3
"""tx_selftest: 送信（sender.py）とラベル反転のフォールバック（polarity.py）の自己テスト。

実機・本体API・ir-ctl なしで走る。ir-ctl は呼び出しを記録するだけの偽物に、
本体クライアントも偽物に差し替える（HTTPも赤外線も出ない）。

    python3 tools/tx_selftest.py    # 全件PASSなら rc=0

■ 固定データの出どころ（2026-09-11 実測・M4-a）
開発機の GPIO TX（/dev/lirc0）から撃ったフレームを Irdroid（/dev/lirc2・ir_toy）で
受けた MODE2 イベント列そのまま。撃ったのはプリセット2のコード（本番サービスが本体APIを叩かない）。
  INVERTED_0270  0.3秒間隔の2発目。pulse/space のラベルが入れ替わって届いた実物
  NORMAL_0181    同じ条件で正常に届いた実物（対照）
  MISSING_0081   0.6秒間隔の2発目。pulse/space が1組欠けて31ビットで終わった実物
                 （反転ではない＝フォールバックでも救えない。救えないことを試験する）
"""
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from ir_bridge import codes, config  # noqa: E402
from ir_bridge.codes import LightTarget, SceneTarget  # noqa: E402
from ir_bridge.lirc import LircDevice  # noqa: E402
from ir_bridge.nec import NecDecoder, code_to_bytes  # noqa: E402
from ir_bridge.polarity import BURST_MAX_EVENTS, PolarityFallbackDecoder  # noqa: E402
from nec_selftest import GPIO, IRDROID, PROTOCOL_CASES, build_frame  # noqa: E402

# --- 実測の固定データ ---------------------------------------------------------
INVERTED_0270 = [
    ('space', 218925), ('pulse', 39837), ('space', 8757), ('pulse', 4305), ('space', 588),
    ('pulse', 483), ('space', 588), ('pulse', 1575), ('space', 609), ('pulse', 462), ('space',
    588), ('pulse', 1575), ('space', 588), ('pulse', 483), ('space', 588), ('pulse', 1575),
    ('space', 588), ('pulse', 1575), ('space', 588), ('pulse', 483), ('space', 588), ('pulse',
    1575), ('space', 588), ('pulse', 1575), ('space', 609), ('pulse', 441), ('space', 588),
    ('pulse', 1575), ('space', 609), ('pulse', 462), ('space', 588), ('pulse', 483), ('space',
    588), ('pulse', 1575), ('space', 588), ('pulse', 483), ('space', 609), ('pulse', 441),
    ('space', 588), ('pulse', 462), ('space', 588), ('pulse', 483), ('space', 588), ('pulse',
    483), ('space', 588), ('pulse', 1575), ('space', 588), ('pulse', 1575), ('space', 609),
    ('pulse', 1554), ('space', 588), ('pulse', 483), ('space', 588), ('pulse', 483), ('space',
    588), ('pulse', 1575), ('space', 588), ('pulse', 483), ('space', 588), ('pulse', 462),
    ('space', 588), ('pulse', 462), ('space', 588), ('pulse', 483), ('space', 588), ('pulse',
    483), ('space', 588), ('pulse', 483), ('space', 588), ('timeout', 48111),
]

NORMAL_0181 = [
    ('space', 516144), ('space', 41748), ('pulse', 8757), ('space', 4305), ('pulse', 588),
    ('space', 483), ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse',
    588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 1575),
    ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    1575), ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588),
    ('space', 1575), ('pulse', 609), ('space', 441), ('pulse', 588), ('space', 483), ('pulse',
    588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 1575),
    ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588),
    ('space', 483), ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 1575), ('pulse',
    588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483),
    ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    483), ('pulse', 588), ('space', 483), ('pulse', 588), ('timeout', 48117),
]

MISSING_0081 = [
    ('space', 541361), ('space', 41979), ('pulse', 8757), ('space', 4305), ('pulse', 588),
    ('space', 483), ('pulse', 609), ('space', 1554), ('pulse', 588), ('space', 462), ('pulse',
    588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 1575),
    ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    1575), ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588),
    ('space', 1575), ('pulse', 609), ('space', 441), ('pulse', 588), ('space', 483), ('pulse',
    588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 1575),
    ('pulse', 609), ('space', 462), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588),
    ('space', 483), ('pulse', 588), ('space', 1575), ('pulse', 588), ('space', 483), ('pulse',
    588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483),
    ('pulse', 588), ('space', 483), ('pulse', 588), ('space', 483), ('pulse', 588), ('space',
    462), ('pulse', 588), ('timeout', 47347),
]

IRDROID_DEV = LircDevice("/dev/lirc2", 0x30040102, "ir_toy", "Infrared Toy")
GPIO_RX_DEV = LircDevice("/dev/lirc1", 0x10040000, "gpio_ir_recv", "gpio_ir_recv")

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


class _Capture(logging.Handler):
    """ログ文面そのものを試験する（顧客と当方が読む唯一の手がかりなので）。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def __enter__(self):
        root = logging.getLogger("ir_bridge")
        self._saved = root.level
        root.setLevel(logging.INFO)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger("ir_bridge")
        root.removeHandler(self)
        root.setLevel(self._saved)

    def of(self, level):
        return [m for lv, m in self.records if lv == level]


def run_fallback(events):
    """フォールバック付きデコーダへ流し、(表記 or 'repeat', recovered) を全部返す。"""
    dec = PolarityFallbackDecoder()
    out = []
    for k, u in events:
        out.extend(("repeat" if f.repeat else f.code, rec) for f, rec in dec.feed(k, u))
    return out


def run_plain(events):
    dec = NecDecoder()
    return [f for f in (dec.feed(k, u) for k, u in events) if f is not None]


def invert(events):
    """正常な列を「ラベルが1つずれた列」にする（実測の反転と同じ形: 余分な長い値が1つ挟まる）。"""
    swap = {"pulse": "space", "space": "pulse"}
    body = [(swap.get(k, k), u) for k, u in events if k != "timeout"]
    return [("space", 218925), ("pulse", 39837)] + body + [("timeout", 48111)]


def polarity_section() -> None:
    print("■ 実測の反転フレーム（ir_toy の既知の欠陥）")
    check("通常の復号では取れない（＝これまで黙って落ちていた）", run_plain(INVERTED_0270) == [])
    got = run_fallback(INVERTED_0270)
    check(
        "フォールバックで元のコードに復号できる（ビット列は無傷）",
        got == [("nec32:0x4b6a0270", True)],
        str(got),
    )
    # ★確定はバーストの終わり（timeout）。途中で出たら区切りの規則が変わっている。
    dec = PolarityFallbackDecoder()
    at = [i for i, (k, u) in enumerate(INVERTED_0270) if dec.feed(k, u)]
    check("救えたフレームはバーストの終わり（timeout）で出る", at == [len(INVERTED_0270) - 1], f"位置={at}")

    print("■ 正常なフレームは従来と同じ（フォールバックは発火しない）")
    got = run_fallback(NORMAL_0181)
    check("実測の正常フレーム → 通常の復号のみ", got == [("nec32:0x4b6a0181", False)], str(got))
    same = True
    for wave in (GPIO, IRDROID):
        for byte4, _proto, code in PROTOCOL_CASES:
            events = build_frame(byte4, wave=wave)
            plain = [(f.code, False) for f in run_plain(events)]
            if run_fallback(events) != plain or plain != [(code, False)]:
                same = False
                print(f"    差分: {wave.name} {code} plain={plain} fallback={run_fallback(events)}")
    check("GPIO・Irdroid × 既存の全プロトコル例で、素のデコーダと出力が完全一致", same)
    repeat = [("pulse", 8757), ("space", 2247), ("pulse", 462), ("timeout", 44287)]
    got = run_fallback(build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID) + repeat * 3)
    check(
        "データ＋リピート3件が従来どおり（反転側が二重に出さない）",
        got == [("nec:0x4008", False)] + [("repeat", False)] * 3,
        str(got),
    )

    print("■ 救えないものを救ったことにしない")
    check("1組欠けた実測フレームは素のデコーダでも取れない", run_plain(MISSING_0081) == [])
    check("入れ替えても取れない（別の値をでっち上げない）", run_fallback(MISSING_0081) == [],
          str(run_fallback(MISSING_0081)))
    noise = [("pulse", 300 + (i * 37) % 900) if i % 2 == 0 else ("space", 250 + (i * 53) % 2600) for i in range(400)]
    check("ノイズの列から何も出ない", run_fallback(noise + [("timeout", 45000)]) == [])
    dec = PolarityFallbackDecoder()
    for k, u in noise * 3:
        dec.feed(k, u)
    check(f"区切りが来ない列でも溜め込みは上限({BURST_MAX_EVENTS}件)で止まる", len(dec._burst) <= BURST_MAX_EVENTS,
          f"{len(dec._burst)}件")

    print("■ 反転の後に通常の受信が復帰する")
    seq = NORMAL_0181 + INVERTED_0270 + NORMAL_0181
    got = run_fallback(seq)
    check(
        "正常 → 反転 → 正常 の3つとも取れ、救ったのは2つ目だけ",
        got == [("nec32:0x4b6a0181", False), ("nec32:0x4b6a0270", True), ("nec32:0x4b6a0181", False)],
        str(got),
    )
    # 合成の反転（GPIO版の波形でも同じ形なら救える＝受光素子を問わない）
    synth = invert(build_frame((0x2E, 0x7D, 0x32, 0x03), wave=GPIO))
    got = run_fallback(synth)
    check("GPIO版の波形を同じ形で反転させても救える", got == [("nec32:0x7d2e0332", True)], str(got))


# --- 受信スレッド（実デバイスなし・_feed を直接叩く） --------------------------
class FakeClient:
    def __init__(self):
        self.calls = []

    def fire_scene(self, key):
        self.calls.append(("scene", key))

    def set_brightness(self, instance, value):
        self.calls.append(("light", instance, value))


def build_receiver(base=codes.DEFAULT_BASE):
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver
    from ir_bridge.recent import RecentCodes

    client = FakeClient()
    firer = CommandFirer(client)
    store = config.ConfigStore(config.Config(base, 0, {}, {}))
    rx = IrReceiver(store, RecentCodes(), firer, rx_spec=None)
    return client, firer, rx, store


def drain(firer):
    """実行キューが捌けるのを待ってから止める（stop() は残件を捨てる仕様なので）。"""
    deadline = time.monotonic() + 3.0
    while not firer._q.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    firer.stop(timeout=3.0)


def receiver_section() -> None:
    print("■ 受信スレッド: 反転フレームも同じ経路で解釈・実行される")
    client, firer, rx, _ = build_receiver()
    firer.start()
    derived = build_frame(codes.encode_light(codes.DEFAULT_BASE, 3, 50), wave=IRDROID)
    with _Capture() as cap:
        check("起動直後の last_event_at は 0.0（送信側は待たない）", rx.last_event_at == 0.0)
        before = time.monotonic()
        for k, u in invert(derived):
            rx._feed(IRDROID_DEV, k, u)
        check("生イベントで last_event_at が進む", rx.last_event_at >= before)
        drain(firer)
    check("照明3 を 50% が実行される", client.calls == [("light", 3, 50)], str(client.calls))
    info = cap.of("INFO")
    expect = (
        "受信 nec32:0x7d2e0332 は pulse/space のラベルが入れ替わって届いたため、入れ替えて復号した"
        "（/dev/lirc2 ir_toy/Infrared Toy・ir_toy は約1.4秒以内の再受信でこうなる既知の欠陥）"
    )
    check("フォールバックの発火が INFO に1行出る（計器）", info.count(expect) == 1, str(info))
    check("続けて通常の受信ログが出る", "受信 nec32:0x7d2e0332 → 照明3 を 50% に設定（実行を要求）" in info, str(info))
    check("WARNINGは出ない（受信起因は INFO・§18）", cap.of("WARNING") == [], str(cap.of("WARNING")))

    print("■ 受信スレッド: 通常のフレームではフォールバックのログが出ない")
    client, firer, rx, _ = build_receiver()
    firer.start()
    with _Capture() as cap:
        for k, u in derived:
            rx._feed(IRDROID_DEV, k, u)
        drain(firer)
    check("実行される", client.calls == [("light", 3, 50)], str(client.calls))
    check("「入れ替えて復号」の行は無い", not [m for m in cap.of("INFO") if "入れ替えて" in m], str(cap.records))


# --- 送信 -----------------------------------------------------------------------
class FakeRun:
    """subprocess.run の偽物。argv を記録し、指定の結果を返す。"""

    def __init__(self, rc=0, stderr="", raise_=None, delay=0.0):
        self.calls = []
        self.rc, self.stderr, self.raise_, self.delay = rc, stderr, raise_, delay
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        with self._lock:
            self.calls.append((time.monotonic(), list(argv)))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.raise_ is not None:
                raise self.raise_
            time.sleep(self.delay)
            return subprocess.CompletedProcess(argv, self.rc, "", self.stderr)
        finally:
            with self._lock:
                self.active -= 1

    @property
    def argvs(self):
        return [a for _, a in self.calls]


class FakeReceiver:
    def __init__(self, last_event_at=0.0):
        self.last_event_at = last_event_at


class NoisyReceiver:
    """赤外線が浴びせられ続けている状態（最後の受信が常に「いま」）。"""

    @property
    def last_event_at(self):
        return time.monotonic()


def build_sender(receiver=None, run=None, device=IRDROID_DEV, base=codes.DEFAULT_BASE):
    from ir_bridge.sender import IrSender

    store = config.ConfigStore(config.Config(base, 0, {}, {}))
    run = run or FakeRun()
    sender = IrSender(store, receiver or FakeReceiver(), rx_spec=None, run=run, find_device=lambda spec: device)
    return sender, run, store


def sender_section() -> None:
    print("■ send(): 導出コードを正しい表記で ir-ctl へ渡す")
    cases = [
        (LightTarget(3, 50), "nec32:0x7d2e0332", "照明3 を 50% のコードを送信しました（nec32:0x7d2e0332）"),
        (LightTarget(10, 0), "nec32:0x7d2e0a00", "照明10 を 0% のコードを送信しました（nec32:0x7d2e0a00）"),
        (LightTarget(10, 100), "nec32:0x7d2e0a64", "照明10 を 100% のコードを送信しました（nec32:0x7d2e0a64）"),
        (SceneTarget("scene_15"), "nec32:0x7d2e000f", "シーン scene_15 のコードを送信しました（nec32:0x7d2e000f）"),
    ]
    for target, code, line in cases:
        sender, run, _ = build_sender()
        with _Capture() as cap:
            res = sender.send(target)
        check(f"{target.label} → {code}", run.argvs == [["ir-ctl", "-d", "/dev/lirc2", "-S", code]], str(run.argvs))
        check("  結果は成功・表記つき", res.ok and res.code == code and res.reason == "", str(res))
        check("  INFO がちょうど1行・指定の文面", cap.records == [("INFO", line)], str(cap.records))
        # 送ったものを受けたら同じ宛先に戻る＝送受で表記と解釈が食い違わない
        _p, raw = code_to_bytes(code)
        back = codes.decode(codes.DEFAULT_BASE, raw)
        check("  受信側の decode で同じ宛先に戻る", back is not None and back.target == target, str(back))

    sender, run, _ = build_sender(base=codes.PRESETS[1])
    sender.send(LightTarget(3, 50))
    check("base はその時点の設定から取る（プリセット2 → 0x4b6a）",
          run.argvs and run.argvs[0][-1] == "nec32:0x4b6a0332", str(run.argvs))

    print("■ 存在しない照明・シーン: ローカルで検証せず、そのまま送る（§17）")
    for target, code in ((LightTarget(99, 50), "nec32:0x7d2e6332"), (SceneTarget("scene_99"), "nec32:0x7d2e0063")):
        sender, run, _ = build_sender()
        with _Capture() as cap:
            res = sender.send(target)
        check(f"{target.label} は {code} として送られる（一覧を持たない）",
              res.ok and run.argvs == [["ir-ctl", "-d", "/dev/lirc2", "-S", code]], str(run.argvs))
        check("  WARNINGは出ない（404 が出るのは使われたとき・受信側）", cap.of("WARNING") == [])

    print("■ 表現できない宛先は ValueError（呼び側の誤り）で、ir-ctl を呼ばない")
    for target in (LightTarget(0, 50), LightTarget(256, 50), LightTarget(3, 101), SceneTarget("scene_x"),
                   SceneTarget("scene_256")):
        sender, run, _ = build_sender()
        try:
            sender.send(target)
            raised = False
        except ValueError:
            raised = True
        check(f"{target!r}", raised and run.calls == [], f"raised={raised} calls={run.argvs}")

    print("■ 送れないときは WARNING（こちらの失敗）で、黙って別のデバイスへ落とさない")
    sender, run, _ = build_sender(device=None)
    with _Capture() as cap:
        res = sender.send(LightTarget(3, 50))
    check("デバイスが無い → 送らない", not res.ok and run.calls == [], str(res))
    check("  文面", cap.records == [("WARNING", "照明3 を 50% のコードを送信できませんでした（赤外線デバイスが見つからない）"
                                              "— Irdroid の接続を確認すること")], str(cap.records))
    sender, run, _ = build_sender(device=GPIO_RX_DEV)
    with _Capture() as cap:
        res = sender.send(LightTarget(3, 50))
    check("受信デバイスが送信できない（GPIO受信のみ）→ 送らない・GPIO TX へも落とさない",
          not res.ok and run.calls == [] and res.reason == "受信デバイスが送信できない", str(res))
    check("  WARNING 1行", len(cap.of("WARNING")) == 1 and "/dev/lirc1" in cap.of("WARNING")[0], str(cap.records))
    sender, run, _ = build_sender(run=FakeRun(rc=1, stderr="warning: something\nerror: 送信失敗の例\n"))
    with _Capture() as cap:
        res = sender.send(LightTarget(3, 50))
    check("ir-ctl が失敗 → 成功とは言わない", not res.ok and res.reason == "ir-ctl rc=1", str(res))
    check("  WARNING に stderr の最終行", cap.of("WARNING") == [
        "照明3 を 50% のコードを送信できませんでした（ir-ctl rc=1）— error: 送信失敗の例"], str(cap.records))
    check("  「送信しました」は出ない", not [m for m in cap.of("INFO") if "送信しました" in m])
    sender, run, _ = build_sender(run=FakeRun(raise_=FileNotFoundError("ir-ctl")))
    with _Capture() as cap:
        res = sender.send(LightTarget(3, 50))
    check("ir-ctl が無い → WARNING（v4l-utils）", not res.ok and "v4l-utils" in "".join(cap.of("WARNING")), str(cap.records))
    sender, run, _ = build_sender(run=FakeRun(raise_=subprocess.TimeoutExpired("ir-ctl", 5)))
    with _Capture() as cap:
        res = sender.send(LightTarget(3, 50))
    check("ir-ctl が返らない → WARNING", not res.ok and res.reason == "ir-ctl が応答しない", str(res))

    print("■ 受信が静かなときだけ撃つ（デバイスが固まる条件に入らない）")
    from ir_bridge import sender as sender_mod

    check("静かさの基準は 1.5秒（0xffff の約1.4秒より長い）", sender_mod.QUIET_SEC == 1.5)
    # 本物の IrReceiver は未受信で 0.0 を返す＝待たない
    _c, _f, real_rx, _ = build_receiver()
    sender, run, _ = build_sender(receiver=real_rx)
    t0 = time.monotonic()
    sender.send(LightTarget(3, 50))
    check("受信が無ければ待たずに撃つ", run.calls and run.calls[0][0] - t0 < 0.05, f"{(run.calls[0][0] - t0) * 1000:.0f}ms")

    rx = FakeReceiver(time.monotonic())
    sender, run, _ = build_sender(receiver=rx)
    sender.quiet_sec = 0.4
    t_event = rx.last_event_at
    sender.send(LightTarget(3, 50))
    waited = run.calls[0][0] - t_event
    check("最後の受信から quiet_sec 空くまで撃たない", 0.4 <= waited < 0.55, f"{waited * 1000:.0f}ms 後に撃った")

    sender, run, _ = build_sender()
    sender.quiet_sec = 0.3
    sender.send(LightTarget(3, 50))
    sender.send(LightTarget(3, 60))
    gap = run.calls[1][0] - run.calls[0][0]
    check("自分の直前の送信からも quiet_sec 空ける（連打しても詰めて撃たない）", gap >= 0.3, f"{gap * 1000:.0f}ms")

    sender, run, _ = build_sender(receiver=NoisyReceiver())
    sender.quiet_sec, sender.max_wait_sec = 0.2, 0.5
    with _Capture() as cap:
        t0 = time.monotonic()
        res = sender.send(LightTarget(3, 50))
        took = time.monotonic() - t0
    check("受信が途切れない → 見送る（撃たない）", not res.ok and run.calls == [], str(res))
    # 静かになるのを実際に待ち続け（0.2秒ずつ）、max_wait_sec の手前で見込みが無くなった時点で諦める。
    check("  途切れるのを待ち、max_wait_sec を超えない", 0.25 <= took < 0.55, f"{took * 1000:.0f}ms")
    sender, run, _ = build_sender(receiver=NoisyReceiver())
    sender.quiet_sec, sender.max_wait_sec = 1.5, 0.5
    t0 = time.monotonic()
    sender.send(LightTarget(3, 50))
    took = time.monotonic() - t0
    check("  見込みが無ければ（quiet_sec > max_wait_sec）待たずに諦める", took < 0.05 and run.calls == [],
          f"{took * 1000:.0f}ms")
    check("  INFO（外からの赤外線が原因＝§18）・WARNINGにしない",
          cap.of("WARNING") == [] and len(cap.of("INFO")) == 1 and "もう一度押してください" in cap.of("INFO")[0],
          str(cap.records))

    print("■ 同時に押されても ir-ctl は1つずつ")
    run = FakeRun(delay=0.1)
    sender, run, _ = build_sender(run=run)
    sender.quiet_sec = 0.0
    ths = [threading.Thread(target=sender.send, args=(LightTarget(3, v),)) for v in (10, 20, 30)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)
    check("3件とも撃たれ、重なりは無い", len(run.calls) == 3 and run.max_active == 1,
          f"calls={len(run.calls)} 同時最大={run.max_active}")


def around_send_section() -> None:
    print("■ 送信の前後で受信が正常（受信 → 送信 → 反転して届く → 正常に届く）")
    client, firer, rx, store = build_receiver()
    firer.start()
    from ir_bridge.sender import IrSender

    run = FakeRun()
    sender = IrSender(store, rx, rx_spec=None, run=run, find_device=lambda spec: IRDROID_DEV)
    sender.quiet_sec = 0.2
    before = build_frame(codes.encode_light(codes.DEFAULT_BASE, 3, 50), wave=IRDROID)
    after_inv = invert(build_frame(codes.encode_light(codes.DEFAULT_BASE, 4, 60), wave=IRDROID))
    after = build_frame(codes.encode_scene(codes.DEFAULT_BASE, "scene_15"), wave=IRDROID)
    with _Capture() as cap:
        for k, u in before:
            rx._feed(IRDROID_DEV, k, u)
        t_last_rx = rx.last_event_at
        res = sender.send(LightTarget(9, 70))
        for k, u in after_inv + after:
            rx._feed(IRDROID_DEV, k, u)
        drain(firer)
    waited = run.calls[0][0] - t_last_rx if run.calls else -1
    check("受信直後の送信は quiet_sec 待ってから成功", res.ok and waited >= 0.2, f"{waited * 1000:.0f}ms 後・{res}")
    check("前・後（反転）・後（正常）の3件とも実行される",
          client.calls == [("light", 3, 50), ("light", 4, 60), ("scene", "scene_15")], str(client.calls))
    check("受信は止めていない（送信のために受信スレッドを止める機構が無い）", cap.of("WARNING") == [], str(cap.of("WARNING")))


def startup_check_section() -> None:
    """起動時の ir-ctl チェック（顧客が学習UIを押す前に気づけるようにするためのもの）。"""
    import unittest.mock as mock

    from ir_bridge import main as main_mod
    from ir_bridge import sender as sender_mod

    print("■ 起動時に ir-ctl の有無を確かめる")
    with _Capture() as cap, mock.patch("shutil.which", return_value=None):
        main_mod._check_ir_ctl()
    check("無ければ WARNING 1行（永続ログに残る）", len(cap.of("WARNING")) == 1, str(cap.records))
    check("  導入すべきパッケージ名が書いてある", "v4l-utils" in "".join(cap.of("WARNING")), str(cap.records))
    with _Capture() as cap, mock.patch("shutil.which", return_value="/usr/bin/ir-ctl"):
        main_mod._check_ir_ctl()
    check("あれば何も出さない（起動ログを増やさない）", cap.records == [], str(cap.records))
    check("探すのは ir-ctl（送信で実際に使う実行ファイル）", sender_mod.IR_CTL == "ir-ctl")


def main() -> int:
    polarity_section()
    receiver_section()
    sender_section()
    around_send_section()
    startup_check_section()
    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
