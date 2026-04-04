"""Tests for junos_ops.display.

Phase 0: scaffold. Actual display functions are filled in during later
phases, and tests are added alongside each phase.
"""

from junos_ops import display


def test_print_host_header(capsys):
    display.print_host_header("rt1.example.jp")
    captured = capsys.readouterr()
    assert captured.out == "# rt1.example.jp\n"


def test_print_host_footer(capsys):
    display.print_host_footer()
    captured = capsys.readouterr()
    assert captured.out == "\n"


def test_print_facts(capsys):
    display.print_facts("rt1.example.jp", {"model": "MX240", "version": "21.4R3"})
    captured = capsys.readouterr()
    assert "# rt1.example.jp" in captured.out
    assert "MX240" in captured.out
    assert "21.4R3" in captured.out
