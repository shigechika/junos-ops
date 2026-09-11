"""Tests for cli._setup_logging: startup-time logging configuration.

Logging used to be configured at import time from a shipped logging.ini
whose file handler wrote ``logger.log`` relative to the CWD. These tests
pin the replacement: console-only by default, opt-in file logging via
``--log-file`` / ``[DEFAULT] log_file``, ``-d`` wired to DEBUG, and a
user logging.ini loaded without disabling the module loggers.
"""

import argparse
import configparser
import logging
import os
import subprocess
import sys

import pytest

from junos_ops import __version__
from junos_ops import cli
from junos_ops import common


def _args(**overrides):
    base = dict(debug=False, json=False, log_file=None)
    base.update(overrides)
    return argparse.Namespace(**base)


def _handlers(name):
    return [h for h in logging.getLogger().handlers if h.name == name]


@pytest.fixture
def isolated_logging():
    """Snapshot and restore root/module logger state around a test.

    _setup_logging mutates the root logger; fileConfig can also touch the
    ``disabled`` flag of existing loggers. Restore everything so test
    order does not matter.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    watched = ["junos_ops.upgrade", "junos_ops.cli", *cli._NOISY_LOGGERS]
    saved = {
        n: (logging.getLogger(n).level, logging.getLogger(n).disabled)
        for n in watched
    }
    saved_config = common.config
    yield
    for h in list(root.handlers):
        if h not in saved_handlers:
            root.removeHandler(h)
            h.close()
    for h in saved_handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(saved_level)
    for n, (lvl, dis) in saved.items():
        logging.getLogger(n).setLevel(lvl)
        logging.getLogger(n).disabled = dis
    common.config = saved_config


@pytest.fixture
def no_logging_ini(monkeypatch, tmp_path):
    """Run from a directory without logging.ini and with an empty XDG home."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


class TestImportSideEffects:
    def test_no_junos_ops_handlers_at_import(self, isolated_logging):
        """Importing cli must not attach handlers; only _setup_logging does."""
        # cli is already imported by this module; any junos-ops handler
        # present now was attached by a previous _setup_logging call in
        # the session, never by import. Strip and re-check via a fresh
        # interpreter to make the claim unconditional.
        code = (
            "import logging, junos_ops.cli; "
            "print([h.name for h in logging.getLogger().handlers])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == "[]"


class TestDefaultConsole:
    def test_console_only_at_info(self, isolated_logging, no_logging_ini):
        common.config = None
        cli._setup_logging(_args())
        console = _handlers(cli._CONSOLE_HANDLER)
        assert len(console) == 1
        assert console[0].stream is sys.stdout
        assert logging.getLogger().level == logging.INFO
        assert _handlers(cli._FILE_HANDLER) == []

    def test_json_routes_console_to_stderr(self, isolated_logging, no_logging_ini):
        common.config = None
        cli._setup_logging(_args(json=True))
        assert _handlers(cli._CONSOLE_HANDLER)[0].stream is sys.stderr

    def test_debug_raises_root_but_keeps_noisy_at_warning(
        self, isolated_logging, no_logging_ini
    ):
        common.config = None
        for n in cli._NOISY_LOGGERS:
            logging.getLogger(n).setLevel(logging.NOTSET)
        cli._setup_logging(_args(debug=True))
        assert logging.getLogger().level == logging.DEBUG
        assert _handlers(cli._CONSOLE_HANDLER)[0].level == logging.DEBUG
        for n in cli._NOISY_LOGGERS:
            assert logging.getLogger(n).level == logging.WARNING

    def test_explicit_noisy_level_is_honoured(self, isolated_logging, no_logging_ini):
        common.config = None
        logging.getLogger("ncclient").setLevel(logging.DEBUG)
        cli._setup_logging(_args())
        assert logging.getLogger("ncclient").level == logging.DEBUG

    def test_reentry_does_not_stack_handlers(self, isolated_logging, no_logging_ini):
        common.config = None
        cli._setup_logging(_args())
        cli._setup_logging(_args())
        cli._setup_logging(_args(log_file=str(no_logging_ini / "a.log")))
        assert len(_handlers(cli._CONSOLE_HANDLER)) == 1
        assert len(_handlers(cli._FILE_HANDLER)) == 1

    def test_foreign_root_handlers_are_left_alone(self, isolated_logging, no_logging_ini):
        """caplog / a user handler on root must survive _setup_logging."""
        common.config = None
        foreign = logging.StreamHandler(sys.stdout)
        logging.getLogger().addHandler(foreign)
        cli._setup_logging(_args())
        assert foreign in logging.getLogger().handlers


class TestFileLogging:
    def test_cli_flag_creates_file_and_parent(self, isolated_logging, no_logging_ini):
        common.config = None
        path = no_logging_ini / "deep" / "er" / "junos-ops.log"
        cli._setup_logging(_args(log_file=str(path)))
        logging.getLogger("junos_ops.test").info("hello")
        assert path.is_file()
        assert "hello" in path.read_text()
        fh = _handlers(cli._FILE_HANDLER)[0]
        assert fh.level == logging.INFO
        assert fh.backupCount == 10

    def test_config_key_enables_file(self, isolated_logging, no_logging_ini, monkeypatch):
        monkeypatch.setenv("HOME", str(no_logging_ini))
        cfg = configparser.ConfigParser()
        cfg.read_dict({"DEFAULT": {"log_file": "~/from-config.log"}})
        common.config = cfg
        cli._setup_logging(_args())
        fh = _handlers(cli._FILE_HANDLER)[0]
        assert fh.baseFilename == str(no_logging_ini / "from-config.log")

    def test_cli_flag_overrides_config(self, isolated_logging, no_logging_ini):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"DEFAULT": {"log_file": str(no_logging_ini / "cfg.log")}})
        common.config = cfg
        cli._setup_logging(_args(log_file=str(no_logging_ini / "cli.log")))
        assert _handlers(cli._FILE_HANDLER)[0].baseFilename == str(no_logging_ini / "cli.log")

    def test_empty_config_value_means_no_file(self, isolated_logging, no_logging_ini):
        cfg = configparser.ConfigParser()
        cfg.read_dict({"DEFAULT": {"log_file": ""}})
        common.config = cfg
        cli._setup_logging(_args())
        assert _handlers(cli._FILE_HANDLER) == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_unwritable_directory_falls_back_to_console(
        self, isolated_logging, no_logging_ini, capsys
    ):
        common.config = None
        locked = no_logging_ini / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            cli._setup_logging(_args(log_file=str(locked / "sub" / "x.log")))
        finally:
            locked.chmod(0o700)
        assert _handlers(cli._FILE_HANDLER) == []
        assert len(_handlers(cli._CONSOLE_HANDLER)) == 1
        assert "console only" in capsys.readouterr().out


class TestLoggingIni:
    def _write_ini(self, directory):
        ini = directory / "logging.ini"
        ini.write_text(
            "[loggers]\nkeys=root\n\n"
            "[handlers]\nkeys=console\n\n"
            "[formatters]\nkeys=plain\n\n"
            "[logger_root]\nlevel=INFO\nhandlers=console\n\n"
            "[handler_console]\nclass=StreamHandler\nlevel=INFO\n"
            "formatter=plain\nargs=(sys.stdout,)\n\n"
            "[formatter_plain]\nformat=%(message)s\n"
        )
        return ini

    def test_module_loggers_survive_fileconfig(self, isolated_logging, no_logging_ini):
        """fileConfig must not disable already-created junos_ops.* loggers."""
        self._write_ini(no_logging_ini)
        common.config = None
        upgrade_logger = logging.getLogger("junos_ops.upgrade")
        upgrade_logger.disabled = False
        cli._setup_logging(_args())
        assert upgrade_logger.disabled is False
        # programmatic handlers are not added when logging.ini is in charge
        assert _handlers(cli._CONSOLE_HANDLER) == []

    def test_debug_applies_after_fileconfig(self, isolated_logging, no_logging_ini):
        self._write_ini(no_logging_ini)
        common.config = None
        cli._setup_logging(_args(debug=True))
        assert logging.getLogger().level == logging.DEBUG

    def test_xdg_location_is_found(self, isolated_logging, no_logging_ini):
        xdg_dir = no_logging_ini / "xdg" / "junos-ops"
        xdg_dir.mkdir(parents=True)
        self._write_ini(xdg_dir)
        assert cli._find_logging_ini() == str(xdg_dir / "logging.ini")


class TestRunWiring:
    def test_run_configures_logging_and_no_cwd_log(
        self, isolated_logging, no_logging_ini, monkeypatch, capsys
    ):
        """`check --local` end-to-end: logging set up, nothing written to CWD."""
        monkeypatch.setattr(sys, "argv", ["junos-ops", "check", "--local"])
        (no_logging_ini / "config.ini").write_text("[DEFAULT]\nlpath = .\n[h1]\n")
        cli._run()
        assert len(_handlers(cli._CONSOLE_HANDLER)) == 1
        assert not (no_logging_ini / "logger.log").exists()

    def test_run_log_file_flag(self, isolated_logging, no_logging_ini, monkeypatch):
        log = no_logging_ini / "run.log"
        monkeypatch.setattr(
            sys, "argv", ["junos-ops", "check", "--log-file", str(log), "--local"]
        )
        (no_logging_ini / "config.ini").write_text("[DEFAULT]\nlpath = .\n[h1]\n")
        cli._run()
        assert log.is_file()


class TestModuleEntry:
    def test_python_m_junos_ops_version(self):
        out = subprocess.run(
            [sys.executable, "-m", "junos_ops", "--version"],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip().endswith(f" {__version__}")
