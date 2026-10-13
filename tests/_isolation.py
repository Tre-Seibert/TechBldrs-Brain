"""Hermetic test settings, whatever the box's .env or environment says.

Every test module imports this first. On import it:

- stops Settings from reading .env, and drops every env var Settings reads (FLOW_MODE, QDRANT_URL,
  LLM_BASE_URL, BRAIN_USER_JWT_SECRET, ...), so a runtime box with FLOW_MODE=http behaves like a
  fresh checkout;
- pins stub Flow, no Qdrant and an LLM URL that can't resolve, and points BRAIN_DATA_DIR (audit
  rows and tb-brain.log) at a temp dir removed at exit;
- refuses any socket connect except to a loopback port this process bound itself (asyncio's
  socketpair on Windows), so a test that reaches for real Flow, Ollama or Qdrant fails loudly.
"""

from __future__ import annotations

import atexit
import ipaddress
import logging
import os
import shutil
import sys
import tempfile
import weakref

from app.config import Settings, get_settings

TEST_LLM_BASE_URL = "http://tb-brain-test-llm.invalid/v1"


class NetworkBlocked(RuntimeError):
    pass


def _env_names() -> set[str]:
    names = {name.upper() for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        if isinstance(field.validation_alias, str):
            names.add(field.validation_alias.upper())
    return names


def _isolate_settings() -> str:
    Settings.model_config["env_file"] = None
    for name in list(os.environ):
        if name.upper() in _env_names():
            del os.environ[name]
    data_dir = tempfile.mkdtemp(prefix="tb-brain-tests-")
    os.environ.update(
        {
            "FLOW_MODE": "stub",
            "QDRANT_URL": "",
            "LLM_BASE_URL": TEST_LLM_BASE_URL,
            "BRAIN_DATA_DIR": data_dir,
        }
    )
    get_settings.cache_clear()
    atexit.register(_remove_data_dir, data_dir)
    return data_dir


def _remove_data_dir(data_dir: str) -> None:
    # main._configure_logging leaves a FileHandler open on tb-brain.log; Windows won't delete an open file.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.FileHandler) and handler.baseFilename.startswith(os.path.abspath(data_dir)):
            root.removeHandler(handler)
            handler.close()
    shutil.rmtree(data_dir, ignore_errors=True)


# Loopback sockets this process bound itself (asyncio's socketpair on Windows binds 127.0.0.1:0, then connects).
_own_sockets: "weakref.WeakSet" = weakref.WeakSet()


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _own_port(port: object) -> bool:
    for sock in list(_own_sockets):
        try:
            if sock.getsockname()[1] == port:
                return True
        except OSError:
            continue
    return False


def _guard_network(event: str, args: tuple) -> None:
    if event == "socket.bind":
        sock, address = args[0], args[1]
        if isinstance(address, tuple) and len(address) >= 2 and _is_loopback(address[0]):
            _own_sockets.add(sock)
        return
    if event in ("socket.connect", "socket.sendto"):
        address = args[1] if event == "socket.connect" else args[-1]
        if not isinstance(address, tuple) or len(address) < 2:
            return  # AF_UNIX and friends: not the network
        if _is_loopback(address[0]) and _own_port(address[1]):
            return
        raise NetworkBlocked(f"tests may not open network connections (attempted {address[0]}:{address[1]})")
    if event == "socket.getaddrinfo":
        host = args[0]
        if host is None or _is_loopback(host):
            return
        raise NetworkBlocked(f"tests may not resolve hostnames (attempted {host!r})")


DATA_DIR = _isolate_settings()
if not getattr(sys, "_tb_brain_network_guard", False):
    sys.addaudithook(_guard_network)
    sys._tb_brain_network_guard = True  # type: ignore[attr-defined]
