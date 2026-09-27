"""client: 本体API(:8099)のシーン発火と、それを受信ループから切り離すワーカー。

契約は `~/app/API_CONTRACT.md`（本体の実装コードは読まない）:
- すべて GET。`GET /api/scene/{key}` でシーンを発火し、成功は `{"ok": true, "scene": key}`。
  `{key}` は URLエンコード可。存在しない key は 404。機器側エラーは 502。
- `GET /api/light/{instance}/brightness/{value}`。`value` は 0-100 で、**0 は消灯**。
  範囲外は 400・存在しない instance は 404・機器エラーは 502。
- **同一機体（localhost）からの接続は認証の検査自体を行わない**＝トークン不要。
- ECHONET を伴う操作はプロセス内で直列化されるため、応答が遅れることがある。

■ 照明は brightness だけを使う（/on・/off を使わない）
0-100 を**一律** `brightness/{value}` へ流す。経路を2本にすると「BB=0x00 のときだけ
別扱い」が復活し、導出方式（codes.py）の利点——宛先も輝度も1つの規則で表せること——が
消える。`/on` が前回輝度を復元する挙動は実機で再現するが**契約に無い**ので依存しない
（M3-b の調査で実測。契約 §0「本書に書かれていない挙動に依存してはならない」）。

本体URLは環境変数 `ECHOBRIDGE_URL`（既定 `http://127.0.0.1:8099`）。ハードコード禁止
（CLAUDE.md §4）。別機体構成では unit の EnvironmentFile=data/env で上書きする。
（本体のトークン認証が OFF のときだけ成立する。下の「鍵を持たない設計」）

■ 鍵（?key=）を持たない設計
本クライアントはトークンを読まず、URL に ?key= を付けない（HAP版・Matter版の
ECHOBRIDGE_TOKEN に相当する口が無い）。その結果、本体への到達は localhost 免除に
**完全に依存している**。ECHOBRIDGE_URL がループバック以外（LAN のアドレス・別機体）を
指し、かつ本体のトークン認証が ON だと、全リクエストが 401 になる。
401 は 404・502・接続不可と同じく CommandFirer が WARNING に出すだけでプロセスは落ちない
＝赤外線を押しても何も起きず、data/error_addon.log に行が溜まるだけの状態になる。
get_scenes / get_lights（設定UIの一覧取得）も同じく ClientError になる。
"""
import http.client
import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request

from .codes import LightTarget, SceneTarget
from .warnlimit import WarnLimiter, stamp, suffix

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8099"
# 本体はECHONET操作を直列化するため応答が遅れうる。受信スレッドは別なので長めでよい。
DEFAULT_TIMEOUT_SEC = 10.0


class ClientError(Exception):
    """本体API呼び出しの失敗。status はHTTPステータス（接続失敗等では None）。

    ログ文面を宛先の種類ごとに書き分けたい（404が「未知のシーン」なのか
    「未知の instance」なのか）ので、**文字列ではなくコードで分岐できる**ようにする。
    契約 §4 も「error の文字列内容は契約ではない。分岐にはステータスコードを使う」。
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class EchoBridgeClient:
    def __init__(self, base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT_SEC):
        self.base_url = (base_url or os.environ.get("ECHOBRIDGE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as res:
                body = res.read()
        except urllib.error.HTTPError as e:
            # 契約: 400/401/404/502/500。error文字列の中身は契約ではないので分岐に使わない。
            raise ClientError(f"HTTP {e.code} {url}", status=e.code) from e
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            # ★http.client.HTTPException（IncompleteRead・BadStatusLine・LineTooLong）は
            #   OSError の派生では**ない**。ValueError は ECHOBRIDGE_URL にスキームが無いとき
            #   urlopen が投げる。包まないとワーカースレッドの外へ抜けてスレッドが終わり、
            #   以後シーンも照明も二度と動かない（W19-1）。
            raise ClientError(f"接続失敗 {url}: {e}") from e
        try:
            payload = json.loads(body)
        except ValueError as e:
            raise ClientError(f"JSONとして読めない応答 {url}: {e}") from e
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ClientError(f"ok=false 応答 {url}: {payload!r}")
        return payload

    def fire_scene(self, key: str) -> None:
        """シーンを発火する。失敗は ClientError。"""
        self._get(f"/api/scene/{urllib.parse.quote(key, safe='')}")

    def set_brightness(self, instance: int, value: int) -> None:
        """照明の輝度を設定する（0 は消灯）。失敗は ClientError。"""
        self._get(f"/api/light/{int(instance)}/brightness/{int(value)}")

    def get_scenes(self) -> list[dict]:
        """シーン一覧（発火しない）。学習UIの一覧作成用。"""
        return self._get("/api/scenes").get("scenes", [])

    def get_lights(self) -> list[dict]:
        """照明一覧（操作しない）。学習UIの一覧作成用。"""
        return self._get("/api/lights").get("lights", [])


class CommandFirer:
    """本体APIの呼び出しを専用スレッドへ逃がす（受信ループをHTTP待ちで止めないため）。

    M2では SceneFirer という名前でシーン発火専用だったが、導出方式（M3-b）では
    **個別照明の輝度も同じ経路で投げる**。シーン専用の名前のままだと、次に読む人が
    「個別照明はどこで処理しているのか」を探すことになるので改名した。

    本体のシーンは2フェーズで0.5秒目標、ECHONETの直列化でさらに延びうる。その間
    受信スレッドが止まると、カーネルのlircバッファが溢れて次の赤外線を取りこぼす。
    キューが詰まるほど連投された場合は**捨てる**（WARNING）。押しっぱなしのリモコンで
    キューが伸び続け、指を離した後も発火が続く、という挙動のほうが有害なため。

    ■ WARNING の間引き（W18 R-1・§6-2）
    実行の失敗とキュー満杯は**受信が引き金**なので、頻度はこちらで制御できない（本体に繋がらない間に
    フルフレームを送り直すリモコンを長押しすると、デバウンスを抜けるたびに1行＝約1秒に1行）。
    理由ごとに10分に1行へ間引き（warnlimit.WarnLimiter）、間引いた件数は次の1行に
    「（この間に同じ失敗が N 件・最後は YYYY-MM-DD HH:MM）」と添える。停止時に残っている件数も1行にまとめて
    出す（理由ごとに「（最後は …）」付き。warnlimit 冒頭）。
    間引いた1回ずつは INFO（journal）に残す——「押したのに動かなかった」の証跡を失わない。
    理由のキー: 接続できない（status なし）は宛先によらず1つ。HTTP の失敗（404・400・502 …）は
    宛先ごと（どのシーン・照明が 404 なのかが、直すための情報なので）。
    """

    QUEUE_MAX = 8

    def __init__(self, client: EchoBridgeClient):
        self._client = client
        self._q: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX)
        self._thread = threading.Thread(target=self._run, name="CommandFirer", daemon=True)
        self._stopped = threading.Event()
        self._limiter = WarnLimiter()

    def start(self) -> None:
        self._thread.start()

    def submit(self, target, code: str) -> bool:
        """宛先の実行を予約する。キューが一杯なら捨てて False。

        target は codes.SceneTarget / codes.LightTarget。code は受信コード（ログ用）。
        """
        try:
            self._q.put_nowait((target, code))
            return True
        except queue.Full:
            suppressed = self._limiter.take("queue_full", "実行キューが一杯のため破棄")
            if suppressed is None:
                logger.info("実行キューが一杯のため破棄（間引き中）: %s（受信コード %s）", target.summary, code)
            else:
                logger.warning(
                    "実行キューが一杯のため破棄: %s（受信コード %s）%s", target.summary, code, suffix(suppressed)
                )
            return False

    def is_alive(self) -> bool:
        """ワーカースレッドが生きているか（常駐ループの見張り用・W19-14）。

        start() 前は True を返す（まだ死んでいない）。死んだままの「稼働中」を残さないために、
        main の常駐ループがこれを60秒ごとに見る。
        """
        return self._thread.ident is None or self._thread.is_alive()

    def stop(self, timeout: float = 3.0) -> None:
        """停止する。**キューに残った実行は捨てる**（意図的な仕様）。

        IR受信から実行までの間に SIGTERM が来たということは、アップデートか再起動が
        走っている。そこで遅れて照明が動くと、顧客には「勝手に動いた」としか見えない。

        ★捨てた件数を必ず1行残す。顧客が「押したのに動かなかった」と言ったとき、
        この行の有無で「受信していない」のか「受信したが終了処理で捨てた」のかが
        分かれる。**WARNING に出す**のは、破棄が起きるのは再起動の最中＝journal(RAM)が
        次の起動で消えている場面だからで、揮発側に書くと肝心なときに残らない。
        1回の停止につき最大1行なので、永続ログの1MB枠には影響しない。
        """
        self._stopped.set()
        if self._thread.ident is not None:
            # start() 済みのときだけ番兵を置いて待つ。起動前に停止しても落とさない
            # （起動途中の異常で stop() だけ呼ばれる経路があっても、破棄の記録は残す）。
            try:
                self._q.put_nowait(None)
            except queue.Full:
                # ★待つ put にしない（W19-2）。ワーカーが死んでいてキューが満杯だと永久に
                #   返らず、SIGTERM が systemd 既定の 90秒効かないうえ、**この下の2行の記録が
                #   1行も残らない**（最後は SIGKILL＝logging も流れない）。
                #   満杯なら番兵は要らない: 直後の排出でキューは空になり、ワーカーは
                #   _stopped を見て抜ける。
                pass
            self._thread.join(timeout=timeout)
        dropped = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                dropped.append(item[0])
        if dropped:
            logger.warning(
                "終了処理のため %d 件の実行を破棄した（再起動・停止の最中に受信したもの）: %s",
                len(dropped),
                "、".join(t.summary for t in dropped),
            )
        # 間引いたまま次の1行が来なかった件数を回収する（件数は失わない）。停止の最中＝journal が
        # 次の起動で消える場面なので WARNING に出す。1回の停止につき最大1行。
        # ★理由ごとに最後に数えた時刻を添える。この行は失敗が止んでから何週間も後に書かれうるので、
        #   添えないと「停止の直前まで失敗していた」と読まれる（warnlimit 冒頭）。
        pending = self._limiter.drain()
        if pending:
            logger.warning(
                "停止までに間引いた WARNING: %s",
                "、".join(f"{label} {n} 件（最後は {stamp(last)}）" for label, n, last in pending),
            )

    # --- 内部 -----------------------------------------------------------------
    def _execute(self, target) -> None:
        if isinstance(target, SceneTarget):
            self._client.fire_scene(target.key)
        elif isinstance(target, LightTarget):
            # 0-100 を一律 brightness へ流す（/on・/off は使わない。本モジュール冒頭）。
            self._client.set_brightness(target.instance, target.brightness)
        else:  # pragma: no cover - 型を増やしたのに分岐を足し忘れた場合
            raise ClientError(f"未知の宛先型: {target!r}")

    @staticmethod
    def _hint(target, status: int | None) -> str:
        """HTTPステータスを宛先の種類に応じた日本語へ。契約 §4 に従いコードで分岐する。"""
        if status is None:
            return "本体に接続できない"
        if status == 404:
            return "未知のシーン" if isinstance(target, SceneTarget) else "未知の instance"
        if status == 400:
            return "本体が入力を拒否"
        if status == 502:
            return "機器側エラー"
        return f"HTTP {status}"

    def _run(self) -> None:
        while True:
            try:
                if self._step():
                    return
            except Exception:  # noqa: BLE001
                # ★1件の失敗でワーカーを失わない（W19-1・W19-14）。ここを抜けるとスレッドが
                #   終わり、本体カードは「稼働中」のまま**シーンも照明も二度と動かない**
                #   （復旧は再起動だけ）。しかもデーモンスレッドの例外は threading.excepthook が
                #   fd 2 へ直接書く＝logging を通らないので回転も起きない（errlog.py 冒頭）。
                #   WARNING は間引く（受信が引き金＝頻度をこちらで制御できない）。
                released = self._limiter.take(("worker_unexpected",), "実行ワーカーの想定外の例外")
                if released is None:
                    logger.info("実行ワーカーで想定外の例外（間引き中・継続する）", exc_info=True)
                else:
                    logger.warning(
                        "実行ワーカーで想定外の例外（継続する）%s", suffix(released), exc_info=True
                    )

    def _step(self) -> bool:
        """キューから1件取り出して実行する。停止すべきなら True。"""
        item = self._q.get()
        if item is None or self._stopped.is_set():
            return True
        target, code = item
        try:
            self._execute(target)
            # ★成功したときだけ出る行。受信側の「実行を要求」と対になる（§26-10）。
            logger.info("%sしました", target.summary)
        except ClientError as e:
            # 404・400・502・接続不可のいずれもここ。落とさない。
            hint = self._hint(target, e.status)
            if e.status is None:
                key, label = ("exec", hint), f"実行（{hint}）"
            else:
                key, label = ("exec", hint, target.summary), f"{target.summary}（{hint}）"
            suppressed = self._limiter.take(key, label)
            if suppressed is None:
                logger.info("%sできませんでした（%s・間引き中）— %s", target.summary, hint, e)
            else:
                logger.warning("%sできませんでした（%s）— %s%s", target.summary, hint, e, suffix(suppressed))
        return False
