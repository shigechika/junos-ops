"""CLI passthrough for ``junos-ops show``: text / json / xml formats.

Core functions here return plain ``dict`` values without touching stdout;
the :mod:`junos_ops.display` layer renders them for terminals, while
non-CLI consumers (e.g. ``junos-mcp``) can feed the dicts into their own
serializers.

The three formats map to PyEZ as follows:

- ``text`` -> ``dev.cli(command)`` returns a string (default, legacy behaviour)
- ``json`` -> ``dev.cli(command, format="json")`` returns a Python ``dict``
  parsed from the device's ``| display json`` output
- ``xml``  -> ``dev.cli(command, format="xml")`` returns an ``lxml._Element``
  which we serialise with pretty-printing

NETCONF note: pipe stages such as ``| match`` and ``| last`` are dropped
by the device when the caller asks for ``format=json|xml``. Callers that
need to filter output should use ``format=text`` or call the equivalent
RPC directly.
"""

import time
import warnings
from logging import getLogger

from jnpr.junos.exception import RpcTimeoutError
from lxml import etree

logger = getLogger(__name__)

VALID_FORMATS = ("text", "json", "xml")


def _cli_with_retry(
    dev, command: str, hostname: str, retry: int, output_format: str
):
    """Call ``dev.cli`` with the given format, retrying on RpcTimeoutError.

    :raises RpcTimeoutError: once the retry budget is exhausted.
    """
    kwargs = {} if output_format == "text" else {"format": output_format}
    for attempt in range(retry + 1):
        try:
            # ``show`` is intentionally a CLI passthrough, so suppress PyEZ's
            # per-call "CLI command is for debug use only" RuntimeWarning.
            # PyEZ prepends a newline to the warning text, so the ``message``
            # regex must allow a leading ``\n`` (``re.match`` is anchored).
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"\s*CLI command is for debug use only",
                    category=RuntimeWarning,
                )
                return dev.cli(command, **kwargs)
        except RpcTimeoutError:
            if attempt < retry:
                wait = 5 * (attempt + 1)
                logger.warning(
                    f"{hostname}: RpcTimeoutError, "
                    f"retry {attempt + 1}/{retry} in {wait}s"
                )
                time.sleep(wait)
            else:
                raise


def _normalise_output(raw, output_format: str):
    """Convert the PyEZ return value into a display-friendly payload.

    - ``text``: already a string, returned as-is
    - ``json``: already a ``dict``, returned as-is
    - ``xml``:  lxml ``_Element``, serialised with ``pretty_print=True``
    """
    if output_format == "xml":
        return etree.tostring(raw, pretty_print=True, encoding="unicode")
    return raw


def run_cli(
    dev,
    command: str,
    *,
    output_format: str = "text",
    retry: int = 0,
    hostname: str = "",
) -> dict:
    """Run a single CLI command and return a dict describing the outcome.

    :param dev: open PyEZ ``Device``.
    :param command: CLI command string (e.g. ``"show version"``).
    :param output_format: ``text`` (default), ``json``, or ``xml``.
    :param retry: number of retries on ``RpcTimeoutError`` (0 = no retry).
    :param hostname: config section name, used in log messages and echoed
        back in the result.
    :return: dict with keys:

        - ``hostname`` (str)
        - ``command`` (str)
        - ``format`` (str): echoed back for display / downstream consumers.
        - ``ok`` (bool)
        - ``output``: ``str`` (text / xml) or ``dict`` (json) on success,
          ``None`` on failure.
        - ``error`` (str | None): exception class name on failure.
        - ``error_message`` (str | None)

    Does not print.
    """
    if output_format not in VALID_FORMATS:
        raise ValueError(
            f"invalid format {output_format!r}; "
            f"expected one of {VALID_FORMATS}"
        )
    try:
        raw = _cli_with_retry(dev, command, hostname, retry, output_format)
        return {
            "hostname": hostname,
            "command": command,
            "format": output_format,
            "ok": True,
            "output": _normalise_output(raw, output_format),
            "error": None,
            "error_message": None,
        }
    except Exception as e:
        logger.error(f"{hostname}: {e}")
        return {
            "hostname": hostname,
            "command": command,
            "format": output_format,
            "ok": False,
            "output": None,
            "error": type(e).__name__,
            "error_message": str(e),
        }


def run_cli_batch(
    dev,
    commands: list[str],
    *,
    output_format: str = "text",
    retry: int = 0,
    hostname: str = "",
) -> dict:
    """Run a list of CLI commands in one session, short-circuiting on failure.

    Stops at the first command that fails (typically a connection drop
    makes subsequent commands pointless). Returns the partial result list
    so callers can see which command broke.

    :return: dict with keys:

        - ``hostname`` (str)
        - ``format`` (str)
        - ``ok`` (bool): True when every command succeeded.
        - ``results`` (list[dict]): per-command :func:`run_cli` dicts
          up to and including the first failure.

    Does not print.
    """
    results: list[dict] = []
    for cmd in commands:
        res = run_cli(
            dev,
            cmd,
            output_format=output_format,
            retry=retry,
            hostname=hostname,
        )
        results.append(res)
        if not res["ok"]:
            break
    return {
        "hostname": hostname,
        "format": output_format,
        "ok": all(r["ok"] for r in results) and len(results) == len(commands),
        "results": results,
    }
