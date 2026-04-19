"""junos-ops: Automated JUNOS package management tool.

Manages Juniper Networks devices via NETCONF/SSH: automatic model
detection, package upgrade, rollback, reboot scheduling, and
RSI/SCF collection.

Subcommands::

    junos-ops upgrade [hostname ...]   # copy and install
    junos-ops version [hostname ...]   # show versions
    junos-ops reboot --at YYMMDDHHMM   # schedule reboot
    junos-ops rsi [hostname ...]       # collect RSI/SCF
    junos-ops show "show bgp summary"  # run CLI command
    junos-ops config -f FILE           # push set commands
    junos-ops check [--connect|--local|--remote|--all]  # pre-flight checks

See Also:
    https://github.com/shigechika/junos-ops
"""

__version__ = "0.16.10"  # x-release-please-version
