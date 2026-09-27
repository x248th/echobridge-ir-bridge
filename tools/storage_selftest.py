#!/usr/bin/env python3
"""storage_selftest: 設定の外置き（~/addon-data/ir-bridge/）・schema_version・読めなかったファイルの保全・
error_addon.log の稼働中の回転の自己テスト（W18）と、
**logging を通らない書き込みでも回ること**（W19-6）・**status.json も fsync すること**（W19-16）・
install.sh の要件と出力（W19-9・W19-18・W19-19）。

実機・本体API なしで走る。置き場所はすべて tools/isolation.py が一時ディレクトリへ逃がし、
前後で本物の data/ と ~/addon-data/ が動いていないことを確かめる。

    python3 tools/storage_selftest.py    # 全件PASSなら rc=0
"""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import isolation  # noqa: E402  ← ir_bridge より先に（置き場所ごと一時ディレクトリへ逃がす・前後で本物を見張る）

from ir_bridge import codes, config, errlog, main as main_mod, status, webui  # noqa: E402

_failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def of(self, level):
        return [m for lv, m in self.records if lv == level]

    def __enter__(self):
        root = logging.getLogger()
        self._saved = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self)
        root.setLevel(self._saved)


def reset_dir():
    """設定の置き場所を空にする（isolation の一時ディレクトリの中だけ）。"""
    d = config.CONFIG_DIR
    assert isolation._inside(d, isolation.TMP), d
    if d.exists():
        for p in d.iterdir():
            if p.is_dir():
                os.rmdir(p)
            else:
                p.unlink()
        d.rmdir()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --- 置き場所 -----------------------------------------------------------------
def location_section() -> None:
    print("■ 置き場所は ~/addon-data/ir-bridge/（§6-1）・status.json と error_addon.log は data/ のまま")
    # 既定値は環境変数を外した別プロセスで評価する（このプロセスは isolation が差し替え済み）。
    # HOME だけ一時ディレクトリにして、Path.home() から組んでいること（ユーザー名を書いていない）を見る。
    with tempfile.TemporaryDirectory() as h:
        env = {k: v for k, v in os.environ.items() if k not in (config.CONFIG_DIR_ENV, status.DATA_DIR_ENV)}
        env["HOME"] = h
        out = subprocess.run(
            [sys.executable, "-c",
             "import ir_bridge.config as c, ir_bridge.status as s, ir_bridge.errlog as e;"
             "print(c.SETTINGS_FILE); print(c.LEARNED_FILE); print(s.STATUS_FILE); print(e.LOG_FILE); print(e.OLD_FILE)"],
            cwd=ROOT, env=env, capture_output=True, text=True, check=True,
        ).stdout.split("\n")
        check("settings.json は $HOME/addon-data/ir-bridge/", out[0] == f"{h}/addon-data/ir-bridge/settings.json", out[0])
        check("learned.json は $HOME/addon-data/ir-bridge/", out[1] == f"{h}/addon-data/ir-bridge/learned.json", out[1])
        check("status.json は data/ のまま（外置きしない・§6-3）", out[2] == str(ROOT / "data" / "status.json"), out[2])
        check("error_addon.log は data/ のまま（§6-2）", out[3] == str(ROOT / "data" / "error_addon.log"), out[3])
        check("  .old の名前は error_addon.log.old（§6-2・ExecStartPre と同じ）",
              out[4] == str(ROOT / "data" / "error_addon.log.old"), out[4])
    src = (ROOT / "ir_bridge" / "config.py").read_text(encoding="utf-8")
    check("config.py にユーザー名・/home を書いていない", "/home/" not in src and "echobridge\"" not in src)
    check("id は status.SERVICE（ディレクトリ名と一致・§14）", config.CONFIG_DIR.name == status.SERVICE == "ir-bridge")

    print("■ 起動時に置き場所が無ければ作る（誰かに消されても起動できる）")
    reset_dir()
    with _Capture() as cap:
        cfg = config.load()
    check("ディレクトリが作られる", config.CONFIG_DIR.is_dir())
    check("  既定値で読める", cfg.base == codes.DEFAULT_BASE and cfg.learned == {})
    check("  2ファイルが作られる", config.SETTINGS_FILE.exists() and config.LEARNED_FILE.exists())
    check("  作ったことは INFO（WARNING ではない＝永続ログを食わない）",
          any("置き場所が無いので作成" in m for m in cap.of("INFO")) and cap.of("WARNING") == [], str(cap.records))

    print("■ 置き場所を作れなくても常駐は続く（既定値・WARNING）")
    reset_dir()
    blocker = config.CONFIG_DIR.parent
    blocker_saved = config.SETTINGS_FILE, config.LEARNED_FILE
    with tempfile.TemporaryDirectory(dir=isolation.TMP) as t:
        file_as_parent = Path(t) / "not-a-dir"
        file_as_parent.write_text("x")
        config.SETTINGS_FILE = file_as_parent / "ir-bridge" / "settings.json"
        config.LEARNED_FILE = file_as_parent / "ir-bridge" / "learned.json"
        try:
            with _Capture() as cap:
                cfg = config.load()
            check("例外を投げず既定値", cfg.base == codes.DEFAULT_BASE)
            check("  WARNING に理由が出る", any("置き場所を作れない" in m for m in cap.of("WARNING")), str(cap.of("WARNING")))
        finally:
            config.SETTINGS_FILE, config.LEARNED_FILE = blocker_saved
    check("（片付け）元の置き場所の親は触っていない", blocker.is_dir())


# --- schema_version -----------------------------------------------------------
def schema_section() -> None:
    print("■ schema_version: 1 を両ファイルに書く")
    reset_dir()
    config.load()
    s, l = read_json(config.SETTINGS_FILE), read_json(config.LEARNED_FILE)
    check("既定生成の settings.json", s.get("schema_version") == 1 and "format" not in s, str(s))
    check("既定生成の learned.json", l.get("schema_version") == 1 and "format" not in l, str(l))
    cfg = config.default_config()._replace(base=codes.PRESETS[2])
    config.save_settings(cfg)
    config.save_learned(cfg)
    check("save_settings も schema_version を書く", read_json(config.SETTINGS_FILE)["schema_version"] == 1)
    check("save_learned も schema_version を書く", read_json(config.LEARNED_FILE)["schema_version"] == 1)
    check("往復する", config.load().base == codes.PRESETS[2])

    print("■ schema_version が 1 以外（欠落を含む）は「読めなかったファイル」と同じ扱い")
    cases = [
        ("欠落（v0.2.0 の format:1 のファイル）", {"format": 1, "base": "0x4b6a"}),
        ("2（将来の版）", {"schema_version": 2, "base": "0x4b6a"}),
        ("文字列 \"1\"", {"schema_version": "1", "base": "0x4b6a"}),
        ("true（bool は 1 と等しいが版番号ではない）", {"schema_version": True, "base": "0x4b6a"}),
    ]
    for label, payload in cases:
        reset_dir()
        config.CONFIG_DIR.mkdir(parents=True)
        original = (json.dumps(payload) + "\n").encode()
        config.SETTINGS_FILE.write_bytes(original)
        config.LEARNED_FILE.write_bytes(b'{"schema_version": 1, "entries": {}}\n')
        with _Capture() as cap:
            cfg = config.load()
        warns = cap.of("WARNING")
        check(f"{label}: 既定値で起動する", cfg.base == codes.DEFAULT_BASE, hex(cfg.base))
        check("  WARNING は1行だけ（起動時の1回）", len(warns) == 1 and "schema_version" in warns[0], str(warns))
        check("  元のファイルは書き換えない", config.SETTINGS_FILE.read_bytes() == original)
        check("  元の中身を .unreadable に保全する",
              (config.CONFIG_DIR / "settings.json.unreadable").read_bytes() == original)


# --- 読めなかったファイルを黙って上書きしない -------------------------------------
def preserve_section() -> None:
    unread = config.CONFIG_DIR / "settings.json.unreadable"
    print("■ 壊れた settings.json: 起動時に保全し、元のファイルはその場に残す")
    reset_dir()
    config.CONFIG_DIR.mkdir(parents=True)
    broken = b'{"schema_version": 1, "base": "0x4b6a",,,}\n'
    config.SETTINGS_FILE.write_bytes(broken)
    with _Capture() as cap:
        cfg = config.load()
    warns = cap.of("WARNING")
    check("既定値で起動する", cfg.base == codes.DEFAULT_BASE)
    check("  WARNING 1行・保全先を書いてある", len(warns) == 1 and "settings.json.unreadable" in warns[0], str(warns))
    check("  元のファイルはそのまま", config.SETTINGS_FILE.read_bytes() == broken)
    check("  .unreadable に元の中身", unread.read_bytes() == broken)
    check("  .unreadable は 600", oct(unread.stat().st_mode)[-3:] == "600")

    print("■ 壊れたまま再起動を繰り返しても .unreadable を書き直さない（SD を書かない）")
    st = unread.stat()
    with _Capture() as cap:
        config.load()
    st2 = unread.stat()
    check("inode・mtime が同じ", (st.st_ino, st.st_mtime_ns) == (st2.st_ino, st2.st_mtime_ns))
    check("  WARNING は毎起動1行（保全済みと書く）",
          len(cap.of("WARNING")) == 1 and "保全済み" in cap.of("WARNING")[0], str(cap.of("WARNING")))

    print("■ 設定UIの保存で元のファイルが置き換わっても、保全した写しは残る")
    app = webui.UiApp(config.ConfigStore(cfg), recent=None, sender=None, client=None)
    status_code, body = app.set_base({"base": f"0x{codes.PRESETS[1]:04x}"})
    check("保存できる（保全できているので拒否しない）", status_code == 200 and body.get("ok"), str(body))
    check("  settings.json は新しい中身", read_json(config.SETTINGS_FILE)["base"] == f"0x{codes.PRESETS[1]:04x}")
    check("  .unreadable は壊れた元の中身のまま", unread.read_bytes() == broken)

    print("■ 別の壊れ方をしたら .unreadable を置き換え、置き換えたと書く（世代は1つ）")
    broken2 = b"[1, 2, 3]\n"
    config.SETTINGS_FILE.write_bytes(broken2)
    with _Capture() as cap:
        config.load()
    check(".unreadable は新しい中身", unread.read_bytes() == broken2)
    check("  WARNING に「以前の保全は置き換えた」",
          len(cap.of("WARNING")) == 1 and "置き換えた" in cap.of("WARNING")[0], str(cap.of("WARNING")))

    print("■ learned.json の一部だけ読めない: 読めた行で起動し、元の中身を保全する")
    reset_dir()
    config.CONFIG_DIR.mkdir(parents=True)
    learned_raw = {"schema_version": 1, "entries": {
        "nec:0x4008": {"target": {"scene": "scene_01"}, "label": "青"},
        "こわれた": {"target": {"scene": "scene_02"}, "label": "赤"},
    }}
    original = (json.dumps(learned_raw, ensure_ascii=False) + "\n").encode()
    config.LEARNED_FILE.write_bytes(original)
    with _Capture() as cap:
        cfg = config.load()
    warns = cap.of("WARNING")
    check("読めた1件で起動", len(cfg.learned) == 1)
    check("  項目の WARNING 1行＋まとめ1行", len(warns) == 2 and "learned.json.unreadable" in warns[1], str(warns))
    check("  .unreadable に元の中身（壊れた行を含む）",
          (config.CONFIG_DIR / "learned.json.unreadable").read_bytes() == original)
    check("  元のファイルはそのまま", config.LEARNED_FILE.read_bytes() == original)
    app = webui.UiApp(config.ConfigStore(cfg), recent=None, sender=None, client=None)
    status_code, _ = app.add_learned({"code": "nec:0x4005", "scene": "scene_03", "label": "黄"})
    check("  学習の追加はできる", status_code == 200)
    check("  保全した写しには壊れた行が残っている（顧客が手で直す材料）",
          "こわれた" in (config.CONFIG_DIR / "learned.json.unreadable").read_text(encoding="utf-8"))

    print("■ 保全できない（写しの置き場所に書けない）なら保存を拒否する")
    reset_dir()
    config.CONFIG_DIR.mkdir(parents=True)
    config.SETTINGS_FILE.write_bytes(broken)
    unread.mkdir()  # 同名のディレクトリがあると写しを書けない
    with _Capture() as cap:
        cfg = config.load()
    check("WARNING に「保存は拒否する」", any("拒否" in m for m in cap.of("WARNING")), str(cap.of("WARNING")))
    try:
        config.save_settings(cfg)
        refused = False
    except config.SaveRefused:
        refused = True
    check("  save_settings が SaveRefused", refused)
    check("  元のファイルは書き換わらない", config.SETTINGS_FILE.read_bytes() == broken)
    check("  learned.json（読めている側）は保存できる",
          config.save_learned(cfg) is None and read_json(config.LEARNED_FILE)["schema_version"] == 1)

    print("■ 設定UIは拒否を 409 と顧客向けの文面で返す（500・トレースバックにしない）")
    app = webui.UiApp(config.ConfigStore(cfg), recent=None, sender=None, client=None)
    handler_cls = type("_T", (webui._Handler,), {"app": app})
    sent = []

    class _FakeHandler(handler_cls):
        def __init__(self, path, payload):  # 通信は始めない
            data = json.dumps(payload).encode()
            self.path = path
            # 顧客の画面と同じ形（fetch の JSON）で送る。入口の検査（W19-4・W19-7）を通る。
            self.headers = {"Content-Length": str(len(data)), "Content-Type": "application/json"}
            self.rfile = io.BytesIO(data)
            self.client_address = ("192.168.1.9", 50000)

        def _send_json(self, status_code, body):
            sent.append((status_code, body))

    with _Capture() as cap:
        _FakeHandler("/api/base", {"base": f"0x{codes.PRESETS[1]:04x}"}).do_POST()
    check("409", sent and sent[0][0] == 409, str(sent))
    check("  文面は SAVE_REFUSED", sent and sent[0][1].get("message") == webui.SAVE_REFUSED, str(sent))
    check("  操作ごとの行は INFO（起動時に WARNING 済み・枠を食わない）",
          cap.of("WARNING") == [] and any("保存しなかった" in m for m in cap.of("INFO")), str(cap.records))
    check("  元のファイルは書き換わらない", config.SETTINGS_FILE.read_bytes() == broken)
    unread.rmdir()

    if os.geteuid() != 0:
        print("■ 元のファイル自体を読めない（権限）なら保全できないので保存を拒否する")
        reset_dir()
        config.CONFIG_DIR.mkdir(parents=True)
        config.SETTINGS_FILE.write_bytes(broken)
        os.chmod(config.SETTINGS_FILE, 0)
        try:
            with _Capture() as cap:
                cfg = config.load()
            check("既定値で起動", cfg.base == codes.DEFAULT_BASE)
            check("  WARNING 1行に「保存は拒否する」",
                  len(cap.of("WARNING")) == 1 and "拒否" in cap.of("WARNING")[0], str(cap.of("WARNING")))
            try:
                config.save_settings(cfg)
                refused = False
            except config.SaveRefused:
                refused = True
            check("  save_settings が SaveRefused", refused)
        finally:
            os.chmod(config.SETTINGS_FILE, 0o600)
        check("  元のファイルは書き換わらない", config.SETTINGS_FILE.read_bytes() == broken)

    print("■ 正常なファイルでは何も出さず、保全もしない")
    reset_dir()
    config.load()
    with _Capture() as cap:
        config.load()
    check("WARNING なし", cap.of("WARNING") == [], str(cap.of("WARNING")))
    check("  .unreadable を作らない", not list(config.CONFIG_DIR.glob("*.unreadable")))


# --- error_addon.log の稼働中の回転 -----------------------------------------------
def rotation_section() -> None:
    print("■ error_addon.log を稼働中も 1MB 超で .old へ回す（§6-2）")
    unit = (ROOT / "systemd" / "ir-bridge.service").read_text(encoding="utf-8")
    pre = [line for line in unit.splitlines() if line.startswith("ExecStartPre=") and ".old" in line]
    check("閾値は unit の ExecStartPre と同じ（-gt 1048576）",
          errlog.MAX_BYTES == 1048576 and len(pre) == 1 and f"-gt {errlog.MAX_BYTES}" in pre[0], str(pre))
    check("  .old の名前も同じ（error_addon.log.old）",
          errlog.OLD_FILE.name == "error_addon.log.old" and "data/error_addon.log.old" in pre[0])

    d = Path(tempfile.mkdtemp(dir=isolation.TMP))
    log, old = d / "error_addon.log", d / "error_addon.log.old"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, "w", encoding="utf-8")
    handler = errlog.RotatingStderrHandler(stream, path=log, old_path=old, max_bytes=300)
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("storage_selftest.rotation")
    lg.propagate = False
    lg.addHandler(handler)
    try:
        for i in range(4):  # 1行 約67バイト × 4 ＝ 閾値 300 以下
            lg.warning("行%02d %s", i, "x" * 60)
        check("閾値以下では回さない", not old.exists() and log.stat().st_size <= 300, str(log.stat().st_size))
        for i in range(4, 12):
            lg.warning("行%02d %s", i, "x" * 60)
        check("超えたら .old ができる", old.exists())
        check("  新しい error_addon.log が 600 でできる", log.exists() and oct(log.stat().st_mode)[-3:] == "600")
        check("  fd の番号はそのまま・中身は新しいファイル（dup2）",
              stream.fileno() == fd and os.fstat(fd).st_ino == log.stat().st_ino)
        lg.warning("回した後の行")
        check("  回した後の行は新しいファイルへ", "回した後の行" in log.read_text(encoding="utf-8"))
        check("  .old には入らない", "回した後の行" not in old.read_text(encoding="utf-8"))
        first_old = old.read_text(encoding="utf-8")
        for i in range(12, 30):
            lg.warning("行%02d %s", i, "y" * 60)
        check("世代は1つ（.old を上書き・.old.1 等を作らない）",
              old.read_text(encoding="utf-8") != first_old and sorted(p.name for p in d.iterdir()) == [log.name, old.name],
              str(sorted(p.name for p in d.iterdir())))
        check("  合計は上限の2倍＋1行程度で頭打ち",
              log.stat().st_size + old.stat().st_size <= 2 * 300 + 2 * 80, f"{log.stat().st_size}+{old.stat().st_size}")
        # 回転に失敗しても（ディレクトリに書けない）常駐を落とさず、1行だけ残して以後は試みない。
        if os.geteuid() != 0:
            os.chmod(d, 0o500)
            try:
                for i in range(30, 40):
                    lg.warning("行%02d %s", i, "z" * 60)
                handler.flush()
            finally:
                os.chmod(d, 0o700)
            text = log.read_text(encoding="utf-8")
            check("回せないときは1行だけ残して以後は試みない（例外で落ちない）",
                  text.count("error_addon.log を回せなかった") == 1 and handler._broken,
                  f"{text.count('error_addon.log を回せなかった')}行")
            check("  名前は元のまま（.old に取り違えない）", log.exists() and old.exists())
    finally:
        lg.removeHandler(handler)
        stream.close()

    print("■ stderr が error_addon.log でないときは何もしない（端末・手起動・試験）")
    other = d / "other.log"
    other.write_text("")
    s2 = open(other, "a", encoding="utf-8")
    h2 = errlog.RotatingStderrHandler(s2, path=d / "absent.log", old_path=d / "absent.log.old", max_bytes=10)
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "長い行 %s", ("w" * 50,), None)
    h2.emit(rec)
    s2.close()
    check("別のファイルは回さない・関係ないパスを作らない",
          other.stat().st_size > 10 and not (d / "absent.log").exists() and not (d / "absent.log.old").exists())
    h3 = errlog.RotatingStderrHandler(io.StringIO(), path=d / "x.log", old_path=d / "x.log.old", max_bytes=1)
    h3.emit(rec)
    check("StringIO（fileno が無い）でも例外を出さない", not h3._broken)

    print("■ main._setup_logging は WARNING を回転付きのハンドラで出す")
    root = logging.getLogger()
    before = list(root.handlers)
    saved_level = root.level
    try:
        main_mod._setup_logging()
        added = [h for h in root.handlers if h not in before]
        rot = [h for h in added if isinstance(h, errlog.RotatingStderrHandler)]
        check("WARNING のハンドラが RotatingStderrHandler", len(rot) == 1 and rot[0].level == logging.WARNING, str(added))
        check("  回す対象は errlog.LOG_FILE（このテストでは一時ディレクトリ）",
              rot and rot[0].path == errlog.LOG_FILE and isolation._inside(errlog.LOG_FILE, isolation.TMP))
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(saved_level)


# =============================================================================
# W19-6: 回転の契機を emit だけにしない
# =============================================================================
def check_size_section() -> None:
    print("■ W19-6 logging を通らずに fd 2 が膨らんでも回る（契機を emit だけにしない）")
    d = Path(tempfile.mkdtemp(dir=isolation.TMP))
    log, old = d / "error_addon.log", d / "error_addon.log.old"
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, "w", encoding="utf-8")
    handler = errlog.RotatingStderrHandler(stream, path=log, old_path=old, max_bytes=300)
    try:
        # ★logging を通さずに直接書く（socketserver のトレースバック・threading.excepthook・
        #   未捕捉例外の終了時トレースバック、が本番でこうなる）。
        stream.write("x" * 500)
        stream.flush()
        check("  前提: 上限を超えている", log.stat().st_size > 300, str(log.stat().st_size))
        check("  前提: それでも .old はできていない（emit が呼ばれていない）", not old.exists())
        rotated = handler.check_size()
        check("  ★check_size() が回す（旧実装には無く、上限が事実上存在しなかった）", rotated is True)
        check("    .old ができる", old.exists() and old.stat().st_size > 300)
        check("    新しい error_addon.log は 0 バイト・600",
              log.stat().st_size == 0 and oct(log.stat().st_mode)[-3:] == "600")
        check("    fd の番号はそのまま（dup2）",
              stream.fileno() == fd and os.fstat(fd).st_ino == log.stat().st_ino)
        check("  閾値以下なら何もしない（SD を書かない）", handler.check_size() is False)
        before = (log.stat().st_mtime_ns, old.stat().st_mtime_ns)
        for _ in range(5):
            handler.check_size()
        check("    何度呼んでもファイルは動かない",
              (log.stat().st_mtime_ns, old.stat().st_mtime_ns) == before)
        handler._broken = True
        check("  一度回転に失敗していたら試みない（既存の方針と同じ）", handler.check_size() is False)
    finally:
        stream.close()

    print("■ W19-6 常駐ループが 60秒ごとに check_size() を呼ぶ")
    src = (ROOT / "ir_bridge" / "main.py").read_text(encoding="utf-8")
    check("  _setup_logging がハンドラを返す", "return err" in src)
    check("  常駐ループから呼んでいる", "err_handler.check_size()" in src)
    check("  不変量が errlog.py の冒頭に書いてある（次に読む人のため）",
          "logging を通らない" in errlog.__doc__ and "check_size" in errlog.__doc__)
    check("  fd 2 へ直接書く経路を塞いだことも書いてある",
          "handle_error" in errlog.__doc__ and "threading.excepthook" in errlog.__doc__)


# =============================================================================
# W19-16: status.json も fsync する（config と同じ1本を通る）
# =============================================================================
def fsync_section() -> None:
    print("■ W19-16 status.json も fsync する（config._write_bytes と非対称だった）")
    check("  書き方は1本（config._write_bytes は status.write_bytes_atomic を呼ぶ）",
          "write_bytes_atomic(path, data)" in (ROOT / "ir_bridge" / "config.py").read_text(encoding="utf-8"))

    calls = []
    real_fsync = status.os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    status.os.fsync = spy
    try:
        status.DATA_DIR.mkdir(parents=True, exist_ok=True)
        status.write_status(status.build_status("9.9.9"))
        check("  ★write_status が fsync する（旧実装は write_text で fsync しなかった）",
              len(calls) == 1, str(calls))
        check("    中身は読める JSON・権限は 600",
              json.loads(status.STATUS_FILE.read_text())["version"] == "9.9.9"
              and oct(status.STATUS_FILE.stat().st_mode)[-3:] == "600")
        check("    .tmp を残さない", not status.STATUS_FILE.with_name("status.json.tmp").exists())
        calls.clear()
        reset_dir()
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config.save_settings(config.default_config())
        check("  設定ファイルも同じ経路（従来どおり fsync する）", len(calls) == 1, str(calls))
    finally:
        status.os.fsync = real_fsync

    print("■ W19-16 書けないときは書きかけ（.tmp）を残さない・例外は OSError のまま")
    d = Path(tempfile.mkdtemp(dir=isolation.TMP)) / "not-a-dir"
    d.write_bytes(b"")
    raised = None
    try:
        status.write_bytes_atomic(d / "x.json", b"{}")
    except OSError as e:
        raised = e
    check("  OSError が上がる（main が捕まえて継続できる形）", isinstance(raised, OSError), repr(raised))
    check("  .tmp は残らない", not (d.parent / "not-a-dir" / "x.json.tmp").exists())


# =============================================================================
# W19-9・W19-18・W19-19: install.sh
# =============================================================================
def install_section() -> None:
    src = (ROOT / "install.sh").read_text(encoding="utf-8")
    body = [line for line in src.splitlines() if not line.lstrip().startswith("#")]

    print("■ W19-9 Python の要件は 3.10 以上（実装と一致させる）")
    check("  実装は PEP 604 の `X | Y` を実行時に評価される位置で使っている（事実の固定）",
          any("Target = SceneTarget | LightTarget" in line
              for line in (ROOT / "ir_bridge" / "codes.py").read_text(encoding="utf-8").splitlines()))
    check("  どのファイルにも from __future__ import annotations は無い",
          not any("from __future__ import annotations" in f.read_text(encoding="utf-8")
                  for f in (ROOT / "ir_bridge").glob("*.py")))
    check("  ★install.sh のしきい値は (3, 10)（旧実装は (3, 9) で、3.9 機は確認を通過して落ちた）",
          any("(3, 10)" in line for line in body), str([x for x in body if "version_info" in x]))
    check("    (3, 9) は残っていない", not any("(3, 9)" in line for line in body))
    check("  ★失敗時に実際の版を出す", any("$PY_VER" in line and "!!" in line for line in body),
          str([x for x in body if "PY_VER" in x]))
    readme = (ROOT / "public" / "README.md").read_text(encoding="utf-8")
    check("  公開 README も 3.10 以上", "Python 3.10 以上" in readme and "Python 3.9 以上" not in readme)
    check("  ir_bridge/__init__.py にも要件が書いてある",
          "3.10" in (ROOT / "ir_bridge" / "__init__.py").read_text(encoding="utf-8"))

    print("■ W19-19 root が /tmp の予測できる名前へ書かない（mktemp）")
    # 見るのは**実行される行**だけ（コメントでは `$$` に言及している）。
    check("  ★`$$` を使った一時ファイル名が無い（旧実装は /tmp/<name>.$$）",
          not any("$$" in x for x in body), str([x for x in body if "$$" in x]))
    for var in ("UNIT_TMP=", "SUDOERS_TMP="):
        line = next(x for x in body if x.startswith(var))
        check(f"  {var[:-1]} は mktemp で作る", "mktemp" in line, line)
    check("  どの経路で抜けても消す（trap）", any(x.startswith("trap ") for x in body))

    print("■ W19-18 「やる前」に「やった」と読める行を出さない（§24）")
    lines = src.splitlines()
    unit_echo = next(i for i, x in enumerate(lines) if "systemd unit配置:" in x and x.lstrip().startswith("echo"))
    unit_cp = next(i for i, x in enumerate(lines) if x.startswith("sudo cp ") and "UNIT_DST" in x)
    check("  ★unit の「配置」は cp の**後**に出す（旧実装は前に出していた）",
          unit_echo > unit_cp, f"echo={unit_echo} cp={unit_cp}")
    sudo_echo = next(x for x in lines if "SUDOERS_DST" in x and x.lstrip().startswith("echo") and "生成" in x)
    check("  sudoers の生成・検証は予定の形（「…する」）", "する:" in sudo_echo, sudo_echo)
    check("  配置したことは visudo -c 合格の後にだけ出す",
          any("sudoers配置:" in x and "合格" in x for x in lines))

    print("■ W19-13 CLAUDE.md の節番号の参照ずれ")
    unit = (ROOT / "systemd" / "ir-bridge.service").read_text(encoding="utf-8")
    check("  依存ゼロは §11（§8 ではない）",
          "CLAUDE.md §11" in src and "CLAUDE.md §11" in unit
          and "CLAUDE.md §8" not in src and "CLAUDE.md §8" not in unit)
    check("  「逼迫時に死ぬのはアドオン側」は §5（§4 ではない）",
          "(CLAUDE.md §5)" in unit and "(CLAUDE.md §4)" not in unit)
    print("■ W19-13 骨格段階のままの記述を落とす（いまの実装と食い違う現在形の記述）")
    # ★過去の実測への言及（「骨格時点 2026-08-14 の dev 機で …」）は**残してよい**。
    #   落とすのは「現状は…」と現在形で書かれていて、いまは事実でないもの。
    stale = [
        ("ir_bridge/__init__.py", "現状は骨格のみ"),
        ("ir_bridge/__init__.py", "IR送受信は未実装"),
        ("systemd/ir-bridge.service", "骨格段階の本アドオン"),
        ("systemd/ir-bridge.service", "骨格の時点で入れておくのは"),
        ("ir_bridge/sender.py", "設定UIはまだ無い"),
    ]
    for name, phrase in stale:
        text = (ROOT / name).read_text(encoding="utf-8")
        check(f"  {name}: 「{phrase}」が残っていない", phrase not in text)
    check("  unit の「本体APIを一切叩かない」も落とした（M2 から叩いている）",
          "本体API(:8099)を一切叩かない" not in unit)


def unit_section() -> None:
    print("■ unit の MemoryMax は 64M（受け渡しW15）・OOMScoreAdjust=500 は据え置き")
    unit = (ROOT / "systemd" / "ir-bridge.service").read_text(encoding="utf-8")
    lines = [line for line in unit.splitlines() if not line.startswith("#")]
    check("MemoryMax=64M", "MemoryMax=64M" in lines, str([x for x in lines if x.startswith("Memory")]))
    check("OOMScoreAdjust=500", "OOMScoreAdjust=500" in lines)


def main() -> int:
    location_section()
    schema_section()
    preserve_section()
    rotation_section()
    check_size_section()
    fsync_section()
    install_section()
    unit_section()
    print()
    if _failures:
        print(f"NG: {len(_failures)}件 失敗 — {_failures}")
        return 1
    print("OK: 全件PASS")
    return 0


if __name__ == "__main__":
    sys.exit(isolation.run(main))
