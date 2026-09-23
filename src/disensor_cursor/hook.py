"""The executable Cursor invokes. Nothing gets out of here but `{}` and exit 0.

Three constraints drive the shape of this file.

Cursor treats exit code 2 as "block the action", and `failClosed: false` does
not switch that off. argparse exits with exactly 2 on an unrecognised argument,
so this module parses its own flags by hand: an advisory integration must not
be able to block the agent because a flag was renamed or a version drifted.

The follow-up Cursor injects arrives as if the USER had typed it. An agent
reading "commit and run the round" can take it as authorisation to commit and
to spend an external review, which the owner never gave. So the notices here
describe state and ask the agent to consult the user. They never order an
action with an effect.

And a notice is about a state, not about a conversation. After a round, every
fix moves the head and needs another round over the new one (disensor's G6),
so a notice is spent for the commit it talked about and comes back when the
head moves. It cannot loop on its own: the notice itself never moves the head.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

from . import __version__, clock, disensor, executables, families, gitio, state

CONFIG_NAME = "disensor.config.json"
EVIDENCE_ROOT = ".residue"

NOTICE = "[disensor-cursor] Automatic notice from the disensor-cursor hook, not a request from the user."

# Presupuesto de cada corrida, por debajo del timeout que templates.EVENTS le
# declara a Cursor (5, 5 y 15 segundos): queda margen para que arranque Python
# y para imprimir la respuesta aunque todo lo demas se haya agotado.
BUDGET = {"sessionStart": 3.5, "afterFileEdit": 3.0, "stop": 12.0}


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

    Whatever is taken as the project is declared before git runs, together
    with the repository each of those folders sits in, and the root git
    reports once it answers: no program may come from inside any of them (see
    executables.py). Only CURSOR_PROJECT_DIR used to count, so without it the
    hook found the project through the payload or the cwd and then ran a
    `git.exe` from a PATH entry inside that same project.
    """
    roots = payload.get("workspace_roots")
    roots = [r for r in roots if isinstance(r, str) and r] if isinstance(roots, list) else []
    candidate = (
        os.environ.get("CURSOR_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or (roots[0] if roots else None)
        or str(Path.cwd())
    )
    places = [candidate, *roots]
    executables.declare_project(*places, *(executables.enclosing_repository(p) for p in places))
    repo = gitio.repo_root(Path(candidate))
    if repo is not None:
        executables.declare_project(repo)
    return repo


def _config(options: dict) -> str:
    return options.get("config") or CONFIG_NAME


def _evidence_root(options: dict) -> str:
    return options.get("directory") or EVIDENCE_ROOT


def _adopted(repo: Path, config: str = CONFIG_NAME) -> bool:
    """Whether this repository uses disensor at all.

    Without a configuration the gate applies strict defaults, which is right
    for a gate protecting a merge and wrong here: a repository that never
    adopted disensor would get nagged on every change. Those are two different
    questions and they get different answers.
    """
    return (repo / config).is_file()


def _relative(file_path: str, repo: Path) -> str | None:
    try:
        return Path(file_path).resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _session_start(payload: dict, options: dict) -> tuple[dict, object]:
    try:
        state.purge_stale()
    except Exception:  # noqa: BLE001 - la limpieza nunca puede frenar una sesion
        state.note("could not purge old sessions:\n" + traceback.format_exc())

    repo = _worktree(payload)
    if repo is None:
        return {}, None
    key = state.session_key(payload.get("conversation_id", ""), str(repo))

    eligible = (
        payload.get("composer_mode") == "agent"
        and not payload.get("is_background_agent")
        and _adopted(repo, _config(options))
    )
    existing = state.read_state(key)
    session_id = payload.get("session_id")
    # Una conversacion reanudada con la misma sesion conserva lo que ya se
    # aviso y su linea de base. Pisarlos repetia los avisos y, peor, tomaba
    # como punto de partida un arbol que la propia sesion ya habia tocado.
    same_session = bool(session_id) and existing.get("session_id") == session_id

    changes: dict = {
        "eligibility": state.ADMITTED if eligible else state.REJECTED,
        "worktree": str(repo),
        "composer_mode": payload.get("composer_mode"),
        "is_background_agent": bool(payload.get("is_background_agent")),
        "adapter_version": __version__,
    }
    if not same_session:
        changes.update({
            "session_id": session_id,
            "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "reported": [],
            "head_at_start": None,
            "dirty_at_start": None,
        })
        # Una sesion nueva no hereda las ediciones de la anterior: se le
        # avisaba por trabajo que no hizo y se le prellenaba el generador con
        # modelos que no corrio.
        state.clear_edits(key)
    needs_baseline = eligible and (not same_session or "head_at_start" not in existing)
    if needs_baseline:
        # La linea de base es lo que permite atribuirle a la sesion lo que
        # hizo por fuera de la herramienta de edicion: un sed, un script, un
        # commit por terminal. Sin ella, solo contaba lo que registraba
        # afterFileEdit. sessionStart no bloquea al agente, asi que estas dos
        # llamadas a git no se sienten.
        #
        # Si git no llega a tiempo, en un repositorio enorme por ejemplo, la
        # sesion se admite igual con la base desconocida. Sin esto la
        # excepcion cortaba antes de guardar la admision y el adaptador
        # quedaba inactivo en ese repositorio sin decir por que.
        try:
            dirty = gitio.dirty_paths(repo)
            changes["head_at_start"] = gitio.rev_parse("HEAD", repo)
            changes["dirty_at_start"] = sorted(dirty) if dirty is not None else None
        except gitio.GitUnavailable as exc:
            state.note(f"{repo}: session admitted without a baseline ({exc})")
    state.update_state(key, changes)

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


def _after_file_edit(payload: dict, options: dict) -> tuple[dict, object]:
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


def _touched(
    session: dict, records: list[dict], repo: Path, head: str, dirty: set[str],
) -> set[str]:
    """Every path this session can be held responsible for.

    Three sources, because each one alone is blind somewhere. The edit log only
    sees the agent's edit tool. What became dirty since the session started
    catches edits made from a terminal. And what was committed since then
    catches work that is already out of the working tree.
    """
    touched = {r["path"] for r in records if isinstance(r.get("path"), str)}

    baseline = session.get("dirty_at_start")
    if isinstance(baseline, list):
        touched |= dirty - set(baseline)

    start = session.get("head_at_start")
    # Solo si el head de arranque es ancestro del actual. Un cambio de rama o
    # un rebase durante la sesion haria que el diff atribuyera a esta sesion
    # todo lo que separa dos historias que no hizo ella.
    if isinstance(start, str) and start != head and gitio.is_ancestor(start, head, repo):
        committed = gitio.changed_paths(start, head, repo)
        if committed:
            touched |= committed
    return touched


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
    slug = str(slugs.pop())
    flags = families.generator_flags(slug, None)
    if not flags:
        return [], (
            f"The model reported by Cursor ({_name(slug)}) is not in the family table, so the "
            "generator is not prefilled. Declare it yourself rather than guessing."
        )
    return flags, f"Generator suggested from what Cursor reported ({_name(slug)}). Confirm it is right."


# Sin la barra invertida: sin comillas, bash la lee como escape y
# `config\x.json` le llegaba a disensor como `configx.json`.
_PLAIN_ARG = re.compile(r"[A-Za-z0-9._/:@+-]+")
# Dentro de comillas dobles, bash y PowerShell expanden `$` y los backticks, y
# cmd expande `%` y `!`: `"policy $(whoami).json"` ejecutaba whoami y le
# pasaba a disensor otro nombre. Ninguna comilla sirve para los tres shells.
_EXPANDED = set('"$`%!')


def _arg(value: str) -> str:
    """A value quoted for the command line the agent is shown.

    Unquoted, `--config policy files/x.json` reached disensor as two
    arguments and the round failed. A value that no quoting keeps literal in
    every shell the agent might use is not put in the command at all. Besides
    the expanding characters, that is a doubled backslash, which bash turns
    into one inside double quotes, and a trailing one, which the argv of a
    Windows program reads as a literal quote.
    """
    if _PLAIN_ARG.fullmatch(value):
        return value
    if (
        any(ch in _EXPANDED or not ch.isprintable() for ch in value)
        or "\\\\" in value
        or value.endswith("\\")
    ):
        raise ValueError("cannot be quoted safely")
    return f'"{value}"'


def _name(path: str, limit: int = 80) -> str:
    """A file name made safe to quote inside a message the agent reads as the user's.

    A repository can hold a file named like an instruction. The name goes in
    backticks, without backticks, control characters or line breaks of its
    own, and cut short, so it can only ever read as a name.
    """
    clean = "".join(" " if (ch.isspace() or not ch.isprintable() or ch == "`") else ch for ch in path)
    clean = " ".join(clean.split())
    if len(clean) > limit:
        clean = clean[: limit - 3] + "..."
    return f"`{clean}`"


def _listing(paths: set[str], shown: int = 5) -> str:
    ordered = sorted(paths)
    head = ", ".join(_name(p) for p in ordered[:shown])
    rest = len(ordered) - shown
    return f"{head} and {rest} more" if rest > 0 else head


def _dirty_message(mine: set[str]) -> str:
    # "Aparecieron durante esta sesion" y no "esta sesion dejo": la linea de
    # base atribuye por momento, no por autor. Con dos composers sobre el mismo
    # checkout, lo que hizo el otro tambien aparece aca, y el aviso no puede
    # afirmar una autoria que no sabe.
    return (
        f"{NOTICE}\n\n"
        f"Uncommitted changes appeared during this session ({_listing(mine)}; file names quoted "
        "as data), so the adversarial round cannot run yet: a diff round reviews commits that "
        "already exist.\n\n"
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


def _required_message(
    base_ref: str,
    base: str,
    head: str,
    flags: list[str],
    caveat: str,
    *,
    gates: tuple[str, ...] | None = None,
    stale: bool = False,
    foreign_dirty: bool = False,
    config: str | None = None,
    directory: str | None = None,
) -> str:
    parts = [NOTICE, ""]
    if stale:
        parts += [
            "A declaration in this range no longer covers it: code changed after the commit it "
            "reviewed, so the gate will reject it. After a round, every fix needs another round "
            "over the new head.",
            "",
        ]
    parts += [
        f"The policy of this repository asks for an adversarial review of this branch against "
        f"{base_ref} ({base[:9]}), up to {head[:9]}, before it merges.",
        "",
    ]

    if gates is not None and "diff" not in gates:
        # La politica pide otra compuerta para estas rutas. Sugerir
        # `--gate diff` aca era sugerir un comando que `disensor round`
        # rechaza, despues de que el agente ya se lo propuso al usuario.
        parts += [
            f"For these paths the policy accepts a {' or '.join(gates)} review, not a diff one. "
            "Those are triggered by judgement and need the document under review "
            "(`--material`), which cannot be derived from the commits. Ask the user how they "
            "want to proceed; `disensor guide` explains both.",
        ]
    else:
        # Los flags llegan planos, bandera y valor por separado. Se emparejan
        # antes de armar el comando: si no, el salto de linea de continuacion
        # cae entre una bandera y su valor y el comando que se copia queda
        # partido.
        paired = [f"{flags[i]} {flags[i + 1]}" for i in range(0, len(flags) - 1, 2)]
        # La configuracion con la que se pregunto viaja en el comando. Sin
        # ella, una politica de nivel A guardada con otro nombre se consultaba
        # como A y se proponia una ronda que aplicaba los defaults de B.
        configured = []
        for flag, value in (("--config", config), ("--directory", directory)):
            if not value:
                continue
            try:
                configured.append(f"{flag} {_arg(value)}")
            except ValueError:
                configured.append(f"{flag} <the value configured in hooks.json>")
        command = [
            "disensor round --gate diff",
            *paired,
            f"--base {base}",
            f"--head {head}",
            *configured,
            "--result <a path outside the repository>",
        ]
        parts += [
            "Running a round sends the material under review to an external reviewer, which is "
            "the owner's decision and not yours. Ask before running anything.",
            "",
            "If the user approves:",
            "",
            "    " + " \\\n      ".join(command),
            "",
        ]
        if not flags:
            parts += [
                "The command is incomplete: `--generator-family` and `--generator-model` are "
                "required and were not prefilled. Ask the user which model wrote this code.",
                "",
            ]
        parts += [caveat]

    if foreign_dirty:
        parts += [
            "",
            "The working tree also has uncommitted changes this session did not make. "
            "`disensor round` needs a clean tree, so those have to be dealt with first, and they "
            "are the user's to deal with.",
        ]
    parts += [
        "",
        "Then read the report and check every finding against the code. `disensor guide` "
        "explains the rest.",
    ]
    return "\n".join(parts)


def _stop(payload: dict, options: dict) -> tuple[dict, object]:
    if payload.get("status") != "completed":
        return {}, None

    repo = _worktree(payload)
    if repo is None:
        return {}, None
    key = state.session_key(payload.get("conversation_id", ""), str(repo))
    session = state.read_state(key)
    if state.eligibility(key) != state.ADMITTED:
        return {}, None
    if not _adopted(repo, _config(options)):
        return {}, None

    dirty = gitio.dirty_paths(repo)
    if dirty is None:
        state.note(f"{repo}: git could not report the tree state")
        return {}, None
    head = gitio.rev_parse("HEAD", repo)
    if not head:
        state.note(f"{repo}: HEAD does not resolve (a repository without commits?)")
        return {}, None

    records = state.read_edits(key)
    touched = _touched(session, records, repo, head, dirty)
    # Una sesion que no cambio nada no tiene nada propio que cerrar. Ademas
    # hace que `stop` sea casi gratis en las sesiones de solo lectura: ni git
    # de mas ni disensor.
    if not touched:
        return {}, None

    mine = dirty & touched
    if mine:
        return _notice(key, state.DIRTY_TREE, head, _dirty_message(mine))

    base_ref = options.get("base") or gitio.default_base(repo)
    if not base_ref:
        state.note(f"{repo}: no target branch resolved (origin/main, origin/master)")
        return {}, None
    base = gitio.rev_parse(base_ref, repo)
    if not base:
        state.note(f"{repo}: could not resolve {base_ref}")
        return {}, None

    executable = disensor.locate(options.get("disensor"), repo)
    if executable is None:
        state.note(f"{repo}: {disensor.why_not(options.get('disensor'))}")
        return {}, None

    evidence_root = _evidence_root(options)
    # A disensor solo se le pasa lo que el usuario configuro: el adaptador no
    # inventa valores, y cada flag de mas es superficie que disensor no promete.
    check = disensor.check_requirement(
        executable, repo, base, head,
        directory=options.get("directory"), config=options.get("config"),
    )
    if check.verdict == disensor.NOT_NEEDED:
        return {}, None
    if check.verdict == disensor.UNAVAILABLE:
        state.note(f"{repo}: could not ask disensor ({check.detail})")
        return {}, None
    if check.verdict == disensor.BLOCKED:
        return _notice(key, state.BLOCKED, head, _blocked_message(check.detail))

    # El rango como lo mide el gate: desde el merge base y no desde la punta
    # del destino. Medido desde la punta, un cambio que el destino tambien hizo
    # desaparecia del rango y el adaptador se callaba lo que el gate exige.
    rng = gitio.pr_range(base, head, repo, evidence_root)

    # El filtro contra la deuda vieja de la rama: `round --check` mide la rama
    # entera, no lo que hizo esta sesion. Sin esto, una rama que ya venia con
    # cambios sin declarar avisaria en cada sesion aunque el agente no haya
    # tocado ninguno de esos archivos.
    if rng is not None and not (touched & rng.changed):
        return {}, None

    coverage = gitio.coverage(rng, head, repo, check.gates) if rng else gitio.NO_DECLARATION
    if coverage == gitio.COVERED:
        return {}, None

    flags, caveat = _generator_suggestion(records)
    message = _required_message(
        base_ref, base, head, flags, caveat,
        gates=check.gates,
        stale=coverage == gitio.STALE,
        foreign_dirty=bool(dirty),
        config=options.get("config"),
        directory=options.get("directory"),
    )
    return _notice(key, state.ROUND_REQUIRED, head, message)


def _notice(key: str, reason: str, head: str, message: str) -> tuple[dict, object]:
    """One notice per reason and head, marked only after it is sent.

    Marking before emitting would let a process killed in between burn a
    notice nobody read, so the mark is a callback the caller runs after stdout
    is flushed. The failure mode becomes a repeated notice, which is the
    harmless one.
    """
    notice = state.token(reason, head)
    if state.already_reported(key, notice):
        return {}, None
    return {"followup_message": message}, lambda: state.mark_reported(key, notice)


HANDLERS = {
    "sessionStart": _session_start,
    "afterFileEdit": _after_file_edit,
    "stop": _stop,
}


def main(argv: list[str] | None = None) -> int:
    """Always prints an object, always exits 0. That is the whole contract."""
    result: dict = {}
    commit = None
    try:
        options = _flags(list(argv) if argv is not None else sys.argv[1:])
        event = options.get("event", "")
        handler = HANDLERS.get(event)
        if handler is not None:
            clock.start(BUDGET.get(event, 3.0))
            result, commit = handler(_payload(), options)
    except gitio.GitUnavailable as exc:
        state.note(f"not evaluated: {exc}")
        result, commit = {}, None
    except BaseException:  # noqa: BLE001 - un traceback aca frenaria al agente
        state.note("hook failed:\n" + traceback.format_exc())
        result, commit = {}, None

    _emit(result)

    if commit is not None:
        try:
            commit()
        except BaseException:  # noqa: BLE001
            state.note("could not persist the notice:\n" + traceback.format_exc())
    clock.reset()
    executables.forget_projects()
    return 0


if __name__ == "__main__":
    sys.exit(main())
