"""isolation: 自己テストを本物の data/ と ~/addon-data/ から隔離し、触れていないことを機械で確かめる（W18）。

■ なぜ要るか
本体側で、テストが本物の ~/data/error.log に書き込む事故があった（IR-15 に続く2件目）。隔離は
書いた時点では完全だったのに、本番コードに書き込み先が1本増えたことで黙って無効になっていた。
**属性を1つずつ差し替える隔離は、差し替え忘れた書き込み先を検出できない。** そこで:
  1. 置き場所ごと逃がす——ir_bridge を import する**前に**環境変数で data/ と ~/addon-data/ir-bridge/
     を一時ディレクトリへ向け、HOME も一時ディレクトリにする（Path.home() から組む経路も逃げる）。
  2. 本物を見張る——本物の `~/addons/ir-bridge/data/` 配下と `~/addon-data/ir-bridge/` 配下の
     全エントリの (size, mtime) を、テストの前後で比べる（inode・ctime・種別も比べる。
     rename での置換や chmod も拾うため）。**不在も状態として扱う**（テストが作ったら検出する）。
     動いていたらテストを失敗させる。
     ★`~/addon-data/` は**丸ごとは見張らない**（W19-17）。そこには他アドオンの置き場所
     （`~/addon-data/matter-bridge/`）も入っていて、**稼働中の他アドオンの書き込み**を
     「隔離が破れた」と報告してしまう（実際に踏んだ: matter-bridge が1時間に1回書く
     稼働時間カウンタ）。「隔離が破れた」は本来いちばん重く扱うべき報告なので、
     誤検出を混ぜない＝狼少年にしない。
     代わりに `~/addon-data/` は**直下の名前の一覧だけ**を比べる（中身の size/mtime は見ない）
     ——「ir-bridge が他アドオンの置き場所を作った／消した」は見逃さず、他アドオンが
     自分のファイルを書くぶんには反応しない。
  3. 空振りしないことを毎回確かめる——見張りの比較器が変化を検出できることを、一時ディレクトリの
     カナリアで毎回（import 時に）試す。検出できなければ例外で止まる。
     end-to-end（本当に rc が 1 になるか）は tools/isolation_selftest.py で確かめる。

■ 使い方（各 *_selftest.py）
    sys.path.insert(0, str(ROOT / "tools"))
    import isolation  # noqa: E402  ← ir_bridge より先に
    from ir_bridge import ...
    ...
    if __name__ == "__main__":
        sys.exit(isolation.run(main))

■ 限界（正直に書いておく）
本物の **ir-bridge** サービスが稼働中にテストを走らせ、その間にサービスが status.json や
設定を書くと、見張りはそれも「動いた」と数えて失敗にする（安全側の誤検出）。そのときは
再実行すればよい。**他アドオン**の書き込みでは失敗しない（上の 2. の名前だけの比較）。
"""
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 環境変数の名前は本番コードの定数と同じもの（status.DATA_DIR_ENV・config.CONFIG_DIR_ENV）。
# ここで ir_bridge を import して読むと、差し替えより先に import されてしまうので文字列で持つ。
DATA_DIR_ENV = "IR_BRIDGE_DATA_DIR"
CONFIG_DIR_ENV = "IR_BRIDGE_CONFIG_DIR"
# isolation_selftest が「見張りが本当に rc を落とすか」を試すときだけ使う（追加の見張り先）。
EXTRA_WATCH_ENV = "IR_BRIDGE_ISOLATION_EXTRA_WATCH"


def _state(path: Path):
    """1エントリの状態。不在は None（不在も状態として比べる）。"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    kind = "dir" if os.path.isdir(path) and not os.path.islink(path) else "file"
    return (kind, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def snapshot(roots) -> dict:
    """roots 配下の全エントリ（root 自身を含む）の状態を取る。"""
    out = {}
    for root in roots:
        root = Path(root)
        out[str(root)] = _state(root)
        if out[str(root)] is None or out[str(root)][0] != "dir":
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames + filenames:
                p = Path(dirpath) / name
                out[str(p)] = _state(p)
    return out


def names(roots) -> dict:
    """roots **直下**の名前の一覧だけを取る（中身の size/mtime は見ない）。不在は None。

    他アドオンと共有するディレクトリ（`~/addon-data/`）用（W19-17）。中身まで比べると
    稼働中の他アドオンの書き込みを「隔離が破れた」と報告する。名前の増減だけなら、
    「ir-bridge が他アドオンの置き場所を作った／消した」は拾えて、誤検出はしない。
    """
    out = {}
    for root in roots:
        try:
            out[str(root)] = sorted(p.name for p in Path(root).iterdir())
        except OSError:
            out[str(root)] = None
    return out


def diff_names(before: dict, after: dict) -> list[str]:
    """名前の一覧の増減を人が読める形で返す。空なら変化なし。"""
    lines = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        if a is None or b is None:
            lines.append(f"ディレクトリ自体が現れた/消えた: {key}（{a} → {b}）")
            continue
        added = sorted(set(b) - set(a))
        removed = sorted(set(a) - set(b))
        lines.append(f"直下の名前が変わった: {key}（増={added} 減={removed}）")
    return lines


def diff(before: dict, after: dict) -> list[str]:
    """変化したエントリを人が読める形で返す。空なら変化なし。"""
    lines = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        if a is None:
            lines.append(f"作られた: {key}")
        elif b is None:
            lines.append(f"消えた: {key}")
        else:
            what = [n for n, x, y in zip(("種別", "size", "mtime", "ctime", "inode"), a, b) if x != y]
            lines.append(f"変わった（{'・'.join(what)}）: {key}")
    return lines


def _canary() -> None:
    """見張りの比較器が変化を検出できることを確かめる（空振りしないこと）。検出できなければ止める。"""
    with tempfile.TemporaryDirectory(prefix="ir-bridge-canary-") as d:
        root = Path(d) / "watched"
        cases = []

        def expect(name, mutate):
            before = snapshot([root])
            mutate()
            cases.append((name, bool(diff(before, snapshot([root])))))

        expect("不在のディレクトリが作られた", lambda: root.mkdir())
        f = root / "status.json"
        expect("ファイルが作られた", lambda: f.write_bytes(b"{}"))
        expect("追記された（size）", lambda: f.write_bytes(b'{"a": 1}'))
        # size を変えずに mtime だけ動かす。
        expect("mtime だけ動いた", lambda: os.utime(f, ns=(0, f.stat().st_mtime_ns + 1_000_000)))

        def same_size_same_mtime_replace():
            st = f.stat()
            tmp = root / "status.json.tmp"
            tmp.write_bytes(b'{"b": 2}')  # 同じ size
            os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))  # 同じ mtime
            tmp.replace(f)  # tmp+rename の置換（本番の書き方）

        expect("tmp+rename で置き換えられた（inode）", same_size_same_mtime_replace)
        expect("消された", lambda: f.unlink())
        before = snapshot([root])
        unchanged = not diff(before, snapshot([root]))
    broken = [name for name, ok in cases if not ok]
    if broken or not unchanged:
        raise RuntimeError(f"隔離の見張りが空振りする: 検出できなかった={broken} 無変化で誤検出={not unchanged}")


def _real_home() -> Path:
    # HOME はこのあと差し替えるので、パスワードDBから本物のホームを取る。
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


# --- import 時に1回だけ走る ----------------------------------------------------
if "ir_bridge" in sys.modules:
    raise RuntimeError("isolation は ir_bridge より先に import すること（置き場所の差し替えが効かない）")

_canary()

REAL_HOME = _real_home()
ADDON_DATA_ROOT = REAL_HOME / "addon-data"
# 中身まで（size・mtime・ctime・inode・種別）見張る先。**自分の置き場所だけ**（W19-17）。
WATCH = [REAL_HOME / "addons" / "ir-bridge" / "data", ADDON_DATA_ROOT / "ir-bridge"]
if (ROOT / "data").resolve() != WATCH[0].resolve():
    WATCH.append(ROOT / "data")  # 別の場所に clone したリポジトリでも、その data/ を見張る
WATCH += [Path(p) for p in os.environ.get(EXTRA_WATCH_ENV, "").split(os.pathsep) if p]
# 直下の名前の一覧だけを見張る先（他アドオンの書き込みでは動かない・上の 2.）。
NAME_WATCH = [ADDON_DATA_ROOT]

TMP = Path(tempfile.mkdtemp(prefix="ir-bridge-selftest-"))
DATA_DIR = TMP / "data"
CONFIG_DIR = TMP / "home" / "addon-data" / "ir-bridge"
DATA_DIR.mkdir()
(TMP / "home").mkdir()
os.environ[DATA_DIR_ENV] = str(DATA_DIR)
os.environ[CONFIG_DIR_ENV] = str(CONFIG_DIR)
os.environ["HOME"] = str(TMP / "home")

BEFORE = snapshot(WATCH)
BEFORE_NAMES = names(NAME_WATCH)

# 差し替えが本当に効いたか（書き込み先ごとに）を確かめる。ここで初めて ir_bridge を読む。
from ir_bridge import config as _config  # noqa: E402
from ir_bridge import errlog as _errlog  # noqa: E402
from ir_bridge import status as _status  # noqa: E402

WRITE_TARGETS = {
    "status.json": _status.STATUS_FILE,
    "error_addon.log": _errlog.LOG_FILE,
    "error_addon.log.old": _errlog.OLD_FILE,
    "settings.json": _config.SETTINGS_FILE,
    "learned.json": _config.LEARNED_FILE,
    "~/（Path.home()）": Path.home(),
}
for _name, _path in WRITE_TARGETS.items():
    if not _inside(_path, TMP) or any(_inside(_path, w) for w in WATCH):
        raise RuntimeError(f"隔離が効いていない: {_name} → {_path}（一時ディレクトリ {TMP} の外）")


def run(main) -> int:
    """main() を走らせ、前後で本物が動いていないことを確かめる。動いていたら rc=1。"""
    rc = 1
    try:
        rc = main()
    finally:
        changed = diff(BEFORE, snapshot(WATCH)) + diff_names(BEFORE_NAMES, names(NAME_WATCH))
        shutil.rmtree(TMP, ignore_errors=True)
        print()
        if changed:
            print("NG: ★隔離が破れた——本物の data/ または ~/addon-data/ir-bridge/ が動いた:")
            for line in changed:
                print(f"    {line}")
            rc = 1
        else:
            print(
                f"隔離: 本物に変化なし（{len(BEFORE)}エントリを (size, mtime, ctime, inode) で比較・"
                f"不在も状態として比較）: " + "、".join(str(w) for w in WATCH)
            )
            print(
                "      （共有の置き場所は直下の名前だけを比較＝他アドオンの書き込みで誤検出しない・W19-17）: "
                + "、".join(str(w) for w in NAME_WATCH)
            )
    return rc
