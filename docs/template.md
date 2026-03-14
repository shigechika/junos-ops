# Jinja2 Template Support

[日本語版 / Japanese](template.ja.md) | [Config Subcommand](config.md)

The `config` subcommand supports Jinja2 templates (`.j2` files) to generate per-host set commands from a single template. This is useful when deploying configurations that vary by host — such as hostnames, NTP servers, or protocol settings based on device role.

## Installation

Jinja2 is an optional dependency:

```bash
pip install junos-ops[template]
```

> **Note:** Jinja2 is already included as a dependency of junos-eznc (PyEZ), so it may already be available without explicit installation.

## Quick Start

### 1. Define variables in config.ini

Common variables go in `[DEFAULT]` and are inherited by all hosts. Host-specific values can override them.

```ini
[DEFAULT]
var_ntp_server = 192.0.2.1
var_syslog_host = 192.0.2.2

[rt1.example.jp]
tags = tokyo, core

[sw1.example.jp]
tags = tokyo, access
```

### 2. Create a template file

```jinja2
{# ntp.set.j2 — NTP and syslog configuration #}
set system host-name {{ hostname }}
set system ntp server {{ ntp_server }}
set system syslog host {{ syslog_host }} any warning
{% if facts.personality == 'SWITCH' %}
set protocols vstp
{% endif %}
```

### 3. Preview with dry-run

```bash
junos-ops config -f ntp.set.j2 --dry-run rt1.example.jp sw1.example.jp
```

For `rt1.example.jp` (router), the rendered output:
```
set system host-name rt1.example.jp
set system ntp server 192.0.2.1
set system syslog host 192.0.2.2 any warning
```

For `sw1.example.jp` (switch), the rendered output includes `set protocols vstp`:
```
set system host-name sw1.example.jp
set system ntp server 192.0.2.1
set system syslog host 192.0.2.2 any warning
set protocols vstp
```

### 4. Apply

```bash
junos-ops config -f ntp.set.j2 rt1.example.jp sw1.example.jp
```

## Template Variables

Three sources of variables are available in templates:

| Source | Description | Example |
|--------|-------------|---------|
| `hostname` | Config section name | `{{ hostname }}` → `rt1.example.jp` |
| `var_*` keys | config.ini keys with `var_` prefix (prefix stripped) | `var_ntp_server` → `{{ ntp_server }}` |
| `facts` | Device facts (dict) retrieved after NETCONF connection | `{{ facts.model }}` → `MX240` |

### Variable precedence

`var_` keys follow configparser's standard inheritance:

1. Host section value (highest priority)
2. `[DEFAULT]` section value (fallback)

```ini
[DEFAULT]
var_ntp_server = 192.0.2.1        # used by all hosts

[rt1.example.jp]
var_ntp_server = 192.0.2.99      # overrides DEFAULT for rt1 only
```

### Device facts

The `facts` variable is a dict containing device information retrieved via NETCONF after connection. Common keys:

| Key | Example | Description |
|-----|---------|-------------|
| `facts.hostname` | `rt1` | Device hostname |
| `facts.model` | `MX240` | Device model |
| `facts.version` | `21.4R3-S5.4` | JUNOS version |
| `facts.personality` | `MX`, `SWITCH`, `SRX_BRANCH` | Device type |
| `facts.serialnumber` | `ABC1234` | Serial number |

Both dot notation (`facts.model`) and bracket notation (`facts['model']`) work.

## Template Patterns

### Conditional configuration by device role

```jinja2
set system ntp server {{ ntp_server }}
{% if facts.personality == 'SWITCH' %}
set protocols vstp
set ethernet-switching-options storm-control interface all
{% elif facts.personality == 'SRX_BRANCH' %}
set security zones security-zone trust
{% endif %}
```

### Loop: multiple NTP servers

Define a comma-separated list in config.ini:

```ini
[DEFAULT]
var_ntp_servers = 192.0.2.1,192.0.2.2,192.0.2.3
```

Use `{% for %}` to iterate:

```jinja2
{% for server in ntp_servers.split(',') %}
set system ntp server {{ server }}
{% endfor %}
```

Rendered output:
```
set system ntp server 192.0.2.1
set system ntp server 192.0.2.2
set system ntp server 192.0.2.3
```

### Loop: SNMP community with multiple prefixes

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

### Comments

Jinja2 comments (`{# ... #}`) and shell-style comments (`# ...`) are both supported. Both are removed from the final output.

```jinja2
{# This Jinja2 comment is removed during rendering #}
# This shell-style comment is removed after rendering
set system ntp server {{ ntp_server }}
```

## Error Handling

### Undefined variables

Templates use Jinja2's `StrictUndefined` mode. Any undefined variable causes an immediate error with a clear message — the template is never partially applied.

```jinja2
set system ntp server {{ undefined_var }}
```
```
jinja2.exceptions.UndefinedError: 'undefined_var' is not defined
```

### Template syntax errors

Jinja2 syntax errors (e.g., missing `{% endif %}`) are caught before any configuration is sent to the device.

### Jinja2 not installed

If Jinja2 is not available, an `ImportError` with installation instructions is raised:

```
ImportError: Jinja2 is required for template support. Install it with: pip install junos-ops[template]
```
