"""The executable Cursor invokes. Nothing gets out of here but `{}` and exit 0.

Two constraints drive the shape of this file.

Cursor treats exit code 2 as "block the action", and `failClosed: false` does
not switch that off. argparse exits with exactly 2 on an unrecognised argument,
so this module parses its own flags by hand: an advisory integration must not
be able to block the agent because a flag was renamed or a version drifted.

And the follow-up Cursor injects arrives as if the USER had typed it. An agent
reading "commit and run the round" can take it as authorisation to commit and
to spend an external review, which the owner never gave. So the notices here
describe state and ask the agent to consult the user. They never order an
action with an effect.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import __version__, disensor, families, gitio, state

CONFIG_NAME = "disensor.config.json"
EVIDENCE_ROOT = ".residue"

NOTICE = "[disensor-cursor] Automatic notice from the disensor-cursor hook, not a request from the user."


def _flags(argv: list[str]) -> dict[str, str]:
    """A hand-rolled parser, because argparse exits with 2 and 2 means block."""
    found: dict[str, str] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            found[token[2:]] = argv[index + 1]
            index += 2
        else:
            index += 1
    return found


def _payload() -> dict:
    """The event Cursor sent, or an empty one, with the reason recorded.

    An unreadable payload has to leave a trace. Staying quiet is the right
    behaviour and being quiet for an unexplained reason is not: from outside,
    a hook that answers `{}` because the JSON was malformed looks exactly like
    one that answered `{}` because there was nothing to say.
    """
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError) as exc:
        state.note(f"could not read the payload: {exc}")
        return {}
    if not raw:
        return {}
    try:
        # `utf-8-sig` y no `utf-8`: Cursor manda el payload CON BOM en Windows,
        # y json.loads rechaza el BOM. Se comprobo corriendo de verdad, no
        # leyendo la documentacion, que no lo menciona. Sin BOM se comporta
        # igual que utf-8, asi que cubre las dos formas.
        data = json.loads(raw.decode("utf-8-sig", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        state.note(f"payload is not valid JSON ({exc}); {len(raw)} bytes received")
        return {}
    if not isinstance(data, dict):
        state.note(f"payload is a {type(data).__name__}, not an object")
        return {}
    return data


def _emit(result: dict) -> None:
    """UTF-8 bytes, always. On Windows stdout can be a local code page."""
    try:
        data = (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(data.decode("utf-8"))
        else:
            buffer.write(data)
            buffer.flush()
    except Exception:  # noqa: BLE001 - emitir es lo ultimo que puede fallar
        pass


def _worktree(payload: dict) -> Path | None:
    """The project this hook is talking about.

    User-level hooks run with `~/.cursor` as their cwd, so the project has to
    come from the environment Cursor sets, not from where the process happens
    to be standing.
    """
    for candidate in (
        os.environ.get("CURSOR_PROJECT_DIR"),
        os.environ.get("CLAUDE_PROJECT_DIR"),
    ):
        if candidate:
            return gitio.repo_root(Path(candidate))
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return gitio.repo_root(Path(roots[0]))
    return gitio.repo_root(Path.cwd())


def _adopted(repo: Path) -> bool:
    """Whether this repository uses disensor at all.

    Without a configuration the gate applies strict defaults, which is right
    for a gate protecting a merge and wrong here: a repository that never
    adopted disensor would get nagged on every change. Those are two different
    questions and they get different answers.
    """
    return (repo / CONFIG_NAME).is_file()


def _relative(file_path: str, repo: Path) -> str | None:
    try:
        return Path(file_path).resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _session_start(payload: dict) -> tuple[dict, object]:
    repo = _worktree(payload)
    if repo is None:
        return {}, None
    key = state.session_key(payload.get("conversation_id", ""), str(repo))

    eligible = (
        payload.get("composer_mode") == "agent"
        and not payload.get("is_background_agent")
        and _adopted(repo)
    )
    state.update_state(key, {
        "eligibility": state.ADMITTED if eligible else state.REJECTED,
        "worktree": str(repo),
        "session_id": payload.get("session_id"),
        "composer_mode": payload.get("composer_mode"),
        "is_background_agent": bool(payload.get("is_background_agent")),
        "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generated_by": __version__,
        "reported": [],
    })
    if not eligible:
        return {}, None
    return {
        "additional_context": (
            "This repository uses disensor. Before the changes of this session merge, they are "
            "attacked by a model from another family and whatever stays open is declared. "
            "`disensor guide` prints the procedure. Running a round sends the material under "
            "review to an external reviewer, so ask the user before doing it."
        )
    }, None


def _after_file_edit(payload: dict) -> tuple[dict, object]:
    repo = _worktree(payload)
    if repo is None:
        return {}, None
    key = state.session_key(payload.get("conversation_id", ""), str(repo))
    if state.eligibility(key) != state.ADMITTED:
        return {}, None
    relative = _relative(str(payload.get("file_path") or ""), repo)
    if not relative:
        return {}, None
    # Solo metadatos. `edits` trae old_string y new_string, y guardarlos
    # retendria codigo y cualquier secreto que llevara, fuera del repositorio y
    # sin vencimiento, para responder una pregunta que la ruta ya responde.
    state.append_edit(key, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "path": relative,
        "model": payload.get("model"),
        "model_id": payload.get("model_id"),
    })
    return {}, None


def _generator_suggestion(records: list[dict]) -> tuple[list[str], str]:
    """Flags to prefill, and the caveat that goes with them.

    A session can switch models and a range can hold work from other hands, so
    this is provenance, not proof. When the edits do not agree on one model,
    nothing is suggested: a wrong generator would make R4 certify a
    decorrelation that never happened.
    """
    slugs = {
        (r.get("model_id") or r.get("model"))
        for r in records
        if (r.get("model_id") or r.get("model"))
    }
    if len(slugs) != 1:
        if len(slugs) > 1:
            return [], (
                "More than one model edited files in this session, so the generator is not "
                "prefilled. Declare it yourself."
            )
        return [], "The generator is not prefilled: no model was observed."
    slug = slugs.pop()
    flags = families.generator_flags(str(slug), None)
    if not flags:
        return [], (
            f"The model reported by Cursor ({slug}) is not in the family table, so the "
            "generator is not prefilled. Declare it yourself rather than guessing."
        )
    return flags, f"Generator suggested from what Cursor reported ({slug}). Confirm it is right."


def _dirty_message() -> str:
    return (
        f"{NOTICE}\n\n"
        "The working tree has uncommitted changes, so the adversarial round cannot run yet: a "
        "diff round reviews commits that already exist.\n\n"
        "Do not commit on your own initiative. Ask the user what they want to do with the "
        "pending changes, and tell them a round needs a clean tree."
    )


def _blocked_message(detail: str) -> str:
    return (
        f"{NOTICE}\n\n"
        "disensor could not decide whether this range needs a review, so nothing should be "
        f"assumed either way.\n\n{detail or 'no detail reported'}\n\n"
        "Report this to the user. Do not treat it as permission to skip the review."
    )


def _required_message(base: str, head: str, flags: list[str], caveat: str) -> str:
    # Los flags llegan planos, bandera y valor por separado. Se emparejan antes
    # de armar el comando: si no, el salto de linea de continuacion cae entre
    # una bandera y su valor y el comando que se copia queda partido.
    paired = [f"{flags[i]} {flags[i + 1]}" for i in range(0, len(flags) - 1, 2)]
    command = [
        "disensor round --gate diff",
        *paired,
        f"--base {base}",
        f"--head {head}",
        "--result <a path outside the repository>",
    ]
    missing = "" if flags else (
        "\nThe command is incomplete: `--generator-family` and `--generator-model` are "
        "required and were not prefilled. Ask the user which model wrote this code.\n"
    )
    return (
        f"{NOTICE}\n\n"
        "The policy of this repository asks for an adversarial review of the changes in "
        f"{base[:9]}..{head[:9]} before they merge.\n\n"
        "Running a round sends the material under review to an external reviewer, which is the "
        "owner's decision and not yours. Ask before running anything.\n\n"
        "If the user approves:\n\n    " + " \\\n      ".join(command) + "\n"
        f"{missing}\n{caveat}\n\n"
        "Then read the report and check every finding against the code. `disensor guide` "
        "explains the rest."
    )


def _stop(payload: dict, options: dict) -> tuple[dict, object]:
    if payload.get("status") != "completed":
        return {}, None

    repo = _worktree(payload)
    if repo is None:
        return {}, None
    key = state.session_key(payload.get("conversation_id", ""), str(repo))
    if state.eligibility(key) != state.ADMITTED:
        return {}, None
    if not _adopted(repo):
        return {}, None

    dirty = gitio.is_dirty(repo)
    if dirty is None:
        state.note(f"{repo}: git could not report the tree state")
        return {}, None
    if dirty:
        return _notice(key, state.DIRTY_TREE, _dirty_message())

    base_ref = options.get("base") or gitio.default_base(repo)
    if not base_ref:
        state.note(f"{repo}: no target branch resolved (origin/main, origin/master)")
        return {}, None
    base = gitio.rev_parse(base_ref, repo)
    head = gitio.rev_parse("HEAD", repo)
    if not base or not head:
        state.note(f"{repo}: could not resolve {base_ref} or HEAD")
        return {}, None

    executable = disensor.locate(options.get("disensor"), repo)
    if executable is None:
        state.note(f"{repo}: disensor not found; run `disensor-cursor doctor`")
        return {}, None

    verdict, detail = disensor.check_requirement(executable, repo, base, head)
    if verdict == disensor.NOT_NEEDED:
        return {}, None
    if verdict == disensor.UNAVAILABLE:
        state.note(f"{repo}: could not ask disensor ({detail})")
        return {}, None
    if verdict == disensor.BLOCKED:
        return _notice(key, state.BLOCKED, _blocked_message(detail))

    # Aproximacion declarada: disensor separa "este rango exige ronda" de "ya
    # hay una declaracion que lo cubre", y solo expone la primera. Ver una
    # declaracion nueva y callarse se equivoca hacia el silencio, que es el
    # lado correcto para equivocarse.
    if gitio.evidence_added(base, head, repo, EVIDENCE_ROOT):
        return {}, None

    # El filtro contra la deuda vieja de la rama: `round --check` mide la rama
    # entera contra la base, no lo que hizo esta sesion. Sin esto, una rama que
    # ya venia con cambios sin declarar avisaria en cada sesion aunque el agente
    # no haya tocado ninguno de esos archivos.
    in_range = gitio.changed_paths(base, head, repo)
    records = state.read_edits(key)
    touched = {r["path"] for r in records if isinstance(r.get("path"), str)}
    if in_range is not None and not (touched & in_range):
        return {}, None

    flags, caveat = _generator_suggestion(records)
    return _notice(key, state.ROUND_REQUIRED, _required_message(base, head, flags, caveat))


def _notice(key: str, reason: str, message: str) -> tuple[dict, object]:
    """One notice per reason, and the reason is marked only after it is sent.

    A single counter for the whole conversation gets spent by the first notice:
    the dirty tree would silence the round that mattered. And marking before
    emitting would let a process killed in between burn a notice nobody read,
    so the mark is a callback the caller runs after stdout is flushed. The
    failure mode becomes a repeated notice, which is the harmless one.
    """
    if state.already_reported(key, reason):
        return {}, None
    return {"followup_message": message}, lambda: state.mark_reported(key, reason)


HANDLERS = {
    "sessionStart": lambda payload, options: _session_start(payload),
    "afterFileEdit": lambda payload, options: _after_file_edit(payload),
    "stop": _stop,
}


def main(argv: list[str] | None = None) -> int:
    """Always prints an object, always exits 0. That is the whole contract."""
    result: dict = {}
    commit = None
    try:
        options = _flags(list(argv) if argv is not None else sys.argv[1:])
        handler = HANDLERS.get(options.get("event", ""))
        if handler is not None:
            result, commit = handler(_payload(), options)
    except BaseException:  # noqa: BLE001 - un traceback aca frenaria al agente
        state.note("hook failed:\n" + traceback.format_exc())
        result, commit = {}, None

    _emit(result)

    if commit is not None:
        try:
            commit()
        except BaseException:  # noqa: BLE001
            state.note("could not persist the notice:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
