#!/usr/bin/env python3
"""derive_selftest: IRコード導出（codes.py）と設定読み込み（config.py）の自己テスト。

実機・本体API・外部依存なしで走る。**HTTPが飛ばないことも試験対象**なので、
本体クライアントは呼び出しを記録するだけの偽物に差し替える。

    python3 tools/derive_selftest.py    # 全件PASSなら rc=0
"""
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ir_bridge import codes  # noqa: E402
from ir_bridge.codes import LightTarget, SceneTarget  # noqa: E402
from ir_bridge.nec import bytes_to_code, code_to_bytes  # noqa: E402

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


def code_of(raw) -> str:
    return bytes_to_code(*raw)[1]


class _CapturingHandler(logging.Handler):
    """ログ文面そのものを試験するためのハンドラ（顧客が読む唯一の手がかりなので）。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def main() -> int:  # noqa: C901 - 章立てが多いだけで分岐は浅い
    base = codes.DEFAULT_BASE

    print("■ プリセット表の構造条件（起動時に検証され、破れていたら起動しない）")
    codes.validate_presets()
    check("既定のプリセット表は全条件を満たす", True, f"{[hex(b) for b in codes.PRESETS]}")
    check("プリセットは3つ", len(codes.PRESETS) == 3)
    check("既定はプリセット1 (0x7d2e)", base == 0x7D2E and codes.preset_number(base) == 1)
    for b in codes.PRESETS:
        hi, lo = codes.base_bytes(b)
        check(
            f"0x{b:04x} が通常NECの反転規則を破る（hi=0x{hi:02x} != ~lo=0x{0xFF - lo:02x}）",
            codes.is_separable_base(b),
        )
        check(f"0x{b:04x} が 0x00/0xFF を含まない", 0x00 not in (hi, lo) and 0xFF not in (hi, lo))
    his = [codes.base_bytes(b)[0] for b in codes.PRESETS]
    los = [codes.base_bytes(b)[1] for b in codes.PRESETS]
    check("上位バイトが互いに異なる（片方しか見ない照合からの逃げ場）", len(set(his)) == 3, str([hex(x) for x in his]))
    check("下位バイトが互いに異なる", len(set(los)) == 3, str([hex(x) for x in los]))

    print("■ 構造条件を破ったプリセット表は起動を止める（assertが実際に発火するか）")
    saved = codes.PRESETS
    for label, table in [
        ("通常NECの反転規則に合致 (0x2ed1)", (0x2ED1, 0x4B6A, 0x2957)),
        ("0x00 を含む (0x7d00)", (0x7D00, 0x4B6A, 0x2957)),
        ("0xFF を含む (0xff2e)", (0xFF2E, 0x4B6A, 0x2957)),
        ("重複がある", (0x7D2E, 0x7D2E, 0x2957)),
        ("上位バイトが重複 (0x7d2e/0x7d6a)", (0x7D2E, 0x7D6A, 0x2957)),
        ("下位バイトが重複 (0x7d2e/0x4b2e)", (0x7D2E, 0x4B2E, 0x2957)),
    ]:
        codes.PRESETS = table
        try:
            codes.validate_presets()
            check(f"{label} → 起動を止める", False, "assertが発火しなかった")
        except AssertionError:
            check(f"{label} → 起動を止める", True)
        finally:
            codes.PRESETS = saved

    print("■ encode/decode の往復（シーン）")
    for key, want in [("scene_01", "nec32:0x7d2e0001"), ("scene_15", "nec32:0x7d2e000f"),
                      ("scene_27", "nec32:0x7d2e001b")]:
        raw = codes.encode_scene(base, key)
        got = codes.decode(base, raw)
        ok = code_of(raw) == want and got.target == SceneTarget(key)
        check(f"{key} → {want} → {key}", ok, "" if ok else f"code={code_of(raw)} got={got}")
    check("シーンは b3=0x00 に予約されている", codes.encode_scene(base, "scene_15")[3] == 0x00)
    check(
        "3桁のシーン番号もそのまま通る（:02d は最小幅）",
        codes.decode(base, codes.encode_scene(base, "scene_150")).target == SceneTarget("scene_150"),
    )

    print("■ encode/decode の往復（個別照明・境界の 0%/100% を含む）")
    for inst, bb in [(1, 0), (1, 100), (3, 50), (10, 25), (10, 100), (154, 0), (255, 50)]:
        raw = codes.encode_light(base, inst, bb)
        got = codes.decode(base, raw)
        ok = got.target == LightTarget(inst, bb)
        check(f"照明{inst} {bb}% → {code_of(raw)} → 照明{inst} {bb}%", ok, "" if ok else f"got={got}")
    check("仕様の例と一致（照明3 50% = nec32:0x7d2e0332）",
          code_of(codes.encode_light(base, 3, 50)) == "nec32:0x7d2e0332")
    check("0% は消灯として同じ規則で表せる（BB=0x00）", codes.encode_light(base, 3, 0)[2] == 0x00)
    check("100% は BB=0x64", codes.encode_light(base, 3, 100)[2] == 0x64)
    check("instance=0 は拒否（シーン予約と衝突するため）",
          _raises(ValueError, codes.encode_light, base, 0, 50))
    check("輝度101 は拒否", _raises(ValueError, codes.encode_light, base, 3, 101))

    print("■ 導出コードは必ず nec32 に分類される（＝実在リモコンと構造的に分離）")
    bad = []
    for b in codes.PRESETS:
        for inst in list(range(1, 60)) + [154, 155, 200, 255]:
            for bb in range(0, 101):
                try:
                    raw = codes.encode_light(b, inst, bb)
                except AssertionError:
                    continue  # 分離条件が破れる組み合わせは encode 自体が拒む（下で別途試験）
                if bytes_to_code(*raw)[0] != "nec32":
                    bad.append((hex(b), inst, bb, bytes_to_code(*raw)))
        for nn in range(0, 256):
            try:
                raw = codes.encode_scene(b, f"scene_{nn:02d}")
            except AssertionError:
                continue
            if bytes_to_code(*raw)[0] != "nec32":
                bad.append((hex(b), "scene", nn, bytes_to_code(*raw)))
    check("全プリセット×全宛先で nec32 以外にならない", not bad, f"例外={bad[:3]}")

    print("■ assert II != 0xFF - BB が実際に発火すること（意図的に破る値で）")
    # instance 155 を 100% にすると 155 == 0xFF-100 ＝ 拡張NECの反転規則に合致してしまう。
    # いまは instance<=154 なら起きないが、上限が動いたときに黙って壊れる箇所。
    check("照明155 を 100% は拒否される", _raises(AssertionError, codes.encode_light, base, 155, 100))
    check("その組み合わせは確かに拡張NECになる", 155 == 0xFF - 100)
    check("照明155 の 99% は通る（境界の隣は正常）",
          codes.encode_light(base, 155, 99)[3] == 155)
    check("scene_255 は拒否される（b3=0x00 == ~0xFF）",
          _raises(AssertionError, codes.encode_scene, base, "scene_255"))
    check("scene_254 は通る", codes.encode_scene(base, "scene_254")[2] == 254)

    print("■ NN が1バイトに収まらないシーンは一覧から外す（黙って別コードを出さない）")
    handler = _CapturingHandler()
    logging.getLogger("ir_bridge.codes").addHandler(handler)
    kept = codes.encodable_scene_keys(["scene_01", "scene_256", "scene_999", "allon", "scene_27"])
    logging.getLogger("ir_bridge.codes").removeHandler(handler)
    check("表現できるものだけ残る", kept == ["scene_01", "scene_27"], str(kept))
    check("外した理由が WARNING に出る", len([r for r in handler.records if r[0] == "WARNING"]) == 3,
          str([m for _, m in handler.records]))
    check("scene_256 は encode 自体も拒否", _raises(ValueError, codes.encode_scene, base, "scene_256"))

    print("■ BB > 100 はローカルで弾く（本体へ投げない）")
    raw = (0x2E, 0x7D, 0xFF, 0x03)  # base一致・II=3・BB=255
    got = codes.decode(base, raw)
    check("解釈は返るが宛先は None", got is not None and got.target is None, str(got))
    check("source=out_of_range", got.source == "out_of_range", got.source)
    check("理由に「本体へ投げない」と書いてある", "本体へ投げない" in got.reason, got.reason)
    # M2のテスト用コード nec32:0x7d2e5b91 は BB=0x91=145 ＝ まさにこの経路に落ちる。
    _p, m2 = code_to_bytes("nec32:0x7d2e5b91")
    check("実例 nec32:0x7d2e5b91 (BB=145) もここに落ちる",
          codes.decode(base, m2).source == "out_of_range")

    print("■ 他プリセットのコードを受信したときの文面")
    other = codes.encode_light(codes.PRESETS[1], 3, 50)
    got = codes.decode(base, other)
    check("宛先は返さない", got is not None and got.target is None)
    check("source=other_preset", got.source == "other_preset", got.source)
    check("「プリセット2 のコードです。現在はプリセット1 です」",
          got.reason == "プリセット2 のコードです。現在はプリセット1 です", got.reason)
    got3 = codes.decode(base, codes.encode_light(codes.PRESETS[2], 3, 50))
    check("プリセット3 も同様に見分ける", got3.reason.startswith("プリセット3 のコードです"), got3.reason)
    check("プリセット2 を使っているときは1と3を見分ける",
          codes.decode(codes.PRESETS[1], codes.encode_light(base, 3, 50)).reason
          == "プリセット1 のコードです。現在はプリセット2 です")

    print("■ 他社リモコンの拡張NECフレーム（decode側・assertしてはいけない経路）")
    # ★encode 側の assert（照明155/scene_255）は「導出規則が衝突コードを作らない」条件。
    #   受信側は中身を制御できないので、同じ形のフレームが来ても**例外を投げず分類する**。
    #   例: 送出 2e 7d 64 9b（BB=100 / II=155）。b2^b3=0xff なので表記は necx になる。
    foreign = (0x2E, 0x7D, 0x64, 0x9B)
    check("表記は necx（nec32 ではない）", bytes_to_code(*foreign) == ("necx", "necx:0x2e7d64"),
          str(bytes_to_code(*foreign)))
    got = codes.decode(base, foreign)
    check("宛先は返さない（発火しない）", got is not None and got.target is None, str(got))
    check("source=foreign_necx", got.source == "foreign_necx", got.source)
    check("「他社リモコンの拡張NECフレーム（当機のコードではない）」と書く",
          got.reason.startswith("他社リモコンの拡張NECフレーム（当機のコードではない）"), got.reason)
    check("どのプリセットと衝突しているかを書く", "プリセット1(0x7d2e)" in got.reason, got.reason)
    check("対処（プリセット切替）まで書く", "プリセットを切り替え" in got.reason, got.reason)
    check("以前は存在しない照明155として404になっていた（回帰）",
          got.target != LightTarget(155, 100))

    # II == 0x00 かつ BB == 0xFF は scene_255 の形だが、拡張NECでもある。
    # encode 側は assert で拒む／decode 側は他社リモコンとして分類する（両者は整合する）。
    scene255 = (0x2E, 0x7D, 0xFF, 0x00)
    check("scene_255 の形も他社リモコン側に分類される",
          codes.decode(base, scene255).source == "foreign_necx", str(codes.decode(base, scene255)))
    check("encode 側は同じ形を拒む（分類が食い違わない）",
          _raises(AssertionError, codes.encode_scene, base, "scene_255"))

    print("■ decode は**どんな4バイトでも例外を投げない**（受信内容は制御できないため）")
    raised, misread = [], []
    for b in codes.PRESETS:
        hi, lo = codes.base_bytes(b)
        for b2 in range(256):
            for b3 in range(256):
                try:
                    r = codes.decode(b, (lo, hi, b2, b3))
                except Exception as e:  # noqa: BLE001 - 例外を投げたこと自体が失敗
                    raised.append(((hex(b), b2, b3), repr(e)))
                    continue
                # 拡張NECの反転規則に合致するフレームを宛先として解釈してはいけない。
                if codes.is_foreign_necx(b2, b3) and r.target is not None:
                    misread.append((hex(b), b2, b3, r))
    check("全プリセット×全(BB,II)の196608通りで例外を投げない", not raised, f"例外={raised[:3]}")
    check("拡張NEC形は1つも宛先として解釈しない", not misread, f"誤読={misread[:3]}")

    print("■ 導出コードは1つもこの分類に落ちない（誤検出しないことの裏取り）")
    wrong = []
    for b in codes.PRESETS:
        for inst in list(range(1, 60)) + [154, 200, 255]:
            for bb in range(0, 101):
                try:
                    raw = codes.encode_light(b, inst, bb)
                except AssertionError:
                    continue
                r = codes.decode(b, raw)
                if r.source != "derived" or r.target != LightTarget(inst, bb):
                    wrong.append((hex(b), inst, bb, r))
        for nn in range(0, 256):
            try:
                raw = codes.encode_scene(b, f"scene_{nn:02d}")
            except AssertionError:
                continue
            r = codes.decode(b, raw)
            if r.source != "derived" or r.target != SceneTarget(f"scene_{nn:02d}"):
                wrong.append((hex(b), "scene", nn, r))
    check("encode したものは必ず derived として往復する", not wrong, f"例外={wrong[:3]}")

    print("■ どのプリセットでもないコードは None（＝未登録へ回す）")
    _p, tv = code_to_bytes("nec:0x4008")
    check("実在リモコン nec:0x4008 は導出として解釈されない", codes.decode(base, tv) is None)

    if _config_section() != 0:
        return 1
    if _pipeline_section() != 0:
        return 1
    if _reload_section() != 0:
        return 1

    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


def _stop_logs_when_empty() -> bool:
    """キューが空のまま stop() したときにログを出さないこと（出したら True）。"""
    from ir_bridge.client import CommandFirer

    handler = _CapturingHandler()
    root = logging.getLogger("ir_bridge")
    saved = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        firer = CommandFirer(object())
        firer.start()
        firer.stop(timeout=1.0)
    finally:
        root.removeHandler(handler)
        root.setLevel(saved)
    return bool(handler.records)


def _raises(exc, fn, *args) -> bool:
    try:
        fn(*args)
    except exc:
        return True
    except Exception:  # noqa: BLE001 - 期待と違う例外は失敗扱い
        return False
    return False


def _config_section() -> int:
    """設定の読み込み（壊れた入力への耐性）。data/ を汚さないよう一時ディレクトリで回す。"""
    from ir_bridge import config

    print("■ 設定の読み込み（顧客のファイル編集が原因の異常では止まらない）")
    handler = _CapturingHandler()
    logging.getLogger("ir_bridge.config").addHandler(handler)
    try:
        check("base が未指定なら既定", config._parse_base(None) == codes.DEFAULT_BASE)
        check("生値の文字列を読む", config._parse_base("0x4b6a") == 0x4B6A)
        check("整数でも読む", config._parse_base(0x4B6A) == 0x4B6A)
        check("読めない値は既定へフォールバック", config._parse_base("ぬるぽ") == codes.DEFAULT_BASE)
        check("2バイトを超える値は既定へ", config._parse_base("0x1234567") == codes.DEFAULT_BASE)
        # ★構造条件を破る base は assert で落とさず、WARNINGを出して既定へ落とす。
        check("構造条件を破る base は既定へ（落とさない）",
              config._parse_base("0x2ed1") == codes.DEFAULT_BASE)
        warned = [m for lv, m in handler.records if lv == "WARNING"]
        check("その理由が WARNING に出る", any("反転規則" in m for m in warned), str(warned[-1:]))

        check("debounce_ms の既定", config._parse_debounce(None) == config.DEFAULT_DEBOUNCE_MS)
        check("負値は既定へ", config._parse_debounce(-1) == config.DEFAULT_DEBOUNCE_MS)
        check("True は int だが拒否する", config._parse_debounce(True) == config.DEFAULT_DEBOUNCE_MS)

        check("brightness_lists の空は {}", config._parse_brightness_lists(None) == {})
        check("instance は文字列キー・値は intのリスト",
              config._parse_brightness_lists({"3": [0, 10, 30, 60, 100]}) == {"3": [0, 10, 30, 60, 100]})
        check("範囲外の値だけ除く", config._parse_brightness_lists({"3": [0, 101, 50]}) == {"3": [0, 50]})

        print("■ learned は1行の破損で全体を捨てない（顧客が全ボタンを失わないため）")
        raw = {
            "format": 1,
            "entries": {
                "nec:0x4008": {"target": {"scene": "scene_01"}, "label": "テレビの青"},
                "こわれた": {"target": {"scene": "scene_02"}},
                "nec:0x4005": {"target": {"light": {"instance": 3}}},
                "nec:0x401e": "文字列",
                "necx:0x866b15": {"target": {"scene": "scene_03"}},
            },
        }
        parsed = config._parse_learned(raw)
        check("読める2件だけが残る", len(parsed) == 2, str(sorted(k[1] for k in parsed)))
        check("正常な行は生きている", parsed.get(code_to_bytes("nec:0x4008")) is not None)
        check("未対応の target 形式は捨てる", parsed.get(code_to_bytes("nec:0x4005")) is None)
        check("label は保持される",
              parsed[code_to_bytes("nec:0x4008")].label == "テレビの青")
        check("label が無ければ空文字",
              parsed[code_to_bytes("necx:0x866b15")].label == "")
    finally:
        logging.getLogger("ir_bridge.config").removeHandler(handler)

    print("■ 保存 → 読み込みの往復（tmp → fsync → rename）")
    with tempfile.TemporaryDirectory() as d:
        s_file, l_file = config.SETTINGS_FILE, config.LEARNED_FILE
        config.SETTINGS_FILE = Path(d) / "settings.json"
        config.LEARNED_FILE = Path(d) / "learned.json"
        try:
            cfg = config.Config(
                base=codes.PRESETS[1],
                debounce_ms=700,
                brightness_lists={"3": [0, 10, 30, 60, 100]},
                learned={code_to_bytes("nec:0x4008"):
                         config.LearnedEntry(SceneTarget("scene_09"), "テレビの青")},
            )
            config.save_settings(cfg)
            config.save_learned(cfg)
            back = config.load()
            check("base が往復する", back.base == codes.PRESETS[1], hex(back.base))
            check("debounce_ms が往復する", back.debounce_ms == 700)
            check("brightness_lists が往復する", back.brightness_lists == {"3": [0, 10, 30, 60, 100]})
            check("learned が往復する", back.learned == cfg.learned, str(back.learned))
            check("未記載の照明は brightness_lists に現れない（既定は設定UI側の1つだけ）",
                  "9" not in back.brightness_lists, str(back.brightness_lists))
            check("記載のある照明は指定どおり", back.brightness_lists["3"] == [0, 10, 30, 60, 100])
            check("format が入っている",
                  json.loads(config.SETTINGS_FILE.read_text())["format"] == config.SETTINGS_FORMAT)
            check("一時ファイルが残らない", not list(Path(d).glob("*.tmp")), str(list(Path(d).glob("*"))))
            check("権限は600", oct(config.SETTINGS_FILE.stat().st_mode)[-3:] == "600")

            print("■ ファイルが無ければ既定で生成する")
            config.SETTINGS_FILE.unlink()
            config.LEARNED_FILE.unlink()
            fresh = config.load()
            check("既定値で読める", fresh.base == codes.DEFAULT_BASE and fresh.learned == {})
            check("settings.json が作られる", config.SETTINGS_FILE.exists())
            check("learned.json が作られる", config.LEARNED_FILE.exists())
        finally:
            config.SETTINGS_FILE, config.LEARNED_FILE = s_file, l_file
    return 0


def _pipeline_section() -> int:
    """受信 → 解釈 → 実行までを、偽クライアントで通す（HTTPは飛ばない）。"""
    import time

    from ir_bridge import config
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver
    from ir_bridge.recent import RecentCodes

    class FakeClient:
        def __init__(self):
            self.calls = []

        def fire_scene(self, key):
            self.calls.append(("scene", key))

        def set_brightness(self, instance, value):
            self.calls.append(("light", instance, value))

    class FakeFrame:
        def __init__(self, raw):
            self.repeat = False
            self.raw_bytes = raw
            self.protocol, self.code = bytes_to_code(*raw)

        @property
        def key(self):
            return (self.protocol, self.raw_bytes)

    def build(learned=None, base=codes.DEFAULT_BASE, debounce_ms=0):
        client = FakeClient()
        firer = CommandFirer(client)
        store = config.ConfigStore(
            config.Config(base, debounce_ms, {}, learned or {})
        )
        rx = IrReceiver(store, RecentCodes(), firer, rx_spec=None)
        return client, firer, rx

    def run(rx, firer, client, raws):
        """フレームを流し、キューが捌けきってから止めてログを返す。

        ★stop() は待機中のキューを**捨てる**仕様（SIGTERM時に残件を実行し続けない）。
        試験ではその手前で捌け切るのを待たないと、何も実行されないまま終わる。
        """
        handler = _CapturingHandler()
        root = logging.getLogger("ir_bridge")
        saved_level = root.level
        root.setLevel(logging.INFO)  # 既定のrootはWARNING＝INFOがhandlerへ届かない
        root.addHandler(handler)
        firer.start()
        try:
            for raw in raws:
                rx._on_frame(FakeFrame(raw))
            deadline = time.monotonic() + 3.0
            while not firer._q.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)  # 取り出し済みの最後の1件が走り終わるのを待つ
            firer.stop(timeout=3.0)
        finally:
            root.removeHandler(handler)
            root.setLevel(saved_level)
        return handler.records

    print("■ learned が導出より先に引かれる（衝突時は顧客の設定が勝つ）")
    # 導出として解釈できるコードを、わざと learned にも入れる。
    raw = codes.encode_light(codes.DEFAULT_BASE, 3, 50)
    learned = {("nec32", raw): config.LearnedEntry(SceneTarget("scene_09"), "衝突テスト")}
    client, firer, rx = build(learned=learned)
    run(rx, firer, client, [raw])
    check("learned のシーンが呼ばれる（照明ではない）", client.calls == [("scene", "scene_09")], str(client.calls))

    client, firer, rx = build()  # learned なし＝導出が効く
    run(rx, firer, client, [raw])
    check("learned が無ければ導出が効く", client.calls == [("light", 3, 50)], str(client.calls))

    print("■ M2のテスト用コードが learned 優先で生き続ける（実例）")
    _p, m2 = code_to_bytes("nec32:0x7d2e5b91")
    check("導出だけなら範囲外で弾かれる", codes.decode(codes.DEFAULT_BASE, m2).source == "out_of_range")
    client, firer, rx = build(
        learned={("nec32", m2): config.LearnedEntry(SceneTarget("scene_15"), "SwitchBot・M2テスト用")}
    )
    run(rx, firer, client, [m2])
    check("learned にあれば発火する", client.calls == [("scene", "scene_15")], str(client.calls))

    print("■ BB > 100 は HTTP が飛ばない")
    client, firer, rx = build()
    records = run(rx, firer, client, [(0x2E, 0x7D, 0xFF, 0x03)])
    check("本体API呼び出しは0件", client.calls == [], str(client.calls))
    check("INFOで理由が出る（WARNINGにはしない＝永続ログ枠を食わない）",
          any(lv == "INFO" and "本体へ投げない" in m for lv, m in records),
          str([m for _, m in records]))
    check("WARNINGは出ない（永続ログ枠を食わない）",
          records and not [m for lv, m in records if lv == "WARNING"], str(records))

    print("■ 未登録コードのログ")
    client, firer, rx = build()
    _p, tv = code_to_bytes("nec:0x4008")
    records = run(rx, firer, client, [tv])
    check("HTTPは飛ばない", client.calls == [])
    check("受信 nec:0x4008 → 未登録 と出る",
          any("受信 nec:0x4008 → 未登録" in m for _, m in records), str([m for _, m in records]))
    check("INFOで出る（journalのみ・WARNINGを1件も出さない）",
          records and all(lv == "INFO" for lv, _ in records), str(records))

    print("■ 他プリセットのコードのログ文面")
    client, firer, rx = build()
    records = run(rx, firer, client, [codes.encode_light(codes.PRESETS[1], 3, 50)])
    check("HTTPは飛ばない", client.calls == [])
    check("プリセット番号を両方出す",
          any("プリセット2 のコードです。現在はプリセット1 です" in m for _, m in records),
          str([m for _, m in records]))

    print("■ ログ規律: 1行目だけ読んで成功と誤読させない（§26-10）")
    client, firer, rx = build()
    records = run(rx, firer, client, [codes.encode_light(codes.DEFAULT_BASE, 3, 50)])
    msgs = [m for _, m in records]
    first = next(m for m in msgs if m.startswith("受信 "))
    check("1行目に宛先まで書いてある", "照明3 を 50% に設定" in first, first)
    check("1行目は「要求」であることが分かる", "実行を要求" in first, first)
    check("成功は2行目で別に出る", "照明3 を 50% に設定しました" in msgs, str(msgs))
    check("仕様の例と同じ文面",
          first == "受信 nec32:0x7d2e0332 → 照明3 を 50% に設定（実行を要求）", first)

    print("■ 失敗時は WARNING で、宛先の種類に応じた理由が付く")
    from ir_bridge.client import ClientError

    class Failing(FakeClient):
        def set_brightness(self, instance, value):
            raise ClientError("HTTP 404", status=404)

        def fire_scene(self, key):
            raise ClientError("HTTP 404", status=404)

    client = Failing()
    firer = CommandFirer(client)
    store = config.ConfigStore(config.Config(codes.DEFAULT_BASE, 0, {}, {}))
    rx = IrReceiver(store, RecentCodes(), firer, rx_spec=None)
    records = run(rx, firer, client, [codes.encode_light(codes.DEFAULT_BASE, 3, 50)])
    warn = [m for lv, m in records if lv == "WARNING"]
    check("404 は「未知の instance」と書く",
          any("照明3 を 50% に設定できませんでした（未知の instance）" in m for m in warn), str(warn))

    client = Failing()
    firer = CommandFirer(client)
    learned = {code_to_bytes("nec:0x4008"): config.LearnedEntry(SceneTarget("scene_99"), "")}
    store = config.ConfigStore(config.Config(codes.DEFAULT_BASE, 0, {}, learned))
    rx = IrReceiver(store, RecentCodes(), firer, rx_spec=None)
    records = run(rx, firer, client, [code_to_bytes("nec:0x4008")[1]])
    warn = [m for lv, m in records if lv == "WARNING"]
    check("シーンの404 は「未知のシーン」と書く",
          any("未知のシーン" in m for m in warn), str(warn))
    check("シーンの失敗文が日本語として通る",
          any("シーン scene_99 を発火できませんでした（未知のシーン）" in m for m in warn), str(warn))

    print("■ 文面の型（summary + 接尾辞で3つの文がすべて崩れずに作れること）")
    # ★summary を名詞で終えると「シーン scene_15しました」になる。M3-bのレビューで
    #   実際に踏んだ欠陥なので、シーン・照明の両方で3文とも試験する。
    client = FakeClient()
    firer = CommandFirer(client)
    learned = {code_to_bytes("nec:0x4008"): config.LearnedEntry(SceneTarget("scene_15"), "")}
    store = config.ConfigStore(config.Config(codes.DEFAULT_BASE, 0, {}, learned))
    rx = IrReceiver(store, RecentCodes(), firer, rx_spec=None)
    msgs = [m for _, m in run(rx, firer, client, [code_to_bytes("nec:0x4008")[1]])]
    check("シーンの要求文", "受信 nec:0x4008 → シーン scene_15 を発火（実行を要求）" in msgs, str(msgs))
    check("シーンの成功文", "シーン scene_15 を発火しました" in msgs, str(msgs))
    for t in (SceneTarget("scene_15"), LightTarget(3, 50)):
        for suffix in ("しました", "できませんでした"):
            text = t.summary + suffix
            check(f"{text} が助詞で終わらない", not text.endswith(("を" + suffix, "に" + suffix)), text)

    print("■ デバウンスは learned と導出で窓を分けない")
    client, firer, rx = build(debounce_ms=10_000)
    run(rx, firer, client, [raw, raw, raw])
    check("同じコードの連投は1回だけ実行される", client.calls == [("light", 3, 50)], str(client.calls))
    client, firer, rx = build(debounce_ms=10_000)
    run(rx, firer, client, [raw, codes.encode_light(codes.DEFAULT_BASE, 4, 50)])
    check("別のコードは別の窓", len(client.calls) == 2, str(client.calls))

    print("■ 他社リモコンの拡張NECフレームは HTTP を飛ばさない")
    client, firer, rx = build()
    records = run(rx, firer, client, [(0x2E, 0x7D, 0x64, 0x9B)])
    check("本体API呼び出しは0件（404を撃たない）", client.calls == [], str(client.calls))
    check("INFOで理由が出る",
          any(lv == "INFO" and "他社リモコンの拡張NECフレーム" in m for lv, m in records),
          str([m for _, m in records]))
    check("WARNINGは出ない（受信起因のログは原則INFO）",
          records and not [m for lv, m in records if lv == "WARNING"], str(records))
    check("RecentCodes 側にも理由が残る（衝突調査の計器）",
          rx._recent.snapshot()[0]["source"] == "foreign_necx", str(rx._recent.snapshot()[0]))

    print("■ 終了処理で捨てた実行を1行残す（「押したのに動かなかった」の切り分け用）")
    # ワーカーを止めたままキューへ積む＝実行されずに残る状況を作る。
    class Blocked(FakeClient):
        pass

    client = Blocked()
    firer = CommandFirer(client)
    handler = _CapturingHandler()
    root = logging.getLogger("ir_bridge")
    saved = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        # start() しない＝誰も取り出さないので2件ともキューに残る。
        firer.submit(LightTarget(3, 50), "nec32:0x7d2e0332")
        firer.submit(SceneTarget("scene_15"), "nec32:0x7d2e000f")
        firer.stop(timeout=0.5)
    finally:
        root.removeHandler(handler)
        root.setLevel(saved)
    records = handler.records
    check("破棄した件数が出る", any("終了処理のため 2 件の実行を破棄した" in m for _, m in records),
          str([m for _, m in records]))
    check("何を捨てたかも出る",
          any("照明3 を 50% に設定" in m and "シーン scene_15 を発火" in m for _, m in records),
          str([m for _, m in records]))
    check("WARNINGで出す（再起動でjournalが消えても残る側）",
          any(lv == "WARNING" for lv, m in records if "破棄" in m), str(records))
    check("実行が0件なら何も出さない（平常運転でログを増やさない）",
          not _stop_logs_when_empty())

    print("■ RecentCodes に解釈結果が入る（設定UIがそのまま出せる形）")
    client = FakeClient()
    firer = CommandFirer(client)
    recent = RecentCodes()
    store = config.ConfigStore(config.Config(codes.DEFAULT_BASE, 0, {}, {}))
    rx = IrReceiver(store, recent, firer, rx_spec=None)
    run(rx, firer, client, [raw, code_to_bytes("nec:0x4008")[1]])
    snap = recent.snapshot()
    check("2件記録される", len(snap) == 2, str(len(snap)))
    check("導出は「照明3 を 50% に設定」",
          snap[1]["code"] == "nec32:0x7d2e0332" and snap[1]["summary"] == "照明3 を 50% に設定",
          str(snap[1]))
    check("未登録は summary に理由が入る",
          snap[0]["code"] == "nec:0x4008" and snap[0]["source"] == "unknown", str(snap[0]))
    check("保持件数は50", RecentCodes()._buf.maxlen == 50)
    return 0


def _reload_section() -> int:
    """設定の差し替え（M3-c）。ファイルは一時ディレクトリで扱い data/ を汚さない。"""
    import threading

    from ir_bridge import config

    print("■ 差し替えは丸ごと（新旧が混ざった Config を観測しないこと）")
    # ★同一性で見る。フィールドを1つずつ書き換える実装なら、A でも B でもない
    #   「新しい base ＋ 古い learned」が観測されるので identity 検査で落ちる。
    a = config.Config(codes.PRESETS[0], 1000, {"1": [0, 100]},
                      {code_to_bytes("nec:0x4008"): config.LearnedEntry(SceneTarget("scene_01"), "A")})
    b = config.Config(codes.PRESETS[1], 500, {"2": [0, 50]},
                      {code_to_bytes("nec:0x4005"): config.LearnedEntry(SceneTarget("scene_02"), "B")})
    store = config.ConfigStore(a)
    torn, reads, stop = [], [0], threading.Event()

    def reader():
        while not stop.is_set():
            c = store.get()
            reads[0] += 1
            if c is not a and c is not b:
                torn.append(c)

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    logging.getLogger("ir_bridge.config").setLevel(logging.CRITICAL)  # 切替ログを黙らせる
    for i in range(20_000):
        store.replace(b if i % 2 else a)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    logging.getLogger("ir_bridge.config").setLevel(logging.NOTSET)
    check("2万回の差し替え中、中間状態を1度も観測しない", not torn,
          f"読み取り{reads[0]}回 / 混在{len(torn)}件")
    check("読み取りが実際に走っている（試験が空回りしていない）", reads[0] > 1000, f"{reads[0]}回")

    print("■ 差し替えた base が即座に decode に反映される")
    store = config.ConfigStore(a)
    raw_a = codes.encode_light(codes.PRESETS[0], 3, 50)
    raw_b = codes.encode_light(codes.PRESETS[1], 3, 50)
    check("差し替え前: プリセット1のコードが照明として解ける",
          codes.decode(store.get().base, raw_a).target == LightTarget(3, 50))
    check("差し替え前: プリセット2のコードは別プリセット扱い",
          codes.decode(store.get().base, raw_b).source == "other_preset")
    logging.getLogger("ir_bridge.config").setLevel(logging.CRITICAL)
    store.replace(b)
    logging.getLogger("ir_bridge.config").setLevel(logging.NOTSET)
    check("差し替え後: プリセット2のコードが照明として解ける",
          codes.decode(store.get().base, raw_b).target == LightTarget(3, 50))
    check("差し替え後: プリセット1のコードが別プリセット扱いになる",
          codes.decode(store.get().base, raw_a).source == "other_preset")

    print("■ ベース変更のログ（顧客が衝突対応で困っている場面なので1行残す）")
    handler = _CapturingHandler()
    lg = logging.getLogger("ir_bridge.config")
    saved = lg.level
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    try:
        store = config.ConfigStore(a)
        store.replace(b)
        store.replace(b._replace(debounce_ms=999))  # base以外の変更ではログを出さない
    finally:
        lg.removeHandler(handler)
        lg.setLevel(saved)
    msgs = [m for _, m in handler.records]
    check("1行だけ出る（base以外の変更では出さない）", len(msgs) == 1, str(msgs))
    check("仕様の文面と一致",
          msgs[0] == "ベースをプリセット1（0x7d2e）からプリセット2（0x4b6a）へ変更しました。"
                     "学習済みのボタンは再学習が必要です", msgs[0])
    check("プリセット表に無い base も読める形で出る",
          "0x1234（プリセット表に無い値）" == config.base_label(0x1234), config.base_label(0x1234))

    print("■ reload() は起動時とまったく同じ検証を通る")
    with tempfile.TemporaryDirectory() as d:
        s_file, l_file = config.SETTINGS_FILE, config.LEARNED_FILE
        config.SETTINGS_FILE = Path(d) / "settings.json"
        config.LEARNED_FILE = Path(d) / "learned.json"
        handler = _CapturingHandler()
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)
        try:
            def write(settings: dict, learned: dict) -> None:
                config.SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
                config.LEARNED_FILE.write_text(json.dumps(learned), encoding="utf-8")

            write({"format": 1, "base": "0x4b6a", "debounce_ms": 700, "brightness_lists": {"3": [0, 60]}},
                  {"format": 1, "entries": {"nec:0x4008": {"target": {"scene": "scene_07"}, "label": "青"}}})
            store = config.ConfigStore(config.load())
            check("初回読み込み: base", store.get().base == codes.PRESETS[1], hex(store.get().base))

            print("  — 壊れた learned を差し替える（壊れた行だけ捨てて常駐は生きる）")
            write({"format": 1, "base": "0x4b6a", "debounce_ms": 700, "brightness_lists": {}},
                  {"format": 1, "entries": {
                      "nec:0x4008": {"target": {"scene": "scene_07"}, "label": "青"},
                      "こわれた": {"target": {"scene": "scene_08"}},
                      "nec:0x4005": "文字列",
                      "necx:0x866b15": {"target": {"scene": "scene_09"}},
                  }})
            new = store.reload()
            check("読める2件が残る", len(new.learned) == 2, str(sorted(k[0] for k in new.learned)))
            check("正常な行は生きている", new.learned.get(code_to_bytes("nec:0x4008")) is not None)
            check("壊れた行の WARNING が出る",
                  len([m for lv, m in handler.records if lv == "WARNING"]) == 2,
                  str([m for lv, m in handler.records if lv == "WARNING"]))
            check("常駐は続く（例外を投げない）", True)

            print("  — 構造条件を破る base を差し替える（起動時と同じ扱いになること）")
            handler.records.clear()
            write({"format": 1, "base": "0x2ed1", "debounce_ms": 700, "brightness_lists": {}},
                  {"format": 1, "entries": {}})
            new = store.reload()
            check("既定値へフォールバックする", new.base == codes.DEFAULT_BASE, hex(new.base))
            check("理由が WARNING に出る",
                  any(lv == "WARNING" and "反転規則" in m for lv, m in handler.records),
                  str([m for _, m in handler.records]))
            # 起動時（load()）と差し替え時（reload()）で結果が一致することを直接突き合わせる。
            check("起動時の load() と同じ結果になる（非対称が無い）",
                  config.load().base == new.base == codes.DEFAULT_BASE)

            print("  — brightness_lists の範囲検証も差し替え時に効く")
            handler.records.clear()
            write({"format": 1, "base": "0x7d2e", "debounce_ms": 700,
                   "brightness_lists": {"3": [0, 101, 50, -1]}},
                  {"format": 1, "entries": {}})
            new = store.reload()
            check("0-100 の外は捨てられる", new.brightness_lists == {"3": [0, 50]}, str(new.brightness_lists))
        finally:
            lg.removeHandler(handler)
            lg.setLevel(saved)
            config.SETTINGS_FILE, config.LEARNED_FILE = s_file, l_file

    print("■ 差し替えの前後で RecentCodes が失われない（共有状態は独立）")
    from ir_bridge.client import CommandFirer
    from ir_bridge.receiver import IrReceiver
    from ir_bridge.recent import RecentCodes

    class FakeClient:
        def __init__(self):
            self.calls = []

        def fire_scene(self, key):
            self.calls.append(("scene", key))

        def set_brightness(self, instance, value):
            self.calls.append(("light", instance, value))

    class FakeFrame:
        def __init__(self, raw):
            self.repeat = False
            self.raw_bytes = raw
            self.protocol, self.code = bytes_to_code(*raw)

        @property
        def key(self):
            return (self.protocol, self.raw_bytes)

    recent = RecentCodes()
    client = FakeClient()
    firer = CommandFirer(client)
    store = config.ConfigStore(config.Config(codes.PRESETS[0], 0, {}, {}))
    rx = IrReceiver(store, recent, firer, rx_spec=None)
    firer.start()
    rx._on_frame(FakeFrame(raw_a))
    before = len(recent.snapshot())
    lg.setLevel(logging.CRITICAL)
    store.replace(config.Config(codes.PRESETS[1], 0, {}, {}))
    lg.setLevel(saved)
    rx._on_frame(FakeFrame(raw_b))
    time.sleep(0.1)
    firer.stop(timeout=3.0)
    snap = recent.snapshot()
    check("差し替え前の記録が残っている", before == 1 and len(snap) == 2, f"before={before} after={len(snap)}")
    check("差し替え後のフレームは新しい base で解釈される",
          snap[0]["summary"] == "照明3 を 50% に設定" and snap[0]["source"] == "derived", str(snap[0]))
    check("差し替え前の記録も無傷", snap[1]["code"] == bytes_to_code(*raw_a)[1], str(snap[1]))
    check("受信スレッド側の実行も両方通っている",
          client.calls == [("light", 3, 50), ("light", 3, 50)], str(client.calls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
