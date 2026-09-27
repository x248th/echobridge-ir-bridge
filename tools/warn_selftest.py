#!/usr/bin/env python3
"""warn_selftest: WARNING の間引き（W18 R-1・R-2）・debounce_ms の下限（R-6）と、
**スレッドが死なない・死んだまま「稼働中」に見せない**（W19-1・W19-2・W19-14）の自己テスト。

実機・本体API なしで走る（本体クライアントは偽物・時計は差し替え）。置き場所は tools/isolation.py が
一時ディレクトリへ逃がし、前後で本物の data/ と ~/addon-data/ が動いていないことを確かめる。

    python3 tools/warn_selftest.py    # 全件PASSなら rc=0
"""
import http.client
import json
import logging
import queue
import sys
import threading
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import isolation  # noqa: E402  ← ir_bridge より先に（置き場所ごと一時ディレクトリへ逃がす・前後で本物を見張る）

from ir_bridge import config, lirc, main as main_mod, receiver, warnlimit  # noqa: E402
from ir_bridge.client import ClientError, CommandFirer, EchoBridgeClient  # noqa: E402
from ir_bridge.codes import LightTarget, SceneTarget  # noqa: E402

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def of(self, level):
        return [m for lv, m in self.records if lv == level]

    def __enter__(self):
        root = logging.getLogger()
        self._saved = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self)
        logging.getLogger().setLevel(self._saved)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FailingClient:
    """呼ばれるたびに失敗する本体クライアント。status=None は接続不可。"""

    def __init__(self, status=None):
        self.status = status
        self.calls = 0

    def fire_scene(self, key):
        self.calls += 1
        raise ClientError(f"失敗 {key}", status=self.status)

    def set_brightness(self, instance, value):
        self.calls += 1
        raise ClientError(f"失敗 {instance}", status=self.status)


def run_queue(firer, targets):
    """ワーカースレッドを立てずに、キューの中身を同じスレッドで処理させる。"""
    for t in targets:  # キューは8件までなので1件ずつ流す
        firer._q.put_nowait((t, "nec32:0x00000000"))
        firer._q.put_nowait(None)
        firer._run()


# 壁時計の基準（表示だけに使う）。ローカル時刻で組むので、TZ によらず期待文字列が決まる。
WALL0 = time.mktime((2026, 9, 1, 12, 0, 0, 0, 0, -1))


def limiter_section() -> None:
    print("■ WarnLimiter: 理由ごとに10分に1行・間引いた件数は次の1行に添える")
    check("窓は receiver._WarnThrottle と同じ10分（値を共有）",
          warnlimit.WARN_REPEAT_SEC == receiver.WARN_REPEAT_SEC == 600.0)
    clock = Clock()
    wall = Clock(WALL0)
    lim = warnlimit.WarnLimiter(clock=clock, wall=wall)
    check("初回は出す（0件）", lim.take("a") == (0, None))
    check("窓の内側は間引く", lim.take("a") is None and lim.take("a") is None)
    check("別の理由は独立に初回を出す", lim.take("b") == (0, None))
    clock.t += 599.9
    wall.t += 300  # 壁時計は monotonic と独立に動かす（判定が壁時計に引きずられないことも見る）
    check("599.9秒後はまだ間引く", lim.take("a") is None)
    clock.t += 0.2
    wall.t += 30 * 86400  # 窓の判定は monotonic だけ。壁時計が何日飛んでも結果は変わらない
    got = lim.take("a")
    check("600秒を過ぎたら出し、間引いた件数（3件）と最後に数えた時刻を返す",
          got == (3, WALL0 + 300), str(got))
    check("  出したら数え直す（時刻も）",
          lim.take("a") is None and lim.drain() == [("a", 1, WALL0 + 300 + 30 * 86400)])
    check("drain の後は0件", lim.drain() == [])
    check("添える文言（件数と最後の時刻）",
          warnlimit.suffix(got) == "（この間に同じ失敗が 3 件・最後は 2026-09-01 12:05）"
          and warnlimit.suffix(warnlimit.Released(0, None)) == "",
          warnlimit.suffix(got))
    print("■ 最後の時刻は「最後に数えた」時刻（失敗が止んでから行が書かれるまでの間を行に含めない）")
    clock, wall = Clock(), Clock(WALL0)
    lim = warnlimit.WarnLimiter(clock=clock, wall=wall)
    lim.take("x")
    wall.t += 60
    lim.take("x")  # 12:01 に間引いた1件
    clock.t += 21 * 86400
    wall.t += 21 * 86400  # 3週間後に同じ失敗
    check("3週間後の窓明けの行は「最後は 12:01」（3週間後の時刻ではない）",
          warnlimit.suffix(lim.take("x")) == "（この間に同じ失敗が 1 件・最後は 2026-09-01 12:01）")
    small = warnlimit.WarnLimiter(clock=clock, max_keys=3)
    for k in range(10):
        small.take(k)
    check("覚える理由の数に上限がある（際限なく育てない）", len(small._state) <= 3, str(len(small._state)))


def firer_section() -> None:
    print("■ R-1 本体に繋がらない間の実行失敗: 理由ごとに間引き、件数は失わない")
    clock = Clock()
    wall = Clock(WALL0)
    firer = CommandFirer(FailingClient(status=None))
    firer._limiter = warnlimit.WarnLimiter(clock=clock, wall=wall)
    presses = [SceneTarget("scene_01")] * 5 + [LightTarget(3, 50)] * 5  # 長押し相当の連続
    with _Capture() as cap:
        run_queue(firer, presses)
    warns = cap.of("WARNING")
    check("10回失敗しても WARNING は1行（接続不可は宛先によらず1つの理由）", len(warns) == 1, str(warns))
    check("  間引いた9回は INFO（journal）に1回ずつ残る",
          len([m for m in cap.of("INFO") if "間引き中" in m]) == 9, str(cap.of("INFO")))
    clock.t += 601
    with _Capture() as cap:
        run_queue(firer, [SceneTarget("scene_02")])
    warns = cap.of("WARNING")
    check("10分後の次の1行に「（この間に同じ失敗が 9 件・最後は …）」",
          len(warns) == 1 and warns[0].endswith("（この間に同じ失敗が 9 件・最後は 2026-09-01 12:00）"), str(warns))

    print("■ R-1 HTTP の失敗（404 等）は宛先ごとの理由")
    clock = Clock()
    wall = Clock(WALL0)
    firer = CommandFirer(FailingClient(status=404))
    firer._limiter = warnlimit.WarnLimiter(clock=clock, wall=wall)
    with _Capture() as cap:
        run_queue(firer, [SceneTarget("scene_98")] * 3)
        wall.t += 120
        run_queue(firer, [SceneTarget("scene_99")] * 3)
    warns = cap.of("WARNING")
    check("scene_98 と scene_99 はそれぞれ1行（どれが 404 かを失わない）",
          len(warns) == 2 and "未知のシーン" in warns[0] and "scene_98" in warns[0] and "scene_99" in warns[1],
          str(warns))

    print("■ R-1 停止時に、まだ添える機会の無い件数を1行にまとめて出す（理由ごとに最後の時刻付き）")
    clock.t += 30 * 86400
    wall.t += 30 * 86400  # 失敗が止んでから1か月後に停止
    with _Capture() as cap:
        firer.stop()
    warns = cap.of("WARNING")
    check("停止時の回収は WARNING 1行（journal は次の起動で消えるため）",
          len(warns) == 1 and "停止までに間引いた WARNING" in warns[0], str(warns))
    check("  理由ごとの件数が載る（各2件）", warns and warns[0].count("2 件") == 2, str(warns))
    check("  理由ごとに最後に数えた時刻が載る（停止した1か月後の時刻ではない）",
          warns and "シーン scene_98 を発火（未知のシーン） 2 件（最後は 2026-09-01 12:00）" in warns[0]
          and "シーン scene_99 を発火（未知のシーン） 2 件（最後は 2026-09-01 12:02）" in warns[0], str(warns))
    with _Capture() as cap:
        CommandFirer(FailingClient()).stop()
    check("間引いたものが無ければ停止時に何も出さない", cap.of("WARNING") == [], str(cap.records))

    print("■ R-1 実行キュー満杯の破棄も間引く")
    clock = Clock()
    firer = CommandFirer(FailingClient())
    firer._limiter = warnlimit.WarnLimiter(clock=clock)
    for _ in range(CommandFirer.QUEUE_MAX):
        firer.submit(SceneTarget("scene_01"), "c")
    with _Capture() as cap:
        results = [firer.submit(SceneTarget("scene_01"), "c") for _ in range(20)]
    check("満杯の20回はすべて破棄（False）", results == [False] * 20)
    check("  WARNING は1行・残り19回は INFO",
          len(cap.of("WARNING")) == 1 and len([m for m in cap.of("INFO") if "間引き中" in m]) == 19, str(cap.records[:3]))
    clock.t += 601
    with _Capture() as cap:
        firer.submit(SceneTarget("scene_01"), "c")
    check("  10分後の次の1行に19件を添える", cap.of("WARNING") and "19 件" in cap.of("WARNING")[0], str(cap.of("WARNING")))


def probe_section() -> None:
    print("■ R-2 lirc.probe: 開けない・features が読めない状態が続いても理由ごとに10分に1行")
    clock = Clock()
    saved = lirc._probe_warn
    lirc._probe_warn = warnlimit.WarnLimiter(clock=clock)
    try:
        missing = str(isolation.TMP / "lirc9")  # 存在しない＝open に失敗する
        not_lirc = isolation.TMP / "not-a-lirc"
        not_lirc.write_bytes(b"")  # 開けるが LIRC_GET_FEATURES は通らない（ENOTTY）
        with _Capture() as cap:
            for _ in range(12):  # 受信スレッドの5秒ごとの再探索1分ぶん
                lirc.probe(missing)
                lirc.probe(str(not_lirc))
        warns = cap.of("WARNING")
        check("24回失敗しても WARNING は理由ごとに1行（計2行）", len(warns) == 2, str(warns))
        check("  開けない／features の文面は従来どおり",
              any("lircデバイスを開けない" in m for m in warns) and any("LIRC_GET_FEATURES" in m for m in warns))
        check("  間引いた回は DEBUG（5秒ごとなので journal にも出さない）",
              cap.of("INFO") == [] and len(cap.of("DEBUG")) == 22, str(len(cap.of("DEBUG"))))
        clock.t += 601
        with _Capture() as cap:
            lirc.probe(missing)
        check("10分後の次の1行に11件を添える",
              cap.of("WARNING") and "（この間に同じ失敗が 11 件・最後は " in cap.of("WARNING")[0], str(cap.of("WARNING")))
    finally:
        lirc._probe_warn = saved


def debounce_section() -> None:
    print("■ R-6 debounce_ms の下限 250ms（NEC のフレーム周期 約110ms の2周期＋余裕）")
    check("下限は 250ms・既定 1000ms は据え置き", config.MIN_DEBOUNCE_MS == 250 and config.DEFAULT_DEBOUNCE_MS == 1000)
    for raw, want, warned in [(0, 250, True), (110, 250, True), (249, 250, True), (250, 250, False),
                              (700, 700, False), (-1, 1000, True), (True, 1000, True), ("500", 1000, True)]:
        lost = []
        got = config._parse_debounce(raw, lost)
        check(f"{raw!r} → {want}{'（WARNING）' if warned else ''}", got == want and bool(lost) == warned, f"{got} {lost}")
    print("■ R-6 ファイルに下限未満が書いてあれば、切り上げて起動し元の中身を保全する（W18 の保全と同じ経路）")
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    original = (json.dumps({"schema_version": 1, "base": "0x7d2e", "debounce_ms": 0, "brightness_lists": {}}) + "\n").encode()
    config.SETTINGS_FILE.write_bytes(original)
    config.LEARNED_FILE.write_bytes(b'{"schema_version": 1, "entries": {}}\n')
    with _Capture() as cap:
        cfg = config.load()
    check("250ms で起動", cfg.debounce_ms == 250)
    check("  WARNING は項目1行＋まとめ1行", len(cap.of("WARNING")) == 2, str(cap.of("WARNING")))
    check("  元の中身は .unreadable に・元のファイルはそのまま",
          (config.CONFIG_DIR / "settings.json.unreadable").read_bytes() == original
          and config.SETTINGS_FILE.read_bytes() == original)
    print("■ debounce_ms は設定画面から変えられない（事実の固定）")
    src = (ROOT / "ir_bridge" / "webui.py").read_text(encoding="utf-8")
    # ★見るのは「経路が無いこと」。語そのものの不在にすると、コメントで言及しただけで落ちる。
    check("webui.py に debounce の経路が無い（手で settings.json を書き、再起動するしかない）",
          "debounce_ms" not in src and "/api/debounce" not in src)


# =============================================================================
# W19-1・W19-2・W19-14: スレッドが死なない／死んだまま「稼働中」に見せない
# =============================================================================
class _Boom(Exception):
    """ClientError でも OSError でもない例外（catch-all だけが受けられる）。"""


class ExplodingClient:
    """呼ばれるたびに ClientError 以外の例外を投げる本体クライアント。"""

    def __init__(self):
        self.calls = 0

    def fire_scene(self, key):
        self.calls += 1
        raise _Boom(f"想定外 {key}")

    def set_brightness(self, instance, value):
        self.calls += 1
        raise _Boom(f"想定外 {instance}")


def _wait(pred, timeout=3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def client_exception_section() -> None:
    """W19-1: `_get` は ClientError 以外を外へ出さない（旧実装では素通りしてスレッドが死んだ）。"""
    print("■ W19-1 本体APIの失敗は**すべて** ClientError に包む（スレッドの外へ出さない）")

    cl = EchoBridgeClient(base_url="http://127.0.0.1:1")

    def raises(exc):
        def _fake(url, timeout=None):
            raise exc
        return _fake

    import urllib.request as _ur

    saved = _ur.urlopen
    try:
        # ★http.client.HTTPException は OSError の派生では**ない**（旧実装の捕捉は
        #   (URLError, OSError) だけだったので、これらはそのまま抜けていた）。
        for exc in (
            http.client.IncompleteRead(b"abc"),
            http.client.BadStatusLine("\r\n"),
            http.client.LineTooLong("header line"),
        ):
            check(f"  {type(exc).__name__} は OSError の派生ではない（事実の固定）",
                  not issubclass(type(exc), OSError))
            _ur.urlopen = raises(exc)
            try:
                cl.fire_scene("scene_01")
                got = None
            except ClientError as e:
                got = e
            except Exception as e:  # noqa: BLE001
                got = e
            check(f"  {type(exc).__name__} → ClientError", isinstance(got, ClientError), repr(got))
            check("    status は None（接続失敗の扱い）", isinstance(got, ClientError) and got.status is None)
        # RemoteDisconnected は ConnectionResetError 派生なので旧実装でも捕まっていた（回帰の固定）。
        _ur.urlopen = raises(http.client.RemoteDisconnected("closed"))
        try:
            cl.fire_scene("scene_01")
            got = None
        except ClientError as e:
            got = e
        check("  RemoteDisconnected → ClientError（従来どおり）", isinstance(got, ClientError))
        # URLError・OSError は従来どおり。
        _ur.urlopen = raises(urllib.error.URLError("unreachable"))
        try:
            cl.fire_scene("scene_01")
            got = None
        except ClientError as e:
            got = e
        check("  URLError → ClientError（従来どおり）", isinstance(got, ClientError))
    finally:
        _ur.urlopen = saved

    # ★ECHOBRIDGE_URL を書き損じると urlopen が ValueError を投げる（data/env の打ち間違い）。
    bad = EchoBridgeClient(base_url="notascheme")
    try:
        bad.get_scenes()
        got = None
    except ClientError as e:
        got = e
    except Exception as e:  # noqa: BLE001
        got = e
    check("スキームの無い ECHOBRIDGE_URL（ValueError）→ ClientError", isinstance(got, ClientError), repr(got))


def worker_survives_section() -> None:
    """W19-1・W19-14: ワーカーは1件の失敗で死なない。"""
    print("■ W19-1・W19-14 実行ワーカーは想定外の例外で死なない（1件の失敗で常駐機能を失わない）")
    client = ExplodingClient()
    firer = CommandFirer(client)
    with _Capture() as cap:
        firer.start()
        firer.submit(SceneTarget("scene_01"), "c1")
        check("  1件目が処理された", _wait(lambda: client.calls >= 1))
        check("  ★スレッドは生きている（旧実装ではここで死んだ）", firer._thread.is_alive())
        check("  is_alive() も True", firer.is_alive())
        # 続けて投げても処理され続ける＝機能が恒久停止しない。
        for i in range(5):
            firer.submit(SceneTarget(f"scene_0{i}"), f"c{i}")
        check("  以後も処理し続ける", _wait(lambda: client.calls >= 6), str(client.calls))
        firer.stop(timeout=1.0)
    # 停止時の回収行（「停止までに間引いた WARNING: …」）は別の記録なので数から外す。
    warns = [m for m in cap.of("WARNING") if "想定外" in m and "停止までに" not in m]
    infos = [m for m in cap.of("INFO") if "想定外" in m]
    check("  WARNING は間引かれて1行（6件の失敗に対して）", len(warns) == 1, str(warns))
    check("  残りは INFO（journal に証跡を残す）", len(infos) == 5, str(len(infos)))
    check("  間引いた5件は停止時に回収される（件数を失わない）",
          any("停止までに間引いた WARNING" in m and "5 件" in m for m in cap.of("WARNING")),
          str(cap.of("WARNING")))
    check("  停止後はスレッドが終わっている", not firer._thread.is_alive())


def stop_sentinel_section() -> None:
    """W19-2: ワーカーが死んでキューが満杯でも stop() が返り、2つの記録が必ず出る。"""
    print("■ W19-2 ワーカーが死んでキューが満杯でも stop() は返り、停止時の記録が残る")
    firer = CommandFirer(FailingClient(status=404))
    # ワーカーを起動して**すぐ殺した**状態を作る（_thread.ident は立つが誰も取り出さない）。
    firer._thread.start()
    firer._stopped.set()
    firer._q.put_nowait(None)
    firer._thread.join(timeout=2.0)
    check("  前提: ワーカーは居ない", not firer._thread.is_alive())
    check("  is_alive() は False（見張りが気づける）", not firer.is_alive())
    firer._stopped.clear()
    # キューを満杯にする（取り出す者が居ない）。
    while True:
        try:
            firer._q.put_nowait((SceneTarget("scene_07"), "c"))
        except queue.Full:
            break
    check("  前提: キューは満杯", firer._q.full())
    # 間引かれた件数も残しておく（停止時の回収が出ることを見る）。
    firer._limiter.take(("exec", "本体に接続できない"), "実行（本体に接続できない）")
    firer._limiter.take(("exec", "本体に接続できない"), "実行（本体に接続できない）")

    done = threading.Event()
    with _Capture() as cap:
        threading.Thread(target=lambda: (firer.stop(timeout=0.5), done.set()), daemon=True).start()
        returned = done.wait(5.0)
    check("  ★stop() が返る（旧実装は put(None) で永久に返らなかった）", returned)
    warns = cap.of("WARNING")
    check("  記録1: 破棄した件数が1行出る", any("実行を破棄した" in m for m in warns), str(warns))
    check("  記録2: 間引いた WARNING の回収が1行出る",
          any("停止までに間引いた WARNING" in m for m in warns), str(warns))


def receiver_survives_section() -> None:
    """W19-14: 受信スレッドは1イベントの失敗で死なない。"""
    print("■ W19-14 受信スレッドは想定外の例外で死なない（_feed の失敗で読み続ける）")

    class _Store:
        def get(self):
            return config.default_config()

    rx = receiver.IrReceiver(_Store(), recent=None, firer=None)
    calls = []

    def boom(device, kind, usec):
        calls.append((kind, usec))
        raise _Boom("受信イベントの処理が壊れた")

    rx._feed = boom
    with _Capture() as cap:
        for i in range(4):
            rx._feed_guarded(None, 0, 100 + i)
    check("  ★4イベントとも処理を試みた（1件目で止まらない）", len(calls) == 4, str(calls))
    warns = [m for m in cap.of("WARNING") if "想定外" in m]
    infos = [m for m in cap.of("INFO") if "想定外" in m]
    check("  WARNING は間引かれて1行", len(warns) == 1, str(warns))
    check("  残り3件は INFO", len(infos) == 3, str(len(infos)))

    print("■ W19-14 _run も catch-all で包む（_cycle が投げてもループを抜けない）")
    rx2 = receiver.IrReceiver(_Store(), recent=None, firer=None)
    cycles = []

    def boom_cycle(stop):
        cycles.append(1)
        if len(cycles) >= 3:
            stop.set()
            return True
        raise _Boom("デバイス探索が壊れた")

    rx2._cycle = boom_cycle
    stop = threading.Event()
    rx2._stop = stop
    saved_retry = receiver.RETRY_SEC
    receiver.RETRY_SEC = 0.01
    try:
        with _Capture() as cap:
            rx2._run()
    finally:
        receiver.RETRY_SEC = saved_retry
    check("  ★3周とも回った（旧実装は1周目で例外がスレッドの外へ抜けた）", len(cycles) == 3, str(len(cycles)))
    check("  「受信スレッド停止」が出る（例外で終わると出ない行）",
          any("受信スレッド停止" in m for m in cap.of("INFO")), str(cap.of("INFO")))
    check("  間引いた件数を停止時に回収する",
          any("停止までに間引いた WARNING" in m for m in cap.of("WARNING")), str(cap.of("WARNING")))


def watchdog_section() -> None:
    """W19-14: 死んだスレッドを常駐ループが見つけ、ERROR 1行を出して非0で終了する。"""
    print("■ W19-14 死んだまま「稼働中」に見せない（常駐ループの見張り）")

    class _Thread:
        def __init__(self, alive):
            self.alive = alive

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            return None

        def stop(self, timeout=None):
            return None

    stop = threading.Event()
    with _Capture() as cap:
        main_mod._watchdog(_Thread(True), _Thread(True), stop, None)
    check("  両方生きていれば何も出さない・終了しない", cap.records == [] and not stop.is_set(), str(cap.records))

    for label, alive_rx, alive_fire, want in (
        ("受信", False, True, "受信"),
        ("実行", True, False, "実行"),
        ("両方", False, False, "受信と実行"),
    ):
        stop = threading.Event()
        with _Capture() as cap:
            try:
                main_mod._watchdog(_Thread(alive_rx), _Thread(alive_fire), stop, None)
                code = None
            except SystemExit as e:
                code = e.code
        check(f"  {label}が死んでいたら非0で終了する（systemd が起動し直す）", code == 1, repr(code))
        errs = [m for lv, m in cap.records if lv == "ERROR"]
        check("    ERROR は1行だけ（永続ログの1MB枠を食わない）", len(errs) == 1, str(errs))
        check(f"    どちらが死んだかを書く（{want}）", errs and want in errs[0], str(errs))
        check("    stop を立ててから畳む（receiver.join が待ちっぱなしにならない）", stop.is_set())


def main() -> int:
    limiter_section()
    firer_section()
    probe_section()
    debounce_section()
    client_exception_section()
    worker_survives_section()
    stop_sentinel_section()
    receiver_survives_section()
    watchdog_section()
    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(isolation.run(main))
