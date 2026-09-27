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

import isolation  # noqa: E402  ← ir_bridge より先に（置き場所ごと一時ディレクトリへ逃がす・前後で本物を見張る）

from ir_bridge import codes, config, webui  # noqa: E402
from ir_bridge.client import ClientError  # noqa: E402
from ir_bridge.codes import LightTarget, SceneTarget  # noqa: E402
from ir_bridge.nec import bytes_to_code, code_to_bytes  # noqa: E402
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
    print("■ ページHTML（5カードが上から設置順に出る）")
    html = webui.PAGE_HTML
    # ★見出しのマークアップで位置を見る（本文やCSSコメントにも同じ語が出るため、
    #   素の文字列だと最初のヒットが見出しとは限らない）。
    order = ["<h2>IRリモコン設定</h2>", "<h2>学習用コード一覧</h2>", "<h2>受信ログ</h2>",
             f"<summary>{OWN_REMOTE_HEADING}</summary>", "<summary>詳細設定</summary>"]
    positions = [html.find(h) for h in order]
    check("5カードの見出しがすべてある", all(p >= 0 for p in positions), str(dict(zip(order, positions))))
    check("上から設置順に並んでいる", positions == sorted(positions), str(positions))
    for text in [
        "この画面から、シーンや照明ごとの赤外線コードをスマートリモコンに学習させます。",
        "詳しい手順",
        "お手持ちの他のリモコンを操作したときに照明が動いてしまう場合は、プリセットを変更してください。",
        OWN_REMOTE_HEADING,
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
          and 0 <= html.find(OWN_REMOTE_HEADING) < html.index("本機のほうに向けて"))
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
    check("登録できる", status == 200 and body.get("overlap") is None, str(body))
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
    check("導出コードと重なる登録は許すが、重なった宛先を返す（■J: 文面は画面が表示名で組む）",
          status == 200 and body.get("overlap") == {"kind": "light", "instance": 3, "brightness": 50}, str(body))
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
    check("ir-ctl が無いときの文面（■H で顧客向けの文が先・原因が後ろに）",
          body["message"].startswith(webui.SEND_NO_IR_CTL), str(body))
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


# =============================================================================
# W19-3・W19-4・W19-5・W19-7・W19-8・W19-11・W19-13・W19-15: :8100 の入口
# =============================================================================
def fake_handler(app, path="/", method="GET", body=None, headers=None, sent=None):
    """通信を始めないハンドラ。`sent` に (status, body) を積む。

    実HTTPを張らないのは、検査したいのが**入口の判断**だからで、ここに socket を持ち込むと
    タイミングで揺れる試験になる（既存の http_section と同じ流儀）。
    """
    handler_cls = type("_T", (webui._Handler,), {"app": app})
    data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    hdrs = {"Content-Length": str(len(data)), "Content-Type": "application/json"}
    hdrs.update(headers or {})
    hdrs = {k: v for k, v in hdrs.items() if v is not None}

    class _H(handler_cls):
        def __init__(self):
            self.path = path
            self.headers = hdrs
            self.rfile = io.BytesIO(data)
            self.client_address = ("192.168.1.9", 50000)
            self.close_connection = False

        def _send_json(self, status_code, body_):
            self._responded = True
            sent.append((status_code, body_))

        def _send_html(self, html):
            self._responded = True
            sent.append((200, {"html": len(html)}))

        def _send_text(self, text):
            self._responded = True
            sent.append((200, {"text": len(text)}))

    return _H()


def call(app, path, method="GET", body=None, headers=None):
    """1要求を投げて (status, body, 捕まえたログ, ハンドラ) を返す。stderr も見張る。"""
    sent = []
    h = fake_handler(app, path=path, method=method, body=body, headers=headers, sent=sent)
    saved, sys.stderr = sys.stderr, io.StringIO()
    try:
        with _Capture(logging.DEBUG) as cap:
            (h.do_GET if method == "GET" else h.do_POST)()
        captured = sys.stderr.getvalue()
    finally:
        sys.stderr = saved
    status, body_ = (sent[0] if sent else (None, None))
    return status, body_, cap, captured, h


def get_catchall_section(tmp: Path) -> None:
    """W19-3・W19-15: do_GET にも受けがある。fd 2 へは1バイトも出さない。"""
    print("■ W19-3 do_GET にも catch-all（トレースバックを永続ログの1MB枠へ流さない）")
    app, _, _ = build(tmp)

    def boom():
        raise RuntimeError("codes_list が壊れた")

    app.codes_list = boom
    webui._Handler._limiter = webui.WarnLimiter()  # 試験ごとに間引きを初期化
    status, body, cap, err, _h = call(app, "/api/codes")
    check("  ★500 を返す（旧実装は応答を返さず接続が切れた）", status == 500, str((status, body)))
    check("  文面は顧客向け（トレースバックを出さない）", body and body.get("message") == "本機の内部エラーです")
    check("  ★stderr に1バイトも出ない（socketserver の印字経路を塞いだ）", err == "", repr(err[:200]))
    check("  WARNING は1行だけ", len([m for m in cap.of("WARNING")]) == 1, str(cap.of("WARNING")))

    print("■ W19-15 繰り返し叩かれても永続ログは1行（無認証なので回数は制御できない）")
    warns = 0
    infos = 0
    for _ in range(20):
        _s, _b, cap, err, _h = call(app, "/api/codes")
        warns += len(cap.of("WARNING"))
        infos += len([m for m in cap.of("INFO") if "設定UIの処理で例外" in m])
        if err:
            break
    check("  ★20回叩いても WARNING は0行（10分の窓の中・旧実装は毎回1KB書いた）", warns == 0, str(warns))
    check("  間引いたぶんは INFO（journal・揮発）に残る", infos == 20, str(infos))
    check("  stderr は最後まで空", err == "", repr(err[:200]))

    print("■ W19-3 送信済みなら二重に送らない")
    app2, _, _ = build(tmp)
    orig = app2.recent

    def half(*a, **kw):
        return orig(*a, **kw)

    sent = []
    h = fake_handler(app2, path="/api/recent", sent=sent)

    def send_then_boom(status_code, body_):
        h._responded = True
        sent.append((status_code, body_))
        raise RuntimeError("応答の途中で落ちた")

    h._send_json = send_then_boom
    with _Capture(logging.DEBUG):
        h.do_GET()
    check("  応答は1回だけ（_fallback が二重送信しない）", len(sent) == 1, str(sent))
    check("  接続を閉じる側に倒す", h.close_connection is True)
    webui._Handler._limiter = webui.WarnLimiter()


def body_limit_section(tmp: Path) -> None:
    """W19-4: Content-Length の上限。本文を読まずに返す。"""
    print("■ W19-4 POST 本文の上限（無認証なので LAN の誰でも投げられる）")
    app, _, _ = build(tmp)
    check("  上限は 64KiB（learned 1件は数百バイト）", webui.MAX_BODY_BYTES == 64 * 1024)

    big = str(webui.MAX_BODY_BYTES + 1)
    sent = []
    h = fake_handler(app, path="/api/base", body=b"{}", headers={"Content-Length": big}, sent=sent)
    with _Capture(logging.DEBUG):
        h.do_POST()
    check("  ★上限超は 413（旧実装は 80MiB でも丸ごと読んだ）", sent and sent[0][0] == 413, str(sent))
    check("  ★本文を読んでいない（読んだ時点でメモリに載る）", h.rfile.tell() == 0, str(h.rfile.tell()))
    check("  接続を閉じる（本文が未読のまま残る）", h.close_connection is True)

    for label, cl in (("欠落", None), ("負", "-1"), ("非数", "abc"), ("空", "")):
        sent = []
        h = fake_handler(app, path="/api/base", body=b"{}", headers={"Content-Length": cl}, sent=sent)
        with _Capture(logging.DEBUG):
            h.do_POST()
        check(f"  {label}の Content-Length は 411", sent and sent[0][0] == 411, str(sent))
        check("    本文を読んでいない", h.rfile.tell() == 0)

    status, body, _cap, _err, _h = call(
        app, "/api/base", "POST", {"base": f"0x{codes.PRESETS[2]:04x}"}
    )
    check("  上限内の正常な要求は従来どおり通る", status == 200, str((status, body)))
    check("  ちょうど上限は通る（境界）",
          call(app, "/api/base", "POST", {"base": f"0x{codes.PRESETS[1]:04x}"},
               {"Content-Length": str(webui.MAX_BODY_BYTES)})[0] in (200, 400), "")


def csrf_section(tmp: Path) -> None:
    """W19-7: 外部ページから撃たれた POST を弾く。★認証は足していない。"""
    print("■ W19-7 別オリジンからの POST を弾く（CSRF・認証は足さない）")
    app, store, _ = build(tmp)
    good = {"base": f"0x{codes.PRESETS[2]:04x}"}

    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good,
                                  {"Host": "192.168.1.5:8100", "Origin": "http://192.168.1.5:8100",
                                   "Sec-Fetch-Site": "same-origin"})
    check("  同一オリジン（顧客の設定画面）は通る", status == 200, str(status))

    base_before = store.get().base
    status, body, _c, _e, _h = call(app, "/api/base", "POST", good,
                                    {"Host": "192.168.1.5:8100", "Origin": "https://evil.example"})
    check("  ★別オリジンの Origin は 403（旧実装は 200 で base が変わった）", status == 403, str((status, body)))
    check("    設定は変わっていない", store.get().base == base_before)

    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good, {"Sec-Fetch-Site": "cross-site"})
    check("  Sec-Fetch-Site: cross-site は 403", status == 403, str(status))
    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good,
                                  {"Host": "h:8100", "Origin": "null"})
    check("  Origin: null（sandbox・file:）も 403", status == 403, str(status))
    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good, {"Sec-Fetch-Site": "same-origin"})
    check("  same-origin は通る", status == 200, str(status))
    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good, {"Sec-Fetch-Site": "none"})
    check("  none（アドレスバーから）は通る", status == 200, str(status))

    print("■ W19-7 HTML フォームで作れる Content-Type は受けない")
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", None):
        status, body, _c, _e, _h = call(app, "/api/base", "POST", good, {"Content-Type": ctype})
        check(f"  {ctype or '（無し）'} は 415", status == 415, str((status, body)))
    status, _b, _c, _e, _h = call(app, "/api/base", "POST", good,
                                  {"Content-Type": "application/json; charset=utf-8"})
    check("  charset 付きの application/json は通る", status == 200, str(status))

    print("■ W19-7 ★認証は足していない（§21 の判断は変えていない）")
    src = (ROOT / "ir_bridge" / "webui.py").read_text(encoding="utf-8")
    # 文面（「Basic 認証にすると…」）で触れているのは判断の記録なので、実装の語だけを見る。
    for token in ("Authorization", "WWW-Authenticate", "check_password", "hmac.compare_digest"):
        check(f"  {token!r} は webui.py に無い", token not in src)
    check("  認証を付けていない理由は冒頭に残っている", "認証を付けていない" in webui.__doc__)
    check("  足したのが入口の検査だけであることも書いてある", "認証は足していない" in webui.__doc__)
    check("  ★「本体は v1.0.5 で凍結中」は根拠から外してある（事実でない・W19-13）",
          "v1.0.5" not in src, "webui.py に v1.0.5 が残っている")


def conn_limit_section(tmp: Path) -> None:
    """W19-8: 接続のタイムアウトと同時接続数の上限。"""
    print("■ W19-8 接続を掴みっぱなしにしない（タイムアウトと同時接続数の上限）")
    check("  ★ハンドラにタイムアウトがある（旧実装は None＝永久に待った）",
          webui._Handler.timeout == webui.CONN_TIMEOUT_SEC and webui.CONN_TIMEOUT_SEC > 0,
          str(webui._Handler.timeout))
    check("  BaseHTTPRequestHandler の既定は None（対比）",
          webui.BaseHTTPRequestHandler.timeout is None)
    app, _, _ = build(tmp)
    server = webui.make_server(app, port=0)
    try:
        check("  上限は 32 接続", server.max_connections == webui.MAX_CONNECTIONS == 32)
        closed = []

        class _FakeSock:
            def shutdown(self, how):
                pass

            def close(self):
                closed.append(True)

        for _ in range(server.max_connections):
            check_ok = server._slots.acquire(blocking=False)
            if not check_ok:
                break
        with _Capture(logging.DEBUG) as cap:
            server.process_request(_FakeSock(), ("192.168.1.9", 51000))
        check("  ★枠が尽きたら受け付けずに閉じる（待たせない）", closed == [True], str(closed))
        check("    1行だけ記録する（WARNING・以後は間引く）",
              len(cap.of("WARNING")) == 1, str(cap.of("WARNING")))
        with _Capture(logging.DEBUG) as cap2:
            server.process_request(_FakeSock(), ("192.168.1.9", 51001))
        check("    2回目は間引かれて INFO", cap2.of("WARNING") == [] and len(cap2.of("INFO")) == 1,
              str(cap2.records))
        server._slots.release()
        check("  枠は戻る（処理が終われば次を受けられる）", server._slots.acquire(blocking=False))
    finally:
        server.server_close()

    print("■ W19-3 接続処理の例外も fd 2 へ印字しない（handle_error を上書き）")
    server = webui.make_server(app, port=0)
    saved, sys.stderr = sys.stderr, io.StringIO()
    try:
        with _Capture(logging.DEBUG) as cap:
            try:
                raise ConnectionResetError("相手が切った")
            except ConnectionResetError:
                server.handle_error(None, ("192.168.1.9", 51002))
            try:
                raise RuntimeError("こちらの失敗")
            except RuntimeError:
                server.handle_error(None, ("192.168.1.9", 51003))
        captured = sys.stderr.getvalue()
    finally:
        sys.stderr = saved
        server.server_close()
    check("  ★stderr に1バイトも出ない（既定はトレースバックを印字する）", captured == "", repr(captured[:200]))
    check("  切断系は永続ログに残さない（DEBUG）",
          cap.of("WARNING") == [] or all("相手が切った" not in m for m in cap.of("WARNING")),
          str(cap.of("WARNING")))
    check("  こちらの失敗は WARNING 1行", len(cap.of("WARNING")) == 1, str(cap.of("WARNING")))


def learned_limit_section(tmp: Path) -> None:
    """W19-5: learned の件数と文字列長の上限。"""
    print("■ W19-5 learned の件数と文字列長に上限（1件追加ごとに全件を書き直すため）")
    check("  件数 256・label 128字・scene 128字",
          (webui.MAX_LEARNED, webui.MAX_LABEL_LEN, webui.MAX_SCENE_LEN) == (256, 128, 128))
    app, store, _ = build(tmp)
    status, body = app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "あ" * 129})
    check("  ★label が上限超なら 400（旧実装は 20,000字が 200 で通った）", status == 400, str(body))
    check("    画面に理由が出る", body.get("message") and "128" in body["message"], str(body))
    status, body = app.add_learned({"code": "nec:0x4008", "scene": "s" * 129, "label": "x"})
    check("  ★scene が上限超なら 400（旧実装は 3,000字が通った）", status == 400, str(body))
    status, body = app.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "あ" * 128})
    check("  ちょうど上限は通る（境界）", status == 200, str(body))

    # 件数の上限。learned を直接 256 件にしてから1件足す。
    full = {}
    for i in range(webui.MAX_LEARNED):
        # ★キーは (プロトコル名, 送出順4バイトのタプル)。code_to_bytes が返すのと同じ形にする
        #   （bytes と tuple は等しくないので、ここを間違えると「満杯なのに別物」になる）。
        raw = (0x11, 0x22, (i >> 8) & 0xFF, i & 0xFF)
        full[code_to_bytes(bytes_to_code(*raw)[1])] = config.LearnedEntry(
            target=SceneTarget("scene_01"), label=f"r{i}"
        )
    app2, store2, _ = build(tmp, learned=full)
    status, body = app2.add_learned({"code": "nec:0x4008", "scene": "scene_15", "label": "もう1件"})
    check("  ★256件に達したら 400（旧実装は 5,001件まで通った）", status == 400, str(body))
    check("    画面に理由が出る（不要な行を削除してください）",
          body.get("message") and "256" in body["message"], str(body))
    check("    learned は増えていない", len(store2.get().learned) == webui.MAX_LEARNED)
    # ★既存コードの上書きは上限に当たっても通す（顧客が付け替えられなくなるのを避ける）。
    existing_code = bytes_to_code(0x11, 0x22, 0x00, 0x00)[1]
    status, body = app2.add_learned(
        {"code": existing_code, "scene": "scene_02", "label": "付け替え"}
    )
    check("  満杯でも既存コードの付け替えはできる", status == 200, str(body))
    check("  ★実在するシーンかどうかは確かめない（§17・長さだけを見る）",
          app.add_learned({"code": "nec:0x4009", "scene": "scene_99", "label": "未知でも登録できる"})[0] == 200)


def bool_brightness_section(tmp: Path) -> None:
    """W19-11: 真偽値の輝度を弾く。"""
    print("■ W19-11 真偽値の輝度を int として受けない（画面に True% と出るのを止める）")
    check("  前提: Python では isinstance(True, int) が真", isinstance(True, int))
    app, store, _ = build(tmp, brightness_lists={"1": [0, 100]})
    status, body = app.set_brightness_list({"instance": 1, "value": True, "action": "add"})
    check("  ★真偽値の value は 400（旧実装は 200 で通り True% と表示された）", status == 400, str(body))
    status, body = app.set_brightness_list({"instance": True, "value": 50, "action": "add"})
    check("  真偽値の instance も 400", status == 400, str(body))
    status, body = app.set_brightness_list({"instance": 1, "value": 50, "action": "add"})
    check("  通常の int は従来どおり通る", status == 200, str(body))
    check("  送信の宛先でも弾く（/api/send）",
          webui._target_from({"kind": "light", "instance": True, "brightness": True}) is None)
    print("■ W19-11 設定ファイル側も同じ（config._parse_brightness_lists）")
    lost = []
    got = config._parse_brightness_lists({"1": [0, True, 100]}, lost)
    check("  ★settings.json に true があっても取り込まない", got == {"1": [0, 100]}, str(got))
    check("    除いたことを1行残す", bool(lost), str(lost))
    check("  _parse_debounce は従来どおり弾く（対処が片側だけ、だった）",
          config._parse_debounce(True) == config.DEFAULT_DEBOUNCE_MS)


def ui_port_section(tmp: Path) -> None:
    """W19-13: /diagnostics のポートが定数ではなく実体になる。"""
    print("■ W19-13 /diagnostics のポートは実際に bind できた値（定数ではない）")
    app, _, _ = build(tmp)
    check("  既定は PORT", f":{webui.PORT} 稼働中" in app.diagnostics_text())
    app.set_ui_port(9999)
    check("  ★set_ui_port で実体に揃う（旧実装は常に定数 8100 を出した）",
          ":9999 稼働中" in app.diagnostics_text(), "")
    app.set_ui_port(None)
    check("  None（UIが立たなかった）なら変えない", ":9999 稼働中" in app.diagnostics_text())
    src = (ROOT / "ir_bridge" / "main.py").read_text(encoding="utf-8")
    check("  main が bind できたポートを渡している", "app.set_ui_port(webui.config_port(ui))" in src)


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
    check("未登録の行は target が None（画面は display を出す）",
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
    check("宛先が無い行は顧客向けの display を出す（■I・summary は /diagnostics 用）",
          "if (!t) return e.display" in html)
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


# =============================================================================
# UI調整（本体 W22 の作法に合わせる・2026-09-26）■A〜■J
# =============================================================================
def _braced(html: str, head: str) -> tuple[int, int, int] | None:
    """`head … { … }` の (head の位置, 開き波括弧, 閉じ波括弧) を返す（入れ子を数える）。"""
    i = html.find(head)
    if i < 0:
        return None
    start = html.index("{", i)
    depth = 0
    for j in range(start, len(html)):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return i, start, j
    return None


def css_block(html: str, head: str) -> str:
    """`head{ ... }` の中身を返す。無ければ空文字。"""
    span = _braced(html, head)
    return html[span[1] + 1:span[2]] if span else ""


def js_function(html: str, name: str) -> str:
    """ページ内の JS 関数 `name` の定義を丸ごと返す。無ければ空文字。"""
    span = _braced(html, "function " + name + "(")
    if not span:
        return ""
    # `async function` は async ごと取る（落とすと await が構文エラーになる）。
    start = span[0] - 6 if html[max(0, span[0] - 6):span[0]] == "async " else span[0]
    return html[start:span[2] + 1]


def narrow_layout_section() -> None:
    """■A: スマホ幅で受信ログと learned 一覧が1文字ずつ折り返す。狭い幅では1件を縦に積む。"""
    print("■A 狭い幅では1件を縦に積む（横スクロールは使わない）")
    html = webui.PAGE_HTML
    narrow = css_block(html, "@media (max-width:")
    check("  ★狭い幅用の @media がある（旧実装は無く、4列をスマホ幅に押し込んだ）", bool(narrow))
    check("  ★横スクロールを使わない（動かした先で「どの行の話か」を見失う）",
          "overflow-x" not in html, "overflow-x がある")
    check("  積む表は grid で1件を縦に並べる", "table.stack tr{display:grid" in narrow, narrow[:120])
    check("  狭い幅では見出し行を出さない（積んだ形では列見出しが意味を持たない）",
          "table.stack thead{display:none}" in narrow)
    # 積む順は「顧客が読むものを上、開発者が見るもの（コード）を下」。
    check("  ★受信ログ: 1行目＝時刻＋内容／2行目＝コード",
          'grid-template-areas:"when what what" "code code act"' in narrow, narrow)
    check("  ★お手持ちのリモコン: 1行目＝名前と［削除］／2行目＝→ シーン名／3行目＝コード",
          'grid-template-areas:"label act" "scene scene" "code code"' in narrow, narrow)
    check("    2行目の「→」は狭い幅のときだけ付ける（広い画面は列見出しの代わりが要らない）",
          '#learned td.scene::before{content:"→ "}' in narrow
          and 'td.scene::before' not in html.replace(narrow, ""))
    rec = js_function(html, "renderRecent")
    lrn = js_function(html, "renderLearned")
    check("  受信ログの表は積む対象（class stack）", 'className:"stack"' in rec, rec[-300:])
    for area in ("when", "what", "code", "act"):
        check(f"    受信ログのセルに {area} がある", f'className:"{area}' in rec)
    check("  learned の表は積む対象（class stack）", 'className:"stack"' in lrn, lrn[-300:])
    for area in ("label", "scene", "code", "act"):
        check(f"    learned のセルに {area} がある", f'className:"{area}' in lrn)
    # ★学習用コード一覧（送信ボタンの並び）は人間が実機で「いい感じ」と判断＝触らない。
    codes_js = js_function(html, "renderCodes")
    check("  学習用コード一覧は積む対象にしない（いまのままでよい・人間の判断）",
          "stack" not in codes_js)


def run_js(source: str, tz: str) -> str | None:
    """node で JS を1回走らせて stdout を返す。node が無ければ None（呼び側が SKIP を出す）。

    ★node は**自己テストのためだけ**に使う（製品は依存ゼロ・CLAUDE.md §11 はアドオン本体の規律）。
    ページの JS を文字列で照合するだけでは、時差や日付の境目の誤りを捕まえられないため。
    """
    import shutil

    node = shutil.which("node") or shutil.which("nodejs")
    if node is None:
        return None
    env = {"TZ": tz, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([node, "-e", source], capture_output=True, text=True, timeout=30, env=env)
    return proc.stdout.strip() if proc.returncode == 0 else f"rc={proc.returncode} {proc.stderr.strip()[:300]}"


def local_time_section() -> None:
    """■B: 受信ログの時刻を端末のローカル時刻に（今日なら HH:MM・別の日なら M/D HH:MM）。"""
    print("■B 受信ログの時刻を端末のローカル時刻で短く出す")
    html = webui.PAGE_HTML
    check("  ★見出しから (UTC) を外した（旧実装は「受信時刻(UTC)」）", "(UTC)" not in html)
    check("  ★UTC の文字列を置換して出す作りをやめた", '.replace("+00:00"' not in html)
    fmt = js_function(html, "fmtTime")
    check("  整形の関数 fmtTime がある", bool(fmt))
    check("  受信ログは fmtTime で出す", "fmtTime(e.last_at" in js_function(html, "renderRecent"))
    if not fmt:
        return
    cases = [
        # (ISO, 期待・東京) 基準時刻は 2026-09-26 10:00 JST
        ("2026-09-26T04:12:33+00:00", "13:12"),   # 今日 → HH:MM
        ("2026-09-25T20:05:00+00:00", "05:05"),   # ★UTC では前日だが東京では今日（日付の境目をUTCで判定していない）
        ("2026-09-25T14:59:00+00:00", "9/25 23:59"),  # 東京で前日 → M/D HH:MM
        ("2026-01-02T00:00:00+00:00", "1/2 09:00"),   # 月・日は0埋めしない
        ("こわれた", ""),                          # ★整形できない値は出さない（生値を出さない）
        ("", ""),
    ]
    src = fmt + "\nconst now = new Date('2026-09-26T10:00:00+09:00');\n" \
        + "console.log(JSON.stringify([" + ",".join(f"fmtTime({json.dumps(c)}, now)" for c, _ in cases) + "]));"
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので整形の実行は確かめない（文字列の照合だけ）")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  fmtTime が走る", False, out)
        return
    for (iso, want), g in zip(cases, got):
        check(f"  東京: {iso or '（空）'} → {want or '（出さない）'}", g == want, repr(g))
    # 同じ関数でも端末の時刻帯が違えば出る値が変わる＝ブラウザのローカル時刻に従っている。
    out_utc = run_js(fmt + "\nconsole.log(fmtTime('2026-09-26T04:12:33+00:00', new Date('2026-09-26T10:00:00Z')));", "UTC")
    check("  UTC の端末では 04:12（端末のローカル時刻に従う）", out_utc == "04:12", repr(out_utc))


JS_STUB_DOM = """
function el(tag, props, ...kids) {
  const n = Object.assign({tag, kids:[], append(...k){this.kids.push(...k)}, prepend(...k){this.kids.unshift(...k)}}, props || {});
  n.kids.push(...kids);
  return n;
}
const CALLS = [];
function toast(text, sticky) { CALLS.push(["toast", text, !!sticky]); }
function notice(text) { CALLS.push(["notice", text]); }
let RESP = null;
async function api(path, body) { CALLS.push(["api", path]); return RESP; }
"""


def result_display_section() -> None:
    """■C: 成功は数秒で消える浮いた表示、失敗は閉じるまで残るモーダル。表の中に文字を差し込まない。"""
    print("■C 結果の出し方（表の中に文字を差し込まない）")
    html = webui.PAGE_HTML
    toast_css = css_block(html, ".toast{")
    check("  ★浮いた表示（position:fixed＝レイアウトを押し広げない）", "position:fixed" in toast_css, toast_css)
    check("    下の操作を塞がない（pointer-events:none）", "pointer-events:none" in toast_css)
    check("    画面の端からはみ出さない（幅の上限）", "max-width:calc(100% - 32px)" in toast_css)
    check("  表示の置き場所がある（読み上げにも届く）", 'id="toast" role="status"' in html)
    t = js_function(html, "toast")
    check("  toast は数秒で自動的に消える", "setTimeout" in t and "TOAST_MS" in t, t[:200])
    check("  失敗用の notice（閉じるまで残るモーダル）がある", "showModal" in js_function(html, "notice"))
    send = js_function(html, "sendButton")
    check("  ★送信ボタンは行の中に文字を出さない（旧実装は名前を1文字ずつにし、ボタンを画面外へ押し出した）",
          'className:"msg"' not in send and "msg(" not in send, send[:300])
    check("  ★プリセット変更の結果を行の横に出さない", 'id="base-msg"' not in html and "base-msg" not in html)
    base = js_function(html, "askBase")
    check("    成功は toast・失敗は notice", "toast(" in base and "notice(" in base, base[-400:])
    for fn in ("alert(", "confirm(", "prompt("):
        check(f"  ★ブラウザ標準の {fn}) を使わない", fn not in html)
    a = js_function(html, "api")
    check("  ★通信できないときも api は投げない（旧実装は送信ボタンが押せないまま残った）",
          "catch" in a.split("fetch(")[1][:200] if "fetch(" in a else False, a)
    check("    受信ログの2秒ポーリングは通信できなければ前の表示を残す（従来と同じ見え方）",
          "if (r.offline) return;" in js_function(html, "loadRecent"))
    check("    loadState は失敗の応答で STATE を壊さない", "if (!r.ok) return;" in js_function(html, "loadState"))

    src = JS_STUB_DOM + send + """
(async () => {
  const out = {};
  const box = sendButton({kind:"scene", key:"scene_15"});
  const btn = box.kids.find(k => k.tag === "button");
  out.kids = box.kids.length;
  RESP = {ok:true, message:"送信しました。スマートリモコン側で保存してください"};
  await btn.onclick();
  out.ok = CALLS.filter(c => c[0] !== "api"); out.okDisabled = btn.disabled; CALLS.length = 0;
  RESP = {ok:false, message:"見送りました"};
  await btn.onclick();
  out.ng = CALLS.filter(c => c[0] !== "api"); out.ngDisabled = btn.disabled;
  console.log(JSON.stringify(out));
})().catch(e => console.log("ERR " + e));
"""
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので送信ボタンの流れは走らせない")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  送信ボタンの流れが走る", False, out)
        return
    check("  送信ボタンの行にはボタンしか無い（結果の文字の置き場所を持たない）", got["kids"] == 1, str(got))
    check("  ★成功 → 送信中を出したあと toast（消える側）",
          got["ok"][-1] == ["toast", "送信しました。スマートリモコン側で保存してください", False], str(got["ok"]))
    check("    押している間は「送信中…」を出す（行の中ではなく浮いた表示で）",
          got["ok"][0] == ["toast", "送信中…", True], str(got["ok"]))
    check("  ★失敗 → notice（閉じるまで残る）", got["ng"][-1] == ["notice", "見送りました"], str(got["ng"]))
    check("  どちらのあともボタンは押せる状態に戻る", got["okDisabled"] is False and got["ngDisabled"] is False, str(got))


OWN_REMOTE_HEADING = "お手持ちのリモコンで操作する（使わなくなったリモコンの再利用）"
# ■D 人間の確定稿（文面は変えない）。HTML の中で1文が途中で改行されていないことも見る
# （日本語の途中の改行はブラウザによって空白として描かれる）。
OWN_REMOTE_TEXT = [
    "使わなくなったリモコン（昔のテレビのリモコンなど）のボタンに、照明のシーンを割り当てられます。",
    "ボタンを押すと、本機が赤外線を受け取って照明を動かします。",
    "スマートリモコンをお使いの場合、この機能は使いません。",
    "スマートリモコンには、上の「学習用コード一覧」から本機が送るコードを覚えさせてください。",
    "いま使っているリモコンのボタンを割り当てると、本来の機器も一緒に反応します。",
    "使わなくなったリモコンをお使いください。",
    "リモコンは機器に向けて使う前提のものが多く、スマートリモコンほど広い範囲には届きません。",
    "効かないときは、本機のほうに向けて押してみてください。",
]


def own_remote_section() -> None:
    """■D: learned を詳細設定の外へ出し、開く前に用途が分かる見出しと確定稿の説明にする。"""
    print("■D お手持ちのリモコン（使わなくなったリモコンの再利用）を詳細設定の外へ")
    html = webui.PAGE_HTML
    # 詳細設定は最後のカード＝そこから <script> までに learned 一覧が無ければよい。
    adv = html[html.index('<details id="advanced">'):html.index("<script>")]
    check("  ★learned 一覧は詳細設定の中に無い（分類としておかしい・旧実装は中にあった）",
          'id="learned"' not in adv, "詳細設定の中に id=learned がある")
    check("  ★見出しで用途が分かる", f"<summary>{OWN_REMOTE_HEADING}</summary>" in html)
    check("  旧見出しは残っていない", "お手持ちのリモコンでシーンを操作する" not in html)
    at = html.find(f"<summary>{OWN_REMOTE_HEADING}</summary>")
    check("  受信ログの下・詳細設定の上に並ぶ",
          html.index("<h2>受信ログ</h2>") < at < html.index("<summary>詳細設定</summary>"), str(at))
    check("  折り畳みのまま（開いた状態で置かない）", '<details id="own-remote">' in html)
    for text in OWN_REMOTE_TEXT:
        check(f"  確定稿: {text[:22]}…", text in html)
    check("  ★「一緒に反応する」注意は強調する", "<strong>いま使っているリモコンのボタンを割り当てると" in html)
    check("  プリセット変更の確認文が learned を「詳細設定」と呼ばない", "（詳細設定）" not in html)


NO_DEVICE_TEXT = ("⚠ 赤外線の受信デバイスが見つかりません。"
                  "Irdroid を本体の USB に挿してから、このページを再読み込みしてください。")


def no_device_section(tmp: Path) -> None:
    """■E: 受信デバイスが無いことを最初の画面で知らせる（旧実装は送信を押して初めて気づいた）。"""
    print("■E 受信デバイスが無いことを一番上のカードで知らせる")
    html = webui.PAGE_HTML
    check("  ★確定稿の1行がある", NO_DEVICE_TEXT in html)
    top = html[html.index("<h2>IRリモコン設定</h2>"):html.index('<ol class="steps main">')]
    check("  一番上のカードの手順より前に置く", 'id="no-device"' in top, top[:200])
    check("  既定では隠しておく（見つかったときは出さない）", 'id="no-device" hidden' in html)
    # サーバ側: 見つからなければ device が None（画面はこれを見て出す）。
    saved = webui.lirc.find_rx_device
    try:
        webui.lirc.find_rx_device = lambda spec=None: None
        app, _, _ = build(tmp)
        _s, state = app.state()
    finally:
        webui.lirc.find_rx_device = saved
    check("  見つからなければ /api/state の device は None", state["device"] is None, str(state["device"]))

    load = js_function(html, "loadState")
    src = """
const NODES = {};
const document = {getElementById(id) {
  if (!NODES[id]) NODES[id] = {id, hidden:true, textContent:"", value:"", dataset:{}, append(){}};
  return NODES[id];
}};
function el(tag, props) { return Object.assign({dataset:{}}, props || {}); }
function renderLearned() {}
let STATE = null, RESP = null;
async function api(path) { return RESP; }
""" + load + """
(async () => {
  const out = {};
  RESP = {ok:true, presets:[], base:"0x7d2e", device:null, learned:[]};
  await loadState(); out.missing = NODES["no-device"].hidden;
  RESP = {ok:true, presets:[], base:"0x7d2e", device:"/dev/lirc2（受信・送信対応）", learned:[]};
  await loadState(); out.found = NODES["no-device"].hidden;
  console.log(JSON.stringify(out));
})().catch(e => console.log("ERR " + e));
"""
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので loadState の出し分けは走らせない")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  loadState が走る", False, out)
        return
    check("  ★見つからないときは出す", got["missing"] is False, str(got))
    check("  見つかったときは出さない", got["found"] is True, str(got))


def save_refused_section(tmp: Path) -> None:
    """■F: 保存できなかったのに黙る経路をなくす（§24: 表示は事後に確かめた事実だけ）。"""
    import re

    print("■F 保存できなかった応答を顧客に届ける（学習の削除・輝度行の追加と削除）")
    learned = {code_to_bytes("nec:0x4008"): config.LearnedEntry(SceneTarget("scene_15"), "寝室リモコンの青")}
    app, store, _ = build(tmp, learned=learned)
    try:
        config._save_refused[str(config.SETTINGS_FILE)] = "読めなかった"
        config._save_refused[str(config.LEARNED_FILE)] = "読めなかった"
        for label, path, body in (
            ("輝度行の追加", "/api/brightness", {"action": "add", "instance": 1, "value": 30}),
            ("輝度行の削除", "/api/brightness", {"action": "remove", "instance": 1, "value": 100}),
            ("学習の削除", "/api/learned/delete", {"code": "nec:0x4008"}),
        ):
            status, resp, _c, err, _h = call(app, path, "POST", body)
            check(f"  サーバは {label} の保存拒否を 409 と文面で返す（前提）",
                  status == 409 and resp and resp.get("message") == webui.SAVE_REFUSED, str((status, resp)))
        check("    拒否したので設定は変わっていない",
              code_to_bytes("nec:0x4008") in store.get().learned and store.get().brightness_lists == {})
    finally:
        config._save_refused.clear()

    html = webui.PAGE_HTML
    script = html[html.index("<script>"):]
    # ★状態を変える POST の応答を捨てない（旧実装は `await api(...)` の結果を見ずに一覧を読み直した）。
    posts = re.findall(r'(.{0,24})api\("(/api/[a-z/]+)', script)
    posts = [(pre, path) for pre, path in posts if path not in ("/api/state", "/api/codes", "/api/recent")]
    check("  状態を変える POST を画面から出している（検査の前提）", len(posts) >= 5, str(posts))
    discarded = [path for pre, path in posts if not re.search(r"(const|let) r = await $", pre)]
    check("  ★POST の応答を捨てている箇所が無い（旧実装は3か所）", discarded == [], str(discarded))

    src = JS_STUB_DOM + "\n".join(js_function(html, n) for n in ("changeBrightness", "deleteLearned")) + """
const RELOADED = [];
function loadCodes() { RELOADED.push("codes"); }
function loadState() { RELOADED.push("state"); }
const NG = {ok:false, message:"設定ファイルを読めなかったため、元のファイルを残すために変更を保存しませんでした。"};
(async () => {
  const out = {};
  RESP = NG; await changeBrightness(1, 30, "add"); out.addNg = CALLS.filter(c => c[0] === "notice"); CALLS.length = 0;
  RESP = NG; await changeBrightness(1, 100, "remove"); out.removeNg = CALLS.filter(c => c[0] === "notice"); CALLS.length = 0;
  RESP = NG; await deleteLearned({code:"nec:0x4008", label:"寝室リモコンの青"}); out.delNg = CALLS.filter(c => c[0] === "notice"); CALLS.length = 0;
  RESP = {ok:true}; await changeBrightness(1, 30, "add"); await deleteLearned({code:"nec:0x4008", label:"x"});
  out.okNotice = CALLS.filter(c => c[0] === "notice").length;
  out.reloaded = RELOADED;
  console.log(JSON.stringify(out));
})().catch(e => console.log("ERR " + e));
"""
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので画面側の流れは走らせない")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  画面側の流れが走る", False, out)
        return
    for key, label in (("addNg", "輝度行の追加"), ("removeNg", "輝度行の削除"), ("delNg", "学習の削除")):
        check(f"  ★{label}: 保存されなかったら閉じるまで残る表示で知らせる",
              len(got[key]) == 1 and got[key][0][1].startswith("設定ファイルを読めなかった"), str(got[key]))
    check("  成功は従来どおり（一覧の更新で分かる＝知らせを出さない）", got["okNotice"] == 0, str(got))
    check("  成否にかかわらず一覧は読み直す（画面を事実に揃える）",
          got["reloaded"].count("codes") == 3 and got["reloaded"].count("state") == 2, str(got["reloaded"]))


def delete_confirm_section() -> None:
    """■G: 学習の削除は取り消せない（名前も消える）ので画面内モーダルで確認する。輝度行には付けない。"""
    print("■G 学習の削除に確認を付ける（輝度行の削除には付けない）")
    html = webui.PAGE_HTML
    check("  ★learned の［削除］は確認を経由する（旧実装は押した瞬間に消えた）",
          "askDeleteLearned(e)" in js_function(html, "renderLearned"))
    check("  輝度行の［削除］は確認しない（導出なので足し直せば同じコードが戻る）",
          'changeBrightness(light.instance, row.brightness, "remove")' in js_function(html, "renderCodes"))
    src = JS_STUB_DOM + js_function(html, "askDeleteLearned") + """
let MODAL = null; const DELETED = [];
function showModal(nodes, actions) { MODAL = {nodes, actions}; }
function closeModal() { MODAL = null; }
async function deleteLearned(e) { DELETED.push(e.code); }
(async () => {
  const out = {};
  const e = {code:"nec:0x4008", label:"寝室リモコンの青", scene:"scene_15"};
  askDeleteLearned(e);
  out.texts = MODAL.nodes.map(n => n.textContent);
  out.buttons = MODAL.actions.map(a => a.textContent);
  await MODAL.actions.find(a => a.textContent === "やめる").onclick();
  out.afterCancel = DELETED.length;
  askDeleteLearned(e);
  await MODAL.actions.find(a => a.textContent === "削除する").onclick();
  out.afterOk = DELETED;
  out.closed = MODAL === null;
  console.log(JSON.stringify(out));
})().catch(e => console.log("ERR " + e));
"""
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので確認の流れは走らせない")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  確認の流れが走る", False, out)
        return
    check("  ★文面は「〈名前〉の登録を削除します。元に戻せません。」で始まる",
          got["texts"][0].startswith("「寝室リモコンの青」の登録を削除します。元に戻せません。"), str(got["texts"]))
    check("  戻し方（押し直して登録し直す）を添える", any("受信ログ" in t for t in got["texts"]), str(got["texts"]))
    check("  ボタンは［削除する］［やめる］", got["buttons"] == ["削除する", "やめる"], str(got["buttons"]))
    check("  ★やめるなら消さない", got["afterCancel"] == 0, str(got))
    check("  削除するなら消す（1回だけ）・モーダルは閉じる",
          got["afterOk"] == ["nec:0x4008"] and got["closed"] is True, str(got))


def failure_wording_section(tmp: Path) -> None:
    """■H: 失敗の文言を顧客向けに（顧客が次にすることを先に・原因は後ろに残してよい）。"""
    from ir_bridge.sender import REASON_CANNOT_TX, REASON_NO_DEVICE, REASON_NO_RESPONSE

    print("■H 失敗の文言を顧客向けにする（次にすることが先・原因は後ろ）")
    cases = [
        # (理由, 顧客向けの定数名, 次の手として必ず含む語)
        (REASON_NO_DEVICE, "SEND_NO_DEVICE", ("Irdroid", "挿し直")),
        # ★USB の抜き差しでしか戻らない（ドライバのハングの可能性・R-4 と同じ案内）。
        (REASON_NO_RESPONSE, "SEND_NO_RESPONSE", ("抜", "挿し直")),
        (REASON_CANNOT_TX, "SEND_CANNOT_TX", ("Irdroid", "挿して")),
        ("ir-ctl rc=1", "SEND_FAILED", ("挿し直", "診断レポート")),
        (REASON_NO_IR_CTL, "SEND_NO_IR_CTL", ("診断レポート",)),
    ]
    for reason, name, words in cases:
        customer = getattr(webui, name, None)
        app, _, _ = build(tmp, sender=FakeSender(SendResult(False, "nec32:0x7d2e0332", reason)))
        _s, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
        m = body.get("message", "")
        check(f"  ★{reason}: 顧客が読む文が先に来る", bool(customer) and m.startswith(customer), m)
        check(f"    次の手がある（{'・'.join(words)}）", bool(customer) and all(w in customer for w in words), str(customer))
        check("    開発者向けの語は顧客の文に入れない",
              bool(customer) and "ir-ctl" not in customer and "rc=" not in customer and "lirc" not in customer, str(customer))
        check("    原因は後ろに残す", m.endswith(f"（原因: {reason}）"), m)
    app, _, _ = build(tmp, sender=FakeSender(SendResult(False, "nec32:0x7d2e0332", REASON_BUSY)))
    _s, body = app.send({"kind": "light", "instance": 3, "brightness": 50})
    check("  見送り（受信が続いている）は従来の文面のまま（元から次の手が書いてある）",
          body["message"] == webui.SEND_BUSY, body["message"])

    app, _, _ = build(tmp, client=FakeClient(fail=True))
    _s, body = app.codes_list()
    check("  ★一覧を取得できない: 本体側の状態を確かめる案内がある",
          "管理画面" in body["error"] and "再読み込み" in body["error"], body["error"])
    _s, body = app.set_brightness_list({"action": "add", "instance": 1, "value": 30})
    check("  輝度行の変更で本体に届かないときも同じ案内", "管理画面" in body["message"], body["message"])


def recent_wording_section(tmp: Path) -> None:
    """■I: 受信ログの画面には顧客向けの短い文。開発者向けの元の文は /diagnostics 側に残す。"""
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver

    print("■I 受信ログの文面を顧客向けにする（開発者向けの文は /diagnostics に残す）")
    base = codes.DEFAULT_BASE
    lo, hi = base & 0xFF, (base >> 8) & 0xFF
    frames = {
        "unknown": (0x40, 0xBF, 0x08, 0xF7),
        "other_preset": codes.encode_light(codes.PRESETS[1], 3, 50),
        "out_of_range": (lo, hi, 145, 3),
        "foreign_necx": (lo, hi, 0x10, 0xEF),
    }
    store = config.ConfigStore(config.Config(base, 0, {}, {}))
    recent = RecentCodes()
    rx = IrReceiver(store, recent, CommandFirer(object()), rx_spec=None)
    for raw in frames.values():
        for k, u in build_frame(raw, wave=IRDROID_WAVE):
            rx._feed(IRDROID_DEV, k, u)
    app = webui.UiApp(store, recent, FakeSender(), FakeClient(), rx_spec=None)
    _s, body = app.recent()
    by_source = {e["source"]: e for e in body["entries"]}
    check("  4種類とも受信ログに載る（検査の前提）", set(by_source) >= set(frames), str(list(by_source)))
    dev_words = ("learned", "導出", "NEC", "本体へ投げない", "0x", "当機")
    for source in frames:
        e = by_source.get(source, {})
        disp = e.get("display") or ""
        check(f"  ★{source}: 顧客向けの文がある", bool(disp), str(e)[:160])
        check(f"    開発者向けの語を含まない（{disp}）", bool(disp) and not any(w in disp for w in dev_words), disp)
        check("    ★開発者向けの文と同じ文字列を使い回さない", disp != e.get("summary"), str(e.get("summary")))
        check("    何もしなかったことが分かる", "何もしません" in disp, disp)
    check("  別プリセットはどのプリセットのコードかを言う（顧客がプリセットを戻す手がかり）",
          "プリセット2" in by_source.get("other_preset", {}).get("display", "")
          and "プリセット1" in by_source.get("other_preset", {}).get("display", ""),
          str(by_source.get("other_preset", {}).get("display")))
    text = app.diagnostics_text()
    check("  /diagnostics には開発者向けの元の文が残る",
          "未登録（learned にも導出にも一致せず）" in text and "本体へ投げない" in text
          and "他社リモコンの拡張NECフレーム" in text, text[-400:])
    check("  /diagnostics には顧客向けの文を混ぜない", "何もしません" not in text)
    tl = js_function(webui.PAGE_HTML, "targetLabel")
    check("  ★画面は宛先が無い行に display を出し、summary を出さない",
          "e.display" in tl and "e.summary" not in tl, tl)


def overlap_name_section(tmp: Path) -> None:
    """■J: 重複の警告に内部のキー（scene_03 等）を出さない。ほかの場所と同じく表示名にする。"""
    print("■J 重複の警告は表示名で出す（内部のキーを出さない）")
    app, _, _ = build(tmp)
    scene_code = bytes_to_code(*codes.encode_scene(codes.DEFAULT_BASE, "scene_15"))[1]
    status, body = app.add_learned({"code": scene_code, "scene": "scene_01", "label": "重なる登録"})
    check("  サーバは重なった宛先を構造で返す（表記文字列を渡さない＝codes.target_info と同じ筋）",
          status == 200 and body.get("overlap") == {"kind": "scene", "key": "scene_15"}, str(body))
    check("  ★応答に文面としての内部キーを入れない（旧実装は「シーン scene_15 を発火」）",
          "warning" not in body, str(body))
    html = webui.PAGE_HTML
    check("  割り当てモーダルは重なりを notice で出す",
          "notice(overlapText(r.overlap))" in js_function(html, "askAssign"))
    src = "\n".join(js_function(html, n) for n in (
        "sceneName", "lightName", "brightnessLabel", "targetText", "overlapText")) + """
let CODES = {ok:true, scenes:[{key:"scene_15", name:"昼間", group:1}],
             lights:[{instance:2, name:"玄関", dimmable:false}]};
console.log(JSON.stringify([
  overlapText({kind:"scene", key:"scene_15"}),
  overlapText({kind:"light", instance:2, brightness:100}),
]));
"""
    out = run_js(src, "Asia/Tokyo")
    if out is None:
        print("  SKIP  node が無いので文面の組み立ては走らせない")
        return
    try:
        got = json.loads(out)
    except ValueError:
        check("  文面を組み立てられる", False, out)
        return
    check("  ★シーンは表示名（グループ付き）で出す",
          got[0] == "このコードは本機のコード（シーン「グループ1 昼間」を発火）と重なっています。登録した内容が優先されます",
          got[0])
    check("    内部のキーを含まない", "scene_" not in got[0], got[0])
    check("  照明も表示名と輝度の表記（受信ログと同じ規則）",
          got[1] == "このコードは本機のコード（玄関 を ON に設定）と重なっています。登録した内容が優先されます", got[1])


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
            get_catchall_section(tmp)
            body_limit_section(tmp)
            csrf_section(tmp)
            conn_limit_section(tmp)
            learned_limit_section(tmp)
            bool_brightness_section(tmp)
            ui_port_section(tmp)
            status_section()
            display_name_section(tmp)
            logs_section(tmp)
            counters_section(tmp)
            diagnostics_section(tmp)
            narrow_layout_section()
            local_time_section()
            result_display_section()
            own_remote_section()
            no_device_section(tmp)
            save_refused_section(tmp)
            delete_confirm_section()
            failure_wording_section(tmp)
            recent_wording_section(tmp)
            overlap_name_section(tmp)
        finally:
            config.SETTINGS_FILE, config.LEARNED_FILE = saved
    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(isolation.run(main))
