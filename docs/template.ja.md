# Jinja2 テンプレートサポート

[English](template.md) | [config サブコマンド](config.ja.md)

`config` サブコマンドで Jinja2 テンプレート（`.j2` ファイル）を使い、ホストごとに異なる set コマンドを1つのテンプレートから生成できます。

## インストール

Jinja2 はオプショナル依存:

```bash
pip install junos-ops[template]
```

> **注:** Jinja2 は junos-eznc（PyEZ）の依存に含まれるため、明示的なインストールなしで利用できる場合があります。

## 使い方

### 1. config.ini に変数を定義

共通変数は `[DEFAULT]` に、ホスト固有値は各セクションでオーバーライドします。

```ini
[DEFAULT]
var_ntp_server = 192.0.2.1
var_syslog_host = 192.0.2.2

[rt1.example.jp]
tags = tokyo, core

[sw1.example.jp]
tags = tokyo, access
```

### 2. テンプレートファイルを作成

```jinja2
{# ntp.set.j2 — NTP・syslog 設定 #}
set system host-name {{ hostname }}
set system ntp server {{ ntp_server }}
set system syslog host {{ syslog_host }} any warning
{% if facts.personality == 'SWITCH' %}
set protocols vstp
{% endif %}
```

### 3. dry-run で確認

```bash
junos-ops config -f ntp.set.j2 --dry-run rt1.example.jp sw1.example.jp
```

`rt1.example.jp`（ルーター）のレンダリング結果:
```
set system host-name rt1.example.jp
set system ntp server 192.0.2.1
set system syslog host 192.0.2.2 any warning
```

`sw1.example.jp`（スイッチ）のレンダリング結果（`set protocols vstp` が追加）:
```
set system host-name sw1.example.jp
set system ntp server 192.0.2.1
set system syslog host 192.0.2.2 any warning
set protocols vstp
```

### 4. 適用

```bash
junos-ops config -f ntp.set.j2 rt1.example.jp sw1.example.jp
```

## テンプレート変数

テンプレート内で使える変数は3種類:

| ソース | 説明 | 例 |
|--------|------|-----|
| `hostname` | config.ini のセクション名 | `{{ hostname }}` → `rt1.example.jp` |
| `var_*` キー | config.ini の `var_` プレフィックス付きキー（プレフィックス除去） | `var_ntp_server` → `{{ ntp_server }}` |
| `facts` | NETCONF 接続後に取得されるデバイスファクト（dict） | `{{ facts.model }}` → `MX240` |

### 変数の優先順位

`var_` キーは configparser の標準的な継承に従います:

1. ホストセクションの値（優先）
2. `[DEFAULT]` セクションの値（フォールバック）

```ini
[DEFAULT]
var_ntp_server = 192.0.2.1        # 全ホスト共通

[rt1.example.jp]
var_ntp_server = 192.0.2.99       # rt1 だけ別の値
```

### デバイスファクト

`facts` は NETCONF 接続後に取得されるデバイス情報の dict です。主なキー:

| キー | 例 | 説明 |
|------|-----|------|
| `facts.hostname` | `rt1` | デバイスのホスト名 |
| `facts.model` | `MX240` | デバイスモデル |
| `facts.version` | `21.4R3-S5.4` | JUNOS バージョン |
| `facts.personality` | `MX`, `SWITCH`, `SRX_BRANCH` | デバイスタイプ |
| `facts.serialnumber` | `ABC1234` | シリアル番号 |

ドット記法（`facts.model`）とブラケット記法（`facts['model']`）の両方が使えます。

## テンプレートパターン

### デバイスロールによる条件分岐

```jinja2
set system ntp server {{ ntp_server }}
{% if facts.personality == 'SWITCH' %}
set protocols vstp
set ethernet-switching-options storm-control interface all
{% elif facts.personality == 'SRX_BRANCH' %}
set security zones security-zone trust
{% endif %}
```

### ループ: 複数の NTP サーバー

config.ini にカンマ区切りリストを定義:

```ini
[DEFAULT]
var_ntp_servers = 192.0.2.1,192.0.2.2,192.0.2.3
```

`{% for %}` で反復:

```jinja2
{% for server in ntp_servers.split(',') %}
set system ntp server {{ server }}
{% endfor %}
```

レンダリング結果:
```
set system ntp server 192.0.2.1
set system ntp server 192.0.2.2
set system ntp server 192.0.2.3
```

### ループ: SNMP コミュニティと複数プレフィックス

```ini
[DEFAULT]
var_snmp_community = public
var_snmp_prefixes = 192.0.2.0/24,198.51.100.0/24,203.0.113.0/24
```

```jinja2
{% for prefix in snmp_prefixes.split(',') %}
set snmp community {{ snmp_community }} clients {{ prefix }}
{% endfor %}
```

### コメント

Jinja2 コメント（`{# ... #}`）とシェル形式コメント（`# ...`）の両方に対応。どちらも最終出力から除去されます。

```jinja2
{# この Jinja2 コメントはレンダリング時に除去 #}
# このシェル形式コメントはレンダリング後に除去
set system ntp server {{ ntp_server }}
```

## エラー処理

### 未定義変数

テンプレートは Jinja2 の `StrictUndefined` モードで実行されます。未定義変数は即座にエラーとなり、中途半端な設定がデバイスに適用されることはありません。

### テンプレート構文エラー

Jinja2 の構文エラー（`{% endif %}` の閉じ忘れ等）は、デバイスへの設定送信前に検出されます。

### Jinja2 未インストール

Jinja2 が利用できない場合、インストール方法を含む `ImportError` が発生します:

```
ImportError: Jinja2 is required for template support. Install it with: pip install junos-ops[template]
```
