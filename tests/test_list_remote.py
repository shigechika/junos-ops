"""Tests for upgrade.list_remote_path (dict) and display.print_list_remote."""

from unittest.mock import patch, MagicMock

from junos_ops import display, upgrade


class TestListRemotePath:
    """Core returns a dict and does not print."""

    def _fake_dir_info(self):
        return {
            "path": "/var/tmp",
            "file_count": 2,
            "files": {
                "foo.tgz": {
                    "type": "file",
                    "path": "/var/tmp/foo.tgz",
                    "size": 123,
                    "owner": "root",
                    "permissions_text": "-rw-r--r--",
                    "ts_date": "Jan  1 00:00",
                },
                "sub": {
                    "type": "dir",
                    "path": "/var/tmp/sub",
                    "size": 4096,
                    "owner": "root",
                    "permissions_text": "drwxr-xr-x",
                    "ts_date": "Jan  1 00:00",
                },
            },
        }

    def test_returns_dict_shape(self, junos_upgrade, mock_args, mock_config, capsys):
        mock_dev = MagicMock()
        fake = self._fake_dir_info()
        with patch.object(upgrade, "FS") as MockFS:
            MockFS.return_value.ls.return_value = fake
            result = junos_upgrade.list_remote_path("test-host", mock_dev)
        assert result["hostname"] == "test-host"
        assert result["path"] == "/var/tmp"
        assert result["file_count"] == 2
        assert result["format"] is None  # args.list_format is None in mock_args
        assert len(result["files"]) == 2
        names = {f["name"] for f in result["files"]}
        assert names == {"foo.tgz", "sub"}
        # core は print しない
        assert capsys.readouterr().out == ""

    def test_picks_up_format_from_args(
        self, junos_upgrade, mock_args, mock_config
    ):
        mock_args.list_format = "long"
        mock_dev = MagicMock()
        with patch.object(upgrade, "FS") as MockFS:
            MockFS.return_value.ls.return_value = self._fake_dir_info()
            result = junos_upgrade.list_remote_path("test-host", mock_dev)
        assert result["format"] == "long"


class TestPrintListRemote:
    """display.print_list_remote walks the dict."""

    def _result(self, fmt):
        return {
            "hostname": "test-host",
            "path": "/var/tmp",
            "file_count": 2,
            "format": fmt,
            "files": [
                {
                    "name": "foo.tgz",
                    "type": "file",
                    "path": "/var/tmp/foo.tgz",
                    "size": 123,
                    "owner": "root",
                    "permissions_text": "-rw-r--r--",
                    "ts_date": "Jan  1 00:00",
                },
                {
                    "name": "sub",
                    "type": "dir",
                    "path": "/var/tmp/sub",
                    "size": 4096,
                    "owner": "root",
                    "permissions_text": "drwxr-xr-x",
                    "ts_date": "Jan  1 00:00",
                },
            ],
        }

    def test_short(self, capsys):
        display.print_list_remote(self._result("short"))
        out = capsys.readouterr().out
        assert "/var/tmp:" in out
        assert "/var/tmp/foo.tgz" in out
        assert "/var/tmp/sub/" in out
        assert "total files" not in out

    def test_long(self, capsys):
        display.print_list_remote(self._result("long"))
        out = capsys.readouterr().out
        assert "/var/tmp:" in out
        assert "-rw-r--r--" in out
        assert "drwxr-xr-x" in out
        assert "total files: 2" in out
