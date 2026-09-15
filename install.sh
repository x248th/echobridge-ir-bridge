#!/usr/bin/env bash
# EchoBridge IR Bridge インストーラ（冪等・再実行安全）。
# data/準備・systemd unitを実パスに置換して配置・sudoers配置・enable/startまで行う。
# 構成はMatter版 install.sh の踏襲（npm ci の工程は不要＝標準ライブラリのみで動くため落とした）。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
# アドオンid。**ディレクトリ名（~/addons/<id>）・unit名の茎・status.json の service キーの
# 3者がこの文字列で完全一致していなければならない。** 本体は
#   - unit名を ADDON_SERVICE_WHITELIST（実測 {"hap-bridge", "matter-bridge"}）に照合し、
#   - _purge_all_addons() と api_addon_catalog_install() では**ディレクトリ名をそのまま**照合する。
# M1で独自形 echobridge-addon-ir-bridge を作って外した（DEV_LOG 2026-08-14 M1-C）。
# 既存2アドオン（hap-bridge / matter-bridge）と同じ短名が正しい形である。
ADDON_ID="ir-bridge"
SERVICE_NAME="$ADDON_ID"
UNIT_SRC="$REPO_DIR/systemd/$SERVICE_NAME.service"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"
UNIT_TMP="/tmp/$SERVICE_NAME.service.$$"
# sudoers: 本体WebUIがアドオンを起動/停止/再起動するための最小権限（手動visudo工程を廃止）。
# ★ファイル名だけは echobridge-addon-<id> のフル形にする（matter/hap の実物と同じ:
#   unit=matter-bridge.service に対し /etc/sudoers.d/echobridge-addon-matter-bridge）。
#   /etc/sudoers.d は他用途のファイルと同居する共有ディレクトリで、素の "ir-bridge" では
#   どの製品のものか分からず、本体側の一括撤去がプレフィクスで拾う場合にも漏れるため。
#   **許可対象のコマンドに書くのは unit名（$SERVICE_NAME）** で、ここが本体の呼び方と一致する。
SUDOERS_DST="/etc/sudoers.d/echobridge-addon-$ADDON_ID"
SUDOERS_TMP="/tmp/echobridge-addon-$ADDON_ID.$$"
SYSTEMCTL="$(command -v systemctl)"
PYTHON_BIN="$(command -v python3 || true)"
STATUS_JSON="$REPO_DIR/data/status.json"
# 起動確認のタイムアウト（秒）。Matter版と同じ90秒（実測は1秒未満だが、値を揃えて驚きを減らす）。
# 環境変数で上書き可（検証用）。
STARTUP_TIMEOUT="${IR_BRIDGE_STARTUP_TIMEOUT:-90}"

# 起動確認: data/status.json が「確認開始時刻より後に」書き直されるのを待つ。
# systemctl is-active 等の起動直後判定では、起動後に遅れてクラッシュする故障が確認をすり抜ける
# 偽陽性窓があり、rc=0でインストール成功扱い→クラッシュループ→status.json不在の半導入状態、
# という袋小路を生む（Matter版S1で実際に踏んだ）。
# アドオンは起動時に必ず status.json を書く（ir_bridge/main.py の初回書き出しは差分判定を通さない）
# ので、その新規書き込みを「プロセスが実際に立ち上がりきった」ことの実証に使う。
# 既存ファイル（前回導入時のもの）での誤検知を避けるため、restart より前に置いたマーカーと
# mtime を比較する（-nt＝マーカーより新しい）。
wait_for_started() {
    local status_path="$1" marker="$2" timeout="$3" waited=0
    echo "==> 起動確認: $status_path の新規書き込みを待つ（最大 ${timeout}秒）"
    while [ "$waited" -lt "$timeout" ]; do
        if [ -e "$status_path" ] && [ "$status_path" -nt "$marker" ]; then
            echo "==> 起動確認OK: ${waited}秒でアドオンが status.json を書き直した（起動完了）"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "!! 起動確認に失敗: ${timeout}秒以内に status.json が書き直されなかった。" >&2
    echo "!! アドオンが起動できていない（または起動直後にクラッシュした）可能性が高い。" >&2
    echo "!! ログを確認すること:" >&2
    echo "!!     journalctl -u $SERVICE_NAME -n 50 --no-pager" >&2
    echo "!!     systemctl status $SERVICE_NAME" >&2
    echo "!!     $REPO_DIR/data/error_addon.log" >&2
    return 1
}

echo "==> REPO=$REPO_DIR USER=$USER_NAME PYTHON=$PYTHON_BIN"

# sudo で実行された場合、$USER_NAME は root になる（unitのUser=とsudoers対象がrootになり不正）。
# 実ユーザ(SUDO_USER)へ倒す＝「sudo ./install.sh」で正しい所有者が入る。
if [ "$USER_NAME" = "root" ] && [ -n "${SUDO_USER:-}" ]; then
    USER_NAME="$SUDO_USER"
    echo "==> sudo実行を検出: USER=$USER_NAME を対象にする"
fi

# 1. ランタイム確認（依存インストールは無い。外部パッケージを使わない設計＝CLAUDE.md §8）。
if [ -z "$PYTHON_BIN" ]; then
    echo "!! python3 が見つからない。" >&2
    exit 1
fi
"$PYTHON_BIN" - <<'PY' || { echo "!! Python 3.9 以上が必要。" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

# 2. data/（status.json・error_addon.log・将来のIRコードDBの置き場。gitignore済み）
mkdir -p "$REPO_DIR/data"
chown "$USER_NAME" "$REPO_DIR/data"
# error_addon.log の所有権と権限を揃える。
# systemd の StandardError=append: は mode を指定できず、systemd が新規作成すると root:root 644
# になる（status.json は 600 なので非対称になる）。unit側の ExecStartPre でも 600 を立てるが、
# そちらは User= 権限で走るため root所有の既存ファイルには chmod できない。ここ（root権限）で揃える。
touch "$REPO_DIR/data/error_addon.log"
chown "$USER_NAME" "$REPO_DIR/data/error_addon.log"
chmod 600 "$REPO_DIR/data/error_addon.log"

# 3. unitテンプレートのプレースホルダを実パスに置換 → 配置
echo "==> systemd unit配置: $UNIT_DST"
sed -e "s|%REPO%|$REPO_DIR|g" -e "s|%USER%|$USER_NAME|g" -e "s|%PYTHON%|$PYTHON_BIN|g" "$UNIT_SRC" > "$UNIT_TMP"
sudo cp "$UNIT_TMP" "$UNIT_DST"
rm -f "$UNIT_TMP"

# 4. sudoers 自動配置（本体WebUIのアドオン起動/停止/再起動をNOPASSWDで許可）
#    一時ファイルに生成 → visudo -c で構文検証 → 合格時のみ配置（検証失敗なら配置せず中断）。
#    絶対パスの systemctl・サービス名完全形（本体WebUIの呼び方に一致）・3動詞限定:
#      systemctl enable --now  ir-bridge.service
#      systemctl disable --now ir-bridge.service
#      systemctl restart       ir-bridge.service
#    ※本体のホワイトリストには W14（本体 commit 70e9cb1）で登録済み。本体WebUIのトグルがこれを撃つ。
echo "==> sudoers生成・検証: $SUDOERS_DST"
cat > "$SUDOERS_TMP" <<SUDO
# EchoBridge 赤外線アドオン: 本体WebUIがアドオンを起動/停止/再起動するための最小権限。
# install.sh が visudo -c 検証を通してから配置する（手動 visudo 工程は不要）。
$USER_NAME ALL=(ALL) NOPASSWD: $SYSTEMCTL enable --now $SERVICE_NAME.service, $SYSTEMCTL disable --now $SERVICE_NAME.service, $SYSTEMCTL restart $SERVICE_NAME.service
SUDO
if sudo visudo -c -f "$SUDOERS_TMP" >/dev/null; then
    # install=cp相当＋権限を原子的に設定（sudoers.dは0440/root:root必須）。冪等（毎回上書き）。
    sudo install -m 0440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_DST"
    rm -f "$SUDOERS_TMP"
    echo "==> sudoers配置: $SUDOERS_DST（visudo -c 合格）"
else
    rm -f "$SUDOERS_TMP"
    echo "!! sudoers構文検証(visudo -c)に失敗。配置せず中断する。" >&2
    exit 1
fi

# 5. 反映＋自動起動有効化＋起動
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# 起動確認の基準時刻マーカーは restart より前に置く（これより新しい status.json だけを
# 「今回の起動が書いたもの」と認める）。失敗時は exit 1＝呼び出し側の自動ロールバックが
# 発火する側へ倒す（半導入状態の袋小路を構造的に消す）。
STARTUP_MARKER="$(mktemp "${TMPDIR:-/tmp}/$SERVICE_NAME-startmark.XXXXXX")"
sudo systemctl restart "$SERVICE_NAME"
if ! wait_for_started "$STATUS_JSON" "$STARTUP_MARKER" "$STARTUP_TIMEOUT"; then
    rm -f "$STARTUP_MARKER"
    exit 1
fi
rm -f "$STARTUP_MARKER"

cat <<EOF

==> インストール完了。
    状態:     systemctl status $SERVICE_NAME
    ログ:     journalctl -u $SERVICE_NAME -f
    異常ログ: data/error_addon.log（WARNING以上のみ・起動時に1MB超で.oldへローテ）

    起動/停止は本体の管理画面（アドオンのカード）から行えます。
    設定画面: http://<この機体のアドレス>:8100/（同じネットワークの端末から開けます）
EOF
