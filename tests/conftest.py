import argparse
import configparser
import pytest

from junos_ops import common
from junos_ops import upgrade as junos_upgrade_mod


@pytest.fixture
def junos_common():
    """common モジュールを返す"""
    return common


@pytest.fixture
def junos_upgrade():
    """junos_ops.upgrade モジュールを返す"""
    return junos_upgrade_mod


@pytest.fixture
def mock_args(junos_common):
    """テスト用の args グローバル変数を設定"""
    junos_common.args = argparse.Namespace(
        debug=False,
        dry_run=False,
        force=False,
        config="config.ini",
        list_format=None,
        rebootat=None,
        configfile=None,
        confirm_timeout=1,
        health_check=["ping count 3 255.255.255.255 rapid"],
        no_health_check=False,
        show_command=None,
        showfile=None,
        tags=None,
        retry=0,
        rpc_timeout=None,
        no_confirm=False,
        specialhosts=[],
    )
    return junos_common.args


@pytest.fixture
def mock_config(junos_common):
    """テスト用の config グローバル変数を設定"""
    cfg = configparser.ConfigParser(allow_no_value=True)
    cfg.read_dict(
        {
            "DEFAULT": {
                "id": "testuser",
                "pw": "testpass",
                "sshkey": "id_ed25519",
                "port": "830",
                "hashalgo": "md5",
                "rpath": "/var/tmp",
                "ex2300-24t.file": "junos-arm-32-22.4R3-S6.5.tgz",
                "ex2300-24t.hash": "abc123def456",
            },
            "test-host": {"host": "192.0.2.1"},
        }
    )
    junos_common.config = cfg
    return cfg
