"""recent: 直近に受信した赤外線コードのリングバッファ（RAMのみ・既定50件）。

**ファイルには書かない。** SD摩耗を避けるためで、プロセス再起動で消えてよい
（M3の学習UIが「今押したリモコンのコード」を拾うための一時的な窓であって、永続記録ではない）。

M3では :8100 の設定UIを**同一プロセス内**に立てて、この同じインスタンスを読む想定。
そのため全操作をロックで保護し、snapshot() は呼び出し側へ**コピー**を返す
（UIスレッドが保持したdictを受信スレッドが書き換える、という共有事故を作らない）。

■ なぜ50件か（M3-bで10件から増やした）
未登録コードのログが「顧客宅で導出コードと実在リモコンが衝突していないかを測る
**唯一の計器**」に格上げされたため。顧客が犯人のリモコンを探して何ボタンも押すと、
10件では調べている最中に窓から溢れる。RAMのみ・1件あたり数百バイトなので
コストは無視できる（永続化はしない＝SD摩耗と1MBログ枠を買わない）。

■ 解釈結果も一緒に持つ
設定UIがそのまま一覧に出せるように、コード文字列だけでなく「何と解釈したか」を
併せて保持する:
    "nec32:0x7d2e0332" → 照明3 を 50% に設定
    "nec:0x4008"       → 未登録

「1エントリ ＝ 1回の押下」で数える。同一コードの連投（リモコンが1押下で複数フレームを
送る場合）とリピートフレームは、新しいエントリを積まずに先頭エントリの count を増やす
＝窓が一瞬で流れて消えるのを防ぐ。
"""
import threading
from collections import deque
from datetime import datetime, timezone

DEFAULT_SIZE = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RecentCodes:
    def __init__(self, size: int = DEFAULT_SIZE):
        self._buf: deque[dict] = deque(maxlen=size)
        self._lock = threading.Lock()

    def record(
        self,
        code: str,
        protocol: str,
        summary: str,
        source: str,
        fired: bool,
        target: dict | None = None,
    ) -> None:
        """新しい押下として1件積む（最古を押し出す）。

        summary は「照明3 を 50% に設定」「未登録」等、そのまま画面に出せる短文。
        source は learned / derived / other_preset / out_of_range / unknown。
        target は宛先の構造（codes.target_info）。**設定UIが表示名を差し込むために使う**
        ——ここでは表記文字列ではなく key / instance を持つ（理由は codes.target_info）。
        宛先が無い行（未登録・範囲外・別プリセット）は None で、画面は summary をそのまま出す。
        """
        now = _now_iso()
        with self._lock:
            self._buf.appendleft(
                {
                    "code": code,
                    "protocol": protocol,
                    "summary": summary,
                    "target": target,
                    "source": source,
                    "first_at": now,
                    "last_at": now,
                    "count": 1,
                    "repeat": False,
                    # fired=本体APIへ実行を指示したか（成否は別。失敗はWARNINGでログに出る）。
                    "fired": fired,
                }
            )

    def bump(self, code: str | None = None, repeat: bool = False) -> bool:
        """先頭エントリの計数を増やす。code 指定時は先頭が同じコードのときだけ。

        リピートフレームは値を運ばないので code=None で呼ばれる（直前の押下に紐づける）。
        対応する押下が無い（バッファが空）ときは何もせず False を返す＝
        受信途中から聞き始めた孤立リピートを、コード不明のまま積まない。
        """
        with self._lock:
            if not self._buf:
                return False
            head = self._buf[0]
            if code is not None and head["code"] != code:
                return False
            head["count"] += 1
            head["last_at"] = _now_iso()
            if repeat:
                head["repeat"] = True
            return True

    def snapshot(self) -> list[dict]:
        """新しい順のコピーを返す（M3のUIがそのままJSONにできる形）。"""
        with self._lock:
            return [dict(e) for e in self._buf]
