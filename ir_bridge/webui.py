"""webui: 設定UI（:8100）。M3(d)。

同一プロセス内に立てる（受信スレッド・CommandFirer と同居）。共有状態は ConfigStore と
RecentCodes で、どちらもロック付き。**5秒ごとのRXデバイス監視ループには責務を足さない。**

■ ★認証を付けていない（付け忘れではない。足す前にここを読むこと）
  - 触れるのは IRコードの表示・プリセット選択・受信ログで、守る範囲は :8099
    （同一LANから認証なしで照明を操作できる）と同程度である。ここだけ固くしても意味が薄い。
  - Basic 認証にすると別オリジンなので、本体管理画面から飛んだときに再入力になる
    ＝顧客は同じパスワードを2回入れることになる。
  - プロキシで1回に畳むには**本体側に転送処理を入れる**ことになる＝本体の改修が要り
    （CLAUDE.md §1「本体不可侵」）、失敗の切り分けも1段増える。
    ※ここには以前「本体は（ある版で）凍結中」という但し書きが付いていたが、**その版数は
      事実ではなかった**ので根拠から落とした（W19-13。正しい凍結の版と日付、およびその後の
      版の話は DEV_LOG の W19 起票の手当てを見ること）。判断そのものは版数に依らない
      ——本体を触らずに済むかどうかだけが理由なので、判断は変えていない。
  - 引き受けるリスク: **受信ログから生活パターンが読める**（同一LAN内の相手が前提）。
    これを承知のうえでの判断である。
  - ★**`/diagnostics` で learned と受信ログが1か所に集約されるぶん、個別画面より性質が重い**
    （スクリプトで定期取得すれば生活パターンを継続的に集められる）。判断は変えていない——
    ここに到達できる相手は同一LAN内で、その相手は :8099 で照明を操作できるので、
    集約の度合いが上がっても**守れる範囲は増えない**。**認証を再検討するならここが判断材料になる。**

■ ★入口の検査は足したが、認証は足していない（W19-4・W19-7・W19-8）
上の判断（認証を付けない）は変えていない。塞いだのは、**§21 が引き受けたリスクの外**にある3つ:
  - 本文の大きさ（Content-Length の上限 MAX_BODY_BYTES）——上限が無いと LAN の誰でも
    要求1つで MemoryMax=64M を越えさせられる（実測: 80MiB の本文で maxRSS 183MiB）。
  - 別オリジンからの POST（Sec-Fetch-Site / Origin / Content-Type の検査）——CSRF の相手は
    **LAN の外**に居るので、「ここに到達できる相手は :8099 も叩ける」という §21 の前提が
    そもそも成り立たない。顧客が悪意あるページを開くだけでプリセットを壊せる。
  - 接続の掴みっぱなし（ソケットのタイムアウトと同時接続数の上限）。
どれも顧客に入力を求めない＝管理画面から飛んだときの再入力は生まれない。

■ ★アクセスログを永続ログへ流さない
`http.server` の既定は stderr にアクセスログを出す。stderr は
`data/error_addon.log`（WARNING以上・1MB で .old へ回す）へ行くので、**2秒ポーリングの
UIを開きっぱなしにすると一晩で枠を食い潰す**（M2-D で踏んだのと同じ形）。
`log_message` を潰して DEBUG へ落としてある。

■ 表示名をキャッシュしない
シーン名・照明名は**毎回** :8099 から取る。公式アプリでの改名をそのまま映すため。
一覧を持たない方針（CLAUDE.md §17）とも揃う。
"""
import json
import logging
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import codes, config, diagnostics, lirc
from .client import ClientError
from .codes import LightTarget, SceneTarget
from .nec import bytes_to_code, code_to_bytes
from .sender import (
    REASON_BUSY,
    REASON_CANNOT_TX,
    REASON_NO_DEVICE,
    REASON_NO_IR_CTL,
    REASON_NO_RESPONSE,
    ir_ctl_path,
)
from .stats import Counters
from .warnlimit import WarnLimiter, suffix

logger = logging.getLogger(__name__)

PORT = 8100
# 未記載の照明に出す既定の輝度リスト（＝学習させる行）。**輝度の既定はこの1つだけ**
# （M3-b の config.DEFAULT_BRIGHTNESS_LIST は M3-d で削除した。値の違う既定が2か所にあると、
# 次に読む人がどちらが正か調べ直すことになる）。顧客が最初に見る行数を絞る意図で 0% と 100%。
DEFAULT_UI_BRIGHTNESS = [0, 100]

# ■ 入口の上限（W19-4・W19-5・W19-8）。どれも「実用に足りる値」で、顧客の操作は当たらない。
# 要求本文の上限。いまの最大は learned 1件（コード＋シーンkey＋名前）で数百バイト。
MAX_BODY_BYTES = 64 * 1024
# 1接続の読み書きのタイムアウト（秒）。ヘッダを送り終えない接続を永久に掴まない
# （スリープした端末・切れた Wi-Fi でも起きる。悪意は要らない）。
CONN_TIMEOUT_SEC = 10.0
# 同時接続数の上限。1接続＝1スレッドなので、上限が無いとスタックと受信バッファで
# MemoryMax=64M の天井に当たる。UI の要求は軽いので 32 で足りる。
MAX_CONNECTIONS = 32
# learned の件数と文字列長の上限。顧客が手で登録するのは数十件で、名前は「テレビのリモコン 赤」程度。
MAX_LEARNED = 256
MAX_LABEL_LEN = 128
MAX_SCENE_LEN = 128


def _is_int(value) -> bool:
    """真偽値を int として受けない（`isinstance(True, int)` は真・W19-11）。

    config._parse_debounce は最初からこれを弾いていたのに、輝度の経路だけ素通りしていた
    （同じ罠への対処が片側だけ）。素通りすると settings.json に `true` が残り、
    顧客の画面に `True%` という行が出る。
    """
    return isinstance(value, int) and not isinstance(value, bool)

def role_label(device) -> str:
    """顧客向けの受信デバイスの役割表記。

    ★**ドライバ名（`ir_toy` 等）は UI に出さない。** "Toy" は製品の画面にそぐわないうえ、
    顧客には Irdroid のことだとも読めず、情報として機能していない。**顧客が知りたいのは
    「認識されているか」**なので役割だけ出す（認識されていなければ「見つかりません」と出る）。
    ドライバ名・デバイス名・lircデバイス一覧は `/diagnostics` に残してある——**宛先が違う**
    （UIは顧客向け、診断レポートは開発側向け）。切り分けが要るときは診断レポートで足りる。
    """
    if device.can_rx and device.can_tx:
        return "受信・送信対応"
    if device.can_rx:
        return "受信のみ"
    return "送信のみ"


def row_label(brightness: int, dimmable: bool) -> str:
    """輝度の行に出す表記。顧客がスマートリモコンのボタンに付ける名前になる。

    - `0%` は **OFF**。`brightness/0` は物理的な消灯（IR-11 で目視確認済み）で、顧客が
      作っているのはボタンなので「リビング OFF」が自然（「リビング 0%」は不自然）。
    - `100%` は**調光対応ならそのまま「100%」**。★「ON」と書かない——`/on`（前回の明るさで
      点灯）は使わない方針なので、この行は本当に全開である。「ON」と書くと顧客は前回値の
      復元を期待し、実際は全開になる（調光している顧客ほど気づく）。
    - 調光非対応は 0/100 しか意味を持たないので「OFF」「ON」。
    """
    if brightness == 0:
        return "OFF"
    if brightness == codes.MAX_BRIGHTNESS and not dimmable:
        return "ON"
    return f"{brightness}%"


def row_deletable(brightness: int, dimmable: bool) -> bool:
    """その行を顧客が消せるか。

    ★**OFF（0%）は全照明で消せない**。消せると「50% だけ残して OFF を消した」状態が作れ、
    顧客が赤外線から照明を消せなくなる。OFF を守ればリストが空にもならないので、
    「最後の1行は消せない」という別ルールは要らない（M3-d のレビューで1つに統合した）。
    調光非対応は OFF と ON の2行で固定＝どちらも消せない。
    """
    return brightness != 0 and dimmable


SEND_OK = "送信しました。スマートリモコン側で保存してください"
SEND_BUSY = (
    "本機以外の赤外線が飛び続けているため、送信を見送りました。"
    "他のリモコン等を操作していないことを確認してから、もう一度試してみてください。"
)
# ■H 失敗の文言は**顧客が次にすることを先に**書く。原因（sender の REASON_*＝ログと同じ語）は
# 後ろに「（原因: …）」として残す——問い合わせのときに顧客が読み上げられるように。
# ★sender の REASON_* を顧客向けに書き換えない（ログと診断は開発側が読む。宛先が違う）。
SEND_NO_DEVICE = (
    "赤外線デバイス（Irdroid）が見つかりません。"
    "Irdroid を本体の USB に挿し直してから、このページを再読み込みして、もう一度送信してください。"
)
# ★USB の抜き差しでしか戻らない（ir_toy は受信の途中で送信を始めると固まる＝CLAUDE.md §20。
#   ドライバのハングの可能性）。顧客向けの案内は R-4 の判断（「反応しなくなったら USB を挿し直す」）と揃える。
SEND_NO_RESPONSE = (
    "Irdroid が応答しなくなっています。"
    "Irdroid を USB から抜き、数秒待ってから挿し直してください。そのあと、もう一度送信してください。"
)
SEND_CANNOT_TX = (
    "いまつながっている赤外線デバイスは送信に対応していません。送信には Irdroid が必要です。"
    "Irdroid を本体の USB に挿してから、このページを再読み込みしてください。"
)
SEND_FAILED = (
    "送信できませんでした。Irdroid を USB から抜いて挿し直してから、もう一度送信してください。"
    "続く場合は、本体の管理画面から診断レポートを作成してください。"
)
SEND_NO_IR_CTL = (
    "送信に必要なソフトウェアが本機に入っていないため、送信できません。"
    "本体の管理画面から診断レポートを作成してください。"
)
_SEND_FAILURES = {
    REASON_NO_DEVICE: SEND_NO_DEVICE,
    REASON_NO_RESPONSE: SEND_NO_RESPONSE,
    REASON_CANNOT_TX: SEND_CANNOT_TX,
    REASON_NO_IR_CTL: SEND_NO_IR_CTL,
}
UPSTREAM_CHECK = "本体の管理画面を開き、EchoBridge の状態に異常が出ていないか確かめてから、"
CODES_UNAVAILABLE = (
    "本体（EchoBridge）から一覧を取得できませんでした。" + UPSTREAM_CHECK + "このページを再読み込みしてください。"
)
SAVE_REFUSED = (
    "設定ファイルを読めなかったため、元のファイルを残すために変更を保存しませんでした。"
    "本体の管理画面から診断レポートを作成してください"
)


class UiApp:
    """UIの中身（HTTPから切り離してある＝自己テストが実HTTPなしで叩ける）。

    各メソッドは (ステータス, JSONにできるオブジェクト) を返す。
    """

    def __init__(self, store: config.ConfigStore, recent, sender, client, rx_spec=None,
                 counters: Counters | None = None, ui_port: int = PORT):
        self._store = store
        self._recent = recent
        self._sender = sender
        self._client = client
        self._rx_spec = rx_spec
        self._counters = counters if counters is not None else Counters()
        self._ui_port = ui_port
        # 設定ファイルの読み書きを直列化する（UIスレッドは複数）。
        self._write_lock = threading.Lock()

    def set_ui_port(self, port: int | None) -> None:
        """実際に bind できたポートを控える（W19-13）。

        `config_port` は定数ではなく実体（`webui.config_port`）を返すのに、同じ値を出す
        `/diagnostics` の行だけが定数 PORT を使っていた——避けようとした食い違いが片側に
        残っていた。None（UIが立たなかった）なら既定のまま（そのときは誰も読まない）。
        """
        if port:
            self._ui_port = int(port)

    # --- 参照 -----------------------------------------------------------------
    def state(self) -> tuple[int, dict]:
        """プリセット・受信デバイス（詳細設定カード）と learned 一覧（お手持ちのリモコンのカード）。"""
        cfg = self._store.get()
        device = lirc.find_rx_device(self._rx_spec)
        learned = []
        for (proto, raw), entry in cfg.learned.items():
            learned.append(
                {
                    "code": bytes_to_code(*raw)[1],
                    "protocol": proto,
                    "scene": entry.target.key,
                    "label": entry.label,
                }
            )
        learned.sort(key=lambda e: e["code"])
        return 200, {
            "ok": True,
            "base": f"0x{cfg.base:04x}",
            "preset": codes.preset_number(cfg.base),
            "presets": [
                {"number": i + 1, "base": f"0x{b:04x}"} for i, b in enumerate(codes.PRESETS)
            ],
            # ★受信デバイスは表示のみ。UIから変更させない（IR_BRIDGE_RX_DEVICE は注入型で、
            #   ここから書けるようにすると真実の在処が2つになる）。
            "device": None if device is None else f"{device.path}（{role_label(device)}）",
            "learned": learned,
        }

    def codes_list(self) -> tuple[int, dict]:
        """学習用コード一覧。**毎回** 本体から取る（表示名をキャッシュしない）。"""
        cfg = self._store.get()
        try:
            scenes_raw = self._client.get_scenes()
            lights_raw = self._client.get_lights()
        except ClientError as e:
            logger.warning("一覧を取得できませんでした（本体API）— %s", e)
            return 200, {"ok": False, "error": CODES_UNAVAILABLE}

        scenes = []
        for s in scenes_raw:
            key = s.get("key")
            if not isinstance(key, str):
                continue
            try:
                raw = codes.encode_scene(cfg.base, key)
            except ValueError:
                # コード化できないシーンは一覧から外す（encodable_scene_keys と同じ判断）。
                logger.warning("シーンをコード化できないため一覧から外す: %s", key)
                continue
            scenes.append(
                {
                    "key": key,
                    "name": s.get("display_name") or key,
                    "group": s.get("group", 0),
                    "code": bytes_to_code(*raw)[1],
                }
            )

        lights = []
        for light in lights_raw:
            instance = light.get("instance")
            if not isinstance(instance, int):
                continue
            dimmable = bool(light.get("dimmable"))
            # ★調光非対応は OFF と ON の2行で固定（settings.json に記載があっても使わない）。
            #   0/100 以外の輝度は機器が解釈できず、行を増やしても意味がない。
            values = (
                cfg.brightness_lists.get(str(instance), DEFAULT_UI_BRIGHTNESS)
                if dimmable
                else DEFAULT_UI_BRIGHTNESS
            )
            rows = []
            for value in sorted(set(values)):
                try:
                    raw = codes.encode_light(cfg.base, instance, value)
                except ValueError:
                    continue
                rows.append(
                    {
                        "brightness": value,
                        "label": row_label(value, dimmable),
                        "deletable": row_deletable(value, dimmable),
                        "code": bytes_to_code(*raw)[1],
                    }
                )
            lights.append(
                {
                    "instance": instance,
                    "name": light.get("name") or f"照明{instance}",
                    "dimmable": dimmable,
                    "rows": rows,
                    # 行を足せるか（調光非対応には追加のプルダウンを出さない）。
                    "can_add": dimmable,
                }
            )
        return 200, {"ok": True, "scenes": scenes, "lights": lights}

    def recent(self) -> tuple[int, dict]:
        """受信ログ（50件・毎回全部返して描き直す。差分もカーソルも持たない）。

        ★未登録コードの行にだけ [シーンに割り当てる] を出す。導出コード・登録済み
        learned の行には出さない。**`source` は受信した時点の解釈なので、それだけで
        判断しない**——登録した直後の行は `unknown` のまま残り、ボタンが消えないと
        顧客は同じコードを二度登録しようとする。いまの learned と突き合わせて出し分ける。
        """
        cfg = self._store.get()
        entries = self._recent.snapshot()
        for e in entries:
            parsed = code_to_bytes(e.get("code") or "")
            e["assignable"] = e.get("source") == "unknown" and parsed is not None and parsed not in cfg.learned
        return 200, {"ok": True, "entries": entries}

    def diagnostics_text(self) -> str:
        """診断レポートに貼る本文（プレーンテキスト）。本体が叩く想定（本体改修⑥）。"""
        devices = lirc.find_devices()
        device = lirc.select_rx_device(devices, self._rx_spec)
        return diagnostics.render(
            self._store.get(),
            devices=devices,
            device=device,
            rx_spec=self._rx_spec,
            ir_ctl=ir_ctl_path(),
            ui_port=self._ui_port,
            recent=self._recent.snapshot(),
            counters=self._counters.snapshot(),
            # 調光可否は本体に聞かないと分からないので、設定に載っている＝調光対応として表記する。
            label_of=lambda v: row_label(v, True),
        )

    # --- 送信 -----------------------------------------------------------------
    def send(self, payload: dict) -> tuple[int, dict]:
        target = _target_from(payload)
        if target is None:
            return 400, {"ok": False, "message": "送信先が不正です"}
        try:
            result = self._sender.send(target)
        except ValueError as e:
            return 400, {"ok": False, "message": f"このコードは送信できません（{e}）"}
        if result.ok:
            return 200, {"ok": True, "message": SEND_OK, "code": result.code}
        if result.reason == REASON_BUSY:
            return 200, {"ok": False, "message": SEND_BUSY, "code": result.code}
        # それ以外（ir-ctl rc=N 等）は SEND_FAILED。顧客の文が先、原因は後ろ（■H）。
        customer = _SEND_FAILURES.get(result.reason, SEND_FAILED)
        return 200, {"ok": False, "message": f"{customer}\n（原因: {result.reason}）", "code": result.code}

    # --- 変更 -----------------------------------------------------------------
    def set_brightness_list(self, payload: dict) -> tuple[int, dict]:
        """輝度リストへの行の追加・削除。settings.json に保存して即時反映する。"""
        instance = payload.get("instance")
        value = payload.get("value")
        action = payload.get("action")
        if not _is_int(instance) or not _is_int(value) or action not in ("add", "remove"):
            return 400, {"ok": False, "message": "入力が不正です"}
        if not 0 <= value <= codes.MAX_BRIGHTNESS:
            return 400, {"ok": False, "message": f"輝度は 0〜{codes.MAX_BRIGHTNESS} です"}
        # ★OFF は誰も消せない（消すと赤外線から照明を消せなくなる）。本体に聞く前に弾く。
        if action == "remove" and value == 0:
            return 400, {"ok": False, "message": "OFF の行は削除できません（赤外線から照明を消せなくなります）"}
        # 調光非対応は2行固定。UIにはボタンを出していないが、経路としても塞ぐ。
        try:
            lights = self._client.get_lights()
        except ClientError as e:
            logger.warning("照明一覧を取得できないため輝度リストを変更しなかった — %s", e)
            return 400, {
                "ok": False,
                "message": "本体（EchoBridge）から照明の情報を取得できないため変更できません。"
                + UPSTREAM_CHECK + "もう一度試してください。",
            }
        light = next((li for li in lights if li.get("instance") == instance), None)
        if light is None:
            return 404, {"ok": False, "message": "その照明がありません"}
        if not light.get("dimmable"):
            return 400, {"ok": False, "message": "この照明は調光に対応していないため、OFF と ON の2行のままです"}
        with self._write_lock:
            cfg = self._store.get()
            lists = dict(cfg.brightness_lists)
            current = list(lists.get(str(instance), DEFAULT_UI_BRIGHTNESS))
            if action == "add":
                if value in current:
                    return 200, {"ok": True, "message": "すでにあります"}
                current.append(value)
            else:
                if value not in current:
                    return 200, {"ok": True, "message": "ありません"}
                current.remove(value)
            lists[str(instance)] = sorted(set(current))
            new = cfg._replace(brightness_lists=lists)
            config.save_settings(new)
            self._store.replace(new)
        # ★顧客の操作で設定が変わったことを1行残す（§18 と衝突しない——受信起因ではなく
        #   顧客操作起因で、頻度はこちらで制御できる側。一生に数回で永続ログの枠に影響しない）。
        logger.info(
            "照明%d の輝度リストから %s を削除しました（%s）" if action == "remove"
            else "照明%d の輝度リストに %s を追加しました（%s）",
            instance,
            row_label(value, True),
            " / ".join(row_label(v, True) for v in lists[str(instance)]),
        )
        return 200, {"ok": True}

    def set_base(self, payload: dict) -> tuple[int, dict]:
        """プリセットの切替。settings.json を書いてから新しい Config へ差し替える（M3-c の経路）。"""
        raw = payload.get("base")
        try:
            base = int(raw, 16) if isinstance(raw, str) else int(raw)
        except (TypeError, ValueError):
            return 400, {"ok": False, "message": "プリセットの指定が不正です"}
        if base not in codes.PRESETS:
            return 400, {"ok": False, "message": "プリセット表にない値です"}
        with self._write_lock:
            cfg = self._store.get()
            if cfg.base == base:
                return 200, {"ok": True, "message": "変更ありません"}
            new = cfg._replace(base=base)
            config.save_settings(new)
            # ★reload() ではなく replace()。reload はファイルを読み直す経路で、いまは
            #   自分が書いた内容をそのまま反映すればよい（読み直す必要が無い）。
            self._store.replace(new)
        return 200, {"ok": True}

    def add_learned(self, payload: dict) -> tuple[int, dict]:
        """受信ログの未登録コードをシーンに割り当てる。"""
        code = payload.get("code")
        scene = payload.get("scene")
        label = (payload.get("label") or "").strip()
        parsed = code_to_bytes(code) if isinstance(code, str) else None
        if parsed is None:
            return 400, {"ok": False, "message": "コードを読めません"}
        if not isinstance(scene, str) or not scene:
            return 400, {"ok": False, "message": "シーンを選んでください"}
        if not label:
            # ★必須。無いと顧客が後で「どのリモコンのどのボタンか」を判別できない。
            return 400, {"ok": False, "message": "名前を入力してください"}
        # ★上限は「実用に足りる値」（W19-5）。顧客の操作は当たらない——効くのは §21 が
        #   引き受けた無認証の面から、スクリプトで叩かれた場合である。1件追加のたびに
        #   learned.json を全件書き直す（SD）ので、件数は青天井にしない。
        #   ★実在するシーンかどうかは**ここでも確かめない**（CLAUDE.md §17。一覧を持つと
        #     状態を持たない設計が崩れる）。長さだけを見る。
        if len(label) > MAX_LABEL_LEN:
            return 400, {"ok": False, "message": f"名前は {MAX_LABEL_LEN} 文字までです"}
        if len(scene) > MAX_SCENE_LEN:
            return 400, {"ok": False, "message": f"シーンの指定は {MAX_SCENE_LEN} 文字までです"}
        with self._write_lock:
            cfg = self._store.get()
            if parsed not in cfg.learned and len(cfg.learned) >= MAX_LEARNED:
                return 400, {
                    "ok": False,
                    "message": f"学習コードは {MAX_LEARNED} 件までです（不要な行を削除してください）",
                }
            learned = dict(cfg.learned)
            learned[parsed] = config.LearnedEntry(target=SceneTarget(scene), label=label)
            new = cfg._replace(learned=learned)
            config.save_learned(new)
            self._store.replace(new)
        # ★顧客が手で育てたデータなので、追加も削除も1行残す（失うと学習し直しになる）。
        logger.info("学習コードを追加しました（%s → %s「%s」）", code, scene, label)
        # 導出コードと衝突する場合は**登録を許したうえで**重なった宛先を返す（learned が先に引かれる）。
        # ★文面は画面が表示名で組む（■J）。ここで summary を使うと「シーン scene_03 を発火」の
        #   ように内部のキーが顧客に出る。渡すのは照合できる値だけ（codes.target_info と同じ筋）。
        derived = codes.decode(cfg.base, parsed[1])
        overlap = None
        if derived is not None and derived.target is not None:
            overlap = codes.target_info(derived.target)
            logger.info("learned に登録したコードが導出コードと重なっている: %s", code)
        return 200, {"ok": True, "overlap": overlap}

    def delete_learned(self, payload: dict) -> tuple[int, dict]:
        code = payload.get("code")
        parsed = code_to_bytes(code) if isinstance(code, str) else None
        if parsed is None:
            return 400, {"ok": False, "message": "コードを読めません"}
        with self._write_lock:
            cfg = self._store.get()
            if parsed not in cfg.learned:
                return 404, {"ok": False, "message": "登録がありません"}
            learned = dict(cfg.learned)
            removed = learned.pop(parsed)
            new = cfg._replace(learned=learned)
            config.save_learned(new)
            self._store.replace(new)
        logger.info(
            "学習コードを削除しました（%s → %s「%s」）", code, removed.target.key, removed.label
        )
        return 200, {"ok": True}


def _target_from(payload: dict):
    kind = payload.get("kind")
    if kind == "scene" and isinstance(payload.get("key"), str):
        return SceneTarget(payload["key"])
    if kind == "light" and _is_int(payload.get("instance")) and _is_int(payload.get("brightness")):
        return LightTarget(payload["instance"], payload["brightness"])
    return None


# --- HTTP ---------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    app: UiApp = None  # サブクラスで差し込む
    server_version = "ir-bridge-ui"
    sys_version = ""
    # ★ソケットのタイムアウト（W19-8）。StreamRequestHandler.setup が settimeout を撃ち、
    #   handle_one_request が socket.timeout を「閉じる」側に倒す。設定していないと、
    #   ヘッダを送り終えない接続を readline で**永久に**待つ（実測: 60本張ると 62 スレッドの
    #   まま自然に閉じない）。悪意は要らない——スリープした端末・切れた Wi-Fi で起きる。
    timeout = CONN_TIMEOUT_SEC
    # 例外の WARNING を間引く（W19-15）。**クラス変数＝全接続で共有**する（接続ごとに
    # ハンドラが作り直されるので、インスタンスに持たせると間引きが効かない）。
    _limiter = WarnLimiter()
    # この要求の応答を送ったか（_fallback が二重送信しないための番兵）。
    _responded = False

    def log_message(self, fmt, *args):
        """★アクセスログを stderr（＝永続ログ）へ出さない。モジュール冒頭の注記参照。"""
        logger.debug("ui %s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        # 既定は log_message 経由だが、明示しておく（400等でも永続ログへ出さない）。
        logger.debug("ui %s - %s", self.address_string(), fmt % args)

    # --- 入口 -----------------------------------------------------------------
    def do_GET(self):
        self._responded = False
        self._guard(self._route_get)

    def do_POST(self):
        self._responded = False
        self._guard(self._route_post)

    def _guard(self, route) -> None:
        """1要求の処理を包む。**例外をここから外へ出さない**（W19-3・W19-6・W19-15）。

        外へ出すと socketserver がトレースバックを fd 2 へ直接印字する。fd 2 は
        `data/error_addon.log`（永続・1MB枠）で、そこへ logging を通らない書き込みが入ると
        回転が起きない（errlog.py 冒頭）。設定画面は `/api/recent` を2秒ごとに叩くので、
        例外が出る状態になると約2.7MB/時＝1MB枠が約20分で1周し、本来残すべき WARNING が
        `.old` へ押し出される（M2-D・§21 で潰したのと同じ型）。

        WARNING は **WarnLimiter を通す**（W19-15）。ここは無認証なので、500 になる要求を
        繰り返せば1件約1KB のトレースバックを好きなだけ書かせられる（実測 1016 回で枠が一杯）。
        トレースバックは窓明けの1本にだけ付け、間引かれたぶんは INFO（journal・揮発）に残す。
        """
        path = self.path.split("?", 1)[0]
        try:
            route()
        except config.SaveRefused as e:
            # 読めなかった設定ファイルを保全できていないので上書きしない（config.py 冒頭）。
            # 起動時に WARNING を1行出してあるので、操作ごとの行は INFO に置く（枠を食わない）。
            logger.info("設定を保存しなかった — %s", e)
            self._fallback(409, SAVE_REFUSED)
        except Exception:  # noqa: BLE001 - UIの1操作で常駐を落とさない
            released = self._limiter.take(("ui", path, sys.exc_info()[0].__name__), f"設定UIの例外（{path}）")
            if released is None:
                logger.info("設定UIの処理で例外（%s・間引き中）", path, exc_info=True)
            else:
                logger.warning("設定UIの処理で例外（%s）%s", path, suffix(released), exc_info=True)
            self._fallback(500, "本機の内部エラーです")

    def _fallback(self, status: int, message: str) -> None:
        """応答をまだ送っていなければ送る。**二重に送らない**（送信の途中で落ちた場合）。"""
        if self._responded:
            self.close_connection = True
            return
        try:
            self._send_json(status, {"ok": False, "message": message})
        except OSError:
            self.close_connection = True  # 相手がもう居ない（タブを閉じた等）

    def _route_get(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send_html(PAGE_HTML)
        if path == "/api/state":
            return self._send_json(*self.app.state())
        if path == "/api/codes":
            return self._send_json(*self.app.codes_list())
        if path == "/api/recent":
            return self._send_json(*self.app.recent())
        if path == "/diagnostics":
            # 本体が叩いて診断レポートへ貼る（本体改修⑥）。人が見ても読める形で返す。
            return self._send_text(self.app.diagnostics_text())
        return self._send_json(404, {"ok": False, "message": "not found"})

    def _route_post(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/send": self.app.send,
            "/api/brightness": self.app.set_brightness_list,
            "/api/base": self.app.set_base,
            "/api/learned": self.app.add_learned,
            "/api/learned/delete": self.app.delete_learned,
        }
        handler = routes.get(path)
        if handler is None:
            return self._send_json(404, {"ok": False, "message": "not found"})

        # ★入口の3つの検査（W19-4・W19-7）。**認証は足していない**（§21 の判断は変えない。
        #   モジュール冒頭）。いずれも顧客の画面（同一オリジンの fetch）は素通りする。
        if not self._same_origin():
            self.close_connection = True
            return self._send_json(403, {"ok": False, "message": "このページからは操作できません"})
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            # ★HTML フォームでは application/json を作れない（作ろうとするとプリフライトが
            #   要り、CORS 応答を返さない本サーバでは止まる）＝外部ページからの POST を
            #   認証なしで塞げる。顧客の画面は fetch で JSON を送っているので影響しない。
            self.close_connection = True
            return self._send_json(
                415, {"ok": False, "message": "Content-Type: application/json で送ってください"}
            )
        length = self._body_length()
        if length is None:
            return None  # 応答は _body_length が送った（本文は読んでいない）
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("object ではない")
        except (ValueError, OSError):
            return self._send_json(400, {"ok": False, "message": "入力を読めません"})
        status, body = handler(payload)
        return self._send_json(status, body)

    def _body_length(self) -> int | None:
        """本文の長さを決める。受け付けられないなら**本文を読まずに**応答して None（W19-4）。

        読んでから弾いたのでは意味がない（読んだ時点でメモリに載る）。
        欠落・非数・負は 411（長さが分からないものを読み始めない）、上限超は 413。
        どちらも接続を閉じる——本文が未読のまま残るので、この接続は使い回せない。
        """
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw)
            if length < 0:
                raise ValueError(raw)
        except (TypeError, ValueError):
            self.close_connection = True
            self._send_json(411, {"ok": False, "message": "Content-Length が必要です"})
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            self._send_json(
                413, {"ok": False, "message": f"要求が大きすぎます（上限 {MAX_BODY_BYTES} バイト）"}
            )
            return None
        return length

    def _same_origin(self) -> bool:
        """外部のページから撃たれた要求を弾く（W19-7・CSRF）。**認証ではない。**

        §21 の判断は「ここに到達できる相手は同一LAN内で、その相手は :8099 で照明を操作
        できる」ことに全面的に依存している。CSRF の相手は **LAN の外**に居る（顧客が開いた
        ページが本機の LAN アドレスへ POST する）ので、その前提が成り立たない。
        応答は同一生成元ポリシーで読めない＝**盗めないが壊せる**（/api/base はプリセットを
        変える＝学習させた全ボタンが効かなくなる）。

        - `Sec-Fetch-Site: cross-site` は弾く（現行ブラウザが自分で付ける＝ページ側から
          偽装できない。`same-origin` / `none` は通す）。
        - `Origin` が在れば Host と一致することを求める（Sec-Fetch-* の無い古いブラウザ用）。
          `null`（sandbox・file:）は一致しようがないので弾く。
        - どちらのヘッダも無い要求は通す——ブラウザ以外（curl・本体の /diagnostics 取得・
          自己テスト）がそれで、そこは §21 が引き受けた範囲のままにする。
        """
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        if origin.lower() == "null":
            return False
        host = (self.headers.get("Host") or "").strip()
        try:
            netloc = urllib.parse.urlsplit(origin).netloc
        except ValueError:
            return False
        return bool(host) and netloc.lower() == host.lower()

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str):
        data = text.encode("utf-8")
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str):
        data = html.encode("utf-8")
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def config_port(server) -> int | None:
    """status.json へ書く設定UIのポート。**UIが立たなかった（server is None）なら None＝キーを書かない。**

    ★**URL ではなくポートを書く**（本体改修⑤・2026-09-13 に確定）。顧客の管理画面は
    ブラウザで開かれていて `location.hostname` を持っているので、**同じ機体の別ポート**は
    ブラウザ側で組み立てられる。アドオンが自分のIPやホスト名を知る必要がない＝
    多NIC・VPN での誤判定も、DHCP でのIP変更も、mDNS 不達も起こらない。
    顧客が管理画面に到達している時点でその経路は通っているので、同じ経路の別ポートが最も確実。

    本体はキーが無ければリンクを出さない（受け入れ条件a）。ポートが塞がっていてUIが開けないのに
    管理画面にリンクだけ出る、という状態を作らないための対（受け入れ条件b）——`webui.start` は
    WARNING を出して常駐を続けるので、対が無いとリンクだけが残りうる。

    実際に **bind できたポート**を返す（定数ではない）。将来ポートを可変にしても値が実体とずれない。
    """
    if server is None:
        return None
    return server.server_address[1]


class _UiServer(ThreadingHTTPServer):
    """同時接続数に上限を持ち、例外を fd 2 へ印字しない ThreadingHTTPServer（W19-8・W19-3）。"""

    daemon_threads = True
    max_connections = MAX_CONNECTIONS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._limiter = WarnLimiter()

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            # ★上限に当たったら**受け付けずに閉じる**（待たせない）。待たせると、待っている
            #   分だけ記述子と受信バッファを掴む＝W19-4 と同じ着地になる。
            released = self._limiter.take(("ui_conn_limit",), "設定UIの同時接続が上限")
            if released is None:
                logger.info("設定UIの同時接続が上限 %d に達したので接続を閉じた（間引き中）", self.max_connections)
            else:
                logger.warning(
                    "設定UIの同時接続が上限 %d に達したので接続を閉じた%s",
                    self.max_connections,
                    suffix(released),
                )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # スレッドを起こせなかった（RuntimeError 等）。枠を戻してから投げ直す。
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        """socketserver の既定（fd 2 へトレースバックを印字）を置き換える（W19-3・W19-6）。

        fd 2 は `data/error_addon.log`（永続・1MB枠）で、logging を通らない書き込みは
        **回転を起こさない**（errlog.py 冒頭）。ここを塞いで「fd 2 へ書くのは logging だけ」を保つ。
        切断系はこちらの失敗ではない（タブを閉じた・Wi-Fi が切れた・タイムアウトで閉じた）ので
        永続ログに残さない。それ以外は間引いた WARNING を1行だけ出す。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            logger.debug("ui 接続が切れた %s: %s", client_address, exc)
            return
        released = self._limiter.take(("ui_conn", type(exc).__name__), "設定UIの接続処理の例外")
        if released is None:
            logger.info("設定UIの接続処理で例外（間引き中）: %s", exc, exc_info=True)
        else:
            logger.warning("設定UIの接続処理で例外: %s%s", exc, suffix(released), exc_info=True)


def make_server(app: UiApp, port: int = PORT) -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    return _UiServer(("", port), handler)


def start(app: UiApp, port: int = PORT) -> ThreadingHTTPServer | None:
    """UIサーバを別スレッドで立てる。立てられなければ WARNING を出して None（常駐は続ける）。

    ★ポートが塞がっていても受信は止めない。設定UIが開けないことより、赤外線を
    受け続けることのほうが顧客にとって重要である。
    """
    try:
        server = make_server(app, port)
    except OSError as e:
        logger.warning("設定UI(:%d)を開けませんでした（受信は継続）— %s", port, e)
        return None
    threading.Thread(target=server.serve_forever, name="WebUi", daemon=True).start()
    logger.info("設定UI: http://<この機体のIP>:%d/", port)
    return server


PAGE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>赤外線リモコン連携</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--line:#e3e5e9;--text:#23262b;--muted:#6b7280;--accent:#3f6fb5;--warn:#8a5a00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font-family:system-ui,-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
 font-size:15px;line-height:1.7}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 64px}
h1{font-size:20px;margin:8px 0 20px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:16px;margin:0 0 12px;font-weight:600}
.hint{color:var(--muted);font-size:13.5px;margin:0 0 10px}
.alert{background:#fff7e6;border:1px solid #f0d9a8;color:var(--warn);border-radius:8px;padding:10px 12px;margin:0 0 12px}
.alert[hidden]{display:none}
.tabs{display:flex;gap:8px;margin-bottom:14px}
.tab{padding:7px 16px;border:1px solid var(--line);background:#fafbfc;border-radius:999px;cursor:pointer;font-size:14px}
.tab[aria-selected="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
.group{margin:14px 0 6px;color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse}
td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-size:12.5px;color:var(--muted);font-weight:500}
td.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--muted);white-space:nowrap}
td.right{text-align:right;white-space:nowrap}
button{font:inherit;padding:6px 14px;border:1px solid var(--line);background:#fafbfc;border-radius:7px;cursor:pointer}
button:hover{background:#f0f2f5}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.link{border:none;background:none;color:var(--accent);padding:4px 6px}
button:disabled{opacity:.5;cursor:default}
select{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:7px;background:#fff}
input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:7px;width:100%}
.msg{margin-left:10px;font-size:13px;color:var(--muted)}
.msg.ok{color:#2f6f43}.msg.ng{color:var(--warn)}
details summary{cursor:pointer;color:var(--accent);font-size:14px}
details[open] summary{margin-bottom:10px}
.steps{color:var(--muted);font-size:14px;margin:0;padding-left:1.2em}
.steps.main{color:var(--text);font-size:14.5px;margin:0 0 12px}
.steps.main li{margin-bottom:6px}
/* 手順の中の注記。症状で書いてあるので、検証していない機種に当たった顧客も自分で当てはめられる。 */
.steps.main .note{color:var(--muted);font-size:13.5px;margin:6px 0 0;padding-left:10px;
 border-left:3px solid var(--line)}
/* 受信ログは高さを固定してスクロールさせる。行数で下のカードの位置が動くと、
   顧客が毎回探すことになるため。10行ぶん程度。 */
.detail{font-size:14px;color:var(--text)}
.detail h3{font-size:14px;margin:16px 0 6px;font-weight:600}
.detail h3:first-child{margin-top:4px}
.detail p{margin:0 0 8px}
.detail ul{margin:0 0 8px;padding-left:1.2em}
.detail table.trouble{margin:4px 0 10px}
.detail table.trouble th{font-size:12.5px}
.detail table.trouble td{font-size:13.5px;vertical-align:top}
.learned-note{color:var(--muted);font-size:13.5px;margin:0 0 12px;padding-left:10px;
 border-left:3px solid var(--line)}
.learned-note p{margin:0 0 6px}
.learned-note ul{margin:0;padding-left:1.2em}
.scrollbox{height:340px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;padding:0 10px}
.scrollbox table{margin:0}
.scrollbox thead th{position:sticky;top:0;background:var(--card)}
.modal{position:fixed;inset:0;z-index:30;background:rgba(20,22,26,.45);display:flex;align-items:center;justify-content:center;padding:16px}
.modal[hidden]{display:none}
.modal .box{background:#fff;border-radius:12px;padding:22px 24px;max-width:520px;width:100%}
.modal p{margin:0 0 12px;white-space:pre-line}
.modal .note{color:var(--muted);font-size:13.5px}
.modal .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.empty{color:var(--muted);font-size:13.5px;padding:10px 0}
.row-actions{display:flex;gap:6px;justify-content:flex-end;align-items:center}
td.when{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
/* ■C 結果の出し方。成功（ただの結果報告）は数秒で消える浮いた表示、失敗（次に何かする必要が
   ある知らせ）は閉じるまで残るモーダル。★表の中に文字を差し込まない——行の中に結果を出すと
   名前が1文字ずつに折り返し、送信ボタンが画面外へ押し出される（人間が実機で確認）。 */
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);width:max-content;
 max-width:calc(100% - 32px);background:#23262b;color:#fff;padding:10px 16px;border-radius:8px;
 font-size:14px;line-height:1.5;box-shadow:0 4px 16px rgba(0,0,0,.2);pointer-events:none;
 opacity:0;transition:opacity .2s;z-index:20}
.toast.show{opacity:1}
/* ■A 狭い幅では1件を縦に積む（受信ログと learned 一覧）。3〜4列をスマホ幅に押し込むと
   1文字ずつ折り返し、時刻の列は画面外へ切れる。★横スクロールは使わない——指で横に動かした
   先で「どの行の話か」を見失う。積む順は「顧客が読むものを上、開発者が見るもの（コード）を下」。
   広い画面は表のまま。学習用コード一覧（送信ボタンの並び）は対象にしない（実機で見て良しと判断）。 */
@media (max-width:640px){
  table.stack thead{display:none}
  table.stack tr{display:grid;column-gap:10px;row-gap:2px;padding:9px 0;border-bottom:1px solid var(--line);align-items:center}
  table.stack td{display:block;padding:0;border:none;min-width:0}
  table.stack td.code{font-size:12px;white-space:normal;overflow-wrap:anywhere}
  table.stack td.act{justify-self:end}
  #recent tr{grid-template-columns:auto 1fr auto;grid-template-areas:"when what what" "code code act"}
  #recent td.when{grid-area:when}
  #recent td.what{grid-area:what}
  #recent td.code{grid-area:code}
  #recent td.act{grid-area:act}
  #learned tr{grid-template-columns:1fr auto;grid-template-areas:"label act" "scene scene" "code code"}
  #learned td.label{grid-area:label}
  #learned td.scene{grid-area:scene}
  #learned td.scene::before{content:"→ "}
  #learned td.code{grid-area:code}
  #learned td.act{grid-area:act}
}
</style>
</head>
<body>
<div class="wrap">
<h1>赤外線リモコン連携</h1>

<div class="card">
  <h2>IRリモコン設定</h2>
  <!-- ■E 受信デバイスが無いときだけ出す（旧実装は送信を押して初めて気づき、アドオンだけ入れた顧客が
       「使えない」と誤解した）。文面は人間の確定稿。 -->
  <p class="alert" id="no-device" hidden>⚠ 赤外線の受信デバイスが見つかりません。Irdroid を本体の USB に挿してから、このページを再読み込みしてください。</p>
  <p class="hint">この画面から、シーンや照明ごとの赤外線コードをスマートリモコンに学習させます。</p>
  <ol class="steps main">
    <li>スマートリモコンは、本機と赤外線が届く場所に置いてください。
      同じ部屋であれば、壁に反射する程度でも届くことがほとんどです。
      機種によって届く範囲は異なるため、うまく動かないときは本機に近づけたり、
      間に物が入らない場所に移したりして試してください。
      <p><strong>学習のときだけは、本機にできるだけ近づけてください。</strong></p></li>
    <li><strong>スマートリモコンで新しいリモコンを追加するとき、「照明」ではなく「その他の機器」などのカテゴリを選んでください。</strong>
      「照明」で登録すると、スマートリモコン側が既存メーカーの照明として解釈することがあり、
      <strong>学習できたように見えて動かない</strong>場合があります。
      <p class="note">※ 一部のスマートリモコンアプリでは、学習モード（受信待機）にしたあと
        アプリを閉じてこの画面に切り替えると、赤外線を受け付けられないことがあります
        （Nature Remo で確認しています）。</p>
      <p class="note">うまく学習できない場合は、この画面を同じネットワークにつないだ
        別の端末（パソコン・タブレット・別のスマートフォンなど）で開き、そちらから送信してください。</p></li>
    <li>学習モードにしたら、下の一覧から学習させたい行の [送信] を押します（1回押すと1回だけ送ります）。</li>
    <li>受け取ったら、スマートリモコン側でボタンに名前を付けて保存してください。</li>
  </ol>
  <details>
    <summary>詳しい手順</summary>
    <div class="detail">
      <h3>作業の全体像</h3>
      <p>シーンと個別照明を合わせて<strong>数十件</strong>を1件ずつ学習させる作業になります
        （例: シーン16件＋照明20件）。同じ操作の繰り返しなので、途中でやめて後日続けても構いません。</p>

      <h3>置き場所を決める</h3>
      <p>本機とスマートリモコンの位置には、<strong>向きの違う制約が2つ</strong>重なります。</p>
      <ul>
        <li><strong>運用中はずっと</strong>: スマートリモコンが撃った赤外線を<strong>本機が受け取ります</strong>。</li>
        <li><strong>学習のときだけ</strong>: 逆に<strong>本機が撃ち</strong>、スマートリモコンが受け取ります。</li>
      </ul>
      <p>この2つは最適な位置が違います。運用中は同じ部屋なら反射でも届くことがほとんどですが、
        学習のときはスマートリモコンを本機に近づける必要があります。
        <strong>学習が終わったら、運用中に届く場所へ戻してください</strong>——届かない場所に置くと、
        学習したボタンが効かなくなります。</p>
      <p>届かないときは距離だけでなく<strong>向き</strong>も見てください。本機の受信部（USBの受発光部）は、
        USB延長ケーブルで棚の外へ引き出すこともできます。</p>

      <h3>ボタンの名前の付け方</h3>
      <p>数十件になるので、スマートリモコン側の名前に<strong>照明名と%（またはシーン名）</strong>を
        入れておくと後から探せます。例:「リビング 50%」「ダイニング OFF」「就寝前」。</p>

      <h3>どちらから作るか</h3>
      <p><strong>シーンから</strong>をおすすめします。件数が少なく、1行＝1ボタンで対応が分かりやすいためです。
        個別照明は1台につき複数の行（OFF・100% など）があります。</p>

      <h3>うまくいかないとき</h3>
      <table class="trouble">
        <tr><th>症状</th><th>試すこと</th></tr>
        <tr><td>スマートリモコンが受け取らない</td>
            <td>本機に近づける／間に物が入らない位置にする。それでも駄目なら、この画面を別の端末で開いて送信する</td></tr>
        <tr><td>受け取ったのに照明が動かない</td>
            <td>「その他の機器」などのカテゴリで登録し直す</td></tr>
        <tr><td>たまに反応しない</td><td>もう一度押す</td></tr>
        <tr><td>関係のないリモコンで照明が動く</td>
            <td>下の「詳細設定」でIRコードのプリセットを変更する</td></tr>
      </table>
      <p><strong>受信ログが切り分けの道具になります。</strong>スマートリモコンのボタンを押して
        <strong>受信ログに出れば、本機までは届いています</strong>（出ないなら距離か向きの問題です）。
        出ているのに照明が動かないなら、届き方ではなく登録のしかたを見直してください。</p>

      <h3>同じ端末で学習できないとき</h3>
      <p>一部のスマートリモコンアプリは、学習モード（受信待機）にしたあとアプリを閉じてこの画面に
        切り替えると、赤外線の受信を止めます（Nature Remo で確認しています）。この場合、
        スマートフォン1台だけでは学習できません。同じネットワークにつないだ別の端末
        （パソコン・タブレット・別のスマートフォンなど）でこの画面を開き、
        スマートフォンでは学習モードの操作だけを行ってください。</p>
    </div>
  </details>
</div>

<div class="card">
  <h2>学習用コード一覧</h2>
  <div class="tabs">
    <button class="tab" id="tab-scenes" role="tab" aria-selected="true" onclick="switchTab('scenes')">シーン</button>
    <button class="tab" id="tab-lights" role="tab" aria-selected="false" onclick="switchTab('lights')">個別照明</button>
  </div>
  <div id="codes"><p class="empty">読み込み中…</p></div>
</div>

<div class="card">
  <h2>受信ログ</h2>
  <p class="hint">本機が受け取った赤外線です（新しい順・最大50件）。</p>
  <div id="recent" class="scrollbox"><p class="empty">読み込み中…</p></div>
</div>

<!-- ■D learned は「詳細設定」ではなく独立した機能なので、詳細設定と並べる。見出しで用途が
     分かるようにする（開く前に「自分に関係あるか」を判断できる）。説明文は人間の確定稿。
     ★1文を HTML の途中で改行しない（日本語の途中の改行は、ブラウザによって空白として描かれる）。 -->
<div class="card">
  <details id="own-remote">
    <summary>お手持ちのリモコンで操作する（使わなくなったリモコンの再利用）</summary>
    <div style="margin-top:14px">
      <div class="learned-note">
        <p>使わなくなったリモコン（昔のテレビのリモコンなど）のボタンに、照明のシーンを割り当てられます。ボタンを押すと、本機が赤外線を受け取って照明を動かします。</p>
        <ul>
          <li>スマートリモコンをお使いの場合、この機能は使いません。スマートリモコンには、上の「学習用コード一覧」から本機が送るコードを覚えさせてください。</li>
          <li><strong>いま使っているリモコンのボタンを割り当てると、本来の機器も一緒に反応します。</strong>使わなくなったリモコンをお使いください。</li>
          <li>リモコンは機器に向けて使う前提のものが多く、スマートリモコンほど広い範囲には届きません。効かないときは、本機のほうに向けて押してみてください。</li>
        </ul>
      </div>
      <div id="learned"></div>
    </div>
  </details>
</div>

<div class="card">
  <details id="advanced">
    <summary>詳細設定</summary>
    <div style="margin-top:14px">
      <h2 style="font-size:15px">IRコードのプリセット
        <span class="msg">お手持ちの他のリモコンを操作したときに照明が動いてしまう場合は、プリセットを変更してください。</span>
      </h2>
      <select id="preset"></select>
      <button onclick="askBase()">変更</button>

      <h2 style="font-size:15px;margin-top:22px">受信デバイス</h2>
      <p class="hint" id="device">—</p>
    </div>
  </details>
</div>
</div>

<div class="toast" id="toast" role="status" aria-live="polite"></div>

<div class="modal" id="modal" hidden><div class="box">
  <div id="modal-body"></div>
  <div class="actions" id="modal-actions"></div>
</div></div>

<script>
let TAB = "scenes", CODES = null, STATE = null, RECENT = null;

// ★通信できないときも投げない（投げると呼び側の後始末が走らず、送信ボタンが押せないまま残る）。
//   offline で区別できるようにしておく（2秒ポーリングは前の表示を残す側に倒す）。
const OFFLINE = "本機に接続できませんでした。本機の電源とネットワークを確かめてから、このページを再読み込みしてください。";
async function api(path, body) {
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {};
  let res;
  try { res = await fetch(path, opt); } catch (e) { return {ok:false, offline:true, message:OFFLINE}; }
  try { return await res.json(); } catch (e) { return {ok:false, message:"応答を読めません"}; }
}
function el(tag, props, ...kids) {
  const n = Object.assign(document.createElement(tag), props || {});
  for (const k of kids) n.append(k);
  return n;
}
function msg(node, text, ok) {
  node.textContent = text;
  node.className = "msg " + (ok ? "ok" : "ng");
}

// ■C 成功の知らせ（数秒で消える）。sticky は送信中のように結果が来るまで出しておくもの。
const TOAST_MS = 4000;
let toastTimer = null;
function toast(text, sticky) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => t.classList.remove("show"), TOAST_MS);
}
function hideToast() {
  clearTimeout(toastTimer);
  document.getElementById("toast").classList.remove("show");
}
// ■C 失敗・次に何かする必要がある知らせ（閉じるまで残る）。
function notice(text) {
  hideToast();
  const close = el("button", {className:"primary", textContent:"閉じる"});
  close.onclick = closeModal;
  showModal([el("p", {textContent:text})], [close]);
}

function switchTab(tab) {
  TAB = tab;
  document.getElementById("tab-scenes").setAttribute("aria-selected", tab === "scenes");
  document.getElementById("tab-lights").setAttribute("aria-selected", tab === "lights");
  renderCodes();
}

async function loadCodes() {
  CODES = await api("/api/codes");
  renderCodes();
  // ★表示名は CODES の中にある。learned 一覧と受信ログは先に描かれていることがあるので
  //   （loadState / loadRecent と並行に走る）、取れた時点で描き直す。
  renderLearned();
  renderRecent();
}

// 表示名は CODES（/api/codes の結果）から引く。★これは**ブラウザがページを開いている間だけ**
// 持つもので、アドオンが永続的に持つわけではない（§26-8 はアドオン側の規律）。リロードで取り直す。
// 一覧に無い・まだ取れていないときは key や「照明N」にフォールバックする——**名前が出ないことより、
// 行が消えたり空欄になるほうが悪い**。
function sceneName(key) {
  const list = (CODES && CODES.ok && CODES.scenes) || [];
  const hit = list.find(s => s.key === key);
  // ★グループ名を付ける。本体は「シーン名にグループ名を付けて重複を解消する」設計なので、
  //   名前だけだと同名シーンを区別できない。**受信ログは学習用コード一覧より深刻**で、
  //   一覧はグループごとの小見出しで分かれるが、受信ログは1列に並ぶ（衝突調査で困る）。
  return hit ? ("グループ" + hit.group + " " + hit.name) : key;
}
function lightName(instance) {
  const list = (CODES && CODES.ok && CODES.lights) || [];
  const hit = list.find(l => l.instance === instance);
  return hit ? hit.name : "照明" + instance;
}
// 輝度の表記は**学習用コード一覧（サーバ側 row_label）と同じ規則**にする。顧客から見れば
// 同じものなので、一覧で OFF と書いたボタンを押して受信ログに 0% と出てはいけない。
// ★調光可否は**受信時点では分からない**が、**描画時には一覧（/api/codes）から引ける**。
//   一覧に無い照明（消された等）は調光対応として扱う＝数値のまま出す。
// ★規則がサーバ側（row_label）と2か所になる。片方だけ直すと表記がずれるので、
//   自己テストで両方の分岐を突き合わせている。
function brightnessLabel(instance, brightness) {
  if (brightness === 0) return "OFF";
  const list = (CODES && CODES.ok && CODES.lights) || [];
  const hit = list.find(l => l.instance === instance);
  if (brightness === 100 && hit && !hit.dimmable) return "ON";
  return brightness + "%";
}

// 宛先（codes.target_info の形）を表示名で書く。受信ログと重複の警告（■J）が同じ規則を使う。
function targetText(t) {
  if (t.kind === "scene") return "シーン「" + sceneName(t.key) + "」を発火";
  if (t.kind === "light") {
    return lightName(t.instance) + " を " + brightnessLabel(t.instance, t.brightness) + " に設定";
  }
  return "";
}

function targetLabel(e) {
  const t = e.target;
  // 宛先が無い行（未登録・範囲外・別プリセット）は顧客向けの display を出す。★summary は
  // 開発者向け（/diagnostics が読む）なので画面に出さない（■I・宛先が違う）。
  if (!t) return e.display || "—";
  return targetText(t) || e.display || "—";
}

// ■J learned が導出コードと重なったときの知らせ。サーバは宛先の構造だけを返し、表示名はここで差し込む
// （以前はサーバが summary で文面を作っていて、内部のキー scene_NN が顧客に出ていた）。
function overlapText(overlap) {
  return "このコードは本機のコード（" + targetText(overlap) + "）と重なっています。登録した内容が優先されます";
}

function sendButton(payload) {
  const btn = el("button", {className:"primary", textContent:"送信"});
  btn.onclick = async () => {
    // 送信は受信が静かになるまで数秒待つことがある。待っている間も行の中には何も出さない。
    btn.disabled = true; toast("送信中…", true);
    const r = await api("/api/send", payload);
    btn.disabled = false;
    if (r.ok) toast(r.message || "送信しました");
    else notice(r.message || "送信できませんでした");
  };
  return el("div", {className:"row-actions"}, btn);
}

function renderCodes() {
  const box = document.getElementById("codes");
  box.textContent = "";
  if (!CODES || !CODES.ok) {
    box.append(el("p", {className:"empty", textContent:(CODES && (CODES.error || CODES.message)) || "一覧を取得できませんでした"}));
    return;
  }
  if (TAB === "scenes") {
    const groups = new Map();
    for (const s of CODES.scenes) {
      if (!groups.has(s.group)) groups.set(s.group, []);
      groups.get(s.group).push(s);
    }
    if (!groups.size) { box.append(el("p", {className:"empty", textContent:"シーンがありません"})); return; }
    for (const [g, items] of [...groups.entries()].sort((a,b) => a[0]-b[0])) {
      box.append(el("p", {className:"group", textContent:"グループ " + g}));
      const tb = el("tbody");
      for (const s of items) {
        const td = el("td", {className:"right"});
        td.append(sendButton({kind:"scene", key:s.key}));
        tb.append(el("tr", null, el("td", {textContent:s.name}), el("td", {className:"code", textContent:s.code}), td));
      }
      box.append(el("table", null, tb));
    }
  } else {
    if (!CODES.lights.length) { box.append(el("p", {className:"empty", textContent:"照明がありません"})); return; }
    for (const light of CODES.lights) {
      box.append(el("p", {className:"group", textContent:light.name + (light.dimmable ? "" : "（調光なし）")}));
      const tb = el("tbody");
      for (const row of light.rows) {
        const td = el("td", {className:"right"});
        const actions = sendButton({kind:"light", instance:light.instance, brightness:row.brightness});
        if (row.deletable) {
          const del = el("button", {className:"link", textContent:"削除"});
          del.onclick = () => changeBrightness(light.instance, row.brightness, "remove");
          actions.prepend(del);
        }
        td.append(actions);
        tb.append(el("tr", null, el("td", {textContent:row.label}),
                                 el("td", {className:"code", textContent:row.code}), td));
      }
      box.append(el("table", null, tb));
      if (light.can_add) {
        const sel = el("select");
        for (let v = 0; v <= 100; v += 5) sel.append(el("option", {value:String(v), textContent:v + "%"}));
        sel.value = "30";
        const add = el("button", {textContent:"を追加"});
        add.onclick = () => changeBrightness(light.instance, parseInt(sel.value, 10), "add");
        box.append(el("p", null, sel, " ", add));
      }
    }
  }
}

// ■F 状態を変える操作は**応答を捨てない**。旧実装は結果を見ずに一覧を読み直すだけで、
// 「設定ファイルを読めなかったので保存しなかった」（409）が顧客に届かず、保存されていないのに
// 成功に見えた（§24: 表示は事後に確かめた事実だけ）。失敗は閉じるまで残る表示で出す。
// 成功は従来どおり一覧の更新で分かる。成否にかかわらず一覧は読み直す（画面を事実に揃える）。
async function changeBrightness(instance, value, action) {
  const r = await api("/api/brightness", {action:action, instance:instance, value:value});
  if (!r.ok) notice(r.message || "変更できませんでした");
  loadCodes();
}
async function deleteLearned(e) {
  const r = await api("/api/learned/delete", {code:e.code});
  if (!r.ok) notice(r.message || "削除できませんでした");
  loadState();
}

async function loadRecent() {
  const r = await api("/api/recent");
  if (r.offline) return;   // 通信できないときは前の表示を残す（2秒ごとにモーダルを出さない）
  RECENT = r;
  renderRecent();
}

function renderRecent() {
  if (!RECENT) return;   // まだ取れていない＝「読み込み中…」を消さない
  const box = document.getElementById("recent");
  box.textContent = "";
  if (!RECENT.ok || !RECENT.entries.length) {
    box.append(el("p", {className:"empty", textContent:"まだ受信していません"}));
    return;
  }
  const tb = el("tbody");
  for (const e of RECENT.entries) {
    const td = el("td", {className:"act right"});
    if (e.assignable) {
      const b = el("button", {textContent:"シーンに割り当てる"});
      b.onclick = () => askAssign(e.code);
      td.append(b);
    }
    const when = fmtTime(e.last_at);
    tb.append(el("tr", null,
      el("td", {className:"code", textContent:e.code}),
      el("td", {className:"what", textContent:targetLabel(e) + (e.count > 1 ? "（" + e.count + "回）" : "")}),
      el("td", {className:"when", textContent:when}), td));
  }
  box.append(el("table", {className:"stack"}, el("thead", null, el("tr", null,
    el("th", {textContent:"コード"}), el("th", {textContent:"内容"}),
    el("th", {textContent:"受信時刻"}), el("th", {textContent:""}))), tb));
}

// ■B 時刻は**端末のローカル時刻**で短く出す（今日なら HH:MM、別の日なら M/D HH:MM）。
// サーバは UTC の ISO8601 のまま渡す（/diagnostics は開発側向けなので ISO のまま）。
// ★「今日か」は端末のローカル日付で判定する（UTC の日付で見ると、日本では朝9時前の行が前日になる）。
// 整形できない値は**出さない**（機械可読の生値を顧客の画面に出さない。本体 §6-3 の整形と同じ扱い）。
function fmtTime(iso, now) {
  const d = new Date(iso);
  if (!iso || isNaN(d.getTime())) return "";
  const n = now || new Date();
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  const today = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  return today ? hm : (d.getMonth() + 1) + "/" + d.getDate() + " " + hm;
}

function closeModal() { document.getElementById("modal").hidden = true; }
function showModal(nodes, actions) {
  const body = document.getElementById("modal-body"), act = document.getElementById("modal-actions");
  body.textContent = ""; act.textContent = "";
  for (const n of nodes) body.append(n);
  for (const a of actions) act.append(a);
  document.getElementById("modal").hidden = false;
}

function askBase() {
  const sel = document.getElementById("preset");
  if (!STATE) return;
  const wanted = sel.value, to = parseInt(sel.selectedOptions[0].dataset.number, 10);
  // プリセット表に無い base（顧客がファイルを直接編集した場合）は番号が無いので生値を見せる。
  const from = STATE.preset === null ? STATE.base : STATE.preset;
  if (from === to) { toast("変更ありません"); return; }
  const nodes = [
    el("p", {textContent:"IRコードのプリセットを " + from + " から " + to + " に変更します。"}),
    el("p", {textContent:"スマートリモコンに学習させたボタンは、変更後は動作しなくなります。\\nすべて学習し直しが必要です。"}),
    el("p", {className:"note", textContent:"（元のプリセットに戻せば、学習済みのボタンは再び動作します。）"}),
  ];
  if (STATE.learned.length > 0) {
    nodes.push(el("p", {textContent:"お手持ちのリモコンから登録したボタンは、\\n変更後もそのまま動作します。"}));
  }
  const ok = el("button", {className:"primary", textContent:"変更する"});
  ok.onclick = async () => {
    closeModal();
    const r = await api("/api/base", {base:wanted});
    if (r.ok) toast(r.message || "変更しました");
    else notice(r.message || "変更できませんでした");
    await loadState(); loadCodes();
  };
  const cancel = el("button", {textContent:"やめる"});
  cancel.onclick = () => { closeModal(); document.getElementById("preset").value = STATE.base; };
  showModal(nodes, [ok, cancel]);
}

async function askAssign(code) {
  if (!CODES || !CODES.ok) await loadCodes();
  // ★グループごとに <optgroup> で分ける。本体は「シーン名にグループ名を付けて重複を解消する」
  //   設計なので、1列に混ぜると別グループの同名シーンを見分けられない（押すまで分からず、
  //   間違えると learned は登録し直しになる）。学習用コード一覧の小見出しと同じ見え方にする。
  const sel = el("select");
  const byGroup = new Map();
  for (const s of (CODES.scenes || [])) {
    if (!byGroup.has(s.group)) byGroup.set(s.group, []);
    byGroup.get(s.group).push(s);
  }
  for (const [g, items] of [...byGroup.entries()].sort((a, b) => a[0] - b[0])) {
    const grp = el("optgroup", {label:"グループ " + g});
    for (const s of items) grp.append(el("option", {value:s.key, textContent:s.name}));
    sel.append(grp);
  }
  const label = el("input", {type:"text", placeholder:"例: 寝室リモコンの青ボタン"});
  const err = el("p", {className:"msg"});
  const nodes = [
    el("p", {textContent:"受信したコード " + code + " をシーンに割り当てます。"}),
    el("p", null, "シーン ", sel),
    el("p", null, "名前（必須） ", label),
    err,
  ];
  const ok = el("button", {className:"primary", textContent:"登録する"});
  ok.onclick = async () => {
    const r = await api("/api/learned", {code:code, scene:sel.value, label:label.value});
    if (!r.ok) { msg(err, r.message || "登録できませんでした", false); return; }
    closeModal();
    if (r.overlap) notice(overlapText(r.overlap));
    loadState(); loadRecent();
  };
  const cancel = el("button", {textContent:"やめる"});
  cancel.onclick = closeModal;
  showModal(nodes, [ok, cancel]);
}

// ■G 学習の削除は取り消せない（名前も消え、戻すにはリモコンを押し直して名前を入れ直す）ので、
// 画面内モーダルで確認する。★輝度行の削除には付けない（コードは導出なので、足し直せば同じものが戻る）。
function askDeleteLearned(e) {
  const nodes = [
    el("p", {textContent:"「" + e.label + "」の登録を削除します。元に戻せません。"}),
    el("p", {className:"note", textContent:"もう一度使うには、リモコンのボタンを押して、受信ログから割り当て直してください。"}),
  ];
  const ok = el("button", {className:"primary", textContent:"削除する"});
  ok.onclick = async () => { closeModal(); await deleteLearned(e); };
  const cancel = el("button", {textContent:"やめる"});
  cancel.onclick = closeModal;
  showModal(nodes, [ok, cancel]);
}

async function loadState() {
  const r = await api("/api/state");
  if (!r.ok) return;   // 取れなければ前の STATE のまま（プリセットの選択肢を空にしない）
  STATE = r;
  const sel = document.getElementById("preset");
  sel.textContent = "";
  for (const p of STATE.presets) {
    const o = el("option", {value:p.base, textContent:"プリセット " + p.number});
    o.dataset.number = String(p.number);
    sel.append(o);
  }
  sel.value = STATE.base;
  document.getElementById("device").textContent = STATE.device || "見つかりません";
  document.getElementById("no-device").hidden = !!STATE.device;
  renderLearned();
}

function renderLearned() {
  if (!STATE) return;
  const box = document.getElementById("learned");
  box.textContent = "";
  if (!STATE.learned.length) {
    box.append(el("p", {className:"empty", textContent:"登録はありません。受信ログの [シーンに割り当てる] から登録できます。"}));
    return;
  }
  const tb = el("tbody");
  for (const e of STATE.learned) {
    const td = el("td", {className:"act right"});
    const del = el("button", {className:"link", textContent:"削除"});
    del.onclick = () => askDeleteLearned(e);
    td.append(del);
    tb.append(el("tr", null, el("td", {className:"label", textContent:e.label}),
                            el("td", {className:"scene", textContent:sceneName(e.scene)}),
                            el("td", {className:"code", textContent:e.code}), td));
  }
  box.append(el("table", {className:"stack"}, tb));
}

loadState(); loadCodes(); loadRecent();
setInterval(loadRecent, 2000);
</script>
</body>
</html>
"""
