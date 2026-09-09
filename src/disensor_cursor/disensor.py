"""The disensor CLI, spoken over subprocess.

The adapter imports nothing from disensor. It talks to the surface disensor's
own README declares stable within a major series: the exit codes of
`disensor round`, the documented flags, and the keys of disensor.config.json.
That is a contract, and an import of internals would not be one.

It also means the adapter keeps working against a disensor it was not built
with, and can say so when it does not.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import gitio

# Los codigos que publica `disensor round`, estables dentro de una serie mayor.
OK = 0
ERROR = 1
NOT_REQUIRED = 3
CHAIN_EXHAUSTED = 4
TREE_MODIFIED = 5
UNDECIDABLE = 6

# Lo que el adaptador concluye, que no es lo mismo que el codigo que leyo.
REQUIRED = "required"
NOT_NEEDED = "not_needed"
BLOCKED = "blocked"
UNAVAILABLE = "unavailable"

DEFAULT_TIMEOUT = 8.0


def locate(hint: str | None = None, repo: Path | None = None) -> Path | None:
    """Absolute path of the disensor executable, or None.

    `hint` is what `disensor-cursor init` resolved and wrote into the hook
    command. Preferring it is not just a shortcut: on Windows with Python 3.10
    and 3.11, `shutil.which` searches the current directory first, and the hook
    runs with the repository as its cwd. A repository shipping its own
    `disensor.cmd` would otherwise choose the program that runs.

    When falling back to the search, anything resolving inside the repository
    under review is refused for the same reason.
    """
    if hint:
        candidate = Path(hint)
        if candidate.is_file():
            return candidate
        return None
    found = shutil.which("disensor")
    if not found:
        return None
    resolved = Path(found)
    if repo is not None and gitio.inside(resolved, repo):
        return None
    return resolved


def _run(executable: Path, args: list[str], cwd: Path, timeout: float) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            [str(executable), *args],
            cwd=str(cwd), capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"disensor did not answer within {timeout}s"
    except OSError as exc:
        return -1, "", f"disensor could not run: {exc}"
    # Bytes y no `text=True`: en Windows el decodificado usa la pagina local y
    # un mensaje con acentos rompe la lectura. disensor tuvo este fallo real en
    # su propio runner.
    return (
        done.returncode,
        (done.stdout or b"").decode("utf-8", "replace"),
        (done.stderr or b"").decode("utf-8", "replace"),
    )


def check_requirement(
    executable: Path,
    repo: Path,
    base: str,
    head: str,
    *,
    directory: str = ".residue",
    config: str = "disensor.config.json",
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, str]:
    """Ask disensor whether this range demands a round. Returns (verdict, detail).

    `--check` returns before the runner touches the tree, the registry or the
    network, so this is safe to call on a dirty tree and costs only git.

    `--generator-family` is required by the CLI and unused by `--check` on a
    diff gate: the classification happens and returns before the runner reads
    it. A placeholder keeps the invocation valid without asserting anything
    about who generated the code, which is what the prefill is for and what
    this call must not decide.
    """
    code, _, err = _run(
        executable,
        [
            "round", "--gate", "diff",
            "--generator-family", "other",
            "--base", base, "--head", head,
            "--directory", directory, "--config", config,
            "--check",
        ],
        repo,
        timeout,
    )
    detail = err.strip().splitlines()[-1] if err.strip() else ""
    if code == OK:
        return REQUIRED, detail
    if code == NOT_REQUIRED:
        return NOT_NEEDED, detail
    if code == UNDECIDABLE:
        return BLOCKED, detail
    # Cualquier otra cosa, incluido un disensor viejo que no entiende un flag y
    # sale con el 2 de argparse, es "no pude preguntar". Nunca se convierte en
    # un aviso, y nunca en un silencio sin registro.
    return UNAVAILABLE, detail or f"disensor exited {code}"


def probe(executable: Path, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Whether this disensor understands what the adapter needs.

    `disensor --version` is not a usable probe: it did not exist before 0.9.5,
    and asking an older build answers with an argparse usage error. Asking the
    help of the subcommand actually used answers the question that matters.
    """
    code, out, err = _run(executable, ["round", "--help"], Path.cwd(), timeout)
    if code != 0:
        return False, (err or out).strip()[:200] or f"exit {code}"
    if "--check" not in out:
        return False, "this disensor has no `round --check`: 0.9.0 or newer is needed"
    return True, out.strip().splitlines()[0] if out.strip() else ""
