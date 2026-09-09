"""The git this adapter needs, with the flags disensor's own runner uses.

Two things here are not stylistic.

The cleanliness check copies `round.STATUS_ARGS` literally. A plain
`git status --porcelain` honours user configuration that can hide untracked
files and dirty submodules, so the adapter would announce a tree ready for a
round that `disensor round` then refuses. Announcing a green light that the
next command contradicts is worse than not announcing anything.

Every call carries a timeout. disensor's own `gitctx._git` has none, which is
right for CI and wrong inside an agent loop: Cursor kills the hook after its
own timeout, and a hook killed mid-flight never gets to print the empty object
that keeps the loop moving.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Los mismos flags que usa el runner de disensor. `--ignore-submodules=none`
# porque sin el un submodulo sucio pasa desapercibido, y `--untracked-files=all`
# porque la configuracion del usuario puede cambiar el default.
STATUS_ARGS = [
    "status", "--porcelain=v1", "-z",
    "--untracked-files=all",
    "--ignore-submodules=none",
]

DEFAULT_TIMEOUT = 5.0


class GitUnavailable(Exception):
    """git could not answer. Never a reason to block the agent."""


def run_git(args: list[str], cwd: Path, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run git, never raising for a non-zero exit code.

    `--no-optional-locks` because this runs in the background while the user
    works: git itself recommends it so a read does not take the index lock out
    from under the editor.
    """
    try:
        done = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=str(cwd), capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailable(f"git {args[0]} timed out after {timeout}s") from exc
    except OSError as exc:
        raise GitUnavailable(f"git could not run: {exc}") from exc
    out = (done.stdout or b"").decode("utf-8", "replace")
    err = (done.stderr or b"").decode("utf-8", "replace")
    return done.returncode, out, err


def repo_root(cwd: Path) -> Path | None:
    code, out, _ = run_git(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out.strip():
        return None
    return Path(out.strip())


def is_dirty(repo: Path) -> bool | None:
    """Whether the tree has anything at all. None when git could not answer."""
    code, out, _ = run_git(STATUS_ARGS, repo)
    if code != 0:
        return None
    return bool(out.strip())


def rev_parse(ref: str, repo: Path) -> str | None:
    """Canonical OID of a commit, or None. Never raises for an unknown ref."""
    if not ref or not str(ref).strip():
        return None
    code, out, _ = run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], repo
    )
    oid = out.strip()
    return oid if code == 0 and len(oid) == 40 else None


def merge_base(base: str, head: str, repo: Path) -> str | None:
    """The single merge base, or None when there is more than one.

    Several merge bases means there is no defined review scope, which disensor
    treats as undecidable. Picking one silently would invent a scope.
    """
    code, out, _ = run_git(["merge-base", "--all", base, head], repo)
    if code != 0:
        return None
    bases = [line.strip() for line in out.splitlines() if line.strip()]
    return bases[0] if len(bases) == 1 else None


def default_base(repo: Path) -> str | None:
    """The target branch, resolved once at install time and never guessed later.

    Same order disensor's own template uses to prefill a declaration:
    `origin/main`, then `origin/master`. `origin/HEAD` is deliberately not in
    the list: it can point at the default branch while the real target is a
    release branch, and the policy the gate applies is read from the tip of
    whatever base is chosen, so picking the wrong one changes the verdict.
    """
    for candidate in ("origin/main", "origin/master"):
        if rev_parse(candidate, repo):
            return candidate
    return None


def changed_paths(base: str, head: str, repo: Path) -> set[str] | None:
    code, out, _ = run_git(["diff", "--name-only", "-z", base, head], repo)
    if code != 0:
        return None
    return {p for p in out.split("\0") if p}


def evidence_added(base: str, head: str, repo: Path, evidence_root: str) -> bool:
    """Whether this range already adds a declaration under the evidence root.

    This is an APPROXIMATION and the caller says so. disensor separates "this
    range demands a round" from "a valid declaration covers it", and only the
    first one is exposed. Recomputing real coverage would mean rebuilding the
    gate's pipeline, which belongs in disensor and not in an adapter. Seeing a
    new artifact and staying quiet errs towards silence, which is the side to
    err on.
    """
    code, out, _ = run_git(
        ["diff", "--name-only", "-z", "--diff-filter=A", base, head, "--", evidence_root], repo
    )
    if code != 0:
        return False
    return any(p.endswith(".json") for p in out.split("\0") if p)


def inside(path: Path, repo: Path) -> bool:
    try:
        path.resolve().relative_to(repo.resolve())
        return True
    except (ValueError, OSError):
        return False
