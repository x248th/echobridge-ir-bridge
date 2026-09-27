"""errlog: 永続エラーログ data/error_addon.log に**稼働中も**上限を持たせる（W18・§6-2）。

■ なぜ要るか
unit は `StandardError=append:data/error_addon.log` で、systemd が開いたファイルを fd 2 として
渡してくる。回転は unit の `ExecStartPre`（1MB 超なら `.old` へ mv）だけで、これは**起動時にしか
効かない**。再起動しないまま WARNING が出続けると上限が無い（W18 第1段の1で確認した事実）。
API_CONTRACT.md §6-2 は「稼働中も上限を持つこと（目安: 1MB を超えたら .old へ回す）」を求める。

■ 回し方（ExecStartPre と同じ規則に揃える）
- 閾値は **1048576 バイトを超えたら**（ExecStartPre の `-gt 1048576` と同じ）。
- 世代は1つ。名前は `error_addon.log.old`（上書き）。本体は `.old` の中身を読まず、
  有無・サイズ・最終更新時刻だけを診断に出す（§6-2）。
- 回したあとの新しいファイルは 600（ExecStartPre の touch+chmod と同じ権限）。

■ fd 2 ごと付け替える
ファイル名を mv しただけでは、fd 2 は `.old` を指したまま書き続ける（inode が同じなので）。
新しい `error_addon.log` を開いて **dup2 で fd 2 に被せる**。こうすると logging の WARNING だけでなく、
未捕捉例外のトレースバック（Python が fd 2 へ直接書く）も新しいファイルへ行く。

■ 回すのは「fd 2 が本当に data/error_addon.log のとき」だけ
(st_dev, st_ino) を突き合わせる。手で起動した・端末から動かした・自己テストの中、のように
stderr が別物のときは**何もしない**（関係ないファイルを動かさない）。

■ ★fd 2 へ書くのは logging だけ、という不変量に依存している（W19-6）
回転は emit の中でしか起こらないので、**logging を通らずに fd 2 へ書かれたぶんは回転を
起こさない**。この unit の fd 2 には、放っておくと次のものが直接入る:
  - socketserver.BaseServer.handle_error のトレースバック（設定UIの接続で例外が出たとき）
  - スレッドが例外で死んだときの threading.excepthook
  - 未捕捉例外の終了時トレースバック
どれも logging を通らないので、WARNING が1本も出ない（＝正常運転の）機体では**上限が事実上
存在しない**状態になる。§6-2 の「稼働中も上限を持つこと」を満たすために、両側から閉じる:
  (a) 書き手を塞ぐ——webui は do_GET / do_POST を catch-all で包み（W19-3）、
      `_UiServer.handle_error` を上書きして logging へ流す（W19-3）。受信ループと実行
      ワーカーも catch-all で包む（W19-14）。
  (b) 契機を emit だけにしない——`check_size()` を main の60秒ループから呼ぶ。サイズを
      見るだけなので SD には書かない。
この不変量を破る書き込み（print(file=sys.stderr)・subprocess の stderr 継承など）を足す
ときは、ここを読み直すこと。

■ 間引き（receiver._WarnThrottle）は残す
回転は「枠を超えない」ための歯止めで、「本来残すべき行が .old へ押し出される」ことは防がない。
異常の継続を10分に1行へ間引くのは、そちらのための仕組みとして引き続き要る。

■ 時刻を使わない
回すかどうかはサイズだけで決める。RTC 非搭載機で NTP 同期前の時刻が狂っていても判定は変わらない。
"""
import logging
import os
import sys
import time

from .status import DATA_DIR

LOG_FILE = DATA_DIR / "error_addon.log"
OLD_FILE = DATA_DIR / "error_addon.log.old"
# unit の ExecStartPre（`-gt 1048576`）と同じ値。片方だけ変えないこと。
MAX_BYTES = 1048576


class RotatingStderrHandler(logging.StreamHandler):
    """WARNING 以上を stderr へ書き、stderr が error_addon.log なら 1MB 超で .old へ回す。

    回転は emit の中（＝ logging.Handler のロックを握った状態）で行うので、
    複数スレッドからの WARNING と回転が交錯しない。
    """

    def __init__(self, stream=None, path=LOG_FILE, old_path=OLD_FILE, max_bytes: int = MAX_BYTES):
        super().__init__(stream if stream is not None else sys.stderr)
        self.path = path
        self.old_path = old_path
        self.max_bytes = max_bytes
        # 回転に一度失敗したら二度と試みない（失敗を書き続けて枠を食うのを避ける）。
        # 次の再起動で ExecStartPre が回すので、上限が完全に失われるわけではない。
        self._broken = False

    def emit(self, record):
        super().emit(record)
        if self._broken:
            return
        try:
            self._maybe_rotate()
        except OSError as e:
            self._broken = True
            self._note_broken(e)

    def check_size(self) -> bool:
        """emit 以外の契機で回す（main の常駐ループから60秒ごと）。回したら True。

        ★回転の契機を emit だけにできない理由はモジュール冒頭（fd 2 には logging を
        通らない書き込みも入る・W19-6）。**サイズを見るだけで、要らなければ何も書かない**
        ＝SD への書き込みは増えない。
        ハンドラのロックを握って呼ぶので、他スレッドの emit 中の回転と交錯しない。
        """
        if self._broken:
            return False
        self.acquire()
        try:
            return self._maybe_rotate()
        except OSError as e:
            self._broken = True
            self._note_broken(e)
            return False
        finally:
            self.release()

    def _note_broken(self, e: OSError) -> None:
        """回転に失敗したことを1回だけ直接書く。

        logging を通さない（このハンドラへ再入するため）。**この1行だけは上の不変量の例外**で、
        回転が壊れていることを永続ログに残す価値のほうが大きい（1回だけ・短い1行）。
        """
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self.stream.write(
                f"[{now}] WARNING error_addon.log を回せなかった（以後は再起動時の回転だけになる）: {e}\n"
            )
            self.flush()
        except (OSError, ValueError):
            pass

    def _maybe_rotate(self) -> bool:
        """必要なら回す。回したら True。stderr が当のファイルでなければ何もしない。"""
        try:
            fd = self.stream.fileno()
        except (AttributeError, OSError, ValueError):
            return False  # StringIO 等（自己テストのキャプチャ）
        st = os.fstat(fd)
        if st.st_size <= self.max_bytes:
            return False
        try:
            on_disk = os.stat(self.path)
        except FileNotFoundError:
            return False
        if (on_disk.st_dev, on_disk.st_ino) != (st.st_dev, st.st_ino):
            return False  # stderr は別物（端末・手起動・試験）。関係ないファイルは動かさない
        self.flush()
        os.replace(self.path, self.old_path)
        try:
            new_fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        except OSError:
            # 新しいファイルを作れないなら名前を戻す（fd は元の inode のまま＝書き続けられる）。
            os.replace(self.old_path, self.path)
            raise
        try:
            os.fchmod(new_fd, 0o600)  # umask に左右されない
            os.dup2(new_fd, fd)
        finally:
            os.close(new_fd)
        return True
