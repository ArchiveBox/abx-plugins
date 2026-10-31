#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
# ruff: noqa: E402
#
# Clones a git repository from a provided URL into the current working directory.
# Supports configurable git arguments and timeout via environment variables.
#
# Usage:
#     ./on_Snapshot__34_git.finite.bg.py --url=<url> > events.jsonl

import signal
import sys

# Snapshot cleanup sends SIGTERM before the hard SIGKILL deadline. This finite
# downloader must finish within its hook timeout instead of corrupting a fetch
# or checkout during polite cleanup. Git installs its own SIGTERM handlers, so
# SIG_IGN does not survive inside Git: block the signal across child execs.
# The runner's hard SIGKILL deadline still applies to the entire process tree.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})

if any(arg == "--url" or arg.startswith("--url=") for arg in sys.argv[1:]):
    print("git clone started", flush=True)

import os
import fnmatch
import fcntl
import hashlib
import re
import shutil
import tempfile
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "git"
BIN_NAME = "git"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def rel_output(path_str: str | None) -> str | None:
    if not path_str:
        return path_str
    path = Path(path_str)
    resolved = path.resolve()
    if not resolved.exists():
        return path_str
    try:
        return str(resolved.relative_to(SNAP_DIR.resolve()))
    except Exception:
        return path.name or path_str


def is_git_url(url: str) -> bool:
    """Return True only for URLs we can normalize into a cloneable repo URL."""
    return normalize_git_url(url) is not None


def normalize_git_url(url: str) -> str | None:
    """Normalize common repository page URLs to a repo clone URL."""
    lower_url = url.lower()
    if (
        lower_url.startswith("git://")
        or lower_url.startswith("ssh://git@")
        or lower_url.startswith("git@")
    ):
        return url
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path.lower().endswith(".git"):
        return url

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host.removeprefix("www.")

    git_domains = {
        domain.strip().lower()
        for domain in CONFIG.GIT_DOMAINS.split(",")
        if domain.strip()
    }
    if host not in git_domains:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return None

    owner = path_parts[0]
    repo = path_parts[1]
    # GitHub's site routes can have two path components without naming a repo.
    if host == "github.com" and owner.lower() in {"sponsors", "topics", "orgs"}:
        return None
    if not owner or not repo:
        return None

    return urlunsplit((parsed.scheme, parsed.netloc, f"/{owner}/{repo}", "", ""))


def run_git(
    cmd: list[str | Path],
    timeout: int,
    *,
    capture: bool = False,
    lock_fd: int | None = None,
):
    """Let Git release its own locks before killing its owned process group."""
    deadline = time.monotonic() + timeout
    # Every caller uses git -C <repo> <verb>. Log only that verb, never remote
    # arguments that may contain credentials. Separate fetch/ref publication
    # from checkout/submodules: a timeout does not necessarily mean slow network.
    operation = f"git {cmd[3]}"
    print(f"[*] {operation} started", file=sys.stderr, flush=True)
    proc = subprocess.Popen(
        cmd,
        start_new_session=True,
        pass_fds=(lock_fd,) if lock_fd is not None else (),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )

    def signal_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    try:
        # Reserve cleanup time inside the configured command deadline.
        stdout, _ = proc.communicate(timeout=max(0, deadline - time.monotonic() - 1))
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGINT)
        signal_group(signal.SIGCONT)
        try:
            proc.communicate(timeout=max(0, deadline - time.monotonic() - 0.25))
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Also stop helpers if their parent exited without waiting for them.
            signal_group(signal.SIGKILL)
        proc.communicate(timeout=max(0, deadline - time.monotonic()))
        raise subprocess.TimeoutExpired(cmd, timeout) from None
    finally:
        if proc.poll() is None:
            signal_group(signal.SIGKILL)
        if proc.stdout is not None:
            proc.stdout.close()
        elapsed = time.monotonic() - (deadline - timeout)
        print(
            f"[*] {operation} ended after {elapsed:.3f}s",
            file=sys.stderr,
            flush=True,
        )


def clone_git(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Clone git repository.

    Returns: (success, output_path, error_message)
    """
    config = load_config()
    timeout = config.GIT_TIMEOUT
    error_prefix = "git clone failed"
    # A snapshot restart is a fresh capture. SIGKILL cannot release Git's ref
    # locks, and reusing that repository can poison every later attempt. Acquire
    # independently; leave previous complete or partial output intact on failure.
    runtime_tmp = (
        Path(config.TMP_DIR).expanduser()
        if config.TMP_DIR
        else Path(tempfile.gettempdir())
    )
    scratch = (
        runtime_tmp / PLUGIN_DIR / hashlib.sha256(str(SNAP_DIR).encode()).hexdigest()
    )
    repo_dir = scratch / "repo"
    try:
        # Do not follow a replaced namespace into another snapshot's scratch.
        if scratch.parent.is_symlink() or scratch.is_symlink() or repo_dir.is_symlink():
            raise ValueError("Git scratch directories must not be symlinks")
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner_fd = os.open(
            scratch / "owner.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(owner_fd, "a") as owner:
            # The local OS lease survives in Git children if this hook is killed.
            # Never clear another live attempt; after all owners exit, startup
            # discards only this snapshot's scratch instead of accumulating GBs.
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                if repo_dir.exists():
                    shutil.rmtree(repo_dir)
                repo_dir.mkdir()
                git_dir = repo_dir / ".git"
                init_cmd = [binary, "-C", repo_dir, "init"]
                add_remote_cmd = [
                    binary,
                    "-C",
                    repo_dir,
                    "remote",
                    "add",
                    "origin",
                    url,
                ]
                for cmd, error_prefix in (
                    (init_cmd, "git init failed"),
                    (add_remote_cmd, "git remote add failed"),
                ):
                    result = run_git(cmd, timeout, lock_fd=owner.fileno())
                    if result.returncode != 0:
                        return False, None, f"{error_prefix} (exit={result.returncode})"

                # These are the existing legacy Git sidecars. Current per-process
                # artifacts use the UUID naming contract also recognized by hashes.
                extra_excludes = [
                    f"{PLUGIN_DIR}.jsonl",
                    f"{Path(__file__).name}.sh",
                    f"{Path(__file__).stem}.stdout.log",
                    f"{Path(__file__).stem}.stderr.log",
                    f"{Path(__file__).stem}.pid",
                    f"{Path(__file__).stem}.stdout.*.log",
                    f"{Path(__file__).stem}.stderr.*.log",
                    "on_*__*.*.sh",
                    "on_*__*.*.pid",
                    "on_*__*.*.stdout.log*",
                    "on_*__*.*.stderr.log*",
                ]
                (git_dir / "info/exclude").write_text("\n".join(extra_excludes) + "\n")
                # Fetch HEAD explicitly while retaining every other shallow branch
                # tip: the first wildcard FETCH_HEAD entry is not necessarily HEAD.
                fetch_cmd = [
                    binary,
                    "-C",
                    repo_dir,
                    "fetch",
                    "--depth=1",
                    "origin",
                    "HEAD",
                    "+refs/heads/*:refs/remotes/origin/*",
                ]
                reset_cmd = [binary, "-C", repo_dir, "reset", "--hard", "FETCH_HEAD"]
                submodule_cmd = [
                    binary,
                    "-C",
                    repo_dir,
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                    "--depth=1",
                ]
                for cmd, error_prefix in (
                    (fetch_cmd, "git fetch failed"),
                    (reset_cmd, "git reset failed"),
                    (submodule_cmd, "git submodule update failed"),
                ):
                    result = run_git(cmd, timeout, capture=True, lock_fd=owner.fileno())
                    if result.returncode != 0:
                        output = result.stdout or ""
                        if (
                            error_prefix == "git fetch failed"
                            and "not found" in output.lower()
                        ):
                            return True, "No git repository found", ""
                        return (
                            False,
                            None,
                            f"{error_prefix} (exit={result.returncode}): {output.strip()}",
                        )

                def is_sidecar(name: str) -> bool:
                    return bool(
                        re.fullmatch(
                            r"on_[A-Za-z]+__.+\.[0-9a-f]{32}\.(?:sh|pid|stdout\.log|stderr\.log)(?:\.\d+)?",
                            name,
                        ),
                    ) or any(
                        fnmatch.fnmatchcase(name, pattern)
                        for pattern in extra_excludes[:7]
                    )

                # The output directory also holds runner logs. Keep their inodes,
                # but replace repository contents (including deleted upstream files)
                # only after all acquisition/checkouts succeeded. Relative paths use
                # the held cwd instead of repeatedly resolving remote mount ancestors.
                error_prefix = "git publication failed"
                publication_started = time.monotonic()
                print("[*] git publication started", file=sys.stderr, flush=True)
                try:
                    for entry in repo_dir.iterdir():
                        if is_sidecar(entry.name):
                            raise ValueError(
                                f"Repository path collides with hook metadata: {entry.name}",
                            )
                    for entry in Path(".").iterdir():
                        if is_sidecar(entry.name):
                            continue
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                    shutil.copytree(repo_dir, ".", dirs_exist_ok=True, symlinks=True)
                finally:
                    elapsed = time.monotonic() - publication_started
                    print(
                        f"[*] git publication ended after {elapsed:.3f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                return True, str(OUTPUT_DIR), ""

            finally:
                if repo_dir.exists():
                    shutil.rmtree(repo_dir)

    except subprocess.TimeoutExpired:
        return False, None, f"{error_prefix}: Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{error_prefix}: {type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="Git repository URL")
def main(url: str):
    """Clone a git repository from a URL."""

    output = None
    status = "failed"
    error = ""

    try:
        git_url = normalize_git_url(url)
        if git_url is None:
            print(f"Skipping git clone for non-git URL: {url}", file=sys.stderr)
            emit_archive_result_record("noresults", "Not a git URL")
            sys.exit(0)
        if git_url != url:
            print(f"Normalizing git URL to repo root: {git_url}", file=sys.stderr)

        config = load_config()
        # Get binary from environment
        binary = config.GIT_BINARY

        # Run extraction
        success, output, error = clone_git(git_url, binary)
        status = (
            "noresults"
            if output == "No git repository found"
            else ("succeeded" if success else "failed")
        )

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    # Output clean JSONL (no RESULT_JSON= prefix)
    emit_archive_result_record(status, rel_output(output) or error or "")

    sys.exit(1 if status == "failed" else 0)


if __name__ == "__main__":
    main()
