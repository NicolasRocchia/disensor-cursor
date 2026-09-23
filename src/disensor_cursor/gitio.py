"""The git this adapter needs, read the way disensor reads it.

Four things here are not stylistic.

The cleanliness check uses the flags of `round.STATUS_ARGS`, with one
deliberate exception. A plain `git status --porcelain` honours user
configuration that can hide untracked files, so the adapter would announce a
tree ready for a round that `disensor round` then refuses. The exception is
submodules: see STATUS_ARGS.

`git status` is also the one call here that can run programs a configuration
names: an fsmonitor command, which the environment switches off (see
executables.py), and the clean filter of whatever driver `.gitattributes`
assigns. Filters are switched off for that call, every driver any
configuration defines, whatever its scope: a filter the user installed can
run commands the repository configures, as git-lfs does with its extensions.

Every call draws from the run's single budget (see clock.py). disensor's own
`gitctx._git` has no timeout, which is right for CI and wrong inside an agent
loop: Cursor kills the hook after its own timeout, and a hook killed
mid-flight never gets to print the empty object that keeps the loop moving.

Whether a declaration still covers the range is answered the way the gate
answers it, not approximately: the range is measured from the merge base, its
paths are split the way `gate.classify_changes` splits them, the declaration
has to pass the range checks of G5, and the witness condition is G7's. A first
version of this module took shortcuts, and each one diverged from the gate in
a direction that mattered: it stayed quiet on a nested file the gate counts as
code, and nagged about a file that had been put back to its base content.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import clock, executables

# Los flags del runner de disensor, salvo uno. `--untracked-files=all` porque
# la configuracion del usuario puede cambiar el default. `--no-renames` no
# cambia si el arbol esta sucio o no, y deja una ruta por entrada: sin el, un
# renombre ocupa dos campos y el parser tendria que adivinar cual leer.
#
# `--ignore-submodules=dirty` y no el `none` de disensor: con `none`, git
# corre otro `git status` adentro de cada submodulo, con la configuracion
# propia de ese submodulo, y esa configuracion puede definir un filtro que git
# ejecuta. Se comprobo corriendolo. Con `dirty` se sigue viendo cuando un
# submodulo apunta a otro commit; lo que se pierde son los cambios sin
# commitear adentro de un submodulo, que `disensor round` si va a ver.
STATUS_ARGS = [
    "status", "--porcelain=v1", "-z",
    "--untracked-files=all",
    "--ignore-submodules=dirty",
    "--no-renames",
]

DEFAULT_TIMEOUT = 5.0
MIN_CALL = 0.05

REGULAR_BLOB_MODES = ("100644", "100755")
GATES = ("plan", "diff", "architecture")


class GitUnavailable(Exception):
    """git could not answer. Never a reason to block the agent."""


def _with_config(env: dict[str, str], config) -> dict[str, str]:
    """`env` plus configuration for one call, through GIT_CONFIG_COUNT.

    That channel and not `-c`: `-c` splits at the first `=`, and a driver name
    can hold one. It overrides every configuration file; only the user's own
    `git -c` settings rank above it.
    """
    if not config:
        return env
    try:
        count = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        count = 0
    for index, (key, value) in enumerate(config, start=count):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(config))
    return env


def run_git(
    args: list[str], cwd: Path, timeout: float = DEFAULT_TIMEOUT, config=(),
    exclude=(), raw: bool = False,
) -> tuple[int, str, str]:
    """Run git, never raising for a non-zero exit code.

    `--no-optional-locks` because this runs in the background while the user
    works: git itself recommends it so a read does not take the index lock out
    from under the editor, and without the lock `status` never writes the
    index, so no hook runs. `--literal-pathspecs` because disensor's gate uses
    it on every call: a path is a path, never pathspec magic such as `:(top)`.
    The environment switches off the fsmonitor command and lazy fetches,
    `config` adds settings for this call only, and `exclude` directories git
    itself may not come from. With `raw`, stdout comes back as the bytes git
    wrote, for the one caller that cannot afford a replacement character.
    """
    budget = clock.left(timeout)
    if budget < MIN_CALL:
        raise GitUnavailable(f"no time left for git {args[0]}")
    # Ruta absoluta desde el PATH y nunca el nombre pelado: en Windows un
    # `git.exe` en el directorio actual, que en los hooks de proyecto es el
    # repositorio, se ejecutaria antes que el instalado.
    git = executables.find("git", exclude)
    if git is None:
        raise GitUnavailable("git is not on PATH")
    try:
        done = subprocess.run(
            [git, "--no-optional-locks", "--literal-pathspecs", *args],
            cwd=str(cwd), capture_output=True, timeout=budget, check=False,
            env=_with_config(executables.child_environment(), config),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailable(f"git {args[0]} timed out after {budget:.1f}s") from exc
    except OSError as exc:
        raise GitUnavailable(f"git could not run: {exc}") from exc
    out = done.stdout or b""
    err = (done.stderr or b"").decode("utf-8", "replace")
    return done.returncode, (out if raw else out.decode("utf-8", "replace")), err


def _fields(out: str) -> list[str]:
    return [f for f in out.split("\0") if f]


def repo_root(cwd: Path) -> Path | None:
    code, out, _ = run_git(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out.strip():
        return None
    return Path(out.strip())


def configured_filters(repo: Path) -> list[str]:
    """Every filter driver some configuration names, whatever its scope.

    Every scope, and not only the repository's: a filter the user installed
    can run what the repository configures. git-lfs, which Git for Windows
    ships in its system configuration, runs the `lfs.extension.*` commands of
    `.git/config` on every clean. Includes are followed.

    Raises GitUnavailable when the configuration cannot be read at all, a
    malformed `.git/config` for instance, or when a driver is named in bytes
    that are not UTF-8: read with replacement characters that is another
    name, and switching that one off left the real driver running. Only the
    names are decoded that strictly; a value is never used.
    """
    code, out, err = run_git(["config", "--null", "--get-regexp", r"^filter\."], repo, raw=True)
    if code == 1 and not out:
        return []
    if code != 0:
        detail = err.strip().splitlines()[-1] if err.strip() else f"exit {code}"
        raise GitUnavailable(f"could not read the repository's git configuration ({detail})")
    names = set()
    # Con --null cada entrada es `clave\nvalor\0`, y la clave, filter.<nombre>.<variable>.
    for entry in out.split(b"\0"):
        key = entry.split(b"\n", 1)[0]
        if not key.startswith(b"filter.") or key.count(b".") < 2:
            continue
        try:
            names.add(key[len(b"filter."):].rsplit(b".", 1)[0].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise GitUnavailable(
                "the git configuration names a filter driver in bytes that are not UTF-8, so it "
                "cannot be switched off by name; the tree was not checked"
            ) from exc
    return sorted(names)


def dirty_paths(repo: Path) -> set[str] | None:
    """Every path git reports as changed, staged or untracked. None if git failed.

    Each `-z` entry is two status letters, a space and the path, and with
    `--no-renames` there is never a second path to consume.

    No filter runs: `git status` would run the clean command of a file whose
    timestamps changed, and `.gitattributes` decides which files those are.
    Without `required` a missing command is not an error, so the check still
    answers. The cost is that such a file, under git-lfs for instance, shows
    up as changed until its timestamps are refreshed, even when only they did.
    """
    neutral = [
        (f"filter.{name}.{variable}", value)
        for name in configured_filters(repo)
        for variable, value in (("clean", ""), ("process", ""), ("required", "false"))
    ]
    code, out, _ = run_git(STATUS_ARGS, repo, config=neutral)
    if code != 0:
        return None
    return {entry[3:] for entry in out.split("\0") if len(entry) > 3}


def is_dirty(repo: Path) -> bool | None:
    """Whether the tree has anything at all. None when git could not answer."""
    paths = dirty_paths(repo)
    return None if paths is None else bool(paths)


def rev_parse(ref: str, repo: Path) -> str | None:
    """Canonical OID of a commit, or None. Never raises for an unknown ref."""
    if not isinstance(ref, str) or not ref.strip():
        return None
    code, out, _ = run_git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], repo
    )
    oid = out.strip()
    return oid if code == 0 and len(oid) == 40 else None


def is_ancestor(ancestor: str, descendant: str, repo: Path) -> bool:
    code, _, _ = run_git(["merge-base", "--is-ancestor", ancestor, descendant], repo)
    return code == 0


def merge_base(base: str, head: str, repo: Path) -> str | None:
    """The single merge base, or None when there is none or more than one.

    Several merge bases leave no defined review scope, which disensor treats
    as undecidable. Picking one silently would invent a scope.
    """
    code, out, _ = run_git(["merge-base", "--all", base, head], repo)
    if code != 0:
        return None
    bases = [line.strip() for line in out.splitlines() if line.strip()]
    return bases[0] if len(bases) == 1 else None


def default_base(repo: Path) -> str | None:
    """The target branch, resolved in the same order disensor's template uses.

    `origin/main`, then `origin/master`. `origin/HEAD` is deliberately not in
    the list: it can point at the default branch while the real target is a
    release branch, and the policy the gate applies is read from the tip of
    whatever base is chosen, so picking the wrong one changes the verdict.
    """
    for candidate in ("origin/main", "origin/master"):
        if rev_parse(candidate, repo):
            return candidate
    return None


def changed_paths(a: str, b: str, repo: Path) -> set[str] | None:
    """Paths whose entry differs between a and b, as disensor's gitctx computes them."""
    code, out, _ = run_git(["diff", "--no-renames", "--name-only", "-z", f"{a}..{b}"], repo)
    if code != 0:
        return None
    return set(_fields(out))


def canonical_root(raw) -> str | None:
    """The evidence root as the gate reads it, or None if the gate would refuse it.

    Same rules as `gate.canonical_repo_path`: backslashes become slashes, `.`
    segments go, and an absolute path, a drive letter, pathspec magic, `..` or
    the repository root itself are not an evidence root. Without this,
    `--directory ./.residue` matched no path git reports and every declaration
    looked absent.
    """
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).replace("\\", "/").strip()
    if ":" in text:
        return None
    pure = PurePosixPath(text)
    if pure.is_absolute():
        return None
    parts = [p for p in pure.parts if p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _under_root(rev: str, root: str, repo: Path, recursive: bool) -> dict[str, tuple[str, str]]:
    """path -> (mode, type) of the entries under the evidence root at rev.

    Recursive listings include the trees themselves (`-t`): the gate asks
    whether the PATH existed at the merge base, and a directory named
    `<id>.json` that a PR replaces with a declaration existed as a tree. Listing
    only blobs made that replacement look like a brand-new declaration.
    """
    args = ["ls-tree", "-z", "--full-tree"]
    if recursive:
        args += ["-r", "-t"]
    code, out, _ = run_git([*args, rev, "--", f"{root}/"], repo)
    if code != 0:
        return {}
    found: dict[str, tuple[str, str]] = {}
    for field in _fields(out):
        meta, _, path = field.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and path:
            found[path] = (parts[0], parts[1])
    return found


@dataclass(frozen=True)
class Range:
    """A PR's range split the way the gate splits it."""

    merge_base: str
    changed: frozenset[str]
    ordinary: frozenset[str]
    declarations: tuple[str, ...]


def pr_range(base: str, head: str, repo: Path, evidence_root: str) -> Range | None:
    """merge_base..head, classified like `gate.classify_changes`.

    Under the evidence root, a path that already existed at the merge base is
    a mutation of history (G8): neither code nor a new declaration. A new
    immediate `.json` regular blob is a declaration. Anything else there, a
    nested file, a symlink, a gitlink, is ordinary content, because otherwise
    the evidence directory would be a place to park code where nobody looks.
    """
    root = canonical_root(evidence_root)
    if root is None:
        return None
    mb = merge_base(base, head, repo)
    if mb is None:
        return None
    changed = changed_paths(mb, head, repo)
    if changed is None:
        return None
    prefix = f"{root}/"
    under = sorted(p for p in changed if p.startswith(prefix))
    existed = set(_under_root(mb, root, repo, recursive=True)) if under else set()
    at_head = _under_root(head, root, repo, recursive=False) if under else {}

    ordinary = {p for p in changed if not p.startswith(prefix)}
    declarations: list[str] = []
    for path in under:
        if path in existed:
            continue
        relative = path[len(prefix):]
        entry = at_head.get(path)
        if (
            "/" not in relative
            and relative.endswith(".json")
            and entry is not None
            and entry[1] == "blob"
            and entry[0] in REGULAR_BLOB_MODES
        ):
            declarations.append(path)
        else:
            ordinary.add(path)
    return Range(mb, frozenset(changed), frozenset(ordinary), tuple(declarations))


# Lo que puede pasar con las declaraciones de un rango.
NO_DECLARATION = "none"
COVERED = "covered"
STALE = "stale"


def _event(head: str, path: str, repo: Path) -> dict | None:
    code, text, _ = run_git(["show", f"{head}:{path}"], repo)
    if code != 0:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    event = data.get("event") if isinstance(data, dict) else None
    return event if isinstance(event, dict) else None


def coverage(
    rng: Range, head: str, repo: Path, accepted_gates: tuple[str, ...] | None = None,
) -> str:
    """Whether some declaration of the range is a G7 witness of the final tree.

    A witness declares a gate the policy accepts for the range, passes the
    range checks of G5 (its reviewed commit descends from the merge base and
    reaches the head, and a diff review names the merge base as its base), and
    reviewed a commit after which no ordinary path of the PR changed. Only the
    fields those checks need are read; the artifact is not validated, which is
    the gate's job.

    A declaration that fails any check covers nothing. The gate would reject it
    too, and for an advisory notice the cheaper mistake is to say so.
    """
    if not rng.declarations:
        return NO_DECLARATION
    for path in rng.declarations:
        event = _event(head, path, repo)
        if event is None:
            continue
        gate = event.get("gate")
        if gate not in GATES:
            continue
        if accepted_gates is not None and gate not in accepted_gates:
            continue
        reviewed = rev_parse(event.get("head_commit"), repo)
        if not reviewed:
            continue
        if not is_ancestor(rng.merge_base, reviewed, repo) or not is_ancestor(reviewed, head, repo):
            continue
        if gate == "diff" and rev_parse(event.get("base_commit"), repo) != rng.merge_base:
            continue
        after = changed_paths(reviewed, head, repo)
        if after is None:
            continue
        if not (rng.ordinary & after):
            return COVERED
    return STALE


def declaration_coverage(
    base: str, head: str, repo: Path, evidence_root: str,
    accepted_gates: tuple[str, ...] | None = None,
) -> str:
    """pr_range and coverage in one call, for callers that need only the verdict."""
    rng = pr_range(base, head, repo, evidence_root)
    if rng is None:
        return NO_DECLARATION
    return coverage(rng, head, repo, accepted_gates)


def inside(path: Path, repo: Path) -> bool:
    try:
        path.resolve().relative_to(repo.resolve())
        return True
    except (ValueError, OSError):
        return False
