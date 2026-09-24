"""Per-vault git versioning. One repo per vault, local-only (never push/fetch)."""

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from vaults.config import _git_enabled, _logger
from vaults.indexes import _INDEX_LOCK, _invalidate_vault
from vaults.notes import _note_lock, _note_path, _rel, _vault_root

# Per-invocation identity so we never touch the user's gitconfig.
_GIT_IDENTITY = [
    "-c",
    "user.name=vaults-hub",
    "-c",
    "user.email=vaults-hub@localhost",
    "-c",
    "commit.gpgsign=false",
]
# Written to $GIT_DIR/info/exclude (never a visible .gitignore).
_GIT_EXCLUDES = [".obsidian/", ".*.lock", ".*.tmp"]
# vault name -> "ours" | "external" | "disabled" (process-local cache).
_GIT_MODE: dict[str, str] = {}

# Read-only git subcommands get a shorter timeout and GIT_OPTIONAL_LOCKS=0.
_READONLY_CMDS = frozenset({"rev-parse", "log", "ls-files", "show", "status"})


@contextlib.contextmanager
def _git_lock(root: Path):
    """Serialize git operations per vault via a lock file next to the repo."""
    try:
        import fcntl  # POSIX-only; same import style as vaults.notes.
    except ImportError as exc:
        raise RuntimeError("vaults-hub requires POSIX fcntl (unavailable on Windows)") from exc
    lock_path = root / ".vaults-hub-git.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git in the vault root with per-invocation identity. Never raises."""
    readonly = bool(args) and args[0] in _READONLY_CMDS
    timeout = 10 if readonly else 30
    env = None
    if readonly:
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        return subprocess.run(
            ["git", *_GIT_IDENTITY, *args],
            cwd=root,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(args), 127, b"", str(exc).encode())


def _git_ensure_excludes(root: Path) -> None:
    """Append our exclude patterns to $GIT_DIR/info/exclude if missing."""
    exclude = root / ".git" / "info" / "exclude"
    try:
        text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    except OSError:
        return
    missing = [p for p in _GIT_EXCLUDES if p not in text.splitlines()]
    if not missing:
        return
    try:
        with exclude.open("a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("".join(p + "\n" for p in missing))
    except OSError:
        pass


def _git_cached_valid(root: Path, cached: str) -> bool:
    """True if the cached mode still matches the .git on disk.

    An externally created/removed vault-root .git flips validity without a
    full rescan (rev-parse) on every call.
    """
    try:
        has_git = (root / ".git").exists()
    except OSError:
        return True
    if cached == "ours":
        return has_git
    return not has_git


def _git_mode(root: Path, vault: str) -> str:
    """Return 'ours', 'external', or 'disabled', lazily initing one repo per vault.

    Call on mutation paths while holding the note's fcntl lock. Only the
    process-local cache touches _INDEX_LOCK; never held across git subprocesses.
    """
    if not _git_enabled() or shutil.which("git") is None:
        return "disabled"
    with _INDEX_LOCK:
        cached = _GIT_MODE.get(vault)
    if cached is not None and _git_cached_valid(root, cached):
        return cached
    mode = _git_detect(root)
    with _INDEX_LOCK:
        _GIT_MODE[vault] = mode
    return mode


def _git_detect(root: Path) -> str:
    """Adopt a vault-root .git, defer to an outer repo, or lazily init ours."""
    if (root / ".git").exists():
        _git_ensure_excludes(root)
        return "ours"
    if _git_run(root, "rev-parse", "--git-dir").returncode == 0:
        return "external"  # vault nested in someone else's repo: hands off.
    init = _git_run(root, "init", "-b", "main")
    if init.returncode != 0:  # pre-2.28 git has no -b; retry plainly.
        init = _git_run(root, "init")
    if init.returncode != 0:
        _logger.warning(
            "vaults git init failed for %s: %s",
            root,
            init.stderr.decode("utf-8", errors="replace")[:200],
        )
        return "disabled"
    _git_ensure_excludes(root)
    return "ours"


def _git_commit(root: Path, rels: list[str], subject: str, sha_hex: str | None) -> str | None:
    """Path-scoped `git add` + `git commit --only`. None on success, error on failure.

    Skips the commit when there is nothing to commit. Fail-open by contract:
    callers surface the error string; the file write always stands.
    Retries the whole add/status/commit sequence up to 3 times when the
    failure mentions index.lock (concurrent git writers); never deletes
    .git/index.lock directly. Callers must hold _git_lock(root).
    """
    backoffs = (0.2, 0.5)
    err: str | None = None
    for attempt in range(3):
        err = _git_commit_once(root, rels, subject, sha_hex)
        if err is None or "index.lock" not in err:
            return err
        if attempt < 2:
            time.sleep(backoffs[attempt])
    return err


def _git_commit_once(root: Path, rels: list[str], subject: str, sha_hex: str | None) -> str | None:
    """Single add/status/commit attempt. Callers must hold _git_lock(root)."""
    add = _git_run(root, "add", "--", *rels)
    if add.returncode != 0:
        return add.stderr.decode("utf-8", errors="replace").strip()[:300]
    status = _git_run(root, "status", "--porcelain", "--", *rels)
    if status.returncode != 0:
        return status.stderr.decode("utf-8", errors="replace").strip()[:300] or "git status failed"
    if not status.stdout.strip():
        return None
    msg = subject if sha_hex is None else f"{subject}\n\nSha256: {sha_hex}"
    commit = _git_run(root, "commit", "--only", "-m", msg, "--", *rels)
    if commit.returncode != 0:
        err = (
            commit.stderr.decode("utf-8", errors="replace")
            + commit.stdout.decode("utf-8", errors="replace")
        ).strip()
        if "nothing to commit" in err:
            return None
        return err[:300] or "git commit failed"
    return None


def _git_result(root: Path, vault: str, rels: list[str], subject: str, sha_hex: str | None) -> dict:
    """One versioning step for a mutation. Never raises (fail open).

    Mutation paths call this while holding the note lock; the git lock is
    taken inside (note -> git order).
    """
    try:
        with _git_lock(root):
            mode = _git_mode(root, vault)
            if mode != "ours":
                return {"versioning": mode}
            err = _git_commit(root, rels, subject, sha_hex)
    except Exception as exc:
        _logger.warning("vaults git versioning failed for %s: %s", vault, exc)
        return {"versioning": "disabled", "commit_error": str(exc)[:200]}
    out: dict = {"versioning": "ok"}
    if err:
        out["commit_error"] = err
    return out


def _git_tracked(root: Path, rel: str) -> bool:
    """True if rel is in the vault repo index. Call only when mode is 'ours'."""
    return _git_run(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0


def _git_track_before_delete(root: Path, vault: str, rel: str, sha_hex: str | None) -> str | None:
    """Commit an untracked note before delete so restore can recover it.

    Error string on failure, None when already tracked or versioning is off.
    Never raises (fail open).
    """
    try:
        with _git_lock(root):
            if _git_mode(root, vault) != "ours" or _git_tracked(root, rel):
                return None
            return _git_commit(root, [rel], f"vaults: track before delete {rel}", sha_hex)
    except Exception as exc:
        _logger.warning("vaults pre-delete track failed for %s: %s", rel, exc)
        return str(exc)[:200]


def _git_read_mode(root: Path, vault: str) -> str:
    """Read-only mode probe for history: never inits a repo ('none' if absent)."""
    if not _git_enabled():
        return "disabled"
    if shutil.which("git") is None:
        return "missing"
    with _INDEX_LOCK:
        cached = _GIT_MODE.get(vault)
    if cached is not None and _git_cached_valid(root, cached):
        return cached
    if (root / ".git").exists():
        return "ours"
    if _git_run(root, "rev-parse", "--git-dir").returncode == 0:
        return "external"
    return "none"


def _git_mode_error(mode: str) -> str:
    if mode == "disabled":
        return "versioning is disabled (VAULTS_HUB_GIT=0); re-run with it unset to enable"
    if mode == "missing":
        return "git binary not found on PATH; install git to use history/restore"
    return "vault lives inside an external git repo; versioning is hands-off"


def history(vault: str, path: str | None = None, limit: int = 50) -> dict:
    limit = max(1, min(200, int(limit)))
    root = _vault_root(vault)
    if path is not None:
        _note_path(root, path)  # validate: stays inside the vault
    # Read-only: no locks taken, so history never blocks writers. The git
    # calls run with GIT_OPTIONAL_LOCKS=0 (see _git_run); a concurrent
    # commit may make the read fail, surfaced as ValueError below.
    mode = _git_read_mode(root, vault)
    if mode in ("disabled", "missing", "external"):
        raise ValueError(_git_mode_error(mode))
    if path is not None and (mode == "none" or not _git_tracked(root, path)):
        return {
            "vault": vault,
            "path": path,
            "limit": limit,
            "history": [],
            "untracked": True,
            "truncated": False,
        }
    # limit+1 probe so the core owns the truncated contract directly.
    args = ["log", "--no-decorate", f"--max-count={limit + 1}", "--pretty=format:%H%x00%aI%x00%s"]
    if path is not None:
        args += ["--follow", "--", path]
    proc = _git_run(root, *args)
    if proc.returncode != 0:
        raise ValueError(
            "git log failed: " + proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        )
    entries = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("\x00")
        if len(parts) != 3 or not parts[0]:
            continue
        entries.append({"sha": parts[0], "date": parts[1], "message": parts[2]})
    truncated = len(entries) > limit
    entries = entries[:limit]
    out: dict = {
        "vault": vault,
        "path": path,
        "limit": limit,
        "history": entries,
        "truncated": truncated,
    }
    if path is not None:
        out["untracked"] = False
    return out


def restore(vault: str, path: str, rev: str, expected_sha256: str | None = None) -> dict:
    from vaults.notes import _atomic_write, _ensure_utf8

    root = _vault_root(vault)
    p = _note_path(root, path)
    if p.suffix != ".md":
        raise ValueError("note path must end in .md")
    if not rev or not rev.strip():
        raise ValueError("rev must be a non-empty git revision")
    rev = rev.strip()
    if re.search(r"[\x00-\x1f\x7f]", rev):
        raise ValueError(f"invalid rev: {rev!r}")
    # Consistent note -> git lock order everywhere: the note lock is held
    # for the whole restore (it is the OCC guard) and the git lock is taken
    # inside it only around the two short git-mutating commits. Read-only
    # git calls run lock-free with GIT_OPTIONAL_LOCKS=0.
    with _note_lock(p):
        try:
            mode = _git_mode(root, vault)
        except Exception as exc:
            raise ValueError(f"versioning unavailable: {exc}") from exc
        if mode != "ours":
            raise ValueError(_git_mode_error(mode))
        rel = _rel(root, p)
        try:
            current = p.read_bytes() if p.is_file() else None
        except OSError as exc:
            raise ValueError(f"note not found: {path!r}") from exc
        current_sha = hashlib.sha256(current).hexdigest() if current is not None else None
        if expected_sha256 is not None and expected_sha256 != current_sha:
            raise ValueError(
                "note has changed since the supplied expected_sha256;"
                " read it again before restoring"
            )
        status = _git_run(root, "status", "--porcelain", "--", rel)
        if status.returncode == 0 and status.stdout.strip():
            with _git_lock(root):
                snap_err = _git_commit(root, [rel], "vaults: snapshot before restore", current_sha)
            if snap_err:
                _logger.warning("vaults pre-restore snapshot failed for %s: %s", rel, snap_err)
        show = _git_run(root, "show", f"{rev}:{rel}")
        if show.returncode != 0:
            raise ValueError(f"unknown revision or path not in revision: {rev!r} for {path!r}")
        text = show.stdout.decode("utf-8", errors="replace")
        _ensure_utf8(text)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_write(p, text)
        except OSError as exc:
            raise ValueError(
                f"cannot restore {path!r}: {exc.strerror or type(exc).__name__}"
            ) from exc
        encoded = text.encode("utf-8")
        new_sha = hashlib.sha256(encoded).hexdigest()
        with _git_lock(root):
            commit_err = _git_commit(root, [rel], f"vaults: restore {rel} from {rev}", new_sha)
        versioning: dict = {"versioning": "ok"}
        if commit_err:
            versioning["commit_error"] = commit_err
        _invalidate_vault(vault)
        return {
            "vault": vault,
            "path": rel,
            "rev": rev,
            "restored_from": rev,
            "bytes": len(encoded),
            "previous_sha256": current_sha,
            "sha256": new_sha,
            **versioning,
        }
