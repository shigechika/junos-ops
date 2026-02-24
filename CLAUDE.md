# CLAUDE.md

このファイルはClaude Codeがリポジトリを理解するためのコンテキストを提供します。

## プロジェクト概要

junos-opsは、Juniper Networksデバイスの運用を自動化するPythonツールです。デバイスモデルの自動検出、JUNOSパッケージの自動更新、ロールバック、リブートスケジュール管理、RSI/SCF収集をNETCONF/SSH経由で行います。

## 技術スタック

- **言語:** Python 3（3.12以上）
- **主要ライブラリ:** junos-eznc（PyEZ）— Juniper公式のPython自動化ライブラリ
- **プロトコル:** NETCONF（ポート830）、SCP（ファイル転送）
- **パッケージ管理:** pyproject.toml（pip installable）
- **テスト:** pytest + モック
- **CI:** GitHub Actions（Python 3.12/3.13 マトリクス、ビルド検証付き）
- **ライセンス:** Apache License 2.0

## ファイル構成

```
junos_ops/
├── __init__.py     # パッケージ定義、__version__
├── __main__.py     # python -m junos_ops 対応
├── cli.py          # サブコマンドルーティング、argparse、main()
├── common.py       # 共通機能（設定読込、接続管理、ターゲット決定、並列実行）
├── upgrade.py      # upgrade系機能（コピー、インストール、ロールバック、バージョン管理）
└── rsi.py          # RSI/SCF収集機能
tests/
├── conftest.py     # pytest フィクスチャ
├── test_config.py  # 設定読込・モデル取得・ハッシュキャッシュのテスト
├── test_connect.py # 接続モックテスト
├── test_version.py # バージョン関連関数のテスト
├── test_process_host.py # process_host統合テスト（後方互換）
├── test_parallel.py    # 並列実行・ターゲット決定のテスト
├── test_reboot.py      # reboot・config変更検出・snapshot削除のテスト
├── test_config_push.py # config サブコマンド（load_config）のテスト
├── test_show.py        # show サブコマンドのテスト
├── test_rsi.py     # RSI/SCF収集のテスト
└── test_cli_parse.py   # CLI引数パース・サブコマンドなし実行のテスト
pyproject.toml      # パッケージメタデータ、エントリポイント
config.ini          # 設定ファイル（設定例）
logging.ini         # ロギング設定
README.md           # 英語版
README.ja.md        # 日本語版
LICENSE
```

## モジュール構成

### common.py — 共通機能
- グローバル変数: `config`, `config_lock`, `args`
- `get_default_config()` — 設定ファイルパスの探索（XDG対応）
- `read_config()` — INIファイル読込
- `connect()` — NETCONF接続（huge_tree対応、個別例外処理）
- `_get_host_tags()` — ホストセクションのタグを set で返す
- `_filter_by_tags()` — タグの AND フィルタでセクションを絞り込む
- `get_targets()` — ターゲットホストリスト決定（`--tags` 対応）
- `run_parallel()` — ThreadPoolExecutorラッパー（max_workers=1でシリアル実行）

### upgrade.py — パッケージ操作
- `delete_snapshots()` — EX/QFXシリーズのスナップショット全削除（ディスク容量確保）
- `copy()` — SCP転送＋チェックサム検証（storage cleanup + snapshot delete 付き）
- `install()` — パッケージインストール（pre/postフライトチェック）
- `rollback()` — 前バージョンへの復帰
- `reboot()` — スケジュールリブート（config変更検出時は自動再インストール）
- `show_version()` — バージョン情報表示（config変更警告付き）
- `get_model_file()` / `get_model_hash()` — モデル→パッケージマッピング
- `get_pending_version()` / `get_planning_version()` / `compare_version()` — バージョン比較
- `get_commit_information()` — 最新コミット情報取得（epoch秒、ユーザー、クライアント）
- `get_rescue_config_time()` — rescue config ファイルの更新時刻取得
- `check_and_reinstall()` — config変更検出＋validation付き自動再インストール
- `get_hashcache()` / `set_hashcache()` — チェックサムキャッシュ（スレッド安全）
- `load_config()` — set コマンドファイルのロード＋コミット（lock→load→diff→commit_check→commit confirmed→health check→confirm→unlock）、`--workers` で並列実行対応
- `list_remote_path()` — リモートファイル一覧

### rsi.py — RSI/SCF収集
- RSI = request support information
- SCF = show configuration | display set
- `get_support_information()` — 機種別タイムアウト設定でRSI取得
- `cmd_rsi()` — 1ホストのSCF+RSI収集→ファイル出力（DISPLAY_STYLEで出力形式変更可能）

### cli.py — サブコマンドルーティング
- `main()` — argparse サブコマンド定義、ディスパッチ
- `cmd_upgrade()`, `cmd_copy()`, `cmd_install()`, `cmd_rollback()`, `cmd_version()`, `cmd_reboot()`, `cmd_ls()`, `cmd_show()`, `cmd_config()`, `cmd_facts()` — サブコマンド用エントリ関数
- `process_host()` — 旧CLI互換の統合処理関数

## CLI設計

```
junos-ops upgrade [hostname ...]           # コピー＋インストール
junos-ops copy [hostname ...]              # コピーだけ
junos-ops install [hostname ...]           # インストールだけ
junos-ops rollback [hostname ...]          # ロールバック
junos-ops version [hostname ...]           # バージョン表示
junos-ops reboot --at YYMMDDHHMM [hostname ...]  # リブート
junos-ops ls [-l] [hostname ...]           # リモートファイル一覧
junos-ops show COMMAND [hostname ...]           # 任意の CLI コマンドを実行
junos-ops config -f FILE [--confirm N] [hostname ...]  # set コマンドファイル適用
junos-ops rsi [hostname ...]               # RSI/SCF収集
junos-ops [hostname ...]                   # サブコマンド省略 → device facts 表示
junos-ops --version                        # プログラムバージョン
```

共通オプション: `-c`, `-n`, `-d`, `--force`, `--workers N`, `--tags TAG,...`

## 開発環境セットアップ

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

## 設定ファイル（config.ini）の構造

INI形式の設定ファイル。configparserで読み込む。
探索順: `--config`指定 → `./config.ini` → `~/.config/junos-ops/config.ini`

### DEFAULTセクション

```ini
[DEFAULT]
id = exadmin          # SSHユーザ名
pw = password         # SSHパスワード
sshkey = id_ed25519   # SSH秘密鍵ファイル
port = 830            # NETCONFポート
hashalgo = md5        # チェックサムアルゴリズム
rpath = /var/tmp      # リモートパス
# huge_tree = true    # 大きなXMLレスポンスを許可
# RSI_DIR = ./rsi/    # RSI/SCFファイル出力先
# DISPLAY_STYLE = display set   # SCF出力形式（デフォルト: display set）
# DISPLAY_STYLE =               # 空にすると show configuration のみ（stanza形式）
```

### モデル→パッケージマッピング

```ini
EX2300-24T.file = junos-arm-32-18.4R3-S10.tgz
EX2300-24T.hash = e233b31a0b9233bc4c56e89954839a8a
```

### ホストセクション

```ini
[rt1.example.jp]           # hostキー省略 → rt1.example.jpに接続
tags = tokyo, core         # --tags でフィルタ可能（AND マッチ）
[rt2.example.jp]
host = 192.0.2.1           # IPアドレスでオーバーライド
```

## テスト

```bash
pytest tests/ -v --tb=short
```

147テスト（バージョン比較、設定読込、接続モック、process_host統合テスト、reboot・config変更検出・snapshot削除、config push・ヘルスチェック、show コマンド、RSI収集モック・DISPLAY_STYLE、並列実行・タグフィルタリング、スレッド安全性、CLI引数パース）。

### ビルド検証

```bash
pip install build && python -m build
```

CI で sdist / wheel のビルドを検証。pyproject.toml の記述ミス（PEP 639 ライセンス競合等）を早期検出する。

## リリース手順

1. `junos_ops/__init__.py` の `__version__` を更新
2. `CHANGELOG.md` にバージョンエントリを追加
3. コミット & push
4. タグを作成して push → GitHub Actions が TestPyPI → PyPI → GitHub Release を自動実行

```bash
git tag v0.X.Y
git push origin v0.X.Y
```

## 既知の注意事項

- `args`と`config`は`common`モジュールのグローバル変数として管理される
- `config`への書き込みは`config_lock`（threading.Lock）で保護済み
- `cli.py`の後方互換alias（`copy = upgrade.copy` 等）は将来のバージョンで削除予定
