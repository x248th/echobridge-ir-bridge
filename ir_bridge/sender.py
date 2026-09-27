"""sender: 導出コードを赤外線で1発だけ送る（M4-a）。M3(d) の学習UIの土台。

入口は `IrSender.send(target)`。呼ぶのは設定UI（`webui.UiApp.send`）と自己テスト。

■ 1発だけ・連続送信はしない（2026-08-05 の連続送信構想を覆した・人間の判断）
  1. テレビのリモコンを学習させるとき人は1回押す。顧客の既存の理解に乗る
  2. NEC にはリピートの仕様があり、同一コードの連続をスマートリモコンが「長押し」と取るか
     「別々の学習」と取るかが読めない。3社で違えば製品が説明を負う
  3. 失敗したら押し直せばいい。回復が顧客の手の中で完結する

■ 送信デバイス ＝ 受信デバイス
製品では Irdroid 1台が RX と TX を兼ねる（features=0x30040102）。受信と同じ選択規則
（`lirc.find_rx_device` と `IR_BRIDGE_RX_DEVICE`）で決め、それが送信できなければ送らない。
★送信専用の別デバイス（開発機の GPIO TX）へは**落とさない**。GPIO TX は転換前の残置物で
製品には載らない。黙ってそちらで撃つと「開発機では学習できたのに製品ではできない」を作る。

■ 受信が静かなときだけ撃つ（デバイスが固まるのを予防する）
ir_toy ドライバ（drivers/media/rc/ir_toy.c `irtoy_tx`）のコメント: 受信の途中で TXSTART を
送るとデバイスが LED 点灯のまま固まり、コマンドに応じなくなることがある。**固まると USB の
抜き差ししか復旧手段が無い**（顧客の Pi は棚の上や天井裏にある）。ソフトで直せない以上、
「起きてから直す」ではなく「条件に入らない」形にする。

QUIET_SEC を 1.5秒 に置く根拠（人間の指示は 200ms 程度。長くした・M4-a の報告で要確認）:
Irdroid にとって受信の終わりは最後のエッジではなく、無信号 約1.4秒で 0xffff を送った時点で
ある（polarity.py 冒頭）。実測でも、受信の0.3秒後に撃つと次のフレームが半分欠けた例が
4回中1回あり、2秒後に撃てば4回とも正常だった。自分の直前の送信からも同じだけ空ける
（連射の直後に反転が出た実測がある）。待つのは受信・送信の直後だけで、学習UIの通常の流れ
（顧客がボタンを押す → 撃つ）では待ちは発生しない。
★確認してから撃つまで（ir-ctl の起動〜TXSTART）の数十msに受信が始まる隙間は残る。
  ドライバの中の話で塞げない。確率を下げているだけである。

■ ir-ctl を使う（依存: v4l-utils）
受信は /dev/lircN を直接読む（CLAUDE.md §12）が、送信は `ir-ctl -S` に任せる。カーネルの
NEC エンコーダを通すので、受信側で確定させた表記（nec32 の ir-ctl 並び・§16）と同じ土俵で撃てる。
"""
import logging
import shutil
import subprocess
import threading
import time
from typing import NamedTuple

from . import codes, lirc
from .nec import bytes_to_code
from .stats import Counters

logger = logging.getLogger(__name__)

# 最後の受信イベント・自分の直前の送信から、これだけ空くまで撃たない（上の根拠）。
QUIET_SEC = 1.5
# 静かになるのをこれ以上は待たない。赤外線が浴びせられ続けている場合は見送って押し直してもらう。
MAX_WAIT_SEC = 5.0
IR_CTL = "ir-ctl"
# Irdroid の実測は1発 67〜68ms。ドライバ側の応答待ちは 500ms × コマンド3つ。
IR_CTL_TIMEOUT_SEC = 5.0

# SendResult.reason の値。**設定UI（webui.py）が文面の出し分けに使う**ので、
# 文字列を直接書かずここを参照すること（ログ文面を直したら UI の分岐が壊れる、を避ける）。
REASON_BUSY = "赤外線の受信が続いているため送信を見送った"
REASON_NO_IR_CTL = "ir-ctl が無い"
REASON_NO_DEVICE = "赤外線デバイスが見つからない"
REASON_CANNOT_TX = "受信デバイスが送信できない"
REASON_NO_RESPONSE = "ir-ctl が応答しない"


def ir_ctl_path() -> str | None:
    """ir-ctl の実体パス。無ければ None。

    起動時に main が呼んで、無ければ WARNING を出す（顧客が学習UIのボタンを押して初めて
    「送信できない」と分かる、という形にしないため）。`ir-ctl` は Raspberry Pi OS の
    パッケージ `v4l-utils` に入っている。
    """
    return shutil.which(IR_CTL)


class SendResult(NamedTuple):
    """send() の結果。UIがそのまま顧客に見せられる形にしてある。"""

    ok: bool
    # 撃った（撃とうとした）コードの表記。失敗時も入る。
    code: str
    # 失敗の理由（成功時は空）。
    reason: str = ""


class IrSender:
    """導出コードを1発送る。send() は同時に1つしか走らない（ロックで直列化）。

    receiver は `last_event_at`（最後に生イベントを受けた monotonic 時刻）を持つもの。
    本番では receiver.IrReceiver。受信を止める機構は持たない（止める必要が無いことを
    M4-a 調査1で確認した: Irdroid は受信で開いたままのノードへ送信でき、自分の送信は受信しない）。
    """

    def __init__(self, config, receiver, rx_spec=None, run=subprocess.run,
                 find_device=lirc.find_rx_device, counters: Counters | None = None):
        self._config = config
        self._receiver = receiver
        self._rx_spec = rx_spec
        self._run = run
        self._find_device = find_device
        # 起動からの累計（診断レポートの計器）。渡されなければ自前で持つ。
        self._counters = counters if counters is not None else Counters()
        self._lock = threading.Lock()
        self._last_tx_at = float("-inf")
        # 試験で短くできるよう属性にしてある（本番は定数のまま）。
        self.quiet_sec = QUIET_SEC
        self.max_wait_sec = MAX_WAIT_SEC

    def send(self, target: codes.Target) -> SendResult:
        """宛先の導出コードを1発送る。

        ★存在しないシーン・照明を**検証しない**（CLAUDE.md §17）。送ったコードは顧客の
        スマートリモコンに学習され、使われたときに本体が 404 を返す——それが唯一の真実。
        表現できない宛先（instance 0・輝度101 等）は ValueError（呼び側の誤り）。
        """
        raw = codes.encode(self._config.get().base, target)
        _proto, code = bytes_to_code(*raw)
        with self._lock:
            return self._send_locked(target, code)

    # --- 内部 -----------------------------------------------------------------
    def _send_locked(self, target, code: str) -> SendResult:
        device = self._find_device(self._rx_spec)
        if device is None:
            return self._fail(target, code, REASON_NO_DEVICE, "Irdroid の接続を確認すること")
        if not device.can_tx:
            return self._fail(
                target, code, REASON_CANNOT_TX, f"{device.path}（{device.label} 役割={device.role}）"
            )

        waited = self._wait_quiet()
        if waited is None:
            # 外から浴びせられている赤外線が原因＝頻度はこちらで制御できない。INFO に置く（§18）。
            reason = REASON_BUSY
            logger.info(
                "%s のコードを送信しませんでした（%s・%.0f秒以内に途切れる見込みが無い）— もう一度押してください",
                target.label,
                reason,
                self.max_wait_sec,
            )
            self._counters.bump("send_skipped")
            return SendResult(False, code, reason)

        argv = [IR_CTL, "-d", device.path, "-S", code]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=IR_CTL_TIMEOUT_SEC)
        except FileNotFoundError:
            return self._fail(target, code, REASON_NO_IR_CTL, "v4l-utils が導入されていない")
        except subprocess.TimeoutExpired:
            return self._fail(target, code, REASON_NO_RESPONSE, f"{IR_CTL_TIMEOUT_SEC:.0f}秒でタイムアウト")
        finally:
            # 失敗でも途中まで発光しているかもしれないので、撃ったものとして間隔を空ける。
            self._last_tx_at = time.monotonic()
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            return self._fail(
                target, code, f"ir-ctl rc={proc.returncode}", detail[-1] if detail else " ".join(argv)
            )

        self._counters.bump("sent")
        # ★送信が完了したときだけ出る行（ir_toy は送信完了の応答を待ってから ir-ctl を返す）。
        logger.info("%s のコードを送信しました（%s）", target.label, code)
        return SendResult(True, code)

    def _wait_quiet(self) -> float | None:
        """受信と自分の送信が quiet_sec 途切れるまで待ち、待った秒数を返す。

        開始から max_wait_sec 以内に静かになる見込みが無ければ None（その時点で諦める。
        待ち切ってから諦めるのではない＝ログに「N秒待った」とは書かない）。
        """
        start = time.monotonic()
        deadline = start + self.max_wait_sec
        while True:
            now = time.monotonic()
            ready_at = max(self._receiver.last_event_at, self._last_tx_at) + self.quiet_sec
            if now >= ready_at:
                return now - start
            if ready_at > deadline:
                return None
            time.sleep(ready_at - now)

    def _fail(self, target, code: str, reason: str, detail: str) -> SendResult:
        # こちら側の失敗（デバイス・ir-ctl）。1回の操作につき1行なので永続ログの枠は食わない。
        self._counters.bump("send_failed")
        logger.warning("%s のコードを送信できませんでした（%s）— %s", target.label, reason, detail)
        return SendResult(False, code, reason)
