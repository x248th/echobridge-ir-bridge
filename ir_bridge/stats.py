"""stats: 起動からの累計カウンタ（RAMのみ・再起動でリセット）。診断レポート用。

■ なぜ数えるか（M3-e）
**顧客環境での実測を集める計器**である。開発機で0件だった症状が顧客宅で起きているか、
やり取り無しで分かる必要がある（ゼロサポート型）。とくに:
  - `recovered`（ラベル反転から救済）… ir_toy の欠陥が顧客の機体で出ている頻度。
    IR-14 の実測は開発機で0件だったが、別機種・別の操作パターンは未知である。
  - `unknown`（未登録）… 導出コードと実在リモコンの衝突の実測。
**判定器を降ろして計器を製品側に置く**という、このリポジトリで繰り返している形と同じ。

■ 持ち方
ファイルには書かない（SD摩耗を買わない・RecentCodes と同じ方針）。整数がいくつかあるだけで
動作には影響しない。読み書きはロックで守る——書くのは受信スレッドと送信スレッド、
読むのは設定UIのスレッドで、`snapshot()` は**コピー**を返す（読み手が途中経過を持ち歩かない）。
"""
import threading

# 数える項目。診断レポートの並び順もこの順に対応する。
KEYS = (
    "received",  # データフレーム（リピートを除く・デバウンスで抑制したものも除く）
    "scene",  # 解釈できた: シーン
    "light",  # 解釈できた: 個別照明
    "unknown",  # 未登録（learned にも導出にも一致せず）
    "other_preset",  # 他プリセットのコード
    "out_of_range",  # 導出コードだが輝度が範囲外
    "foreign_necx",  # 他社リモコンの拡張NECフレーム
    "recovered",  # ラベル反転から救済（polarity.py）
    "repeats",  # リピートフレーム（押しっぱなし）
    "debounced",  # デバウンス窓の内側で抑制
    "sent",  # 送信できた
    "send_skipped",  # 受信が続いていて見送った
    "send_failed",  # 送信しようとして失敗した（デバイス無し・ir-ctl 失敗等）
)


class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self._values = dict.fromkeys(KEYS, 0)

    def bump(self, key: str, n: int = 1) -> None:
        """1つ増やす。未知のキーは黙って無視する（計器のために常駐を落とさない）。"""
        if key not in self._values:
            return
        with self._lock:
            self._values[key] += n

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._values)
