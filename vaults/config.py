"""Runtime configuration: vaults root, env switches, logging, CLI args."""

import argparse
import functools
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

VAULTS_ROOT = Path(os.environ.get("VAULTS_ROOT", str(Path.home() / ".vaults"))).resolve()

_logger = logging.getLogger("vaults")


def _setup_logging() -> None:
    """Enable debug file logging when VAULTS_HUB_DEBUG=1. No-op otherwise."""
    if os.environ.get("VAULTS_HUB_DEBUG") != "1":
        _logger.disabled = True
        return
    _logger.disabled = False
    try:
        log_dir = Path.home() / ".cache" / "vaults-hub"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "debug.log", maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _logger.addHandler(handler)
        _logger.setLevel(logging.DEBUG)
    except OSError:
        pass


_setup_logging()


def _logged(fn):
    """Log tool entry/exit at INFO and failures at ERROR with traceback."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        vault = kwargs.get("vault", args[0] if args else None)
        path = kwargs.get("path", kwargs.get("src_path"))
        _logger.info("vaults.%s called vault=%r path=%r", fn.__name__, vault, path)
        try:
            result = fn(*args, **kwargs)
        except Exception:
            _logger.error("vaults.%s failed", fn.__name__, exc_info=True)
            raise
        _logger.info("vaults.%s ok", fn.__name__)
        return result

    return wrapper


def _git_enabled() -> bool:
    """Opt-out switch: VAULTS_HUB_GIT=0 disables all versioning."""
    return os.environ.get("VAULTS_HUB_GIT", "1") != "0"


def _parse_args(argv=None):
    """CLI args parsed in __main__ only; precedence: flag > env > default."""
    parser = argparse.ArgumentParser(
        description="Serve Obsidian-style Markdown vaults over stdio (MCP)."
    )
    parser.add_argument(
        "--vaults-root",
        default=None,
        help="Root dir holding one subdir per vault. Overrides VAULTS_ROOT env; "
        "defaults to ~/.vaults. Created on startup if missing.",
    )
    return parser.parse_args(argv)


def _resolve_vaults_root(cli_value: str | None) -> Path:
    """Resolve the vaults root, creating it on startup when missing."""
    raw = cli_value or os.environ.get("VAULTS_ROOT") or str(Path.home() / ".vaults")
    root = Path(raw).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"error: cannot create vaults root {root}: {exc}") from exc
    if not root.is_dir():
        raise SystemExit(f"error: vaults root is not a directory: {root}")
    return root
