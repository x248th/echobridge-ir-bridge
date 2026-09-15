"""status: 本体WebUIが読む契約ファイル data/status.json を書き出す。

契約（本体側で確定・本体が読むのはこのファイルのみ）:
  必須 service / display_name / version。**いずれかが欠ける、または version が "unknown" だと
  本体WebUIにカードが出ない。** updated_at はデバッグ用（本体は読まない）。
  本体の走査はリクエスト毎評価なので、置けばリロードでカードが出る。
  未知キーは本体が無視する契約なので、IR固有の情報を足しても本体は壊れない（M2以降）。
  config_port（任意・M3-d）は設定UI のポート。**UIが立っているときだけ書く**。
  URLではなくポートなのは、リンクの組み立てを**ブラウザ側**（location.hostname）に任せる
  ためで、アドオンが自分のIP・ホスト名を知らずに済む。本体側はまだこのキーを読まない
  （本体改修⑤として起票）。

service は **systemdユニット名の茎** かつ **リポジトリのディレクトリ名（~/addons/<id>）** と
完全一致していなければならない。本体は ADDON_SERVICE_WHITELIST（実測 {"hap-bridge",
"matter-bridge"}）にunit名で照合するほか、_purge_all_addons() と api_addon_catalog_install()
では**ディレクトリ名をそのまま**ホワイトリストに照合するため、三者がずれると本体から
トグルもインストールもできない（M1で独自形 echobridge-addon-ir-bridge を作って外した。
DEV_LOG 2026-08-14 M1-C 参照）。
なお ir-bridge はまだ本体のホワイトリストに未登録なので、現状トグルは効かず、
起動停止は systemctl 直叩きで行う。

書き出しの流儀は移植元（hap_bridge/main.py・matter src/status.js）を踏襲する:
tmp+rename のアトミック置換・chmod 600・差分があるときだけ書く。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# リポジトリ直下（ir_bridge/ の1つ上）。CWD非依存にするため __file__ 基準で解決する。
REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
DATA_DIR = REPO_ROOT / "data"
STATUS_FILE = DATA_DIR / "status.json"

# 本体WebUIのトグル対象名。unit名の茎・ディレクトリ名と一字一句同じであること（上のdocstring）。
SERVICE = "ir-bridge"
# 本体の走査ベース表示に使う種別名（HAP版="HomeKit" / Matter版="スマートホーム連携（β版）" と対の位置づけ）。
# これは本体WebUI上の表示名専用で、将来IR機器側に出す名前とは連動させない。
DISPLAY_NAME = "赤外線リモコン連携"


def read_version() -> str:
    """VERSIONファイル（リポジトリ直下・1行）を読む。不在/空/読取失敗は "unknown"（起動は止めない）。"""
    try:
        return VERSION_FILE.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def build_status(version: str, config_port: int | None = None) -> dict:
    """status.json の中身（updated_at を除く）を組む。差分判定の対象でもある。

    骨格段階では3キーとも起動後は不変＝差分が生じない。したがって実質「起動時に1回書く」に
    なるが、これは意図した挙動である（本体の書き込み抑制思想＝SD消耗回避）。
    IR実装で可変の情報（学習済みコード数・lirc デバイスの有無など）を足すと、この関数の
    戻り値が変わる契機ができ、差分監視ループが意味を持ちはじめる。
    """
    status = {
        "service": SERVICE,
        "display_name": DISPLAY_NAME,
        "version": version,
    }
    # ★設定UIが実際に立っているときだけ書く（本体改修⑤: 本体はキーが無ければリンクを出さない）。
    #   ポートが塞がってUIが開けないのにリンクだけ出る、という状態を作らないため。
    if config_port:
        status["config_port"] = int(config_port)
    return status


def write_status(status: dict) -> None:
    """tmp→rename でアトミック置換。読み手（本体WebUI）が半端なJSONを見ることはない。"""
    payload = dict(status)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)  # renameの前に権限を立てる＝600でない瞬間を作らない
    tmp.replace(STATUS_FILE)
