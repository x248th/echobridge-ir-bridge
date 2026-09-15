"""receiver: 受信スレッド。MODE2の生波形 → NECデコード（ラベル反転のフォールバック付き・
polarity.py）→ デバウンス → 解釈 → 実行。

■ 送信（sender.py・M4-a）との関係
送信側へは `last_event_at`（最後に生イベントを受けた時刻・float 1つ）だけを見せる。
送信のために受信を止める機構は**持たない**（M4-a 調査1: Irdroid は受信で開いたままの
ノードへ送信でき、自分の送信は受信しない＝止める必要が無い）。

スレッド構成（M3で :8100 の設定UIを同居させる前提）:
    メインスレッド  status.json のループ（骨格からの継続）
    IrReceiver      /dev/lircN を読み続ける。本モジュール
    CommandFirer    本体APIの呼び出し（受信をHTTP待ちで止めないため別スレッド）
共有状態は RecentCodes と ConfigStore（どちらもロック付き）だけに閉じてある。

■ 解釈の順序（M3-b）
    1. デバウンス   キーは (プロトコル名, 送出順4バイト)。**learned と導出で窓を分けない**
                    （分けると、learned に入れた瞬間にデバウンスが効かなくなる）
    2. learned      顧客が学習させた実在リモコンのコード。**導出より先に引く**
    3. 導出         現在の base と一致すれば宛先を取り出す（codes.decode）
    4. 他プリセット そう言う。「未登録」と一緒くたにすると原因に辿り着けない
    5. 未登録       INFO で出す（顧客宅の衝突を測る唯一の計器）

learned を先に引くのは、**衝突したときに顧客の設定が勝つ**ようにするため。
実例が data/learned.json にある: `nec32:0x7d2e5b91`（M2のテスト用SwitchBotコード）は
現在の base に一致するが BB=0x91=145 で導出としては範囲外になる。learned が先なので
これは生き続ける。
"""
import logging
import threading
import time

from . import codes, lirc
from .client import CommandFirer
from .config import ConfigStore
from .polarity import PolarityFallbackDecoder
from .recent import RecentCodes
from .stats import Counters

logger = logging.getLogger(__name__)

# デバイスを見失った/読めなくなったときの再開待ち。
RETRY_SEC = 5.0
# 同じ異常が続く間、WARNINGを繰り返し出す間隔。
# ★RETRY_SEC ではない。WARNING は data/error_addon.log（永続・1MB枠）へ流れるので、
#   5秒ごとに書くと「USBのIrdroidを抜いたまま一晩」で数MB＝枠を使い切り、本来残すべき
#   発火失敗などが .old へ押し出される。異常が続いていることは10分に1行で足りる。
WARN_REPEAT_SEC = 600.0
# 同一コードの最終受信時刻を覚えておく上限（ノイズが偶然デコードできた場合の際限ない成長を防ぐ）。
_LAST_SEEN_MAX = 256


class _WarnThrottle:
    """同じ理由のWARNINGを間引く。理由が変わるか正常へ戻れば、次の1回はすぐ出す。

    「異常が起きた瞬間」と「異常が続いていること」は別の情報で、前者は取りこぼしたくないが
    後者は10分に1行で足りる。両立させるため、初回は必ず出して以降を間引く。
    """

    def __init__(self, repeat_sec: float = WARN_REPEAT_SEC):
        self._repeat_sec = repeat_sec
        self._key: str | None = None
        self._since = 0.0
        self._last = 0.0

    def take(self, key: str) -> float | None:
        """出すべきなら「その異常が続いている秒数」を返す。間引くなら None。"""
        now = time.monotonic()
        if key != self._key:
            self._key, self._since, self._last = key, now, now
            return 0.0
        if now - self._last < self._repeat_sec:
            return None
        self._last = now
        return now - self._since

    def clear(self) -> bool:
        """正常へ戻ったことを記録する。直前まで異常だったなら True。"""
        was_failing = self._key is not None
        self._key = None
        return was_failing


class IrReceiver:
    def __init__(
        self,
        config: ConfigStore,
        recent: RecentCodes,
        firer: CommandFirer,
        rx_spec: str | None = None,
        counters: Counters | None = None,
    ):
        self._config = config
        self._recent = recent
        self._firer = firer
        # 受信デバイスの明示指定（None＝自動選択）。lirc.rx_spec() の戻り値をそのまま受ける。
        self._rx_spec = rx_spec
        # 起動からの累計（診断レポートの計器。RAMのみ）。渡されなければ自前で持つ。
        self._counters = counters if counters is not None else Counters()
        # 通常の NEC 復号＋ラベル反転のフォールバック（ir_toy の既知の欠陥。polarity.py 冒頭）。
        self._decoder = PolarityFallbackDecoder()
        # 最後に生イベントを受けた時刻（monotonic）。送信側（sender.py）が「受信が静かか」を見る
        # ためだけにある。書くのはこのスレッドだけ・float の代入なのでロックは要らない。
        self._last_event_at = 0.0
        self._warn = _WarnThrottle()
        # (プロトコル名, 4バイト) -> 最後に「押下」として扱った時刻。デバウンスの台帳。
        # ★キーは表記文字列ではなくバイト。照合系を1本に保つ（nec.py 冒頭）。
        self._last_seen: dict[tuple[str, codes.Bytes4], float] = {}
        self._thread = threading.Thread(target=self._run, name="IrReceiver", daemon=True)
        self._stop: threading.Event | None = None

    def start(self, stop: threading.Event) -> None:
        self._stop = stop
        self._thread.start()

    def join(self, timeout: float = 3.0) -> None:
        self._thread.join(timeout=timeout)

    @property
    def last_event_at(self) -> float:
        """最後に生イベント（pulse/space/timeout 等）を受けた monotonic 時刻。未受信なら 0.0。"""
        return self._last_event_at

    # --- 内部 -----------------------------------------------------------------
    def _run(self) -> None:
        stop = self._stop
        assert stop is not None
        while not stop.is_set():
            device = lirc.find_rx_device(self._rx_spec)
            if device is None:
                self._warn_no_device()
                if stop.wait(RETRY_SEC):
                    return
                continue

            recovered = self._warn.clear()
            logger.info(
                "%s: %s（%s 役割=%s）%s",
                "受信デバイスに復帰" if recovered else "受信デバイス",
                device.path,
                device.label,
                device.role,
                "" if self._rx_spec is None else f" ← {lirc.RX_DEVICE_ENV}={self._rx_spec} に合致",
            )
            reader = lirc.Mode2Reader(device.path)
            try:
                reader.open()
                self._decoder.reset()
                for kind, usec in reader.events(stop):
                    self._feed(device, kind, usec)
            except OSError as e:
                # 同じ理由で読めない状態が続く間は間引く（永続ログの枠を守る。WARN_REPEAT_SEC）。
                held = self._warn.take(f"read:{device.path}:{e.errno}")
                if held is not None:
                    logger.warning(
                        "受信が中断した（%.0f秒ごとに再開を試みる%s）: %s — %s",
                        RETRY_SEC,
                        "" if held < 1 else f"・{held / 60:.0f}分継続中",
                        device.path,
                        e,
                    )
                if stop.wait(RETRY_SEC):
                    return
            finally:
                reader.close()
        logger.info("受信スレッド停止")

    def _warn_no_device(self) -> None:
        """デバイスが取れない理由を、指定の有無で書き分ける（原因の切り分けが変わるため）。

        見つからない状態は5秒ごとに再探索するが、WARNINGはその都度は出さない
        （_WarnThrottle。永続ログの1MB枠を平常の異常継続で使い切らないため）。
        """
        held = self._warn.take(f"missing:{self._rx_spec}")
        if held is None:
            return
        ongoing = "" if held < 1 else f"・{held / 60:.0f}分継続中"
        if self._rx_spec is None:
            logger.warning(
                "受信できる lircデバイスが見つからない（%.0f秒ごとに再探索%s）: /dev/lirc* の有無と "
                "unitの SupplementaryGroups=video を確認すること",
                RETRY_SEC,
                ongoing,
            )
            return
        # 明示指定が外れている場合。**自動選択へは落とさない**ので、何が居るかを併記して
        # 人が指定を直せるようにする（ゼロサポート型の製品なのでログが唯一の手がかり）。
        available = lirc.find_devices()
        seen = (
            "、".join(f"{d.path}({d.label} {d.role})" for d in available) if available else "無し"
        )
        logger.warning(
            "%s=%s に合致する受信デバイスが無い（%.0f秒ごとに再探索%s・自動選択には切り替えない）"
            "— 現在の /dev/lirc*: %s",
            lirc.RX_DEVICE_ENV,
            self._rx_spec,
            RETRY_SEC,
            ongoing,
            seen,
        )

    def _feed(self, device, kind: str, usec: int) -> None:
        """生イベント1件を処理する。_run から呼ばれる（自己テストは実デバイスなしでここを叩く）。"""
        self._last_event_at = time.monotonic()
        for frame, recovered in self._decoder.feed(kind, usec):
            if recovered and not frame.repeat:
                self._counters.bump("recovered")
                # 計器として残す（未登録コードのログが衝突の計器になっているのと同じ形）。
                # ir_toy 以外のデバイスでこれが出たら、ラベル反転以外の何かが起きている。
                # 受信起因なので INFO（§18）。1フレームにつき最大1行＝受信ログ本体と同じ頻度。
                logger.info(
                    "受信 %s は pulse/space のラベルが入れ替わって届いたため、入れ替えて復号した"
                    "（%s %s・ir_toy は約1.4秒以内の再受信でこうなる既知の欠陥）",
                    frame.code,
                    device.path,
                    device.label,
                )
            self._on_frame(frame)

    def _on_frame(self, frame) -> None:
        # リピートフレーム（約9000/2250）は押しっぱなしの継続通知。実行はしない＝仕様。
        # 直前の押下エントリの計数だけ増やす（対応する押下が無ければ何もしない）。
        if frame.repeat:
            self._counters.bump("repeats")
            self._recent.bump(repeat=True)
            return

        config = self._config.get()  # 1フレームの処理中は同じ設定を見る（途中で変わらない）
        key = frame.key
        now = time.monotonic()
        last = self._last_seen.get(key)
        if last is not None and (now - last) * 1000.0 < config.debounce_ms:
            # デバウンス窓の内側。1押下でリモコンが複数フレームを送る機種もここで畳まれる。
            self._counters.bump("debounced")
            self._recent.bump(frame.code)
            return

        self._prune_last_seen(now, config.debounce_ms)
        self._last_seen[key] = now

        interp = self._interpret(config, key, frame.raw_bytes)
        self._count(interp)
        fired = self._firer.submit(interp.target, frame.code) if interp.target else False
        summary = interp.target.summary if interp.target else interp.reason
        self._recent.record(
            frame.code, frame.protocol, summary, interp.source, fired,
            target=codes.target_info(interp.target),
        )
        self._log(frame, interp, fired)

    def _count(self, interp: codes.Interpretation) -> None:
        """1押下ぶんを数える（診断レポートの計器。stats.py 冒頭）。"""
        self._counters.bump("received")
        if interp.target is not None:
            self._counters.bump("scene" if isinstance(interp.target, codes.SceneTarget) else "light")
        else:
            # source: unknown / other_preset / out_of_range / foreign_necx のいずれか。
            self._counters.bump(interp.source)

    def _interpret(self, config, key, raw) -> codes.Interpretation:
        """受信コードを宛先へ写す。順序はモジュール冒頭の「解釈の順序」のとおり。"""
        entry = config.learned.get(key)
        if entry is not None:
            # 顧客が学習させたコードが最優先（衝突時は顧客の設定が勝つ）。
            return codes.Interpretation(entry.target, entry.label, "learned")

        derived = codes.decode(config.base, raw)
        if derived is not None:
            return derived

        return codes.Interpretation(None, "未登録（learned にも導出にも一致せず）", "unknown")

    def _log(self, frame, interp, fired: bool) -> None:
        """§26-10 のログ規律: **1行目だけ読んで成功と誤読させない**。

        導出方式では1行目で「何をしようとしているか」まで書けるので、
        「受信 <コード> → <宛先>（実行を要求）」の形にする。実行は別スレッドで
        非同期に走るので、この行はあくまで**要求**であり、成功なら client.py が
        「…しました」を、失敗なら WARNING を続けて出す（2行で状況が閉じる）。
        """
        if interp.target is not None:
            if fired:
                logger.info("受信 %s → %s（実行を要求）", frame.code, interp.target.summary)
            else:
                # キュー満杯で捨てた場合（submit が WARNING を出している）。ここで
                # 「要求した」と書くと事実と食い違うので分ける。
                logger.info(
                    "受信 %s → %s（実行を要求できず・キュー満杯）",
                    frame.code,
                    interp.target.summary,
                )
            return

        # 宛先が無い場合。未登録コードも必ず INFO で出す——顧客宅で導出コードと実在
        # リモコンが衝突していないかを測る唯一の計器であり、設定UIの学習操作の入口でもある。
        # 出力先は stdout → journal のみ（永続ログ data/error_addon.log には入れない。
        # 1MB枠を平常運転で食わないため。M2-Dの教訓）。
        if interp.source == "unknown":
            logger.info(
                "受信 %s → 未登録（learned にも導出にも一致せず）— 設定画面で学習させると"
                "シーンに割り当てられる",
                frame.code,
            )
        else:
            logger.info("受信 %s → %s", frame.code, interp.reason)

    def _prune_last_seen(self, now: float, debounce_ms: int) -> None:
        if len(self._last_seen) <= _LAST_SEEN_MAX:
            return
        window = debounce_ms / 1000.0
        self._last_seen = {c: t for c, t in self._last_seen.items() if now - t < window}
