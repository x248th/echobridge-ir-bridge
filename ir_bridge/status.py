"""status: 本体WebUIが読む契約ファイル data/status.json を書き出す。

契約は `~/app/API_CONTRACT.md` §6-3（本体の走査が読む唯一のファイル）:
  必須 service / display_name / version。**いずれかが欠ける、または version が "unknown" だと
  本体WebUIにカードが出ない。** updated_at はデバッグ用（本体は読まない）。
  本体の走査はリクエスト毎評価なので、置けばリロードでカードが出る。
  未知キーは本体が無視する契約なので、IR固有の情報を足しても本体は壊れない。
  config_port（任意・M3-d）は設定UI のポート。**UIが立っているときだけ書く**。
  URLではなくポートなのは、リンクの組み立てを**ブラウザ側**（location.hostname）に任せる
  ためで、アドオンが自分のIP・ホスト名を知らずに済む。本体は稼働中のときだけリンクを出し、
  同じポートの /diagnostics にも問い合わせる（§6-3）。
  ★status.json は外置き（~/addon-data/）に**置かない**（§6-1・§6-3。本体の走査対象は data/ のまま）。

service は **systemdユニット名の茎** かつ **リポジトリのディレクトリ名（~/addons/<id>）** と
完全一致していなければならない（§6-3: 一致しないものはカードにならず、起動・停止・
アンインストールの対象にもならない。M1で独自形 echobridge-addon-ir-bridge を作って外した。
DEV_LOG 2026-08-14 M1-C 参照）。本体のホワイトリストには W14 で登録済み。

書き出しの流儀は移植元（hap_bridge/main.py・matter src/status.js）を踏襲する:
tmp+rename のアトミック置換・chmod 600・差分があるときだけ書く。
置換の実体は `write_bytes_atomic`（fsync まで行う）で、設定ファイル（config.py）もこれを通る。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# リポジトリ直下（ir_bridge/ の1つ上）。CWD非依存にするため __file__ 基準で解決する。
REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
# data/ の差し替え口（**自己テスト専用**。unit にも data/env にも書かない）。
# 自己テストは ir_bridge を import する**前に**これを一時ディレクトリへ向ける
# （tools/isolation.py）。属性を1つずつ差し替えるのではなく置き場所ごと逃がすので、
# data/ 配下に書き込み先が増えても（status.json・error_addon.log の回転・…）本物へは行かない。
DATA_DIR_ENV = "IR_BRIDGE_DATA_DIR"
DATA_DIR = Path(os.environ.get(DATA_DIR_ENV) or REPO_ROOT / "data")
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

    いまは必須3キーも config_port も起動後は不変＝差分が生じない。したがって実質
    「起動につき1回書く」になるが、これは意図した挙動である（本体の書き込み抑制思想＝SD消耗回避）。
    可変の情報（学習済みコード数・lirc デバイスの有無など）を足すと、この関数の
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


def write_bytes_atomic(path, data: bytes) -> None:
    """tmp → fsync → rename でファイルを置き換える。権限は 600。失敗しても書きかけを残さない。

    fsync まで行うのは、SDカードのRaspberry Piで**書いた直後に電源が落ちる**ことが
    現実に起きるため（顧客宅にUPSは無い）。rename 自体は原子的だが、fsync しないと
    中身がディスクに届く前に rename だけ先に永続化されうる。

    ★status.json も設定ファイル（settings.json・learned.json）も**この1本を通る**（W19-16）。
    以前は config 側だけが fsync していて、同じ docstring（「tmp+rename のアトミック置換」）を
    掲げたまま片方に無い、という非対称だった。置き場所は違う（data/ と ~/addon-data/）が
    電源断に対する要求は同じなので、書き方は1つにする。
    この関数が status.py にあるのは、config.py がこちらを import している側だから。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # renameの前に権限を立てる＝600でない瞬間を作らない
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()  # 書きかけを残さない
        except OSError:
            pass
        raise


def write_status(status: dict) -> None:
    """tmp→fsync→rename でアトミック置換。読み手（本体WebUI）が半端なJSONを見ることはない。"""
    payload = dict(status)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_bytes_atomic(
        STATUS_FILE, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
