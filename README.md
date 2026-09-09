# disensor-cursor

Cursor hooks that remind the agent to close the adversarial round, so nobody
has to remember to run it.

It runs no models and handles no API keys. It asks the `disensor` CLI whether
the changes need a review, and reports what it got back. Every decision that
sends code anywhere stays with the owner.

> **This is a pilot and carries no stability promise.** Flags, messages and
> behaviour can change between commits. It is not published on PyPI, and it is
> not part of disensor: it consumes disensor's CLI the way any third party
> would.

## Install

Needs [disensor](https://github.com/NicolasRocchia/disensor) installed and on
PATH.

```bash
pip install git+https://github.com/NicolasRocchia/disensor-cursor
disensor-cursor init
```

`init` writes to `~/.cursor/hooks.json`. It touches no repository, and the
hooks stay inert everywhere except inside repositories whose root holds a
`disensor.config.json`.

Check what it can see from where you are:

```bash
disensor-cursor doctor
```

## What it does

Three hooks, all advisory:

| Hook | What it does |
|---|---|
| `sessionStart` | Decides whether this session is eligible at all, and tells the agent the method exists. |
| `afterFileEdit` | Records which paths this session touched, and with which model. Metadata only. |
| `stop` | If the session touched code that still owes a review, adds a notice about it. |

When the notice fires, the agent gets told what the state is and asked to
consult you. It never gets told to commit, and never gets told to run a round:
both of those are yours to authorise.

## Why it installs in your home and not in the repository

Cursor also supports project hooks, in `.cursor/hooks.json`. That file is
executable code, versioned in the repository, that Cursor runs on its own in a
trusted workspace, and a pull request can change what it runs.

disensor keeps its reviewer registry outside every repository for exactly that
reason, and says why in its own source: *"The commands live on the machine,
never in the repository. A reviewer entry is executable code: if a pull request
could add one, opening that pull request would run arbitrary commands on the
machine of whoever reviews it."* An adapter for disensor should not be the
thing that lowers that standard.

`disensor-cursor init --project` is available for when you want the hooks to
travel with the repository. It prints what that costs.

## What it deliberately does not do

- **It does not run models.** `disensor round` does, when you ask it to.
- **It does not block.** Every hook is `failClosed: false`, and the executable
  always exits 0. Cursor reads exit code 2 as "block the action" no matter what
  `failClosed` says, so an advisory tool must never be able to produce one.
- **It does not guess the generator.** If the model Cursor reports is not in
  `families.json`, nothing is prefilled and you declare it. A wrong family
  would make disensor's R4 certify a decorrelation that never happened.
- **It does not write inside your repository.** State lives in
  `~/.disensor-cursor/`. The hook asks for a clean tree, so writing its own
  bookkeeping into the tree would dirty exactly what it just asked for.
- **It does not keep your code.** The edit log records timestamp, path and
  model. Cursor also sends the before and after text of every edit, and that
  is dropped.

## Known limits

- **"Needs a review" is not "has no review yet".** disensor answers the first
  question; the second one is only computed inside its CI gate. The adapter
  approximates it by looking for a declaration added in the same range, and
  errs towards staying quiet.
- **The model reported by Cursor is provenance, not proof.** A session can
  switch models, and a commit range can hold work from other hands. When the
  session's edits do not agree on one model, nothing is prefilled.
- **The family table is incomplete on purpose.** Cursor publishes display
  names, not stable model identifiers, so `families.json` is maintained by
  hand and is always behind.
- **Four vendors have nowhere to go.** disensor's family enum is
  `anthropic, openai, google, meta, mistral, other`. Grok, Kimi, GLM and
  Cursor's own Composer models all collapse into `other`, where R4 would read
  two unrelated vendors as correlated. They are left unmapped instead.

## Verified against a live Cursor

Run on Windows 11, Cursor with a project-level install, agent mode.

- **The payload arrives with a UTF-8 BOM.** `json.loads` rejects it, so every
  hook silently did nothing until the decode moved to `utf-8-sig`. The hooks
  documentation does not mention it. This is the reason the adapter logs why
  it stayed quiet: without that line, the failure looked exactly like "the
  workspace is not trusted" and would have been debugged in the wrong place.
- **The full chain works**, both notices. `sessionStart` admitted an agent
  session, `afterFileEdit` recorded `src/app.py` with the model that ran
  (`grok-4.6`) and none of the edit text, and `stop` produced first the dirty
  tree notice and later, after the work was committed, the round one.
- **The notice is not read as authorisation.** Faced with a dirty tree, the
  agent answered "there are uncommitted changes, I will look at what they are
  so I can tell you what is pending", asked for approval to run a read-only
  `git status`, and did not offer to commit. On the round notice it answered
  "the notice is from the disensor hook, not a request from you. Before running
  a round I need your OK and which model wrote the code", and went to read
  `disensor guide`. That is the behaviour the wording is built for.
- **Cutting off by reason, not by count, is what made the second notice
  possible.** A single per-conversation counter would have been spent by the
  dirty tree and would have silenced the round notice, which was the one worth
  sending.
- **The conservative prefill held on a real model.** `grok-4.6` is
  deliberately unmapped, so the emitted command carried no
  `--generator-family`, said the model is not in the table, and told the agent
  to ask rather than guess. The agent did exactly that.
- **The notice carries resolved OIDs**, not `HEAD` or `origin/main`.
- **Project hooks ran with no additional trust prompt** on a folder opened for
  the first time. Not conclusive on a clean machine, since Cursor may inherit
  trust from a parent directory that was already trusted, but it is consistent
  with the risk described above: a cloned repository carrying
  `.cursor/hooks.json` can run those commands on the machine that opens it.
  It is why `--global` is the default here.

## Still to verify

1. Whether the `env` returned by `sessionStart` reaches later hooks, given that
   `sessionStart` is documented as fire-and-forget. The adapter does not rely
   on it (the state file is the source of truth), but the answer changes how
   fast eligibility can be known.
2. Which shell runs the `command` string on Windows. Absolute paths without
   spaces work; quoting has not been exercised.
3. Whether a `hooks.json` that was already accepted is re-approved when it
   changes.
4. `model_id` came back null in every payload seen, with only the legacy
   `model` slug populated. Whether it is ever set, and what it looks like when
   it is, is unknown.
5. Everything above was seen at project level. The user-level install, which is
   the default, has not been exercised end to end.

## Licence

MIT.
