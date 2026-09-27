"""main: 赤外線アドオンのエントリポイント。

M3-b で「受信 → NECデコード → 解釈（learned / 導出）→ 実行」を通した。やることは4つ:
  1. **プリセット表を検証する（破れていたら起動を止める）**——実在リモコンと衝突しうる
     コードを製品が出し続けるより、上がらないほうがましだから
  2. 起動時に data/status.json を書く（本体WebUI契約・install.sh の起動確認もこれを見る）
  3. 受信スレッドを回す（/dev/lircN を直接読む。役割は features で判定）
  4. SIGTERM/SIGINT を受けるまで常駐し、status に差分が出たときだけ書き直す

スレッド構成（M3で :8100 の設定UIを同居させる前提。receiver.py のdocstringも参照）:
    メインスレッド  status.json のループ＋シグナル処理
    IrReceiver      受信ループ
    CommandFirer    本体APIの呼び出し
共有状態は RecentCodes（直近50件）と ConfigStore の2つだけ。どちらもロック付き。

ログの分岐（systemdユニットの StandardOutput/StandardError と対）:
  INFO 以下 → stdout → journal（RAM・揮発）… 受信コードはここ。未登録コードも出す
  WARNING 以上 → stderr → data/error_addon.log（永続・1MB超で .old へ回す。起動時は unit の
                 ExecStartPre、稼働中は errlog.RotatingStderrHandler）… API失敗等
"""
import logging
import signal
import sys
import threading

from . import codes, config, errlog, lirc, sender, stats, webui
from .client import CommandFirer, EchoBridgeClient
from .receiver import IrReceiver
from .recent import RecentCodes
from .status import STATUS_FILE, build_status, read_version, write_status

logger = logging.getLogger(__name__)

# 状態チェック間隔（秒）。差分が出たときだけ書くので、変化の反映は最大この秒数だけ遅れる＝仕様。
_STATUS_INTERVAL = 60.0


def _setup_logging() -> errlog.RotatingStderrHandler:
    """INFO以下をstdout・WARNING以上をstderrへ振り分ける。

    logging.basicConfig の既定は全部stderr＝日常のINFOまで永続ログ(error_addon.log)へ
    流れ込み、1MB枠を平常運転で食い潰す。それを避けるためハンドラを2本に分ける。
    時刻は自前で載せる: journal は行に時刻を付けるが、StandardError=append: の
    生ファイルには付かないため、付けないと永続ログ側だけ時刻を失う。
    WARNING 側は稼働中も 1MB で回す（errlog.py・§6-2）。
    ★戻り値のハンドラを常駐ループが 60秒ごとに check_size() する（W19-6）。回転の契機を
    emit だけにすると、WARNING が1本も出ない機体で上限が事実上無くなる。
    """
    fmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = errlog.RotatingStderrHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(out)
    root.addHandler(err)
    return err


def _log_devices(rx_spec: str | None) -> None:
    """起動時に /dev/lirc* を1行ずつ残す（番号と役割の対応は機体・起動順で変わる）。

    RXが複数ある機体（GPIO + USBのIrdroid 等）では「どれを使うか」が本質的な情報なので、
    各行に選択の結果（← 使用 / 指定と不一致）まで書く。実際に開くのは受信スレッドだが、
    選択規則は同じ lirc.select_rx_device なので、ここの表示と食い違わない。
    """
    devices = lirc.find_devices()
    if not devices:
        logger.warning("/dev/lirc* が1つも見つからない（受信は始まらない）")
        return
    chosen = lirc.select_rx_device(devices, rx_spec)
    logger.info(
        "受信デバイスの指定: %s=%s",
        lirc.RX_DEVICE_ENV,
        rx_spec if rx_spec is not None else f"{lirc.AUTO}（受信できる最初の1台を使う）",
    )
    for d in devices:
        if d == chosen:
            mark = " ← 使用"
        elif d.can_rx and rx_spec is not None:
            mark = " （受信できるが指定と不一致）"
        else:
            mark = ""
        logger.info(
            "lircデバイス: %s features=0x%08x 役割=%s 名前=%s%s",
            d.path,
            d.features,
            d.role,
            d.label,
            mark,
        )
    if rx_spec is None:
        rx_count = sum(1 for d in devices if d.can_rx)
        if rx_count > 1:
            # ★RXが複数あるのに auto＝「受信できる最初の1台」で決まっている（W19-10）。
            #   どれが選ばれたかは上の INFO に出るが、それは journal（RAM・揮発）で消える。
            #   §12 が「別の受光素子で黙って受け続けるのは、受信できないより悪い」と
            #   定めている状態は、**アンインストール→再インストール**という正規の操作でも
            #   作られる（指定の置き場所 data/env はアンインストールで消える）ので、
            #   永続ログに1起動1行だけ残す。製品機（Irdroid 1台）では出ない。
            logger.warning(
                "受信できる lircデバイスが %d 台あるが %s=%s（最初の1台 %s を使う）"
                "— 別の受光素子を使う機体では data/env で %s を指定すること",
                rx_count,
                lirc.RX_DEVICE_ENV,
                lirc.AUTO,
                chosen.path if chosen is not None else "なし",
                lirc.RX_DEVICE_ENV,
            )
    # 指定が外れている場合、ここではWARNINGを出さない。受信スレッドが起動直後に
    # 「今どのデバイスが居るか」まで添えた同趣旨のWARNINGを出す（receiver._warn_no_device）ので、
    # 両方書くと永続ログ(error_addon.log)に毎起動2行の重複が残る。上のINFOに
    # 「受信できるが指定と不一致」が出ているので、journal 側の手がかりも失われない。


def _check_ir_ctl() -> None:
    """送信に使う ir-ctl の有無を起動時に確かめる（無ければ WARNING で永続ログに残す）。

    送信が要るのは M3(d) の学習UIだが、そこで初めて「送信できない」と分かるのでは遅い
    （顧客はボタンを押して無反応を見ることになる）。起動時に1行出しておけば、
    診断が `data/error_addon.log` だけで済む。
    ※ ir-ctl は v4l-utils に入っており、Raspberry Pi OS の既定イメージ（pi-gen stage2）に
      含まれる。それでも確かめるのは、顧客が最小構成で入れ直した機体もありうるため。
    """
    if sender.ir_ctl_path() is None:
        logger.warning(
            "%s が見つからない（赤外線の送信ができない＝学習UIのボタンが無反応になる）"
            "— パッケージ v4l-utils を導入すること",
            sender.IR_CTL,
        )


def main() -> None:
    err_handler = _setup_logging()

    # ★最初にプリセット表を検証する。破れていたら AssertionError で落ちる＝起動しない。
    #   設定ファイル由来の異常（WARNINGで既定へフォールバック）とは扱いを分けている:
    #   こちらはプログラムの誤りで、常駐を続けると実在リモコンと衝突しうるコードを
    #   顧客へ配り続けることになる。理由は codes.validate_presets のdocstring。
    codes.validate_presets()

    version = read_version()
    if version == "unknown":
        # 本体はversion="unknown"のカードを描画しない＝アドオンが存在しないように見える。
        logger.warning("VERSIONファイルを読めず version=unknown（本体はこのカードを描画しない）")
    logger.info("起動: service=%s version=%s", build_status(version)["service"], version)

    rx_spec = lirc.rx_spec()
    _log_devices(rx_spec)
    _check_ir_ctl()

    cfg = config.load()
    store = config.ConfigStore(cfg)
    preset = codes.preset_number(cfg.base)
    logger.info(
        "設定: プリセット%s（base=0x%04x） debounce=%dms 学習コード %d件（%s / %s）",
        preset if preset is not None else "外（表に無い値）",
        cfg.base,
        cfg.debounce_ms,
        len(cfg.learned),
        config.SETTINGS_FILE.name,
        config.LEARNED_FILE.name,
    )

    stop = threading.Event()

    def _on_signal(signum, _frame):
        logger.info("シグナル受信(%s): 停止する", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    recent = RecentCodes()  # M3の設定UIが同一プロセスから読む共有状態
    counters = stats.Counters()  # 起動からの累計（診断レポートの計器・RAMのみ）
    client = EchoBridgeClient()
    logger.info("本体API: %s", client.base_url)
    firer = CommandFirer(client)
    firer.start()
    receiver = IrReceiver(store, recent, firer, rx_spec=rx_spec, counters=counters)
    receiver.start(stop)

    # 設定UI（M3(d)）。受信・送信と同じプロセスで、共有状態は ConfigStore と RecentCodes だけ。
    # ★開けなくても常駐は続ける（webui.start が WARNING を出して None を返す）。
    ir_sender = sender.IrSender(store, receiver, rx_spec=rx_spec, counters=counters)
    app = webui.UiApp(store, recent, ir_sender, client, rx_spec=rx_spec, counters=counters)
    ui = webui.start(app)
    # /diagnostics に出すポートを**実際に bind できた値**に揃える（W19-13。定数を出していた）。
    app.set_ui_port(webui.config_port(ui))

    # 初回は差分に関係なく必ず書く。install.sh の起動確認が「マーカーより新しい status.json」で
    # 起動完了を判定するため、再起動のたびにmtimeが動く必要がある（内容が前回と同一でも）。
    # config_port は UI が立っているときだけ書かれる（webui.config_port）。
    last = build_status(version, config_port=webui.config_port(ui))
    try:
        write_status(last)
        logger.info("status.json書き出し: %s", STATUS_FILE)
    except OSError as e:
        # ここが失敗しても終了しない: exit→Restart=on-failure の再起動ループより、
        # 常駐したまま次周で再試行するほうが復旧しやすい（原因は概ねdata/の権限＝人が直すもの）。
        # 失敗はWARNING＝error_addon.log に残り、install.sh は起動確認タイムアウトで落ちる。
        logger.warning("status.json初回書き出しに失敗（常駐は継続・次周で再試行）: %s", e)
        last = None  # 未書き込み扱い

    # 常駐ループ。stop.wait はシグナルで即座に抜ける（sleepと違い待ち時間を取りこぼさない）。
    while not stop.wait(_STATUS_INTERVAL):
        # ★永続ログの回転を emit 以外の契機でも起こす（W19-6）。fd 2 には logging を
        #   通らない書き込みも入りうるので、emit だけを契機にすると WARNING が1本も出ない
        #   機体で上限が事実上無くなる。サイズを見るだけ＝SDへの書き込みは増えない。
        err_handler.check_size()

        _watchdog(receiver, firer, stop, ui)

        current = build_status(version, config_port=webui.config_port(ui))
        if current == last:
            continue
        try:
            write_status(current)
            last = current  # 書けた時だけ更新＝失敗は次周で再試行される
        except OSError as e:
            logger.warning("status.json書き出しに失敗（継続）: %s", e)

    _shutdown(receiver, firer, ui)
    logger.info("停止完了")


def _watchdog(receiver, firer, stop, ui) -> None:
    """スレッドが死んでいたら1行残して**非0で終了**する（W19-14）。常駐ループから60秒ごと。

    受信スレッドも CommandFirer も死んでいることを誰も見ていなかった。死んでも status.json は
    更新され続け、本体カードは「稼働中」のまま——赤外線だけが無症状で効かなくなる。
    ★常駐を続けても機能は戻らない（復旧は再起動だけ）ので、**死んだまま稼働中に見せない**。
    ERROR を1行だけ出して終了し、systemd に起動し直させる（unit は Restart=on-failure・
    RestartSec=5）。1行なので永続ログの1MB枠には影響しない。
    ここへ来る前に両スレッドは catch-all で包んであるので、到達するのは catch-all でも
    受け切れなかったとき（MemoryError・再入不能な状態）だけである。
    """
    dead = [name for name, alive in (("受信", receiver.is_alive()), ("実行", firer.is_alive())) if not alive]
    if not dead:
        return
    logger.error(
        "%sスレッドが停止している（稼働を続けても赤外線が働かないので、"
        "終了して systemd に起動し直させる）",
        "と".join(dead),
    )
    stop.set()
    _shutdown(receiver, firer, ui)
    sys.exit(1)


def _shutdown(receiver, firer, ui) -> None:
    """停止処理。**stop が立っている前提**で呼ぶ（receiver.join が待ちっぱなしにならない）。

    firer.stop() は「終了処理のため N 件の実行を破棄した」と「停止までに間引いた WARNING」を
    残す（W18）。異常終了の経路でもこの2行を捨てないので、こちらも同じ関数を通す。
    """
    receiver.join()
    firer.stop()
    if ui is not None:
        # serve_forever を抜けさせる（デーモンスレッドなので待たないが、開いている接続を畳む）。
        ui.shutdown()
        ui.server_close()


if __name__ == "__main__":
    main()
