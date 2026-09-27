"""warnlimit: 同じ理由の WARNING を間引き、**間引いた件数は失わない**（W18・§6-2）。

■ なぜ要るか
API_CONTRACT.md §6-2: 「異常が続く間 WARNING を出し続ける実装では、間引き（同じ理由は N 分に1行）も
併せて置くこと」。W18 第1段で、間引きの無い経路が2つ見つかった（R-1 本体API の実行失敗・
キュー満杯、R-2 lirc デバイスを開けない）。receiver._WarnThrottle は「理由が1つだけ・変われば
リセット」の形で、理由が同時に複数ありうる（シーンAは404・シーンBは接続不可 …）経路には合わない。

■ 規則（receiver._WarnThrottle と同じ窓）
- 理由（キー）ごとに、初回は必ず出す（「異常が起きた瞬間」は取りこぼさない）。
- 前に出してから WARN_REPEAT_SEC（10分）以内の同じ理由は出さず、**数える**。
- 窓を過ぎて同じ理由が来たら出し、「（この間に同じ失敗が N 件・最後は YYYY-MM-DD HH:MM）」を添える。
- 次の1行を待たずに件数を回収したいとき（停止時など）は drain() で取り出す。
  「押したのに動かなかった」の証跡なので、件数を黙って捨てない。

■ 間引いた件数には「最後に数えた時刻」を添える
件数を載せる行は、**最後の失敗よりずっと後に書かれうる**。停止時の行は、失敗が止んだあと何週間も
停止しなければ何週間も後に書かれ、窓明けの行は、次に同じ失敗が起きるまで（何週間も先でありうる）
書かれない。行のタイムスタンプだけを見た人は「その直前まで失敗が続いていた」と読む——記録が事実と
食い違う型。最後に数えた時刻を添えて、件数がいつまでのものかを行の中で閉じさせる。

■ 判定は monotonic・壁時計は表示だけ
窓の判定に壁時計（NTP 同期前に飛ぶ）を使わない。壁時計は「最後は …」の表示にだけ使う
（同期前なら表示がずれるだけで、間引きの判定は狂わない）。
"""
import threading
import time
from typing import NamedTuple

# 同じ異常が続く間、WARNING を繰り返し出す間隔。receiver._WarnThrottle と同じ値を共有する。
# ★WARNING は data/error_addon.log（永続・1MB で .old へ回す）へ流れる。異常が続いていることは
#   10分に1行で足りる。
WARN_REPEAT_SEC = 600.0
# 覚えておく理由の数の上限（404 の宛先ごとに理由が増えうるので、際限なく育てない）。
MAX_KEYS = 64


def stamp(wall: float) -> str:
    """表示用の時刻（ローカル時刻・分まで）。"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(wall))


class Released(NamedTuple):
    """take() が「出してよい」と答えたときの中身。前に出してから間引いた件数と、その最後の時刻（壁時計）。"""

    suppressed: int
    last_wall: float | None


def suffix(released: Released) -> str:
    """次に出す1行に添える文言。0件なら何も添えない。"""
    if not released.suppressed:
        return ""
    return f"（この間に同じ失敗が {released.suppressed} 件・最後は {stamp(released.last_wall)}）"


class WarnLimiter:
    """理由ごとに WARNING を間引く。スレッドから同時に呼ばれてよい（ロック付き）。"""

    def __init__(
        self, repeat_sec: float = WARN_REPEAT_SEC, clock=time.monotonic, max_keys: int = MAX_KEYS, wall=time.time
    ):
        self._repeat_sec = repeat_sec
        self._clock = clock
        self._wall = wall
        self._max_keys = max_keys
        self._lock = threading.Lock()
        # key -> [最後に出した時刻(monotonic), 出してから数えた件数, 表示用の短い名前, 最後に数えた時刻(壁時計)]
        self._state: dict = {}

    def take(self, key, label: str = "") -> Released | None:
        """出すべきなら Released（前に出してから間引いた件数・最後の時刻）を返す。間引くなら None（件数は数える）。"""
        now = self._clock()
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                self._evict()
                self._state[key] = [now, 0, label or str(key), None]
                return Released(0, None)
            if now - entry[0] < self._repeat_sec:
                entry[1] += 1
                entry[3] = self._wall()
                return None
            released = Released(entry[1], entry[3])
            entry[0], entry[1], entry[3] = now, 0, None
            return released

    def drain(self) -> list[tuple[str, int, float]]:
        """まだ添える機会の無い件数を (表示名, 件数, 最後に数えた時刻) で取り出し、0に戻す（停止時の回収用）。"""
        with self._lock:
            out = [(e[2], e[1], e[3]) for e in self._state.values() if e[1]]
            for e in self._state.values():
                e[1], e[3] = 0, None
            return out

    def _evict(self) -> None:
        # ロックを握って呼ぶ。件数の残っていない理由から捨てる。全部に件数が残っていれば
        # 最も古いものを捨てる（上限64の理由が同時に10分以内に出続ける＝現実には起きない想定）。
        if len(self._state) < self._max_keys:
            return
        idle = [k for k, e in self._state.items() if e[1] == 0]
        victim = idle[0] if idle else min(self._state, key=lambda k: self._state[k][0])
        del self._state[victim]
