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
  WARNING 以上 → stderr → data/error_addon.log（永続・起動時1MBでローテ）… API失敗等
"""
import logging
import signal
import sys
import threading

from . import codes, config, lirc, sender, stats, webui
from .client import CommandFirer, EchoBridgeClient
from .receiver import IrReceiver
from .recent import RecentCodes
from .status import STATUS_FILE, build_status, read_version, write_status

logger = logging.getLogger(__name__)

# 状態チェック間隔（秒）。差分が出たときだけ書くので、変化の反映は最大この秒数だけ遅れる＝仕様。
_STATUS_INTERVAL = 60.0


def _setup_logging() -> None:
    """INFO以下をstdout・WARNING以上をstderrへ振り分ける。

    logging.basicConfig の既定は全部stderr＝日常のINFOまで永続ログ(error_addon.log)へ
    流れ込み、1MB枠を平常運転で食い潰す。それを避けるためハンドラを2本に分ける。
    時刻は自前で載せる: journal は行に時刻を付けるが、StandardError=append: の
    生ファイルには付かないため、付けないと永続ログ側だけ時刻を失う。
    """
    fmt = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(out)
    root.addHandler(err)


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
    _setup_logging()

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
    ui = webui.start(
        webui.UiApp(store, recent, ir_sender, client, rx_spec=rx_spec, counters=counters)
    )

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
        current = build_status(version, config_port=webui.config_port(ui))
        if current == last:
            continue
        try:
            write_status(current)
            last = current  # 書けた時だけ更新＝失敗は次周で再試行される
        except OSError as e:
            logger.warning("status.json書き出しに失敗（継続）: %s", e)

    receiver.join()
    firer.stop()
    if ui is not None:
        # serve_forever を抜けさせる（デーモンスレッドなので待たないが、開いている接続を畳む）。
        ui.shutdown()
        ui.server_close()
    logger.info("停止完了")


if __name__ == "__main__":
    main()
