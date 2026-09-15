"""webui: 設定UI（:8100）。M3(d)。

同一プロセス内に立てる（受信スレッド・CommandFirer と同居）。共有状態は ConfigStore と
RecentCodes で、どちらもロック付き。**5秒ごとのRXデバイス監視ループには責務を足さない。**

■ ★認証を付けていない（付け忘れではない。足す前にここを読むこと）
  - 触れるのは IRコードの表示・プリセット選択・受信ログで、守る範囲は :8099
    （同一LANから認証なしで照明を操作できる）と同程度である。ここだけ固くしても意味が薄い。
  - Basic 認証にすると別オリジンなので、本体管理画面から飛んだときに再入力になる
    ＝顧客は同じパスワードを2回入れることになる。
  - プロキシで1回に畳むには**本体（v1.0.5 で凍結中）に転送処理を入れる**ことになり、
    失敗の切り分けも1段増える。
  - 引き受けるリスク: **受信ログから生活パターンが読める**（同一LAN内の相手が前提）。
    これを承知のうえでの判断である。
  - ★**`/diagnostics` で learned と受信ログが1か所に集約されるぶん、個別画面より性質が重い**
    （スクリプトで定期取得すれば生活パターンを継続的に集められる）。判断は変えていない——
    ここに到達できる相手は同一LAN内で、その相手は :8099 で照明を操作できるので、
    集約の度合いが上がっても**守れる範囲は増えない**。**認証を再検討するならここが判断材料になる。**

■ ★アクセスログを永続ログへ流さない
`http.server` の既定は stderr にアクセスログを出す。stderr は
`data/error_addon.log`（WARNING以上・1MBローテ）へ行くので、**2秒ポーリングの
UIを開きっぱなしにすると一晩で枠を食い潰す**（M2-D で踏んだのと同じ形）。
`log_message` を潰して DEBUG へ落としてある。

■ 表示名をキャッシュしない
シーン名・照明名は**毎回** :8099 から取る。公式アプリでの改名をそのまま映すため。
一覧を持たない方針（CLAUDE.md §17）とも揃う。
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import codes, config, diagnostics, lirc
from .client import ClientError
from .codes import LightTarget, SceneTarget
from .nec import bytes_to_code, code_to_bytes
from .sender import REASON_BUSY, REASON_NO_IR_CTL, ir_ctl_path
from .stats import Counters

logger = logging.getLogger(__name__)

PORT = 8100
# 未記載の照明に出す既定の輝度リスト（＝学習させる行）。**輝度の既定はこの1つだけ**
# （M3-b の config.DEFAULT_BRIGHTNESS_LIST は M3-d で削除した。値の違う既定が2か所にあると、
# 次に読む人がどちらが正か調べ直すことになる）。顧客が最初に見る行数を絞る意図で 0% と 100%。
DEFAULT_UI_BRIGHTNESS = [0, 100]

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
SEND_NO_IR_CTL = "送信機能が使えません"


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

    # --- 参照 -----------------------------------------------------------------
    def state(self) -> tuple[int, dict]:
        """詳細設定カードの中身（プリセット・受信デバイス・learned 一覧）。"""
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
            return 200, {"ok": False, "error": "本体（EchoBridge）から一覧を取得できませんでした"}

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
        if result.reason == REASON_NO_IR_CTL:
            return 200, {"ok": False, "message": SEND_NO_IR_CTL, "code": result.code}
        return 200, {"ok": False, "message": f"送信できませんでした（{result.reason}）", "code": result.code}

    # --- 変更 -----------------------------------------------------------------
    def set_brightness_list(self, payload: dict) -> tuple[int, dict]:
        """輝度リストへの行の追加・削除。settings.json に保存して即時反映する。"""
        instance = payload.get("instance")
        value = payload.get("value")
        action = payload.get("action")
        if not isinstance(instance, int) or not isinstance(value, int) or action not in ("add", "remove"):
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
            return 400, {"ok": False, "message": "本体（EchoBridge）から照明の情報を取得できないため変更できません"}
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
        with self._write_lock:
            cfg = self._store.get()
            learned = dict(cfg.learned)
            learned[parsed] = config.LearnedEntry(target=SceneTarget(scene), label=label)
            new = cfg._replace(learned=learned)
            config.save_learned(new)
            self._store.replace(new)
        # ★顧客が手で育てたデータなので、追加も削除も1行残す（失うと学習し直しになる）。
        logger.info("学習コードを追加しました（%s → %s「%s」）", code, scene, label)
        # 導出コードと衝突する場合は**登録を許したうえで**警告を返す（learned が先に引かれる）。
        derived = codes.decode(cfg.base, parsed[1])
        warning = None
        if derived is not None and derived.target is not None:
            warning = (
                f"このコードは本機のコード（{derived.target.summary}）と重なっています。"
                "登録した内容が優先されます"
            )
            logger.info("learned に登録したコードが導出コードと重なっている: %s", code)
        return 200, {"ok": True, "warning": warning}

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
    if kind == "light" and isinstance(payload.get("instance"), int) and isinstance(payload.get("brightness"), int):
        return LightTarget(payload["instance"], payload["brightness"])
    return None


# --- HTTP ---------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    app: UiApp = None  # サブクラスで差し込む
    server_version = "ir-bridge-ui"
    sys_version = ""

    def log_message(self, fmt, *args):
        """★アクセスログを stderr（＝永続ログ）へ出さない。モジュール冒頭の注記参照。"""
        logger.debug("ui %s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        # 既定は log_message 経由だが、明示しておく（400等でも永続ログへ出さない）。
        logger.debug("ui %s - %s", self.address_string(), fmt % args)

    def do_GET(self):
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

    def do_POST(self):
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("object ではない")
        except (ValueError, OSError):
            return self._send_json(400, {"ok": False, "message": "入力を読めません"})
        try:
            status, body = handler(payload)
        except Exception:  # noqa: BLE001 - UIの1操作で常駐を落とさない
            logger.warning("設定UIの処理で例外（%s）", path, exc_info=True)
            return self._send_json(500, {"ok": False, "message": "本機の内部エラーです"})
        return self._send_json(status, body)

    def _send_json(self, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str):
        data = html.encode("utf-8")
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


def make_server(app: UiApp, port: int = PORT) -> ThreadingHTTPServer:
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer(("", port), handler)
    server.daemon_threads = True
    return server


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
/* 受信ログは高さを固定してスクロールさせる。行数で下のカード（詳細設定）の位置が動くと、
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
.scrollbox{height:340px;overflow-y:auto;border:1px solid var(--line);border-radius:8px;padding:0 10px}
.scrollbox table{margin:0}
.scrollbox thead th{position:sticky;top:0;background:var(--card)}
.modal{position:fixed;inset:0;background:rgba(20,22,26,.45);display:flex;align-items:center;justify-content:center;padding:16px}
.modal[hidden]{display:none}
.modal .box{background:#fff;border-radius:12px;padding:22px 24px;max-width:520px;width:100%}
.modal p{margin:0 0 12px;white-space:pre-line}
.modal .note{color:var(--muted);font-size:13.5px}
.modal .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.empty{color:var(--muted);font-size:13.5px;padding:10px 0}
.row-actions{display:flex;gap:6px;justify-content:flex-end;align-items:center}
</style>
</head>
<body>
<div class="wrap">
<h1>赤外線リモコン連携</h1>

<div class="card">
  <h2>IRリモコン設定</h2>
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

<div class="card">
  <details id="advanced">
    <summary>詳細設定</summary>
    <div style="margin-top:14px">
      <h2 style="font-size:15px">IRコードのプリセット
        <span class="msg">お手持ちの他のリモコンを操作したときに照明が動いてしまう場合は、プリセットを変更してください。</span>
      </h2>
      <select id="preset"></select>
      <button onclick="askBase()">変更</button>
      <span class="msg" id="base-msg"></span>

      <h2 style="font-size:15px;margin-top:22px">受信デバイス</h2>
      <p class="hint" id="device">—</p>

      <details style="margin-top:18px">
        <summary>お手持ちのリモコンでシーンを操作する</summary>
        <p class="learned-note">お手持ちのリモコン（テレビのリモコンなど）は、機器に向けて使う前提のものが
          多く、スマートリモコンほど広い範囲には届きません。登録したボタンが効かないときは、
          <strong>本機のほうに向けて</strong>押してみてください。</p>
        <div id="learned"></div>
      </details>
    </div>
  </details>
</div>
</div>

<div class="modal" id="modal" hidden><div class="box">
  <div id="modal-body"></div>
  <div class="actions" id="modal-actions"></div>
</div></div>

<script>
let TAB = "scenes", CODES = null, STATE = null, RECENT = null;

async function api(path, body) {
  const opt = body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {};
  const res = await fetch(path, opt);
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

function targetLabel(e) {
  const t = e.target;
  if (!t) return e.summary;   // 宛先が無い行（未登録・範囲外・別プリセット）はそのまま
  if (t.kind === "scene") return "シーン「" + sceneName(t.key) + "」を発火";
  if (t.kind === "light") {
    return lightName(t.instance) + " を " + brightnessLabel(t.instance, t.brightness) + " に設定";
  }
  return e.summary;
}

function sendButton(payload) {
  const out = el("span", {className:"msg"});
  const btn = el("button", {className:"primary", textContent:"送信"});
  btn.onclick = async () => {
    btn.disabled = true; msg(out, "送信中…", true);
    const r = await api("/api/send", payload);
    msg(out, r.message || (r.ok ? "送信しました" : "送信できませんでした"), !!r.ok);
    btn.disabled = false;
  };
  return el("div", {className:"row-actions"}, out, btn);
}

function renderCodes() {
  const box = document.getElementById("codes");
  box.textContent = "";
  if (!CODES || !CODES.ok) {
    box.append(el("p", {className:"empty", textContent:(CODES && CODES.error) || "一覧を取得できませんでした"}));
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
          del.onclick = async () => {
            await api("/api/brightness", {action:"remove", instance:light.instance, value:row.brightness});
            loadCodes();
          };
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
        add.onclick = async () => {
          await api("/api/brightness", {action:"add", instance:light.instance, value:parseInt(sel.value, 10)});
          loadCodes();
        };
        box.append(el("p", null, sel, " ", add));
      }
    }
  }
}

async function loadRecent() {
  RECENT = await api("/api/recent");
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
    const td = el("td", {className:"right"});
    if (e.assignable) {
      const b = el("button", {textContent:"シーンに割り当てる"});
      b.onclick = () => askAssign(e.code);
      td.append(b);
    }
    const when = (e.last_at || "").replace("T", " ").replace("+00:00", "");
    tb.append(el("tr", null,
      el("td", {className:"code", textContent:e.code}),
      el("td", {textContent:targetLabel(e) + (e.count > 1 ? "（" + e.count + "回）" : "")}),
      el("td", {className:"code", textContent:when}), td));
  }
  box.append(el("table", null, el("thead", null, el("tr", null,
    el("th", {textContent:"コード"}), el("th", {textContent:"内容"}),
    el("th", {textContent:"受信時刻(UTC)"}), el("th", {textContent:""}))), tb));
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
  if (from === to) { msg(document.getElementById("base-msg"), "変更ありません", true); return; }
  const nodes = [
    el("p", {textContent:"IRコードのプリセットを " + from + " から " + to + " に変更します。"}),
    el("p", {textContent:"スマートリモコンに学習させたボタンは、変更後は動作しなくなります。\\nすべて学習し直しが必要です。"}),
    el("p", {className:"note", textContent:"（元のプリセットに戻せば、学習済みのボタンは再び動作します。）"}),
  ];
  if (STATE.learned.length > 0) {
    nodes.push(el("p", {textContent:"お手持ちのリモコンから登録したボタン（詳細設定）は、\\n変更後もそのまま動作します。"}));
  }
  const ok = el("button", {className:"primary", textContent:"変更する"});
  ok.onclick = async () => {
    closeModal();
    const r = await api("/api/base", {base:wanted});
    msg(document.getElementById("base-msg"), r.ok ? "変更しました" : (r.message || "変更できませんでした"), !!r.ok);
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
    if (r.warning) alert(r.warning);
    loadState(); loadRecent();
  };
  const cancel = el("button", {textContent:"やめる"});
  cancel.onclick = closeModal;
  showModal(nodes, [ok, cancel]);
}

async function loadState() {
  STATE = await api("/api/state");
  const sel = document.getElementById("preset");
  sel.textContent = "";
  for (const p of STATE.presets) {
    const o = el("option", {value:p.base, textContent:"プリセット " + p.number});
    o.dataset.number = String(p.number);
    sel.append(o);
  }
  sel.value = STATE.base;
  document.getElementById("device").textContent = STATE.device || "見つかりません";
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
    const td = el("td", {className:"right"});
    const del = el("button", {className:"link", textContent:"削除"});
    del.onclick = async () => { await api("/api/learned/delete", {code:e.code}); loadState(); };
    td.append(del);
    tb.append(el("tr", null, el("td", {textContent:e.label}),
                            el("td", {textContent:sceneName(e.scene)}),
                            el("td", {className:"code", textContent:e.code}), td));
  }
  box.append(el("table", null, tb));
}

loadState(); loadCodes(); loadRecent();
setInterval(loadRecent, 2000);
</script>
</body>
</html>
"""
