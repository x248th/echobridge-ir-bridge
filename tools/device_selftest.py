#!/usr/bin/env python3
"""device_selftest: 受信デバイスの選択規則の自己テスト（実機・外部依存なしで走る）。

RXが複数ある機体（GPIOのVS1838B + USBのIrdroid）で「どちらを掴むか」は、赤外線が
届いているのに反応しないという形でしか表面化しない＝現物で切り分けるのが一番高い。
規則そのものを合成デバイス表で固定しておく。

    python3 tools/device_selftest.py    # 全件PASSなら rc=0

末尾に実機の /dev/lirc* も出す（デバイスが無い環境では単に空。試験結果には影響しない）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/

import isolation  # noqa: E402  ← ir_bridge より先に（置き場所ごと一時ディレクトリへ逃がす・前後で本物を見張る）

import logging  # noqa: E402

from ir_bridge import lirc, main as main_mod  # noqa: E402
from ir_bridge.lirc import LircDevice  # noqa: E402
from ir_bridge.receiver import _WarnThrottle  # noqa: E402

# --- dev機の実測構成をそのまま合成デバイス表にしたもの（features は実測値） -----
#   /dev/lirc0 0x00000302 TX     GPIO送信（gpio-ir-tx）
#   /dev/lirc1 0x10040000 RX     GPIO受信（VS1838B・gpio_ir_recv）
#   /dev/lirc2 0x30040102 RX+TX  Irdroid（ir_toy / "Infrared Toy"）
GPIO_TX = LircDevice("/dev/lirc0", 0x00000302, "gpio-ir-tx", "GPIO IR Bit Banging Transmitter")
GPIO_RX = LircDevice("/dev/lirc1", 0x10040000, "gpio_ir_recv", "gpio_ir_recv")
IRDROID = LircDevice("/dev/lirc2", 0x30040102, "ir_toy", "Infrared Toy")
DEVICES = [GPIO_TX, GPIO_RX, IRDROID]

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


def select(spec, devices=DEVICES):
    d = lirc.select_rx_device(devices, spec)
    return d.path if d else None


def spec_from_env(value):
    """環境変数を一時的に置いて lirc.rx_spec() を評価する（unit/data/env の注入を模す）。"""
    saved = os.environ.get(lirc.RX_DEVICE_ENV)
    if value is None:
        os.environ.pop(lirc.RX_DEVICE_ENV, None)
    else:
        os.environ[lirc.RX_DEVICE_ENV] = value
    try:
        return lirc.rx_spec()
    finally:
        if saved is None:
            os.environ.pop(lirc.RX_DEVICE_ENV, None)
        else:
            os.environ[lirc.RX_DEVICE_ENV] = saved


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def of(self, level):
        return [m for lv, m in self.records if lv == level]

    def __enter__(self):
        root = logging.getLogger("ir_bridge")
        self._saved = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger("ir_bridge")
        root.removeHandler(self)
        root.setLevel(self._saved)


def _log_devices_with(devices, spec):
    """main._log_devices を合成デバイス表で走らせる（実機の /dev/lirc* を見ない）。"""
    saved = lirc.find_devices
    lirc.find_devices = lambda: list(devices)
    try:
        with _Capture() as cap:
            main_mod._log_devices(spec)
        return cap
    finally:
        lirc.find_devices = saved


def auto_warning_section() -> None:
    """W19-10: RX が複数あるのに auto のとき、起動ログに1行添える。"""
    print("■ W19-10 RXが複数あるのに auto なら、起動ログに1行添える（再インストールで指定が消える）")
    cap = _log_devices_with(DEVICES, None)
    warns = [m for m in cap.of("WARNING") if "auto" in m]
    check("  ★1行出る（旧実装は黙って最初の1台を選んだ）", len(warns) == 1, str(cap.of("WARNING")))
    check("    何台あるかと、どれを使うかを書く",
          warns and "2 台" in warns[0] and GPIO_RX.path in warns[0], str(warns))
    check("    直し方（data/env で指定する）も書く",
          warns and lirc.RX_DEVICE_ENV in warns[0] and "data/env" in warns[0], str(warns))
    check("  1起動につき1行だけ（永続ログの1MB枠を食わない）", len(cap.of("WARNING")) == 1, str(cap.of("WARNING")))

    print("■ W19-10 影響を広げない（製品機＝RX 1台・指定あり、では出ない）")
    cap = _log_devices_with([GPIO_TX, IRDROID], None)
    check("  RX が1台なら出ない（製品機は Irdroid 1台）", cap.of("WARNING") == [], str(cap.of("WARNING")))
    cap = _log_devices_with(DEVICES, "ir_toy")
    check("  指定があれば出ない（auto ではない）", cap.of("WARNING") == [], str(cap.of("WARNING")))
    cap = _log_devices_with([GPIO_TX], None)
    check("  TX しか無いなら出さない（受信スレッドが「見つからない」を出す・既存の判断）",
          cap.of("WARNING") == [], str(cap.of("WARNING")))
    cap = _log_devices_with([], None)
    check("  /dev/lirc* が1つも無いときは従来どおり「見つからない」1行だけ",
          len(cap.of("WARNING")) == 1 and "見つからない" in cap.of("WARNING")[0], str(cap.of("WARNING")))
    cap = _log_devices_with(DEVICES, "no-such-device")
    check("  指定が外れていても、ここでは出さない（受信スレッドが出す・既存の判断）",
          cap.of("WARNING") == [], str(cap.of("WARNING")))
    check("  一覧の INFO は従来どおり（デバイス数＋指定の1行）",
          len(_log_devices_with(DEVICES, None).of("INFO")) == 1 + len(DEVICES))


def main() -> int:
    print(f"■ 環境変数の解釈（{lirc.RX_DEVICE_ENV}）— None は自動選択の意味")
    for value, want in [
        (None, None),          # 未設定＝既存機体。従来どおり自動
        ("", None),            # data/env に空で書かれた場合
        ("   ", None),
        ("auto", None),
        ("AUTO", None),        # 人が大文字で書いても自動
        (" /dev/lirc2 ", "/dev/lirc2"),  # 前後の空白は落とす（data/env の書き癖）
        ("ir_toy", "ir_toy"),
    ]:
        got = spec_from_env(value)
        check(f"{value!r} → {want!r}", got == want, "" if got == want else f"got={got!r}")

    print("■ 従来の挙動（指定なし＝受信できる最初の1台）が変わらないこと")
    check("指定なしでは lirc1（最初のRX）", select(None) == "/dev/lirc1", str(select(None)))
    check("送信専用のlirc0は選ばれない", select(None) != "/dev/lirc0")
    check("RXが1台だけの機体でもそれを選ぶ", select(None, [GPIO_TX, GPIO_RX]) == "/dev/lirc1")
    check("RXが無ければ None", select(None, [GPIO_TX]) is None)
    check("デバイスが無ければ None", select(None, []) is None)

    print("■ パスでの指定（`/dev/lircN` / `lircN` / `N` は同じ意味）")
    for spec in ("/dev/lirc2", "lirc2", "2"):
        check(f"{spec!r} → /dev/lirc2（Irdroid）", select(spec) == "/dev/lirc2", str(select(spec)))
    check("'/dev/lirc1' → GPIO受信", select("/dev/lirc1") == "/dev/lirc1")
    check("送信専用を名指ししても選ばれない", select("/dev/lirc0") is None, str(select("/dev/lirc0")))
    check("存在しない番号は None", select("/dev/lirc9") is None)

    print("■ 名前での指定（ドライバ名／デバイス名・大小無視）")
    check("'ir_toy' → Irdroid", select("ir_toy") == "/dev/lirc2", str(select("ir_toy")))
    check("'IR_TOY' → Irdroid（大小無視）", select("IR_TOY") == "/dev/lirc2")
    check("'Infrared Toy' → Irdroid（DEV_NAME）", select("Infrared Toy") == "/dev/lirc2")
    check("'infrared toy' → Irdroid（大小無視）", select("infrared toy") == "/dev/lirc2")
    check("'gpio_ir_recv' → GPIO受信", select("gpio_ir_recv") == "/dev/lirc1")
    check("送信専用のドライバ名は選ばれない", select("gpio-ir-tx") is None, str(select("gpio-ir-tx")))
    check("知らない名前は None", select("nosuch") is None)
    check("部分一致では選ばない", select("toy") is None, str(select("toy")))
    check(
        "sysfsが読めず名前が無い機体では、名前指定は当たらない（パス指定は効く）",
        select("ir_toy", [LircDevice("/dev/lirc2", 0x30040102)]) is None
        and select("2", [LircDevice("/dev/lirc2", 0x30040102)]) == "/dev/lirc2",
    )

    print("■ 番号が入れ替わっても名前指定は追随する（USB機を薦める理由）")
    # Irdroidを先に挿した／GPIOのoverlayが後から出た機体。同じ機器が lirc0 になる。
    renumbered = [
        IRDROID._replace(path="/dev/lirc0"),
        GPIO_TX._replace(path="/dev/lirc1"),
        GPIO_RX._replace(path="/dev/lirc2"),
    ]
    check("'ir_toy' は番号が変わっても Irdroid", select("ir_toy", renumbered) == "/dev/lirc0")
    check("'gpio_ir_recv' も追随する", select("gpio_ir_recv", renumbered) == "/dev/lirc2")
    check(
        "パス指定は番号に釘付け＝別の機器を掴む（だからUSB機には薦めない）",
        select("/dev/lirc2", renumbered) == "/dev/lirc2"
        and select("/dev/lirc2", DEVICES) == "/dev/lirc2",
        "同じ '/dev/lirc2' が構成次第で Irdroid にもGPIO受信にもなる",
    )

    print("■ 指定が外れたときに自動選択へ落ちないこと")
    # Irdroidを抜いた状態。ここでGPIO受信を掴むと「別のセンサで黙って受け続ける」ことになる。
    unplugged = [GPIO_TX, GPIO_RX]
    check("'ir_toy' 指定でIrdroid不在 → None", select("ir_toy", unplugged) is None, str(select("ir_toy", unplugged)))
    check("'/dev/lirc2' 指定で不在 → None", select("/dev/lirc2", unplugged) is None)
    check("指定なしなら同じ構成でGPIO受信を掴む（自動は従来どおり）", select(None, unplugged) == "/dev/lirc1")

    print("■ ログ用の呼び名（label）")
    check("ドライバ名とデバイス名を併記", IRDROID.label == "ir_toy/Infrared Toy", IRDROID.label)
    check("同じ語は繰り返さない", GPIO_RX.label == "gpio_ir_recv", GPIO_RX.label)
    check("名前が取れなければ '名前不明'", LircDevice("/dev/lirc0", 0).label == "名前不明")
    check("役割は features から", (GPIO_TX.role, GPIO_RX.role, IRDROID.role) == ("TX", "RX", "RX+TX"))

    print("■ 異常継続時のWARNING間引き（永続ログ data/error_addon.log の1MB枠を守る）")
    # 5秒ごとの再探索でそのままWARNINGを書くと、Irdroidを抜いたまま放置しただけで
    # 枠を使い切り、本来残すべき発火失敗が .old へ押し出される。
    th = _WarnThrottle(repeat_sec=3600.0)
    check("異常の初回は必ず出る", th.take("missing:ir_toy") == 0.0)
    check("同じ理由の2回目は間引く", th.take("missing:ir_toy") is None)
    check("3回目も間引く", th.take("missing:ir_toy") is None)
    check("理由が変われば出る（別の指定・別のerrno）", th.take("read:/dev/lirc2:19") == 0.0)
    check("変わった先でも2回目は間引く", th.take("read:/dev/lirc2:19") is None)
    check("正常へ戻ったことを検出できる", th.clear() is True)
    check("正常が続けば False（復帰ログを二重に出さない）", th.clear() is False)
    check("復帰後に再発したら、また出る", th.take("read:/dev/lirc2:19") == 0.0)

    always = _WarnThrottle(repeat_sec=0.0)
    check(
        "間隔0なら毎回出る（間引きは時間だけで決まる）",
        always.take("x") == 0.0 and always.take("x") is not None,
    )

    auto_warning_section()

    print("■ 参考: この機体の実物（試験対象ではない）")
    found = lirc.find_devices()
    for d in found:
        print(f"    {d.path} features=0x{d.features:08x} 役割={d.role} 名前={d.label}")
    if not found:
        print("    /dev/lirc* は見つからない（この試験の結果には影響しない）")
    else:
        spec = lirc.rx_spec()
        chosen = lirc.select_rx_device(found, spec)
        print(
            f"    {lirc.RX_DEVICE_ENV}={spec if spec is not None else lirc.AUTO} "
            f"→ 受信に使うのは {chosen.path if chosen else '（該当なし）'}"
        )

    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(isolation.run(main))
