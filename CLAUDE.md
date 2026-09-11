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
├── show.py         # show サブコマンド core（run_cli / run_cli_batch、text|json|xml）
├── snapshot.py     # snapshot サブコマンド core（request system snapshot、代替メディア判定）
├── rsi.py          # RSI/SCF収集機能
├── vc.py           # Virtual Chassis（status/replication 取得、vc-switch core、reboot --member の検証）
└── display.py      # 表示層（core が返す dict を人間向け整形 / JSON シリアライズ）
tests/
├── conftest.py     # pytest フィクスチャ
├── test_config.py  # 設定読込・モデル取得・ハッシュキャッシュのテスト
├── test_connect.py # 接続モックテスト
├── test_version.py # バージョン関連関数のテスト
├── test_parallel.py    # 並列実行・ターゲット決定のテスト
├── test_reboot.py      # reboot・config変更検出・snapshot削除のテスト
├── test_config_push.py # config サブコマンド（load_config）のテスト
├── test_show.py        # show サブコマンドのテスト
├── test_snapshot.py    # snapshot サブコマンド（create_snapshot・代替メディア判定）のテスト
├── test_rsi.py     # RSI/SCF収集のテスト
├── test_check.py       # check サブコマンド（local/remote/connect inventory）のテスト
├── test_display.py     # display 層（format_*/print_*/JSON）のテスト
├── test_install.py     # install フローのテスト
├── test_copy.py        # copy フロー（storage cleanup ゲート・remote check・SCP 失敗系）のテスト
├── test_rollback.py    # rollback core（成功マーカー・例外）と cmd_rollback（終了コード・JSON）のテスト
├── test_unlink.py      # --unlink 経路（CLI 直接実行）のテスト
├── test_json_output.py # --json JSONL 出力のテスト
├── test_list_remote.py # ls サブコマンドのテスト
├── test_package_checks.py # ローカル/リモート firmware checksum 検証のテスト
├── test_cli_parse.py   # CLI引数パース・サブコマンドなし実行・reboot --member/--now ガードのテスト
├── test_vc.py          # vc（status/replication パーサ・master_switch の拒否/1回発行/セッション切断・wait_for_master・cmd_vc_switch）のテスト
└── test_logging.py     # _setup_logging（console / --log-file / -d / logging.ini / 冪等性）と python -m junos_ops のテスト
pyproject.toml      # パッケージメタデータ、エントリポイント
config.ini          # 設定ファイル（設定例）
logging.ini.example # ロギング設定の雛形（完全カスタム用。既定はコードで構成）
README.md           # 英語版
README.ja.md        # 日本語版
LICENSE
```

## モジュール構成

### common.py — 共通機能
- グローバル変数: `config`, `config_lock`, `args`
- `get_default_config()` — 設定ファイルパスの探索（XDG対応）
- `read_config()` — INIファイル読込
- `connect()` — NETCONF接続（huge_tree対応、個別例外処理。`auto_probe>0` は `Device(auto_probe=)` に転送 — `check --connect` の 5 秒プローブと `vc.wait_for_master` が使う）
- `_get_host_tags()` — ホストセクションのタグを set で返す
- `_get_model_tags()` — `<model>.tags = ...`（DEFAULT セクション）を set で返す。未設定なら空 set。`check --local` の model フィルタ専用で、ホストの tags 空間とは独立
- `_parse_tag_groups()` — `--tags` CLI 値（list / str / None）を set のリストに正規化
- `_filter_by_tag_groups()` — ホストタグでホストを絞り込み（グループ内 AND、グループ間 OR）
- `_filter_models_by_tag_groups()` — model 名そのもの OR `<model>.tags` の OR で model を絞り込み（グループ内 AND、グループ間 OR）。`--tags ex2300-24t` は model 名一致で動き、`--tags main` は `<model>.tags` 経由で動く
- `get_targets()` — ターゲットホストリスト決定（`--tags` は `action="append"`。ホスト名併記時はタグフィルタ ∩ 名前リスト。`--exclude-tags` は `--tags` 同文法・`action="append"` で最終段の除外フィルタとして適用、単独利用可、全除外時は `_fatal` で sys.exit）
- `run_parallel()` — ThreadPoolExecutorラッパー（max_workers=1でシリアル実行）

### upgrade.py — パッケージ操作（すべて dict を返す）
- `delete_snapshots()` — EX/QFXシリーズのスナップショット全削除（dict: applied/ok/dry_run/message/error）
- `copy()` — SCP転送＋チェックサム検証（dict: storage_cleanup/snapshot_delete/steps/error）
- `install()` — パッケージインストール（dict: copy_result/rollback_result/rescue_save/steps など nested）
- `rollback()` — 前バージョンへの復帰（dict: ok/rpc_output/message/error）
- `reboot(hostname, dev, reboot_dt, *, member=None)` — スケジュールリブート（dict: code/reinstall_result/steps/member/vc_status。code は 0..9。7 は `--force` 無しで `get-reboot-information` の XML 解析に失敗したケース（issue #60）、8 は `--member` の VC 検証失敗（status 取得不能／member 不在／member が現 Master で `--force` 無し）、9 は pending パッケージ（または pending 判定不能 `pending_unknown` — `get_pending_version(strict=True)` で fail-closed）で `--allow-mixed-version` 無し、1 は API 誤用 `reboot_dt=None` かつ `member=None`）。member 指定時は `get-reboot-information` のテキストを `_member_section()` で `fpcN:` ブロックに絞って既存スケジュールを判定し、`clear_reboot(dev, member=N)` が `clear system reboot member N` を発行する。`member` 指定時は PyEZ `SW.reboot(member_id=)` を**使わず** `_reboot_member()` が `dev.rpc.request_reboot(member=N, in=0|at=…)` を直接発行する — SW 版は `all_re=True`（既定）だと `<member>` を付けず、facts 由来の member 一覧に無いと無言で None を返すため。`reboot_dt=None` は「今」（CLI は `--member` 併用時のみ許可）。pending チェックは `check_and_reinstall` の**前**（VC 全体の reinstall を走らせないため）。pending が running（`dev.facts["version"]`、**文字列完全一致** — `compare_version` は `-S`→`00` 正規化で別文字列が等しくなるので使わない）と同じ場合は `_install_log_staged_before_boot()` で `show log install` の最終ヘッダ行（`YYYY-MM-DD HH:MM:SS TZ mgd[...]: ... package -X update`）と `show system uptime` の `System booted`（VC は `fpcN` ブロック）を device local の naive datetime で比較し、**staging が boot より前なら「pending 無し」**、後なら同一バージョン再インストール＝pending、判定不能は fail-closed。host-based QFX は Staging 行が再起動後も残り稼働中バージョンを pending と誤認する（QFX5110 23.4R2-S8.7 で実測、2026-09-11）
- `show_version()` — バージョン情報収集（dict: running/planning/pending/commit/config_changed_after_install 他）
- `get_model_file()` / `get_model_hash()` — モデル→パッケージマッピング
- `get_pending_version()` / `get_planning_version()` / `compare_version()` — バージョン比較
- `get_commit_information()` — 最新コミット情報取得（epoch秒、ユーザー、クライアント）
- `get_rescue_config_time()` — rescue config ファイルの更新時刻取得
- `check_and_reinstall()` — config変更検出＋validation付き自動再インストール（dict）
- `check_running_package()` — running とパッケージ名を突き合わせ（dict: running/expected_file/match）
- `check_local_package(hostname, dev)` / `check_local_package_by_model(hostname, model)` — ローカル firmware checksum 検証（後者は hashlib 直接、NETCONF 不要）
- `check_remote_package(hostname, dev)` / `check_remote_package_by_model(hostname, dev, model)` — リモート firmware checksum 検証（by_model 版はモデルを明示指定）
- `_compute_local_checksum(path, algo)` — hashlib ベースの純粋関数（PyEZ SW 非依存）
- `get_hashcache()` / `set_hashcache()` / `clear_hashcache()` — チェックサムキャッシュ（スレッド安全）。`clear_hashcache` は `storage_cleanup` 後に invoke して cache が stale 化するのを防ぐ
- `load_config()` — set/Jinja2(`.j2`) 設定ファイルのロード＋コミット（dict: steps/template/rendered_commands/diff/commit_mode 等。各ステップを `logger.debug` で実時間エコー、関数自身は print しない）。フローは lock → load → diff → commit_check → **commit confirmed** → ヘルスチェック → confirm → unlock。**ヘルスチェック失敗時は commit を confirm せず**、JUNOS の commit-confirmed タイマー満了で**自動ロールバック**させる（リモート機を締め出さない生命線。この順序は load-bearing）。`.j2` の場合は `common.render_template()` が config.ini ホストセクションの `var_` 変数＋`facts`/`hostname` を StrictUndefined でレンダリング（optional extra `junos-ops[template]` が必要）
- `list_remote_path()` — リモートファイル一覧（dict: files/file_count/format）
- `dry_run()` — local/remote package の検証（dict）
- すべての core 関数は stdout に print しない。人間向け整形は `display` 層が担う。

### vc.py — Virtual Chassis ヘルパ（すべて dict を返す、print しない）
- `get_vc_status(dev)` — `get-virtual-chassis-information` を JSON-native な dict に（members[{id, role, status, priority, model}], master, backup, mode）。`member-role` の末尾 `*`（`Master*`）は剥がす。RPC 失敗・`member-list` 欠落は `ok=False`（例外は握って `error`/`error_message` に載せる）。呼び側は **fail-closed**（`ok=False` を「VC ではない」と解釈しない）
- `find_member(status, member_id)` — id で member エントリを引く（int/str どちらでも）
- `get_replication_state(dev)` — `get-routing-task-replication-state`（`show task replication`、PyEZ `SW._check_gres` と同じ RPC）→ gres/re_mode/protocols{name: state}/complete。`complete` は GRES Enabled ∧ RE Master ∧ protocols 非空 ∧ 全 Complete（空は **fail-closed**）
- `master_switch(hostname, dev)` — mastership 切替。`SWITCH_COMMANDS = (SWITCH_COMMAND, CHASSIS_SWITCH_COMMAND)` を順に `dev.cli(..., warning=False)` で発行（各形式は **最大 1 回**。RPC 名を推測しない。`<command>` 経路は非対話なので `[yes,no]` は出ない）。EX VC 形式 `request virtual-chassis routing-engine master switch` を先に試し、**`RpcError` が送出され**その `.message`（`str(e)` ではない — PyEZ が `RpcError(severity: …, message: …)` で包むため）が `_NOT_VALID_RE`（`command is not valid` / `unknown command` / `syntax error`）に当たったときだけ QFX 用 `request chassis routing-engine master switch no-confirm` にフォールバックする（QFX5110 VC で実測、issue #159）。mgd が dispatch 前に弾いた＝実行されていない証明なので `issued` を False に戻す。**テキストが返ってきた時点で CLI はコマンドを処理しているので、その内容が何であれ 2 つ目は送らない**（成功バナーと parse 診断が混在した応答で mastership を 2 回切り替える事故を防ぐ）。`Not ready …` や `[yes,no]` も「この切替に対する拒否」なので再試行しない。`_precheck_problems()` が拒否理由を列挙（Master/Backup がちょうど 1 つ・全 member Prsnt・replication complete・RPC 失敗は拒否）。`--force` は拒否理由を `warnings` に変えて続行。`issued=True` は `dev.cli()` の**前**に立てる。`RpcTimeoutError`/`ConnectClosedError`/`TimeoutExpiredError`/`OSError` は「切替に伴うセッション切断」（`session_dropped`）で `ok=True`／`status=initiated_unverified`。**`RpcTimeoutError` は `RpcError` のサブクラス**なので except の順序はセッション切断系が先。応答テキストの行頭 `error:`/`syntax error`/`unknown command`/`permission denied`、行中の `not ready|allowed|possible|supported`（chassisd の "Not ready for mastership switch"）、`[yes,no]`（確認プロンプトのエコー＝未実行）は `command_rejected`。それ以外の予期しない例外は `warnings` に載せて `initiated_unverified` のまま返す（result を捨てず `--wait` 検証へ進める）。`status` は `dry_run`/`refused`/`rejected`/`initiated_unverified`（cli が `confirmed`/`verification_failed` に更新）。`hostname` キーは持たない（display が注入）
- `wait_for_master(hostname, expected, timeout, interval=10)` — `common.connect(gather_facts=False)` で再接続を繰り返し `get_vc_status().master == expected` を待つ。接続失敗・RPC 失敗は「まだ」。各 probe は `auto_probe=min(interval, 残り秒)` で上限を付ける（`common.connect` の `auto_probe` は #156 まで `Device` に渡されておらず no-op だった）。`elapsed` は成功・失敗とも実測。`time.sleep`/`time.monotonic` はモジュール属性経由（テストで patch）。成功時に `get_replication_state` を `replication` として返す（ゲートしない）
- 実機（QFX5110 2 member）で確認した XML: `member-status`=`Prsnt`、`member-role`=`Master*`/`Backup`/`Linecard`、`virtual-chassis-mode`=`Enabled`

### display.py — 表示層
- `print_version()`, `print_copy()`, `print_install()`, `print_rollback()`, `print_reboot()`, `print_reinstall()`, `print_load_config()`, `print_list_remote()`, `print_dry_run()`, `print_rsi()`, `print_show()`, `print_snapshot()`, `print_vc_switch()`, `print_connect_error()`, `print_read_config_error()`, `print_host_header()`, `print_host_footer()` — core が返す dict を人間向けに整形（`format_snapshot()` 等の `format_*` が整形ロジック本体）
- `format_json(hostname, result)` / `print_json(hostname, result)` — core dict を `{"hostname": ..., **result}` の 1 行 JSON にシリアライズ（`--json` 用）。`format_json_obj(obj)` / `print_json_obj(obj)` は hostname を注入せず obj をそのまま出す（`check` の model 単位 row 用）。`json.dumps(..., default=str, ensure_ascii=False)` で lxml/datetime 等の非シリアライズ値も str fallback、非 ASCII はそのまま
- `_print_lock` (`threading.Lock`) でマルチワーカー時の出力インターリーブを防止
- junos-mcp など非 CLI 利用者は display を import しなければ stdout 出力ゼロ

### show.py — show サブコマンド core（すべて dict を返す）
- `run_cli(dev, command, *, output_format="text", retry=0, hostname="")` — 単一コマンド実行。`format=text`→`dev.cli(cmd)`（string）、`json`→`dev.cli(cmd, format="json")`（dict）、`xml`→`dev.cli(cmd, format="xml")` の lxml を pretty-print 文字列化
- `run_cli_batch(dev, commands, ...)` — `-f FILE` 用。最初の失敗で短絡
- `_cli_with_retry()` — `RpcTimeoutError` 時のバックオフ付きリトライ（5秒, 10秒, 15秒, ...）
- `VALID_FORMATS` — `("text", "json", "xml")`
- Caveat: `dev.cli()` は NETCONF RPC 経由でコマンドを送るため、`format` に関わらず（`text` を含め）デバイス側で `| match` / `| last` / `| count` などのパイプが無視される。フィルタしたいときはクライアント側で加工

### snapshot.py — snapshot サブコマンド core（すべて dict を返す）
- 用途: `request system snapshot` で稼働中システム（root ＋ config）を**代替（バックアップ）ブートメディア**へ同期。アップグレードは稼働中メディアしか書き換えず代替面が「化石化」するため、フォールバック時の安全性を担保する。MX 中心
- `create_snapshot(hostname, dev)` — core（dict: hostname/ok/dry_run/message/error/steps、no-op 時は `skipped` も）。`dev.facts["personality"]` で機種分類し、`_SNAPSHOT_RPC_ARGS` に無い personality（vmhost MX / mid-high SRX / HA / vMX/vSRX / Evolved / 不明）は **非致命的スキップ**。EX/QFX の out-of-space も `_is_out_of_space()` 判定で clean skip
- `running_on_alternate_media(dev)` — 代替メディア稼働判定（`True`/`False`/`None`=判定不能）。ベストエフォート
- **安全ガード:** 代替メディア稼働中（`on_alt is True`）かつ `--force` 無しなら `error="running_on_alternate_media"` で拒否（古いシステムをプライマリへ複製するのを防ぐ）。`None`（判定不能）は警告付きで続行
- `_SNAPSHOT_RPC_ARGS` / `_SNAPSHOT_LABEL` — personality → RPC 引数・表示ラベルのマッピング（Branch SRX は `slice alternate`）

### rsi.py — RSI/SCF収集
- RSI = request support information
- SCF = show configuration | display set
- `get_support_information()` — 機種別タイムアウト設定でRSI取得（dict: ok/rpc/timeout/node/error）
- `collect_rsi()` — core（dict: scf/rsi ファイルパスとバイト数、error）
- `resolve_rsi_dir(hostname, rsi_dir=None)` — 出力先の解決順: 引数（CLI の `--rsi-dir`）> `[host] RSI_DIR` > `./`（`~` 展開）
- `collect_rsi(hostname, dev, rsi_dir=None)` — core。出力先は `resolve_rsi_dir()`、書き込み前に `os.makedirs(exist_ok=True)`（失敗は `error="rsi_dir"` で早期 return）
- `cmd_rsi()` — CLI エントリ（`common.args.rsi_dir` を渡して collect_rsi を呼び、display.print_rsi で出力）

### cli.py — サブコマンドルーティング
- `main()` — argparse サブコマンド定義、ディスパッチ
- `cmd_upgrade()`, `cmd_copy()`, `cmd_install()`, `cmd_rollback()`, `cmd_version()`, `cmd_reboot()`, `cmd_snapshot()`, `cmd_ls()`, `cmd_show()`, `cmd_config()`, `cmd_facts()` — サブコマンド用エントリ関数（connect → header → core(dict) → display）
- `_check_host(hostname)` — `check` サブコマンド用ワーカー。int ではなく dict を返し、`main()` で結果を集約して `display.print_check_table` にテーブル出力。モデル解決順: `--model` > `config.ini [host].model` > `dev.facts["model"]`
- `_check_local_inventory()` — `check --local` 用。`iter_configured_models()` の出力に対し `--model`（単一名）＋ `--tags` / `--exclude-tags`（`common._filter_models_by_tag_groups` で model 名 OR `<model>.tags` の OR フィルタ）を**積集合**で適用。`--local` 単独実行時 (`check_connect`/`check_remote` ともに false) は `_run()` 側で `get_targets()` をスキップさせ、ホスト側 `--tags` が "no hosts matched tags" で sys.exit するのを回避。フィルタ後 0 件は `logger.info` で「なぜ空か」をログる
- `cmd_vc_switch(hostname)` — `vc.master_switch` → 接続を閉じる → `--wait > 0` かつ（`initiated_unverified` **または** `rejected` かつ `issued`）なら `vc.wait_for_master(hostname, expected_master, wait)` を結果にマージ（`confirmed`/`verification_failed`、`after`/`after_replication`/`wait`）。**`rejected` でも発行済みなら実機の状態を見る**（応答テキストは成功バナーと診断が混在しうるため断定しない。mastership が動いていれば警告付きで `confirmed` に格上げし「再発行するな」と出す、動いていなければ `rejected` のまま「拒否は本物」と記録）。`issued=False`（事前確認拒否・両形式 not valid）は検証しない。終了コードは `ok` ベース（`confirmed`/`dry_run`/`initiated_unverified`(--wait 0) → 0）。`_run` のガード: ホスト名必須・`--wait >= 0`
- `_open_connection()` — NETCONF 接続＋エラー時の display 出力ヘルパー（`--json` 時は connect エラーを JSON で出す）
- `--json` グローバルオプション: 各 `cmd_*` は `_emit_result(hostname, result, formatter)` で「`--json` なら `display.print_json`、通常は `display.print_host_block(formatter(result))`」を分岐。失敗ホストは `_emit_exception` が `{"ok": false, "error", "error_message"}` の JSON 行を出す（JSONL consumer が行欠落で気づけないのを防ぐ）。出力は host ごと 1 行の JSONL（`run_parallel` で並列のため top-level 配列は作らない。`jq -s` で slurp）
- `_setup_logging(args)` — logging の構成。**import 時には何もしない**（junos-mcp 等が `junos_ops.*` を import しても root logger は無傷）。`_run()` で `read_config()` の**後**に呼ぶ（`[DEFAULT] log_file` を見るため）。`logging.ini`（`./` → `$XDG_CONFIG_HOME/junos-ops/`）があれば `fileConfig(..., disable_existing_loggers=False)`（`False` 必須 — この時点で `junos_ops.upgrade` 等のロガーは生成済みで、既定の `True` だと disabled にされる）。無ければ console StreamHandler（INFO、`--json` なら stderr）＋ opt-in のファイル `TimedRotatingFileHandler`（`--log-file` > `log_file`、midnight・10 世代、親ディレクトリ自動作成、失敗時は warning でコンソールのみ続行）。handler は固定名 `junos-ops-console` / `junos-ops-file` を持ち、再入時は自分の handler だけ差し替える（`root.handlers.clear()` はしない — pytest の caplog やテストが root に足した handler を壊す）。`-d` は両ブランチで root を DEBUG に上げるが、`ncclient`/`paramiko`/`jnpr.junos` は NOTSET なら WARNING に固定（firehose 防止）
- `_route_logs_to_stderr()` — `--json` 時に root logger の stdout 向け StreamHandler を stderr へ移す。`_setup_logging` 自身の console handler は最初から stderr を選ぶので、これはユーザーの `logging.ini` が stdout handler を宣言したケースの保険。`_run()` で `_setup_logging` 直後に呼ぶ

## CLI設計

```
junos-ops upgrade [hostname ...]           # コピー＋インストール
junos-ops copy [hostname ...]              # コピーだけ
junos-ops install [hostname ...]           # インストールだけ
junos-ops rollback [hostname ...]          # ロールバック
junos-ops version [hostname ...]           # バージョン表示
junos-ops reboot --at YYMMDDHHMM [hostname ...]  # リブート
junos-ops reboot --member N (--now | --at YYMMDDHHMM) [--allow-mixed-version] hostname ...  # VC member 個別リブート（ホスト名必須）
junos-ops snapshot [--force] [hostname ...] # 代替ブートメディアを同期（request system snapshot、MX中心）
junos-ops vc-switch [--wait SEC] hostname ...  # VC mastership を Backup へ（事前確認 fail-closed・1 回発行・再接続で検証。ホスト名必須）
junos-ops ls [-l] [hostname ...]           # リモートファイル一覧
junos-ops show COMMAND [-F text|json|xml] [hostname ...]   # 任意の CLI コマンドを実行（-F で構造化出力）
junos-ops config -f FILE [--confirm N] [--health-check CMD ...] [--no-health-check] [--no-confirm] [--no-commit] [hostname ...]  # set/.j2 設定ファイル適用（commit confirmed＋ヘルスチェック＋自動ロールバック）
junos-ops check [--connect|--local|--remote|--all] [--model M] [hostname ...]  # pre-flight チェック
junos-ops rsi [--rsi-dir DIR] [hostname ...]  # RSI/SCF収集（--rsi-dir で出力先を上書き）
junos-ops [hostname ...]                   # サブコマンド省略 → device facts 表示
junos-ops --version                        # プログラムバージョン
```

共通オプション: `--config` (`-c`), `--dry-run` (`-n`), `-d`, `--log-file PATH`, `--force`, `--workers N`, `--tags TAG,...`, `--exclude-tags TAG,...`, `--json`（機械可読 JSONL 出力。ログは stderr へ退避）

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
# ssh_config = ~/.ssh/config    # OpenSSH 互換設定（ProxyCommand 等）。未指定時は PyEZ が ~/.ssh/config を自動参照
# lpath = ~/firmware            # ローカルのファームウェア置き場（~ 展開対応、デフォルト: カレントディレクトリ）
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

カバレッジ: バージョン比較、設定読込、接続モック、各サブコマンド（reboot・config・show・rsi）、並列実行・タグフィルタリング、スレッド安全性、CLI引数パース。

### ビルド検証

```bash
pip install build && python -m build
```

CI で sdist / wheel のビルドを検証。pyproject.toml の記述ミス（PEP 639 ライセンス競合等）を早期検出する。

## リリース手順

リリースは [release-please](https://github.com/googleapis/release-please) で自動化されている。手動で `__version__` やタグを触る必要はない。

1. Conventional Commits（`feat:` / `fix:` / `refactor:` / `perf:` など）で main にマージする
2. release-please が自動で "chore(release): ..." の PR を開き、次バージョン候補と CHANGELOG を提示する
3. 内容を確認して PR をマージ → release-please が GitHub Release を **draft** で作成。`release-please-config.json` の top-level `"force-tag-creation": true`（2026-08-11 PR #145 で追加）により、この時点で実タグ `v0.X.Y` も同時に作られる — draft のままでもタグは実在する
4. 同じ `release-please.yml` 内で `deb.yml` / `rpm.yml` が reusable workflow として呼ばれ、.deb / .rpm を draft Release に添付
5. 添付完了後に `publish-release` ジョブが draft を公開し、`release: published` で `release.yml`（TestPyPI → PyPI → homebrew-tap 通知）が発火

draft → 添付 → 公開の順序は **Immutable Releases** 対応のため（公開後は資産の追加・変更・削除が一切できない）。

**force-tag-creation が要る理由**: GitHub は draft リリースに対して公開されるまで実タグを作らない（release-please 公式ドキュメントが "lazy tag creation" と呼ぶ挙動）。これが無いと、deb/rpm 添付が失敗して draft が公開されないたびに release-please が「直前リリース」を見失い、コミット全履歴への再スキャンにフォールバックして、過去の一度きりの `Release-As:` コミットを再度 honor するなどして**既に公開済みのバージョンを再提案 → 同名タグの immutable リリースと衝突 → 失敗 → 再びアンカーを見失う**という無限ループに陥る。2026-07-23〜08-11 に junos-ops で実際に発生し（PR #126/#134/#139/#143/#144 がバージョン番号を行ったり来たり提案し続けた）、`force-tag-creation: true` で解消した。

**deb/rpm 失敗時の復旧**: 同じ run の **「Re-run failed jobs」** で再開する（draft への `--clobber` 再アップロードは冪等）。**「Re-run all jobs」は使わない** — release-please がリリース済みと判定して全ジョブ skip となり、draft が未公開のまま残る。座礁した draft の手動復旧は `gh release upload <tag> <資産>` → PAT（`RELEASE_PLEASE_TOKEN` 相当）で `gh release edit <tag> --draft=false`（`GITHUB_TOKEN` で公開すると release.yml が発火しない）。

**注意（上記「Re-run で冪等」が成り立たないケース）**: `deb.yml`/`rpm.yml` のエラーが `Cannot delete asset from an immutable release` の場合、それは新しく作られた draft のタグ名が**既に公開・immutable 化済みの別リリースと重複している**ことを意味する（`gh release upload` はタグ名で解決するため、新しい draft ではなく古い公開済みリリースにヒットしてしまう）。これは Re-run しても同じエラーで恒久的に失敗する — まず `.release-please-manifest.json` の値と `gh release list` の実際の最新公開バージョンが一致しているかを確認すること。`force-tag-creation` 導入後はこの状態自体が起きないはずなので、再発したら release-please 側の別の不具合を疑う。

`junos_ops/__init__.py` の `__version__` には `# x-release-please-version` マーカーが付いており、release-please が `.release-please-manifest.json` と同期して書き換える。CHANGELOG.md も自動 prepend される。

### 設定ファイル

- `release-please-config.json` — package-name、release-type（python）、extra-files、changelog-sections
- `.release-please-manifest.json` — 現在のバージョン（release-please が更新）
- `.github/workflows/release-please.yml` — push: main で発火。draft Release 作成 → deb/rpm 呼び出し → draft 公開までをオーケストレーション。`RELEASE_PLEASE_TOKEN`（fine-grained PAT）で Release を作成・公開し、下流 workflow を起動
- `.github/workflows/release.yml` — `release: published` をフック、TestPyPI → PyPI 公開と homebrew-tap 通知
- `.github/workflows/deb.yml` / `rpm.yml` — `release-please.yml` から `workflow_call`（tag 入力）で呼ばれ .deb / .rpm をビルドして draft Release に添付。単発ビルドは `workflow_dispatch`（添付なし）。パッケージング設定は `debian/` / `rpm/` ディレクトリ

### 下流連携

PyPI リリース後、`shigechika/homebrew-tap` の `update-formula.yml` が `repository_dispatch` で自動トリガーされ、Formula 更新 → bottle ビルドまで自動で行われる。

## 低容量機種での upgrade (`--unlink`)

EX2300/EX3400 のような low-flash 機種（`/dev/gpt/junos` = 1.3GB）で JUNOS 22.4 → 23.4 のような major アップグレードを行うと、validation 段階で `ERROR: insufficient space` で失敗することがある。これは PyEZ `SW.install()` が `request system software add` の `unlink` オプションをパラメータとして公開しておらず、デフォルト経路では unlink が走らないため。

```bash
# low-flash 機種向け
junos-ops upgrade --unlink ex3400-host.example.jp
```

`--unlink` を指定すると `SW.install()` ではなく `dev.cli("request system software add ... unlink")` を直接実行する。pkgadd が tgz を install 中に unlink してくれるため、容量を確保しながら展開できる。

実装: `junos_ops/upgrade.py:_install_via_cli_with_unlink()` を参照。詳細な経緯は memory の `feedback_lowflash_upgrade.md` に記録。

## 既知の注意事項

- `args`と`config`は`common`モジュールのグローバル変数として管理される
- `config`への書き込みは`config_lock`（threading.Lock）で保護済み
- 新しいサブコマンドは出力を`display.print_host_block(hostname, body)`（または`print_facts`）で**1ブロックとして atomic に出力**すること。`print_host_header`単独 + 別の`print_*`/`print()`に分けると、`--workers N`並列実行時に他ホストの出力がヘッダと本体の間に割り込む。`cmd_facts`/`cmd_rsi`がこの不具合を抱えており v0.23.1 で修正済み
