"""ir_bridge: EchoBridge 赤外線アドオン。

常駐して赤外線を受信（NECデコード→本体APIでシーン発火・輝度設定）し、学習用に送信もする。
本体WebUI向けに data/status.json を書き、設定UI（:8100）を同じプロセスで立てる。
外部依存を持たない（Python 標準ライブラリのみ・venv も requirements.txt も持たない）。
★**Python 3.10 以上が要る**（PEP 604 の `X | Y` 型表記を実行時に評価される位置で使っている。
  install.sh のランタイム確認・public/README.md と同じ値。W19-9）。
"""
