"""diagnostics: 診断レポートに載せる本文（プレーンテキスト）を組む。M3-e。

本体が `:8100/diagnostics` を叩き、**本文をそのまま診断レポートへ貼る**想定
（本体改修⑥として起票。本体側はまだ叩かない）。ファイルに書いて本体に読ませる案を
採らないのは、カウンタが頻繁に変わるためSD摩耗を買うから。HTTPならSD書き込みはゼロで、
値は常に最新になる。**アドオンが落ちていれば取れない＝それ自体が情報**である。

■ 何を載せるか（顧客から来る訴えは3つで、どれも「正常時の状態」が要る）
  リモコンが効かない  → 受信デバイスの認識／ir-ctl の有無／プリセット
  勝手に照明が動く    → 直近の受信／プリセット／他プリセットのコードか
  設定が変になった    → 輝度リスト／learned の中身

★**lircデバイス一覧は正常時こそ要る**。いまは WARNING の中にしか出ず、31日稼働だと
  起動ログは journal から流れている。「Irdroid を挿したのに認識されていない」が
  独立して見えるようにする。
★**learned は件数ではなく中身を出す**。顧客が手で育てたデータで、トラブル時に最も効く。
★**導出コードの一覧は出さない**（計算で出るので冗長）。
"""
from . import codes
from .nec import bytes_to_code

# 数える項目とその意味は stats.KEYS 側が典拠。ここはその並びに対応する見出しを付けるだけで、
# キーを増やしたら stats.KEYS と本モジュールの [IR受信の統計] の両方を触ることになる。


def _num(n: int) -> str:
    return f"{n:,}"


def _device_line(device, rx_spec) -> str:
    spec = f" 指定={rx_spec}" if rx_spec else " 指定=auto"
    if device is None:
        return f"受信デバイス: 見つかりません{spec}"
    return f"受信デバイス: {device.path}（{device.label}・{device.role}）{spec}"


def _devices_line(devices) -> str:
    if not devices:
        return "lircデバイス一覧: なし（/dev/lirc* が見つからない）"
    return "lircデバイス一覧: " + "、".join(f"{d.path}({d.label} {d.role})" for d in devices)


def _brightness_lines(config, label_of) -> list[str]:
    """既定（OFF/100%）以外の輝度リストだけを出す。既定のままの照明は書かない。"""
    if not config.brightness_lists:
        return ["輝度リスト（既定 OFF/100% 以外）: なし"]
    lines = ["輝度リスト（既定 OFF/100% 以外）:"]
    for key in sorted(config.brightness_lists, key=lambda k: int(k) if k.isdigit() else 0):
        values = config.brightness_lists[key]
        lines.append(f"  instance {key}: " + " / ".join(label_of(v) for v in values))
    return lines


def _learned_lines(config) -> list[str]:
    lines = [f"学習コード {len(config.learned)}件"]
    for (_proto, raw), entry in sorted(config.learned.items(), key=lambda kv: bytes_to_code(*kv[0][1])[1]):
        code = bytes_to_code(*raw)[1]
        lines.append(f"  {code} → {entry.target.key}「{entry.label}」")
    return lines


def render(config, *, devices, device, rx_spec, ir_ctl, ui_port, recent, counters, label_of) -> str:
    """診断レポートに貼る本文を組む。

    label_of は輝度の表記（webui.row_label 相当）。調光可否は本体に聞かないと分からないので、
    ここでは**調光対応として**表記する（設定に載っている時点で調光対応のはずである）。
    """
    c = counters
    interpreted = c["scene"] + c["light"]
    out = ["[IRアドオン設定]"]
    preset = codes.preset_number(config.base)
    out.append(
        f"  プリセット: {preset}（base=0x{config.base:04x}）"
        if preset
        else f"  プリセット: 表に無い値（base=0x{config.base:04x}）"
    )
    out.append(f"  デバウンス窓: {config.debounce_ms}ms")
    out.append("  " + _device_line(device, rx_spec))
    out.append(f"  送信コマンド: {ir_ctl} あり" if ir_ctl else "  送信コマンド: 見つかりません（パッケージ v4l-utils）")
    out.append(f"  設定UI: :{ui_port} 稼働中")
    out.append("  " + _devices_line(devices))
    out += ["  " + line for line in _brightness_lines(config, label_of)]
    out += ["  " + line for line in _learned_lines(config)]

    out += ["", "[IR受信の直近]", "  ※RAMのみ・再起動で消える"]
    if not recent:
        out.append("  受信なし")
    for e in recent:
        count = f"（{e['count']}回）" if e.get("count", 1) > 1 else ""
        out.append(f"  {e.get('last_at', '')} {e.get('code', '')} {e.get('summary', '')}{count}")

    out += [
        "",
        "[IR受信の統計]",
        f"  受信フレーム: {_num(c['received'])}",
        f"    解釈できた: {_num(interpreted)}（シーン {_num(c['scene'])} / 個別照明 {_num(c['light'])}）",
        f"    未登録: {_num(c['unknown'])}",
        f"    他プリセット: {_num(c['other_preset'])}",
        f"    範囲外: {_num(c['out_of_range'])}",
        f"    他社リモコンの拡張NEC: {_num(c['foreign_necx'])}",
        f"    ラベル反転から救済: {_num(c['recovered'])}",
        f"  リピート（押しっぱなし）: {_num(c['repeats'])}",
        f"  デバウンスで抑制: {_num(c['debounced'])}",
        f"  送信: {_num(c['sent'])}（見送り {_num(c['send_skipped'])}・失敗 {_num(c['send_failed'])}）",
        "  ※起動からの累計（再起動でリセット）",
    ]
    return "\n".join(out) + "\n"
