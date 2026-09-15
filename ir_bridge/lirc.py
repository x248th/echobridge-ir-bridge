"""lirc: /dev/lirc* の役割判定と MODE2 受信ストリームの読み出し。

外部コマンド（ir-ctl 等）のサブプロセスに依存せず、キャラクタデバイスを直接 read する。

**デバイス番号を決め打ちしない。** 役割は LIRC_GET_FEATURES の結果で判定する
（dev機の実測では lirc0=TX(0x00000302) / lirc1=RX(0x10040000) と、番号の直感とは逆。
番号は起動順で入れ替わりうるので、この実測値にも依存しない）。

定数・ioctl番号の典拠は `/usr/include/linux/lirc.h`（数値をベタ書きせず、同ヘッダの
_IOR/_IOW マクロを Python 側で再現して組み立てる）。

■ 受信デバイスの選択（M2-D）
RXが複数ある機体（GPIOのVS1838B + USBのIrdroid 等）では「最初のRX」では狙った側を
掴めない。環境変数 `IR_BRIDGE_RX_DEVICE` で明示できる（未指定/`auto` は従来どおり
最初のRXを選ぶ＝既存機体の挙動は変えない）。指定の書き方は3通り:

    IR_BRIDGE_RX_DEVICE=auto           自動（既定）
    IR_BRIDGE_RX_DEVICE=/dev/lirc2     パス（`lirc2` / `2` とも書ける）
    IR_BRIDGE_RX_DEVICE=ir_toy         ドライバ名／デバイス名（大小無視）

**USB機（Irdroid）にはドライバ名指定を薦める。** `/dev/lircN` の番号は接続順・起動順で
動く（実測: Irdroidを挿すと lirc2 だが、GPIOのoverlayより先に列挙されれば lirc0 に
なりうる）。ドライバ名は `/sys/class/lirc/lircN/device/uevent` の DRV_NAME/DEV_NAME から
読む（dev機の実測値: gpio-ir-tx / gpio_ir_recv / ir_toy）。sysfs が読めない環境では
None のままにして、パス指定と自動選択だけで動く。

**明示指定が外れたときは自動選択へ落とさない**（受信スレッドが待ち続ける）。
「Irdroidを指定したのに、抜けている間だけ黙ってGPIO側で受け続ける」のは、
どのセンサで受けたか分からなくなるぶん、受信できないより悪い。
"""
import array
import fcntl
import glob
import logging
import os
import select
import struct
from typing import NamedTuple

logger = logging.getLogger(__name__)

# --- lirc.h の値（typeは 'i'・引数は __u32） ---------------------------------
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (ord(type_) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
    )


LIRC_GET_FEATURES = _ioc(_IOC_READ, "i", 0x00, 4)
LIRC_GET_REC_MODE = _ioc(_IOC_READ, "i", 0x02, 4)
LIRC_GET_REC_RESOLUTION = _ioc(_IOC_READ, "i", 0x07, 4)
LIRC_GET_MIN_TIMEOUT = _ioc(_IOC_READ, "i", 0x08, 4)
LIRC_GET_MAX_TIMEOUT = _ioc(_IOC_READ, "i", 0x09, 4)
LIRC_SET_REC_MODE = _ioc(_IOC_WRITE, "i", 0x12, 4)

LIRC_MODE_MODE2 = 0x00000004

LIRC_MODE2_SPACE = 0x00000000
LIRC_MODE2_PULSE = 0x01000000
LIRC_MODE2_FREQUENCY = 0x02000000
LIRC_MODE2_TIMEOUT = 0x03000000
LIRC_MODE2_OVERFLOW = 0x04000000
LIRC_VALUE_MASK = 0x00FFFFFF
LIRC_MODE2_MASK = 0xFF000000

# features のうち役割判定に使うビット（LIRC_MODE2REC(x) = x << 16）。
LIRC_CAN_SEND_PULSE = 0x00000002
LIRC_CAN_REC_MODE2 = 0x00040000

# MODE2イベントの種別（デコーダへはこの文字列で渡す＝テストが波形リストだけで書ける）。
PULSE, SPACE, TIMEOUT, OVERFLOW = "pulse", "space", "timeout", "overflow"

_KIND_BY_MODE2 = {
    LIRC_MODE2_PULSE: PULSE,
    LIRC_MODE2_SPACE: SPACE,
    LIRC_MODE2_TIMEOUT: TIMEOUT,
    LIRC_MODE2_OVERFLOW: OVERFLOW,
}


# 受信デバイスの明示指定（未設定/"auto" は自動選択）。注入点は unit と data/env（後勝ち）。
RX_DEVICE_ENV = "IR_BRIDGE_RX_DEVICE"
AUTO = "auto"

# ドライバ名／デバイス名の出どころ。読めなくても致命ではない（パス指定と自動選択は動く）。
SYS_LIRC_DIR = "/sys/class/lirc"


class LircDevice(NamedTuple):
    path: str
    features: int
    # /sys/class/lirc/lircN/device/uevent の DRV_NAME / DEV_NAME。sysfs非公開なら None。
    driver: str | None = None
    device_name: str | None = None

    @property
    def label(self) -> str:
        """ログ用の短い呼び名。sysfsが読めた機体では人が見て分かる名前になる。"""
        names = [n for n in (self.driver, self.device_name) if n]
        # DRV_NAME と DEV_NAME が同一の機体（gpio_ir_recv）で同じ語を2回書かない。
        uniq = list(dict.fromkeys(names))
        return "/".join(uniq) if uniq else "名前不明"

    @property
    def can_rx(self) -> bool:
        return bool(self.features & LIRC_CAN_REC_MODE2)

    @property
    def can_tx(self) -> bool:
        return bool(self.features & LIRC_CAN_SEND_PULSE)

    @property
    def role(self) -> str:
        if self.can_rx and self.can_tx:
            return "RX+TX"
        if self.can_rx:
            return "RX"
        if self.can_tx:
            return "TX"
        return "不明"


def _get_u32(fd: int, request: int) -> int:
    buf = array.array("I", [0])
    fcntl.ioctl(fd, request, buf, True)
    return buf[0]


def _read_names(path: str) -> tuple[str | None, str | None]:
    """/sys/class/lirc/lircN/device/uevent から (DRV_NAME, DEV_NAME) を読む。

    読めなければ (None, None)。sysfs のレイアウトは契約ではないので、失敗しても
    features による役割判定とパス指定は従来どおり動く＝ここでは黙って諦める。
    """
    uevent = os.path.join(SYS_LIRC_DIR, os.path.basename(path), "device", "uevent")
    try:
        with open(uevent, encoding="utf-8", errors="replace") as f:
            fields = dict(line.strip().split("=", 1) for line in f if "=" in line)
    except (OSError, ValueError):
        return None, None
    return fields.get("DRV_NAME") or None, fields.get("DEV_NAME") or None


def probe(path: str) -> LircDevice | None:
    """1台の features を読む。開けない／ioctlが通らないデバイスは None（WARNINGで継続）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        logger.warning("lircデバイスを開けない（無視して継続）: %s — %s", path, e)
        return None
    try:
        features = _get_u32(fd, LIRC_GET_FEATURES)
    except OSError as e:
        logger.warning("LIRC_GET_FEATURES に失敗（無視して継続）: %s — %s", path, e)
        return None
    finally:
        os.close(fd)
    driver, device_name = _read_names(path)
    return LircDevice(path, features, driver, device_name)


def find_devices() -> list[LircDevice]:
    """/dev/lirc* を番号順に走査して features を読む。"""
    paths = sorted(glob.glob("/dev/lirc[0-9]*"), key=lambda p: int(p[len("/dev/lirc") :]))
    return [d for d in (probe(p) for p in paths) if d is not None]


def rx_spec() -> str | None:
    """環境変数の受信デバイス指定を返す。未設定・空・"auto" は None（＝自動選択）。"""
    value = os.environ.get(RX_DEVICE_ENV, "").strip()
    return None if not value or value.lower() == AUTO else value


def _spec_as_path(spec: str) -> str | None:
    """"2" / "lirc2" / "/dev/lirc2" を "/dev/lirc2" へ正規化する。パス指定でなければ None。"""
    name = spec[len("/dev/") :] if spec.startswith("/dev/") else spec
    if name.isdigit():
        name = "lirc" + name
    if not name.startswith("lirc") or not name[len("lirc") :].isdigit():
        return None
    return "/dev/" + name


def matches(device: LircDevice, spec: str) -> bool:
    """デバイスが指定に合致するか。パス指定はパスで、それ以外は名前（大小無視）で見る。"""
    as_path = _spec_as_path(spec)
    if as_path is not None:
        return device.path == as_path
    wanted = spec.casefold()
    return wanted in {n.casefold() for n in (device.driver, device.device_name) if n}


def select_rx_device(devices: list[LircDevice], spec: str | None) -> LircDevice | None:
    """受信に使う1台を決める。

    spec が None なら「受信できる最初のデバイス」＝従来の挙動（既存機体は何も変わらない）。
    spec があるときは合致し、かつ受信できるものだけを返す。**合致しなければ None を返し、
    自動選択へは落とさない**（黙って別のセンサで受け続けるほうが有害。docstring 冒頭参照）。
    """
    rx = [d for d in devices if d.can_rx]
    if spec is None:
        return rx[0] if rx else None
    for d in rx:
        if matches(d, spec):
            return d
    return None


def find_rx_device(spec: str | None = None) -> LircDevice | None:
    """受信に使うデバイスを探す。spec 既定の None は従来どおり「最初のRX」。"""
    return select_rx_device(find_devices(), spec)


class Mode2Reader:
    """RXデバイスを MODE2 で開き、(種別, マイクロ秒) のイベントを流す。

    stop イベントで抜けられるよう、blocking read ではなく select のタイムアウトを挟む
    （SIGTERM を受けても次の赤外線が来るまで抜けない、という状態を作らない）。
    """

    # select の待ち。停止要求への反応がこの秒数だけ遅れる。
    POLL_SEC = 0.5
    # 1回の read で受け取る最大イベント数（4バイト/件）。
    BATCH = 256

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None
        self._tail = b""

    def open(self) -> None:
        self._fd = os.open(self.path, os.O_RDONLY)
        self._tail = b""
        mode = _get_u32(self._fd, LIRC_GET_REC_MODE)
        if mode != LIRC_MODE_MODE2:
            # rc-core の受信デバイスは既定で MODE2 だが、そうでない場合だけ切り替える。
            fcntl.ioctl(self._fd, LIRC_SET_REC_MODE, struct.pack("=I", LIRC_MODE_MODE2))
            mode = _get_u32(self._fd, LIRC_GET_REC_MODE)
        # **タイムアウト値も分解能も変更しない**（LIRC_SET_REC_TIMEOUT を呼ばない）。
        # 当方のデコーダは32bit揃った時点でフレームを確定し、timeout/overflow は
        # 「状態を捨てる」以外に使わないので、driver既定のままで足りる。
        # 触らない方針は Irdroid で実利がある: 受信タイムアウトの下限が 40000us と高く
        # （GPIO版は下限1us・既定125000us）、GPIO向けに選んだ値を書きに行くと機体によって
        # EINVAL で落ちる。読むだけなら両対応のままでいられる。
        # 分解能は診断目的（Irdroid=21us量子化・GPIO版は ioctl 自体が無い＝ENOTTY）。
        info = []
        for label, request in (
            ("分解能", LIRC_GET_REC_RESOLUTION),
            ("timeout下限", LIRC_GET_MIN_TIMEOUT),
            ("timeout上限", LIRC_GET_MAX_TIMEOUT),
        ):
            try:
                info.append(f"{label}={_get_u32(self._fd, request)}us")
            except OSError:
                continue  # そのデバイスが持たない ioctl（ENOTTY）。診断情報が減るだけ
        logger.info(
            "RX開始: %s rec_mode=0x%08x（MODE2）%s",
            self.path,
            mode,
            " " + " ".join(info) if info else "",
        )

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def events(self, stop):
        """stop がセットされるまで (種別, usec) を yield する。読み取り失敗は OSError で投げる。"""
        assert self._fd is not None, "open() を先に呼ぶこと"
        while not stop.is_set():
            ready, _, _ = select.select([self._fd], [], [], self.POLL_SEC)
            if not ready:
                continue
            chunk = os.read(self._fd, 4 * self.BATCH)
            if not chunk:
                raise OSError("lircデバイスがEOFを返した")
            data = self._tail + chunk
            # 4バイト境界に満たない端数は次回へ持ち越す（カーネルは4の倍数で渡すが保険）。
            usable = len(data) - (len(data) % 4)
            self._tail = data[usable:]
            for (raw,) in struct.iter_unpack("=I", data[:usable]):
                kind = _KIND_BY_MODE2.get(raw & LIRC_MODE2_MASK)
                if kind is None:  # FREQUENCY 等・当方は使わない
                    continue
                yield kind, raw & LIRC_VALUE_MASK
