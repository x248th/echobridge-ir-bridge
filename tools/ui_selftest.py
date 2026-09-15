#!/usr/bin/env python3
"""ui_selftest: 設定UI（webui.py）の自己テスト。

実機・実HTTP・本体API なしで走る。本体クライアントも IrSender も偽物に差し替え、
設定ファイルは一時ディレクトリへ向ける（data/ を汚さない・赤外線も出ない）。

    python3 tools/ui_selftest.py    # 全件PASSなら rc=0
"""
import io
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from ir_bridge import codes, config, webui  # noqa: E402
from ir_bridge.client import ClientError  # noqa: E402
from ir_bridge.codes import LightTarget, SceneTarget  # noqa: E402
from ir_bridge.nec import code_to_bytes  # noqa: E402
from ir_bridge.recent import RecentCodes  # noqa: E402
from ir_bridge.lirc import LircDevice  # noqa: E402
from ir_bridge.sender import REASON_BUSY, REASON_NO_IR_CTL, IrSender, SendResult  # noqa: E402
from ir_bridge.stats import Counters  # noqa: E402
from nec_selftest import IRDROID as IRDROID_WAVE  # noqa: E402
from nec_selftest import build_frame  # noqa: E402
from tx_selftest import FakeReceiver, NoisyReceiver, invert  # noqa: E402

IRDROID_DEV = LircDevice("/dev/lirc2", 0x30040102, "ir_toy", "Infrared Toy")

SCENES = [
    {"key": "scene_01", "display_name": "全消灯", "group": 0},
    {"key": "scene_02", "display_name": "全点灯", "group": 0},
    {"key": "scene_15", "display_name": "昼間", "group": 1},
]
LIGHTS = [
    {"instance": 1, "name": "ロフトダウンライト", "dimmable": True},
    {"instance": 2, "name": "玄関", "dimmable": False},
]

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


class _Capture(logging.Handler):
    def __init__(self, level=logging.INFO):
        super().__init__()
        self.records = []
        self._want = level

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def __enter__(self):
        root = logging.getLogger("ir_bridge")
        self._saved = root.level
        root.setLevel(self._want)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger("ir_bridge")
        root.removeHandler(self)
        root.setLevel(self._saved)

    def of(self, level):
        return [m for lv, m in self.records if lv == level]


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def get_scenes(self):
        self.calls += 1
        if self.fail:
            raise ClientError("接続失敗")
        return [dict(s) for s in SCENES]

    def get_lights(self):
        if self.fail:
            raise ClientError("接続失敗")
        return [dict(light) for light in LIGHTS]


class FakeSender:
    """IrSender の代役。赤外線は出さず、呼ばれた宛先を記録する。"""

    def __init__(self, result=None, raise_=None):
        self.sent = []
        self.result = result
        self.raise_ = raise_

    def send(self, target):
        if self.raise_ is not None:
            raise self.raise_
        self.sent.append(target)
        return self.result or SendResult(True, "nec32:0xdeadbeef")


def build(tmp: Path, base=codes.DEFAULT_BASE, learned=None, brightness_lists=None, client=None, sender=None):
    """一時ディレクトリに設定ファイルを向けた UiApp を作る。"""
    config.SETTINGS_FILE = tmp / "settings.json"
    config.LEARNED_FILE = tmp / "learned.json"
    cfg = config.Config(base, 1000, brightness_lists or {}, learned or {})
    store = config.ConfigStore(cfg)
    recent = RecentCodes()
    app = webui.UiApp(store, recent, sender or FakeSender(), client or FakeClient(), rx_spec=None)
    return app, store, recent


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def page_section() -> None:
    print("■ ページHTML（4カードが上から設置順に出る）")
    html = webui.PAGE_HTML
    # ★見出しのマークアップで位置を見る（本文やCSSコメントにも同じ語が出るため、
    #   素の文字列だと最初のヒットが見出しとは限らない）。
    order = ["<h2>IRリモコン設定</h2>", "<h2>学習用コード一覧</h2>", "<h2>受信ログ</h2>",
             "<summary>詳細設定</summary>"]
    positions = [html.find(h) for h in order]
    check("4カードの見出しがすべてある", all(p >= 0 for p in positions), str(dict(zip(order, positions))))
    check("上から設置順に並んでいる", positions == sorted(positions), str(positions))
    for text in [
        "この画面から、シーンや照明ごとの赤外線コードをスマートリモコンに学習させます。",
        "詳しい手順",
        "お手持ちの他のリモコンを操作したときに照明が動いてしまう場合は、プリセットを変更してください。",
        "お手持ちのリモコンでシーンを操作する",
    ]:
        check(f"文面がある: {text[:24]}…", text in html)
    check("タブは シーン / 個別照明 の2つ", html.count('role="tab"') == 2)
    # ★カテゴリの案内は折り畳みの中に置かない（読まれないため）。
    first_details = html.index("<details>")
    check("「その他の機器」の案内が案内カードの本文にある（折り畳みの外）",
          "その他の機器" in html and html.index("その他の機器") < first_details,
          f"位置={html.index('その他の機器')} 折り畳み={first_details}")
    check("  「照明」カテゴリを選ばないことが書いてある", "「照明」ではなく" in html)
    # ★「失敗が見えない故障」であることは残す（顧客が別の原因を疑うのを避けるため）。
    #   ただし因果（照合されると何が再生されるか）は未実測なので断定形では書かない。
    check("  失敗が見えない故障であることが書いてある", "学習できたように見えて動かない" in html)
    check("  実測していない因果を断定していない",
          "代わりに" not in html and "本機が受け取れず" not in html)
    # ★注記は「症状」で書く。名指しだけだと、検証していない機種に当たった顧客が判断できない。
    note_at = html.index("学習モード（受信待機）にしたあと")
    check("同一端末で学習できない場合の注記が案内カード本文にある（折り畳みの外）",
          note_at < first_details, f"位置={note_at} 折り畳み={first_details}")
    check("  症状で書いてある（機種名だけに頼らない）",
          "赤外線を受け付けられないことがあります" in html and "うまく学習できない場合は" in html)
    check("  網羅的に検証した印象を与えない（「確認済み」と書かない）",
          "Nature Remo で確認しています" in html and "確認済み" not in html)
    check("  別端末で開く先は「この画面」（本体の管理画面 :8080 と混同させない）",
          "この画面を同じネットワークにつないだ" in html and "管理画面" not in html)
    print("■ 案内の段階開示（トップ＝何をするか／詳しい手順＝どうやるか・つまずいたら）")
    # ★カード見出しから切り出す（<ol> からだと冒頭の説明文がスライスの外になり、
    #   いま捕まえたい「冒頭と手順の言い直し」がそもそも検査対象にならない）。
    top = html[html.index("<h2>IRリモコン設定</h2>"):html.index("<details>")]
    detail = html[html.index('<div class="detail">'):html.index("</details>")]
    check("距離の案内がトップの手順の先頭（カテゴリより前）",
          "赤外線が届く場所" in top and top.index("赤外線が届く場所") < top.index("その他の機器"))
    check("  学習時だけ近づける指示がトップにある",
          "学習のときだけは、本機にできるだけ近づけてください" in top)
    check("  壁の反射でも届くことを書いている（本機に向けないと動かない、と思わせない）",
          "壁に反射する程度でも" in top)
    check("  届かないときの対処が同じ場所にある（壊れたと判断させない）",
          "本機に近づけたり" in top and "間に物が入らない場所" in top)
    # ★距離の数字は書かない。8m は上限ではない（測っていないだけ）し、学習時は機種差が出うる。
    check("★距離の数字を書いていない（8m・50cm とも）",
          "8m" not in html and "50cm" not in html and "50 cm" not in html)
    for heading in ("作業の全体像", "置き場所を決める", "ボタンの名前の付け方", "どちらから作るか",
                    "うまくいかないとき", "同じ端末で学習できないとき"):
        check(f"詳しい手順に節がある: {heading}", f"<h3>{heading}</h3>" in detail)
    check("  切り分け表が4症状ぶんある", detail.count("<tr><td>") == 4, f"{detail.count('<tr><td>')}行")
    check("  受信ログが切り分けの道具だと書いてある", "受信ログに出れば" in detail)
    check("  ★学習後に運用位置へ戻す指示がある（戻さないと動かなくなる）",
          "運用中に届く場所へ戻してください" in detail)
    check("  USB延長で受信部を引き出せることを書いている", "USB延長" in detail)
    check("  名前の付け方の例がある", "リビング 50%" in detail)
    # ★段階開示のルール: 詳しい手順はトップより一段深い＝同じ文を繰り返さない。
    check("★トップの文を詳しい手順で繰り返していない",
          html.count("などのカテゴリを選んでください") == 1 and "壁に反射する程度でも" not in detail)
    # ★トップ「内部」の重複も縛る。上の検査は詳しい手順との重複しか見ておらず、
    #   冒頭の前置きが手順3・4を言い直している状態を捕まえられなかった（M3-g の見落とし）。
    #   言い直しは同じ語で書かれるので、手順を特定する語句の出現回数で足りる。
    for phrase in ("[送信] を押", "名前を付けて保存"):
        check(f"  トップ内部で言い直していない: {phrase}", top.count(phrase) == 1,
              f"{top.count(phrase)}回")
    check("  冒頭は「この画面でできること」の1行（手順の前置きにしない）",
          "この画面から、" in top and "学習モードにしてから" not in top)

    print("■ learned カードの注記（普通のリモコンは指向性が強い）")
    check("learned の折り畳みの中に注記がある",
          "本機のほうに向けて" in html
          and html.index("お手持ちのリモコンでシーンを操作する") < html.index("本機のほうに向けて"))
    check("  向きの話だと分かる書き方（距離だけの話にしない）",
          "機器に向けて使う前提" in html and "スマートリモコンほど広い範囲には届きません" in html)

    check("受信ログは高さ固定のスクロール窓（下のカードの位置が動かない）",
          'id="recent" class="scrollbox"' in html and ".scrollbox{height:" in html)
    check("受信ログは2秒ポーリング", "setInterval(loadRecent, 2000)" in html)
    check("外部CDNを読まない（CSSフレームワークを持ち込まない）",
          "http://" not in html and "https://" not in html)
    check("「ハブ」の語を使っていない（スマートリモコンで統一）", "ハブ" not in html)
    check("受信デバイスは表示のみ（変更するUIが無い）",
          "IR_BRIDGE_RX_DEVICE" not in html and 'id="device"' in html)
    # ★シーンを一覧する箇所はすべてグループで分ける（別グループの同名シーンを見分けるため）。
    modal = html[html.index("async function askAssign"):html.index("async function loadState")]
    check("割り当てモーダルのプルダウンは optgroup でグループ分けする",
          "optgroup" in modal and "byGroup" in modal, "askAssign 内に optgroup が無い")
    check("  グループ番号順に並べる", "sort((a, b) => a[0] - b[0])" in modal)
    tab = html[html.index("function renderCodes"):html.index("async function loadRecent")]
    check("学習用コード一覧（シーンタブ）もグループごとの小見出しで分ける",
          'className:"group"' in tab and "groups" in tab)


def device_display_section(tmp: Path) -> None:
    """受信デバイスの表示（UIは役割だけ・診断レポートは完全な情報）。"""
    print("■ 受信デバイスの表示")
    check("RX+TX は「受信・送信対応」", webui.role_label(IRDROID_DEV) == "受信・送信対応")
    check("受信のみのデバイスは「受信のみ」",
          webui.role_label(LircDevice("/dev/lirc1", 0x10040000, "gpio_ir_recv", "gpio_ir_recv")) == "受信のみ")
    check("送信のみのデバイスは「送信のみ」",
          webui.role_label(LircDevice("/dev/lirc0", 0x00000302, "gpio-ir-tx", "GPIO")) == "送信のみ")
    app, _, _ = build(tmp)
    _s, state = app.state()
    if state["device"] is None:
        check("デバイスが無ければ None（画面には「見つかりません」と出る）", True, "この機体では未検出")
    else:
        check("★UIにドライバ名を出さない（ir_toy / Toy を含まない）",
              "ir_toy" not in state["device"] and "Toy" not in state["device"], state["device"])
        check("  パスと役割は出す", state["device"].startswith("/dev/lirc"), state["device"])
    check("画面はデバイスが無いとき「見つかりません」と出す",
          'STATE.device || "見つかりません"' in webui.PAGE_HTML)
    # ★診断レポート側は完全な情報のまま（宛先が違う）。
    text = app.diagnostics_text()
    devices_line = [line for line in text.splitlines() if "lircデバイス一覧" in line]
    check("診断レポートにはドライバ名が残っている（切り分け用）",
          bool(devices_line) and ("ir_toy" in devices_line[0] or "なし" in devices_line[0]),
          devices_line[0] if devices_line else "行が無い")


def codes_section(tmp: Path) -> None:
    print("■ 学習用コード一覧（毎回 本体から取得・キャッシュしない）")
    app, store, _ = build(tmp)
    status, body = app.codes_list()
    check("200 で ok", status == 200 and body["ok"], str(body)[:80])
    scene = [s for s in body["scenes"] if s["key"] == "scene_15"][0]
    check("シーンの導出コード", scene["code"] == "nec32:0x7d2e000f", str(scene))
    check("シーン名は本体の display_name", scene["name"] == "昼間", str(scene))
    check("group を持つ", scene["group"] == 1, str(scene))
    light = body["lights"][0]
    check("既定の輝度リストは 0% と 100% の2行",
          [r["brightness"] for r in light["rows"]] == [0, 100], str(light["rows"]))
    check("照明の導出コード（0%）", light["rows"][0]["code"] == "nec32:0x7d2e0100", str(light["rows"][0]))
    check("照明の導出コード（100%）", light["rows"][1]["code"] == "nec32:0x7d2e0164", str(light["rows"][1]))
    check("dimmable をそのまま返す（プルダウンの出し分け用）",
          body["lights"][0]["dimmable"] is True and body["lights"][1]["dimmable"] is False)

    print("■ 行の表記と可否（★開発機に調光非対応の照明が無いので、両分岐はここでしか確かめられない）")
    dim, fixed = body["lights"][0], body["lights"][1]
    check("調光対応: 0% は OFF、100% は 100% のまま（/on の前回値復元と誤解させない）",
          [r["label"] for r in dim["rows"]] == ["OFF", "100%"], str([r["label"] for r in dim["rows"]]))
    check("調光非対応: OFF と ON",
          [r["label"] for r in fixed["rows"]] == ["OFF", "ON"], str([r["label"] for r in fixed["rows"]]))
    check("調光対応: OFF は消せない・100% は消せる",
          [r["deletable"] for r in dim["rows"]] == [False, True], str([r["deletable"] for r in dim["rows"]]))
    check("調光非対応: どちらも消せない（2行固定）",
          [r["deletable"] for r in fixed["rows"]] == [False, False], str([r["deletable"] for r in fixed["rows"]]))
    check("追加のプルダウンは調光対応にだけ出す",
          dim["can_add"] is True and fixed["can_add"] is False, str((dim["can_add"], fixed["can_add"])))
    check("途中の輝度は % 表記のまま", webui.row_label(30, True) == "30%")
    app_mid, _, _ = build(tmp, brightness_lists={"1": [0, 30, 100]})
    _s, mid = app_mid.codes_list()
    check("  追加した行も OFF/％ の規則どおり",
          [r["label"] for r in mid["lights"][0]["rows"]] == ["OFF", "30%", "100%"],
          str([r["label"] for r in mid["lights"][0]["rows"]]))
    app_fixed, _, _ = build(tmp, brightness_lists={"2": [0, 30, 100]})
    _s, fx = app_fixed.codes_list()
    check("調光非対応は settings.json に記載があっても2行固定",
          [r["brightness"] for r in fx["lights"][1]["rows"]] == [0, 100],
          str([r["brightness"] for r in fx["lights"][1]["rows"]]))
    client = FakeClient()
    app2, _, _ = build(tmp, client=client)
    app2.codes_list()
    app2.codes_list()
    check("呼ぶたびに本体へ取りに行く（改名を反映するため）", client.calls == 2, f"calls={client.calls}")

    app3, _, _ = build(tmp, brightness_lists={"1": [0, 30, 100]})
    _s, body3 = app3.codes_list()
    check("settings.json の brightness_lists があればそれを使う",
          [r["brightness"] for r in body3["lights"][0]["rows"]] == [0, 30, 100], str(body3["lights"][0]["rows"]))

    app4, _, _ = build(tmp, client=FakeClient(fail=True))
    with _Capture() as cap:
        status4, body4 = app4.codes_list()
    check("本体が落ちていても 200 で理由を返す（UIが壊れない）",
          status4 == 200 and body4["ok"] is False and "本体" in body4["error"], str(body4))
    check("  WARNINGは1行だけ", len(cap.of("WARNING")) == 1, str(cap.records))


def brightness_section(tmp: Path) -> None:
    print("■ 輝度リストの追加・削除（settings.json に保存して即時反映）")
    app, store, _ = build(tmp)
    status, _ = app.set_brightness_list({"action": "add", "instance": 1, "value": 30})
    check("追加できる", status == 200)
    check("  メモリ上の Config に反映される", store.get().brightness_lists["1"] == [0, 30, 100],
          str(store.get().brightness_lists))
    check("  settings.json に保存される",
          read_json(config.SETTINGS_FILE)["brightness_lists"]["1"] == [0, 30, 100],
          str(read_json(config.SETTINGS_FILE)))
    _s, body = app.codes_list()
    check("  一覧に行が増える", [r["brightness"] for r in body["lights"][0]["rows"]] == [0, 30, 100])
    app.set_brightness_list({"action": "remove", "instance": 1, "value": 30})
    check("削除できる", store.get().brightness_lists["1"] == [0, 100], str(store.get().brightness_lists))
    check("  settings.json にも反映される",
          read_json(config.SETTINGS_FILE)["brightness_lists"]["1"] == [0, 100])
    status, body = app.set_brightness_list({"action": "add", "instance": 1, "value": 101})
    check("範囲外は弾く", status == 400, str(body))
    status, body = app.set_brightness_list({"action": "remove", "instance": 1, "value": 0})
    check("★OFF は削除できない（赤外線から照明を消せなくなるため・全照明）",
          status == 400 and "OFF" in body["message"], str(body))
    app2, store2, _ = build(tmp, brightness_lists={"1": [0, 50]})
    status, _ = app2.set_brightness_list({"action": "remove", "instance": 1, "value": 50})
    check("  OFF さえ残れば他の行は自由に消せる（リストが空にならない）",
          status == 200 and store2.get().brightness_lists["1"] == [0], str(store2.get().brightness_lists))
    status, body = app.set_brightness_list({"action": "add", "instance": 2, "value": 30})
    check("調光非対応には行を足せない（経路としても塞ぐ）",
          status == 400 and "調光" in body["message"], str(body))
    status, body = app.set_brightness_list({"action": "remove", "instance": 2, "value": 100})
    check("調光非対応は ON も消せない", status == 400, str(body))
    status, body = app.set_brightness_list({"action": "add", "instance": 99, "value": 30})
    check("存在しない照明は 404", status == 404, str(body))
    app_down, _, _ = build(tmp, client=FakeClient(fail=True))
    with _Capture() as cap:
        status, body = app_down.set_brightness_list({"action": "add", "instance": 1, "value": 30})
    check("本体が落ちていたら変更しない（調光可否を確かめられないため）",
          status == 400 and "取得できない" in body["message"], str(body))
    check("  WARNING 1行", len(cap.of("WARNING")) == 1, str(cap.records))
    status, body = app.set_brightness_list({"action": "nonsense", "instance": 1, "value": 10})
    check("不正な操作は 400", status == 400, str(body))


def base_section(tmp: Path) -> None:
    print("■ プリセット切替（settings.json を書いてから replace・M3-c の即時反映）")
    app, store, _ = build(tmp)
    raw = codes.encode_light(codes.PRESETS[0], 3, 50)
    check("切替前: プリセット1のコードが導出として解釈される",
          codes.decode(store.get().base, raw).source == "derived")
    with _Capture() as cap:
        status, _ = app.set_base({"base": "0x4b6a"})
    check("切替できる", status == 200 and store.get().base == codes.PRESETS[1], str(store.get().base))
    check("  settings.json に保存される", read_json(config.SETTINGS_FILE)["base"] == "0x4b6a")
    check("  decode の結果が変わる（同じコードが別プリセット扱いになる）",
          codes.decode(store.get().base, raw).source == "other_preset")
    check("  新プリセットのコードは導出として解釈される",
          codes.decode(store.get().base, codes.encode_light(codes.PRESETS[1], 3, 50)).source == "derived")
    check("  ベース変更のログが出る（M3-c の replace 経路を通っている）",
          any("ベースを" in m for m in cap.of("INFO")), str(cap.records))
    _s, body = app.codes_list()
    check("  一覧のコードも新プリセットになる",
          [s for s in body["scenes"] if s["key"] == "scene_15"][0]["code"] == "nec32:0x4b6a000f")
    status, body = app.set_base({"base": "0x1234"})
    check("プリセット表に無い値は弾く", status == 400 and store.get().base == codes.PRESETS[1], str(body))
    status, body = app.set_base({"base": "ぬるぽ"})
    check("読めない値は弾く", status == 400, str(body))


def learned_section(tmp: Path) -> None:
    print("■ learned の登録・削除（受信ログの [シーンに割り当てる] から）")
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver

    app, store, recent = build(tmp)
    status, body = app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": ""})
    check("label が無ければ登録させない（後で判別できなくなる）", status == 400, str(body))
    status, body = app.add_learned({"code": "こわれた", "scene": "scene_15", "label": "x"})
    check("読めないコードは 400", status == 400, str(body))
    status, body = app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "寝室リモコンの青"})
    check("登録できる", status == 200 and body.get("warning") is None, str(body))
    check("  learned.json に保存される",
          read_json(config.LEARNED_FILE)["entries"]["nec:0x4008"]["target"]["scene"] == "scene_15",
          str(read_json(config.LEARNED_FILE)))
    check("  label も保存される",
          read_json(config.LEARNED_FILE)["entries"]["nec:0x4008"]["label"] == "寝室リモコンの青")
    key = code_to_bytes("nec:0x4008")
    check("  メモリ上の Config に即時反映される", key in store.get().learned, str(store.get().learned))

    # 解釈順序: learned が導出より先に引かれる（受信スレッドの経路で確かめる）
    rx = IrReceiver(store, recent, CommandFirer(object()), rx_spec=None)
    interp = rx._interpret(store.get(), key, key[1])
    check("受信時の解釈で learned が引かれる",
          interp.source == "learned" and interp.target == SceneTarget("scene_15"), str(interp))
    derived_raw = codes.encode_light(store.get().base, 3, 50)
    status, body = app.add_learned(
        {"code": "nec32:0x7d2e0332", "scene": "scene_01", "label": "衝突する登録"}
    )
    check("導出コードと重なる登録は許すが警告を返す",
          status == 200 and body.get("warning") and "重なっています" in body["warning"], str(body))
    interp = rx._interpret(store.get(), ("nec32", derived_raw), derived_raw)
    check("  衝突時は顧客の設定（learned）が勝つ",
          interp.source == "learned" and interp.target == SceneTarget("scene_01"), str(interp))

    _s, state = app.state()
    check("詳細設定の一覧に2件出る", len(state["learned"]) == 2, str(state["learned"]))
    check("  label とシーンとコードが入る",
          all(set(e) >= {"code", "scene", "label"} for e in state["learned"]), str(state["learned"]))
    status, _ = app.delete_learned({"code": "nec:0x4008"})
    check("削除できる", status == 200 and key not in store.get().learned)
    check("  learned.json からも消える", "nec:0x4008" not in read_json(config.LEARNED_FILE)["entries"])
    status, body = app.delete_learned({"code": "nec:0x0001"})
    check("無い登録の削除は 404", status == 404, str(body))


def recent_section(tmp: Path) -> None:
    print("■ 受信ログ（未登録の行にだけ [シーンに割り当てる]）")
    app, store, recent = build(tmp)
    recent.record("nec32:0x7d2e0332", "nec32", "照明3 を 50% に設定", "derived", True)
    recent.record("nec:0x4008", "nec", "未登録（learned にも導出にも一致せず）", "unknown", False)
    recent.record("nec32:0x7d2e5b91", "nec32", "シーン scene_15 を発火", "learned", True)
    recent.record("nec32:0x4b6a0332", "nec32", "プリセット2 のコードです", "other_preset", False)
    status, body = app.recent()
    by_code = {e["code"]: e for e in body["entries"]}
    check("200 で全件返す（差分もカーソルも持たない）", status == 200 and len(body["entries"]) == 4)
    check("未登録の行だけ assignable", by_code["nec:0x4008"]["assignable"] is True)
    check("導出の行は assignable でない", by_code["nec32:0x7d2e0332"]["assignable"] is False)
    check("learned の行は assignable でない", by_code["nec32:0x7d2e5b91"]["assignable"] is False)
    check("別プリセットの行も assignable でない", by_code["nec32:0x4b6a0332"]["assignable"] is False)
    check("解釈結果をそのまま画面に出せる",
          by_code["nec32:0x7d2e0332"]["summary"] == "照明3 を 50% に設定")
    # ★登録した直後の行は source が unknown のまま残る。ボタンが消えないと二度登録しようとする。
    app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "登録後の行"})
    _s, body2 = app.recent()
    after = {e["code"]: e for e in body2["entries"]}
    check("登録した直後の描き直しで割り当てボタンが消える", after["nec:0x4008"]["assignable"] is False,
          str(after["nec:0x4008"]))
    check("  受信ログの行自体は残る（履歴なので消さない）", len(body2["entries"]) == 4)
    app.delete_learned({"code": "nec:0x4008"})
    _s, body3 = app.recent()
    check("登録を消せばまた割り当てられる",
          {e["code"]: e for e in body3["entries"]}["nec:0x4008"]["assignable"] is True)


def send_section(tmp: Path) -> None:
    print("■ 送信ボタン（結果の3分岐）")
    sender = FakeSender(SendResult(True, "nec32:0x7d2e0332"))
    app, _, _ = build(tmp, sender=sender)
    status, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
    check("照明の送信が IrSender へ渡る", sender.sent == [LightTarget(3, 50)], str(sender.sent))
    check("  文面（送信した）", status == 200 and body["ok"] and body["message"] == webui.SEND_OK, str(body))
    app.send({"kind": "scene", "key": "scene_15"})
    check("シーンの送信が IrSender へ渡る", sender.sent[-1] == SceneTarget("scene_15"), str(sender.sent))

    app, _, _ = build(tmp, sender=FakeSender(SendResult(False, "nec32:0x7d2e0332", REASON_BUSY)))
    _s, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
    check("見送ったときの文面", body["ok"] is False and body["message"] == webui.SEND_BUSY, str(body))
    check("  「他のリモコン等を操作していないことを確認」が入る", "他のリモコン等" in body["message"])
    app, _, _ = build(tmp, sender=FakeSender(SendResult(False, "nec32:0x7d2e0332", REASON_NO_IR_CTL)))
    _s, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
    check("ir-ctl が無いときの文面", body["message"] == webui.SEND_NO_IR_CTL, str(body))
    app, _, _ = build(tmp, sender=FakeSender(SendResult(False, "nec32:0x7d2e0332", "ir-ctl rc=1")))
    _s, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
    check("その他の失敗は理由を見せる", body["ok"] is False and "ir-ctl rc=1" in body["message"], str(body))
    app, _, _ = build(tmp, sender=FakeSender(raise_=ValueError("輝度が範囲外: 101")))
    status, body = app.send({"kind": "light", "instance": 3, "brightness": 101})
    check("表現できない宛先は 400（常駐は落ちない）", status == 400, str(body))
    app, _, _ = build(tmp)
    status, body = app.send({"kind": "なにか"})
    check("不正な送信先は 400", status == 400, str(body))


def http_section(tmp: Path) -> None:
    print("■ HTTP層（アクセスログを永続ログへ流さない）")
    app, _, _ = build(tmp)
    handler_cls = type("_T", (webui._Handler,), {"app": app})

    class _FakeHandler(handler_cls):
        def __init__(self):  # BaseHTTPRequestHandler の __init__ は通信を始めるので通さない
            self.client_address = ("192.168.1.9", 51234)

    h = _FakeHandler()
    saved, sys.stderr = sys.stderr, io.StringIO()
    try:
        with _Capture(logging.DEBUG) as cap:
            h.log_message('"%s" %s %s', "GET /api/recent HTTP/1.1", "200", "-")
            h.log_error("code %d, message %s", 400, "Bad request")
        captured = sys.stderr.getvalue()
    finally:
        sys.stderr = saved
    check("stderr に1文字も出さない（error_addon.log の1MB枠を食わない）", captured == "", repr(captured))
    check("  INFO/WARNING にも出さない（journal も汚さない）",
          cap.of("INFO") == [] and cap.of("WARNING") == [], str(cap.records))
    check("  DEBUG へは落としてある（切り分け時に上げられる）",
          all(lv == "DEBUG" for lv, _m in cap.records) and len(cap.records) == 2, str(cap.records))
    check("ポートは 8100", webui.PORT == 8100)
    check("認証を付けていない理由がモジュール冒頭に書いてある",
          "認証を付けていない" in webui.__doc__ and "生活パターン" in webui.__doc__)
    check("  /diagnostics の集約で性質が重くなったことも書いてある（認証再検討の判断材料）",
          "/diagnostics" in webui.__doc__ and "判断材料" in webui.__doc__)


def status_section() -> None:
    """status.json の config_url（本体改修⑤の受け入れ条件: UIが立っていなければ書かない）。"""
    from ir_bridge import status as status_mod

    print("■ status.json の config_port（本体改修⑤: URLではなくポートを書く）")
    base = status_mod.build_status("0.1.0")
    check("既定ではキーを書かない（本体はリンクを出さない）", "config_port" not in base, str(base))
    check("  必須3キーは従来どおり",
          set(base) == {"service", "display_name", "version"}, str(base))
    with_port = status_mod.build_status("0.1.0", config_port=8100)
    check("値があれば書く", with_port.get("config_port") == 8100, str(with_port))
    check("  整数で書く（ブラウザが location.hostname と組み立てる）",
          isinstance(with_port["config_port"], int))
    check("None ならキーを書かない", "config_port" not in status_mod.build_status("0.1.0", config_port=None))
    check("★UIが立たなかったら None（＝キーを書かない）", webui.config_port(None) is None)

    class _FakeServer:
        server_address = ("0.0.0.0", 8100)

    check("UIが立っていれば bind できたポートを返す", webui.config_port(_FakeServer()) == 8100)
    check("  定数ではなく実体を見る（ポートを変えても値がずれない）",
          webui.config_port(type("_S", (), {"server_address": ("", 9999)})()) == 9999)
    check("  既定ポートは 8100", webui.PORT == 8100)


def display_name_section(tmp: Path) -> None:
    """受信ログと learned 一覧に表示名を出すための土台（M3-i）。

    ★サーバが渡すのは **key / instance という照合できる値だけ**で、表示名の差し込みは
    ブラウザが行う。表記文字列を渡して受け手が加工すると、表示がログ文面に結合する。
    """
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver

    print("■ 宛先の構造（表示名を差し込むための土台）")
    check("シーンは key を渡す",
          codes.target_info(SceneTarget("scene_15")) == {"kind": "scene", "key": "scene_15"})
    check("照明は instance と輝度を渡す",
          codes.target_info(LightTarget(3, 50)) == {"kind": "light", "instance": 3, "brightness": 50})
    check("宛先が無ければ None（未登録・範囲外・別プリセット）", codes.target_info(None) is None)
    check("★表記文字列は渡さない（表示がログ文面に結合しないため）",
          "summary" not in str(codes.target_info(LightTarget(3, 50))))

    store = config.ConfigStore(config.Config(codes.DEFAULT_BASE, 0, {}, {}))
    recent = RecentCodes()
    rx = IrReceiver(store, recent, CommandFirer(object()), rx_spec=None)
    for events in (
        build_frame(codes.encode_light(codes.DEFAULT_BASE, 3, 50), wave=IRDROID_WAVE),
        build_frame(codes.encode_scene(codes.DEFAULT_BASE, "scene_15"), wave=IRDROID_WAVE),
        build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID_WAVE),
    ):
        for k, u in events:
            rx._feed(IRDROID_DEV, k, u)
    app = webui.UiApp(store, recent, FakeSender(), FakeClient(), rx_spec=None)
    _s, body = app.recent()
    by_code = {e["code"]: e for e in body["entries"]}
    check("受信ログの照明の行に instance と輝度が入る",
          by_code["nec32:0x7d2e0332"]["target"] == {"kind": "light", "instance": 3, "brightness": 50},
          str(by_code["nec32:0x7d2e0332"]))
    check("受信ログのシーンの行に key が入る",
          by_code["nec32:0x7d2e000f"]["target"] == {"kind": "scene", "key": "scene_15"})
    check("未登録の行は target が None（画面は summary をそのまま出す）",
          by_code["nec:0x4008"]["target"] is None)
    check("  従来の summary も残っている（フォールバック用）",
          by_code["nec32:0x7d2e0332"]["summary"] == "照明3 を 50% に設定")

    print("■ 画面側（表示名の差し込みとフォールバック）")
    html = webui.PAGE_HTML
    check("シーン名・照明名を引く関数がある", "function sceneName(" in html and "function lightName(" in html)
    # ★フォールバックの「式」を見る（実装の1行まるごとを文字列比較すると、表示の作りを
    #   変えただけで落ちる＝検査の意図と違うところで落ちる。M3-i 追補で実際に踏んだ）。
    def js_func(name):
        i = html.index("function " + name + "(")
        return html[i:html.index("\nfunction ", i + 1)]

    check("  一覧に無ければ key / 「照明N」に落とす（行を消さない・空欄にしない）",
          ": key;" in js_func("sceneName") and ': "照明" + instance' in js_func("lightName"),
          f'scene={": key;" in js_func("sceneName")} light={chr(39)}照明{chr(39)}')
    check("宛先が無い行は summary をそのまま出す", "if (!t) return e.summary;" in html)
    check("learned 一覧は表示名で描く", "textContent:sceneName(e.scene)" in html)
    # ★シーン名にはグループを付ける。本体は「シーン名にグループ名を付けて重複を解消する」設計で、
    #   実データでも同名シーンが実在する（この機体では 消灯/点灯/就寝前 が2グループずつ）。
    #   受信ログは1列に並ぶので、一覧（グループ見出しあり）より深刻。
    check("シーン名にグループを付ける", '"グループ" + hit.group' in html)
    print("■ 輝度の表記が学習用コード一覧と揃っている（顧客から見れば同じもの）")
    check("受信ログ側に輝度の表記規則がある", "function brightnessLabel(" in html)
    check("  0% は OFF", 'if (brightness === 0) return "OFF";' in html)
    check("  調光非対応の 100% は ON", 'brightness === 100 && hit && !hit.dimmable' in html)
    check("  それ以外は数値のまま", 'return brightness + "%";' in html)
    check("  受信ログが数値をそのまま出していない",
          'brightnessLabel(t.instance, t.brightness)' in html and '" を " + t.brightness + "%"' not in html)
    # ★規則がサーバ側（row_label）と2か所にある。両方を突き合わせて、片方だけ変わったら落とす。
    check("★サーバ側 row_label と同じ規則（0→OFF）", webui.row_label(0, True) == "OFF")
    check("★サーバ側 row_label と同じ規則（調光非対応の100→ON）", webui.row_label(100, False) == "ON")
    check("★サーバ側 row_label と同じ規則（調光対応の100→100%）", webui.row_label(100, True) == "100%")
    check("★コード一覧が取れた時点で learned と受信ログを描き直す（並行取得の順序対策）",
          "renderLearned();\n  renderRecent();" in html)
    check("  取得前は「読み込み中…」を消さない", "if (!RECENT) return;" in html)

    print("■ ログと診断レポートはキーのまま（宛先が違う）")
    learned = {code_to_bytes("nec32:0x7d2e5b91"): config.LearnedEntry(SceneTarget("scene_15"), "M2テスト用")}
    app2, _, recent2 = build(tmp, learned=learned)
    recent2.record("nec32:0x7d2e0332", "nec32", "照明3 を 50% に設定", "derived", True,
                   target=codes.target_info(LightTarget(3, 50)))
    text = app2.diagnostics_text()
    check("診断レポートの learned はキー表記", 'scene_15「M2テスト用」' in text, text[:200])
    check("診断レポートの直近も受信時点の文字列のまま", "照明3 を 50% に設定" in text)
    check("  表示名（本体の名前）は診断レポートに混ぜない", "ロフトダウンライト" not in text)


def logs_section(tmp: Path) -> None:
    """顧客操作で設定が変わったことを1行残す（§18 と衝突しない＝顧客操作起因で頻度が制御できる）。"""
    print("■ 設定変更の記録（learned / 輝度リスト / プリセット）")
    app, store, _ = build(tmp)
    with _Capture() as cap:
        app.set_brightness_list({"action": "add", "instance": 1, "value": 30})
    check("輝度リストの追加が1行出る",
          cap.of("INFO") == ["照明1 の輝度リストに 30% を追加しました（OFF / 30% / 100%）"], str(cap.records))
    with _Capture() as cap:
        app.set_brightness_list({"action": "remove", "instance": 1, "value": 30})
    check("輝度リストの削除が1行出る",
          cap.of("INFO") == ["照明1 の輝度リストから 30% を削除しました（OFF / 100%）"], str(cap.records))
    with _Capture() as cap:
        app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "テレビの青ボタン"})
    check("学習コードの追加が1行出る（何が・どう変わったかが1行で分かる）",
          cap.of("INFO") == ['学習コードを追加しました（nec:0x4008 → scene_15「テレビの青ボタン」）'], str(cap.records))
    with _Capture() as cap:
        app.delete_learned({"code": "nec:0x4008"})
    check("学習コードの削除が1行出る（消えた中身が分かる＝後から追える）",
          cap.of("INFO") == ['学習コードを削除しました（nec:0x4008 → scene_15「テレビの青ボタン」）'], str(cap.records))
    with _Capture() as cap:
        app.set_base({"base": "0x4b6a"})
    infos = cap.of("INFO")
    check("プリセット切替は1行だけ（ConfigStore.replace のベース変更ログと重複しない）",
          len(infos) == 1 and "ベースを" in infos[0], str(cap.records))
    check("  WARNINGは出ない（顧客操作は失敗ではない）", cap.of("WARNING") == [], str(cap.records))


def counters_section(tmp: Path) -> None:
    """診断レポートの計器が、受信・送信の経路で実際に増えること。"""
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver

    print("■ 統計カウンタ（起動からの累計・RAMのみ）")
    counters = Counters()
    cfg = config.Config(codes.DEFAULT_BASE, 1000, {}, {})
    store = config.ConfigStore(cfg)
    rx = IrReceiver(store, RecentCodes(), CommandFirer(object()), rx_spec=None, counters=counters)
    derived = build_frame(codes.encode_light(codes.DEFAULT_BASE, 3, 50), wave=IRDROID_WAVE)
    unknown = build_frame((0x40, 0xBF, 0x08, 0xF7), wave=IRDROID_WAVE)
    other = build_frame(codes.encode_light(codes.PRESETS[1], 3, 50), wave=IRDROID_WAVE)
    repeat = [("pulse", 8757), ("space", 2247), ("pulse", 462), ("timeout", 44287)]
    # ★反転フレームは**別のコード**にする。同じコードだとデバウンス窓に入って「抑制」としても
    #   数えられ、救済が通常の経路（受信→解釈→実行）を通ったことを見られない。
    inverted = invert(build_frame(codes.encode_light(codes.DEFAULT_BASE, 4, 60), wave=IRDROID_WAVE))
    for events in (derived, derived, unknown, other, repeat, inverted):
        for k, u in events:
            rx._feed(IRDROID_DEV, k, u)
    got = counters.snapshot()
    check("個別照明として解釈した数（救済されたフレームも通常の経路を通る）",
          got["light"] == 2, str(got))
    check("同じコードの連投はデバウンスで抑制として数える", got["debounced"] == 1, str(got))
    check("未登録を数える（衝突の実測）", got["unknown"] == 1, str(got))
    check("他プリセットを数える", got["other_preset"] == 1, str(got))
    check("リピートを数える", got["repeats"] == 1, str(got))
    check("★ラベル反転からの救済を数える（ir_toy 欠陥の発生率を顧客環境で集める）",
          got["recovered"] == 1, str(got))
    check("受信フレームは押下の数（リピート・デバウンス分を含まない）", got["received"] == 4, str(got))
    check("  内訳の合計が受信フレームと一致する（分類の取りこぼしが無い）",
          got["scene"] + got["light"] + got["unknown"] + got["other_preset"]
          + got["out_of_range"] + got["foreign_necx"] == got["received"], str(got))

    class _Run:
        def __init__(self, rc=0):
            self.rc = rc

        def __call__(self, argv, **kw):
            return subprocess.CompletedProcess(argv, self.rc, "", "")

    sender = IrSender(store, FakeReceiver(), rx_spec=None, run=_Run(),
                      find_device=lambda spec: IRDROID_DEV, counters=counters)
    sender.send(LightTarget(3, 50))
    check("送信できた数", counters.snapshot()["sent"] == 1)
    busy = IrSender(store, NoisyReceiver(), rx_spec=None, run=_Run(),
                    find_device=lambda spec: IRDROID_DEV, counters=counters)
    busy.quiet_sec, busy.max_wait_sec = 1.5, 0.2
    busy.send(LightTarget(3, 50))
    check("見送った数", counters.snapshot()["send_skipped"] == 1)
    failing = IrSender(store, FakeReceiver(), rx_spec=None, run=_Run(),
                       find_device=lambda spec: None, counters=counters)
    failing.send(LightTarget(3, 50))
    check("失敗した数", counters.snapshot()["send_failed"] == 1)


def diagnostics_section(tmp: Path) -> None:
    """本体が :8100/diagnostics で取る本文（本体改修⑥）。"""
    print("■ 診断レポート本文")
    learned = {code_to_bytes("nec32:0x7d2e5b91"): config.LearnedEntry(SceneTarget("scene_15"), "SwitchBot・M2テスト用")}
    app, store, recent = build(tmp, brightness_lists={"3": [0, 30, 100]}, learned=learned)
    recent.record("nec32:0x7d2e0332", "nec32", "照明3 を 50% に設定", "derived", True)
    counters = Counters()
    for key, n in (("received", 1284), ("scene", 402), ("light", 849), ("unknown", 12), ("recovered", 21),
                   ("debounced", 340), ("sent", 18), ("send_skipped", 1), ("repeats", 44)):
        counters.bump(key, n)
    app._counters = counters
    text = app.diagnostics_text()
    print("    --- 実際の出力（抜粋） ---")
    for line in text.splitlines()[:14]:
        print("    " + line)
    for expect in [
        "[IRアドオン設定]",
        "  プリセット: 1（base=0x7d2e）",
        "  デバウンス窓: 1000ms",
        "    instance 3: OFF / 30% / 100%",
        "  学習コード 1件",
        '    nec32:0x7d2e5b91 → scene_15「SwitchBot・M2テスト用」',
        "[IR受信の直近]",
        "  ※RAMのみ・再起動で消える",
        "[IR受信の統計]",
        "  受信フレーム: 1,284",
        "    解釈できた: 1,251（シーン 402 / 個別照明 849）",
        "    未登録: 12",
        "    ラベル反転から救済: 21",
        "  デバウンスで抑制: 340",
        "  送信: 18（見送り 1・失敗 0）",
        "  ※起動からの累計（再起動でリセット）",
    ]:
        check(f"行がある: {expect.strip()[:34]}", expect in text.splitlines(), expect)
    check("lircデバイス一覧を正常時も出す（Irdroid 未認識が独立して見える）",
          any(line.startswith("  lircデバイス一覧:") for line in text.splitlines()), text[:200])
    check("直近の受信がそのまま載る",
          any("nec32:0x7d2e0332" in line and "照明3 を 50% に設定" in line for line in text.splitlines()))
    check("★導出コードの一覧は出さない（計算で出るので冗長）",
          "nec32:0x7d2e0001" not in text and text.count("nec32:") <= 2, str(text.count("nec32:")))
    app2, _, _ = build(tmp)
    text2 = app2.diagnostics_text()
    check("輝度リストが既定だけなら「なし」", "輝度リスト（既定 OFF/100% 以外）: なし" in text2)
    check("学習コードが無ければ0件", "学習コード 0件" in text2)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        saved = (config.SETTINGS_FILE, config.LEARNED_FILE)
        try:
            page_section()
            device_display_section(tmp)
            codes_section(tmp)
            brightness_section(tmp)
            base_section(tmp)
            learned_section(tmp)
            recent_section(tmp)
            send_section(tmp)
            http_section(tmp)
            status_section()
            display_name_section(tmp)
            logs_section(tmp)
            counters_section(tmp)
            diagnostics_section(tmp)
        finally:
            config.SETTINGS_FILE, config.LEARNED_FILE = saved
    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
