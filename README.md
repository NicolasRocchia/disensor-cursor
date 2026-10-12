# disensor-cursor

Cursor hooks that remind the agent to close the adversarial round, so nobody
has to remember to run it.

It runs no models and handles no API keys. It asks the `disensor` CLI whether
the changes need a review, and reports what it got back. Every decision that
sends code anywhere stays with the owner.

> **This is a pilot and carries no stability promise.** Flags, messages and
> behaviour can change between commits. It is not published anywhere, and it
> is not part of disensor: it consumes disensor's CLI the way any third party
> would.

## Install

Needs Python 3.10 or newer, and
[disensor](https://github.com/NicolasRocchia/disensor) 0.9.0 or newer on
PATH: the adapter asks `disensor round --check`, which that release
introduced.

### As a Cursor plugin

The plugin brings a setup skill, not hooks of its own. Put this repository
where Cursor looks for local plugins, and reload Cursor:

```bash
git clone https://github.com/NicolasRocchia/disensor-cursor ~/.cursor/plugins/local/disensor-cursor
```

Then ask the agent to set up disensor. The skill checks Python and disensor,
installs the adapter from the plugin's own folder, runs `disensor-cursor init`
and `disensor-cursor doctor`, and asks before every command that installs or
changes something. On Teams and Enterprise plans an admin can switch local
plugins off ("Allow Local Plugin Imports").

The hooks it installs live in `~/.cursor/hooks.json`, not in the plugin, so
disabling or removing the plugin leaves them running: `disensor-cursor
uninstall` removes them. Why the plugin does not bring them itself is below.

### By hand

From a clone of this repository:

```bash
pip install .
disensor-cursor init
```

Not `pip install -e`: `init` refuses a package imported from a checkout (see
[Nothing from the repository runs](#nothing-from-the-repository-runs)).

`init` writes to `~/.cursor/hooks.json`, next to whatever other hooks are
already there. It touches no repository, and the hooks stay inert everywhere
except inside repositories whose root holds a `disensor.config.json`.

| Command | What it does |
|---|---|
| `disensor-cursor init` | Installs the hooks in your home. `--project` installs them in the current repository instead. `--allow-checkout` installs from a checkout, for developing the adapter. |
| `disensor-cursor doctor` | Says what is installed, **runs** the installed command once to prove it answers, says where it imports the package from, how Cursor would start it and which copies of the plugin are on disk, and flags hooks written by another version or installed twice. |
| `disensor-cursor status` | What the hook decided for each session, and what it could not evaluate and why. |
| `disensor-cursor uninstall` | Removes only the entries `init` added. `--project` for a repository install, `--purge` to also delete the session state. |

## Running the tests

```bash
pip install -e ".[test]"
pytest
```

The tests that launch the real hook command do not depend on the install: the
command names the directory it imports the package from, so it runs from a
clone too. Several tests build a hostile repository and first prove the attack
works without the protection, then that it does not with it; the ones that need
Windows or a real disensor say so when they skip.

## Compatibility with disensor

The adapter depends on a narrow surface of the disensor CLI: a few flags of
`round`, its exit codes, and the family enum. disensor's README declares stable
within a major series the exit codes of `round` and the flags that README
documents, which for `round` are `--gate`, `--generator-family`, `--base`,
`--head` and `--result`.

**`--check`, the flag this whole adapter rests on, is not among them.** It
exists since 0.9.0, but nothing promises it, and neither does anything promise
`--generator-model`, which the notice suggests. So the adapter sends nothing
else that is undocumented unless you configure it, and compatibility is
checked, not assumed: `tests/test_contract.py` runs the real executable against
real repositories, covering every verdict of `round --check`, the family enum,
and every flag the notice suggests to the agent.

```bash
pytest tests/test_contract.py
```

It uses the `disensor` on PATH. To check another build, point `DISENSOR_EXE`
at its executable. `tests/test_fidelity.py` compares the coverage witness with
the gate of whatever disensor the test interpreter can import. As of this
version, on Python 3.12 and 3.14, the contract test passes against disensor
0.11.0 from PyPI, and the fidelity test against 0.11.0 and 0.10.0. Python 3.10
and 3.11 are supported by the code and were not available to run the suite on.

## What it does

Three hooks, all advisory:

| Hook | What it does |
|---|---|
| `sessionStart` | Takes the session in, pending, when the repository adopted disensor and it is not a background agent; notes where it started (the head and what was already uncommitted), and tells the agent the method exists. |
| `afterFileEdit` | Records which path the agent's edit tool changed, and with which model. Metadata only. The first edit admits the session. |
| `stop` | If this session changed code that still owes a review, adds a notice about it. |

When the notice fires, the agent gets told what the state is and asked to
consult you. It never gets told to commit, and never gets told to run a round:
both of those are yours to authorise.

## How it decides

**Which sessions it watches** is decided by evidence, not by the mode Cursor
reports. Cursor 3.21 sends the same `composer_mode` (`chat`) for Ask and for
the agent, while its documentation still lists `agent`, `ask` and `edit`, so a
first version that required `agent` stayed mute there. A session is taken in
pending when the repository adopted disensor and it is not a background agent,
and the first edit the agent makes admits it. An Ask conversation never edits
and never gets a notice. A session with no `sessionStart`, which is the case of
cloud agents, is never taken in.

**What the session changed** comes from three places, because each one alone
is blind somewhere: the paths the edit tool touched, what became uncommitted
since the session started (so edits made from a terminal count), and what was
committed since then (so work already out of the tree counts). A session that
changed nothing gets no notice and never costs a call to disensor; seeing that
it changed nothing takes a few git reads, since a terminal edit only shows up
in git. A new session starts from nothing: it does not inherit the edits of
the previous one in the same conversation, and an edit or a `stop` of that one
that arrives late does not count for it either. Every event is matched to its
session, and whatever depends on the state is decided under the lock that
writes it, with the one exception Known limits describes.

**Whose uncommitted changes they are** is told by timing, not by authorship.
The notice about a dirty tree names what appeared during this session and does
not claim who made it: with two composers on one checkout, what the other one
did appears too. Changes that were already there when the session started are
left out, and if the tree holds some, the round notice says the round will
need them dealt with first.

**Whether a declaration still covers the range** is answered the way the gate
answers it, not approximately. The range is measured from the merge base, its
paths are split the way `gate.classify_changes` splits them, a declaration has
to pass the range checks of G5 (its reviewed commit belongs to the PR, and a
diff review names the merge base), and the witness condition is G7's: no
ordinary path of the PR changed after the commit it reviewed. A declaration
followed by a fix no longer covers anything, and that is the most common reason
a round has to happen again, so that is exactly when the notice comes back and
says why. `tests/test_fidelity.py` runs the gate's own functions on the same
repositories and requires the same verdict.

**How often it speaks**: once per reason and commit. After a fix the head is
another commit and the notice can return. The notice itself never moves the
head, and Cursor's `loop_limit` bounds the automatic continuation anyway.

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
travel with the repository. It prints what that costs, and it never overwrites
a rule file you edited.

## Why the plugin brings no hooks

A plugin's hooks come in one file for every machine, and Cursor 3.22 has no
way to give a hook one command per operating system. So the command would
have to find Python by name, and two review rounds on that design found what a
name costs:

- Cursor runs a plugin's `stop` from the project root, so a relative entry in
  `PATH` would let the repository pick the `python` that runs.
- A launcher generated by `pip` starts Python without `-I`, and an empty entry
  in `PYTHONPATH` puts the current directory on the import path. The common
  `export PYTHONPATH=$PYTHONPATH:/x` leaves one when the variable was unset.
- Cursor pastes `${CURSOR_PLUGIN_ROOT}` into the command without escaping it
  for the shell.

A launcher in your home, written by `init`, would avoid all three, at the
price of a migration, collision rules and one more file the shell loads before
any check can run. `init` already writes the command with the absolute
interpreter it runs under, so the plugin keeps that and walks the agent through
it.

## What it deliberately does not do

- **It does not run models.** `disensor round` does, when you ask it to.
- **It does not block.** Every hook is `failClosed: false`, and once its code
  runs the executable always exits 0. Cursor reads exit code 2 as "block the
  action" no matter what `failClosed` says, so an advisory tool must never be
  able to produce one. If the interpreter the command names is gone, the shell
  that could not start it answers instead, with its own code (127 in sh), and
  `doctor` says the command fails.
- **It does not guess the generator.** If the model Cursor reports is not in
  `families.json`, nothing is prefilled and you declare it. A wrong family
  would make disensor's R4 certify a decorrelation that never happened. Family
  and model are prefilled together or not at all: with the model left empty,
  `disensor round` would call the same model a distinct one.
- **It does not suggest a command the policy refuses.** When the policy
  accepts only a plan or architecture review for the changed paths, the notice
  says so instead of proposing a diff round.
- **It does not run code or programs from the repository.** Neither its own
  code, nor a program it starts, nor a command git would run on its behalf.
  How, and where that stops, is below.
- **It does not run a command because a file says so.** `doctor` rebuilds the
  command `init` would write today, with the `--config` and `--directory` the
  entry carries, and runs the installed one only if it is that exact string.
  Anything else is shown, not run: `doctor` is what you would run on a
  repository you do not trust yet.
- **It does not delete a command it did not write.** An entry counts as one of
  its own only if the line is a single command, with nothing a shell would
  read as syntax or expand, its first word is a Python interpreter, and that
  interpreter starts the hook the way some version of `init` did. What comes
  after is not checked: an entry that runs this hook with odd arguments, or
  through the old `-m` form of 0.1.0 that imports from the current directory,
  is still this hook, and `init` replaces it instead of installing a second
  one next to it.
- **It does not wait on anything past its budget.** Each run has one time
  budget below Cursor's timeout, and every git call, the disensor call, the
  lock and the file replace draw from it; a lock left behind by a crashed
  process is reclaimed instead of making every later run wait. Reading its own
  small state files is not bounded, and that is the one part not measured.
- **It does not write inside your repository.** State lives in
  `~/.disensor-cursor/` and expires after 30 days. The hook asks for a clean
  tree, so writing its own bookkeeping into the tree would dirty exactly what
  it just asked for. If that directory is a symlink or a Windows junction,
  nothing is written or deleted there at all: the hook stays inactive, and
  `doctor` says so.
- **It does not keep your code.** The edit log records timestamp, path and
  model. Cursor also sends the before and after text of every edit, and that
  is dropped.

## Nothing from the repository runs

Cursor runs project hooks from the repository root, and a repository is
content somebody else may have written. Each way that content could become
something the hook executes was first proven with a test that plants it and
shows it running without the protection, and then closed.

**The hook's own code.** Python can put a directory on the import path by
itself in three ways that can point at the checkout: `-m` and `-c` put the
current directory first; a relative `PYTHONPATH`, such as the common `.`, does
the same; and a relative `PYTHONUSERBASE` moves the user site-packages into
the checkout, where `site.py` executes the `import` lines of any `.pth` file
before anything else loads. `-E` stops the second and not the third, because
`site.py` reads that variable directly. The command runs Python with `-I`,
which stops all three, and names the directory `init` found the package in, so
a package installed with `pip install --user` is still found. It adds `-S`,
because `-I` keeps the system's site-packages ahead of that directory, and an
older copy installed there ran instead. A runtime whose `._pth` file adds its
own paths still puts them first, and even loads site, so the program checks
where the hook module came from before running it. From anywhere else it does
not run that copy and answers `{}`, although importing the copy already ran
its module code, as 0.2.2 did before running its `main`. `doctor` says where
the hook imports it from.

The package itself must not be repository content either. After
`pip install -e`, the hooks would import a checkout, and every later edit to
it, an agent's included, would run inside them. `init` refuses a package whose
`hook.py` is committed to the repository it lives in, the same rule it applies
to disensor. That catches `pip install -e` from a clone; a copy without its git
history, or with that file uncommitted, gets through. `--allow-checkout` is
there for developing the adapter.

**The programs it starts.** On Windows a bare program name is searched for in
the current directory first, so a `git.exe` committed to a repository would be
the git that runs. `git` and `disensor` are resolved from `PATH` entries that
are absolute and outside the project: the folder Cursor names in
`CURSOR_PROJECT_DIR` or, when it names none, the one the hook found through
the payload or its working directory, and the repository around that folder,
found by its `.git` before git ever runs. A program found there is judged by
where it really is, too: a link in an entry outside the project that points at
a program inside it is skipped. The disensor `init` found is refused inside
the project too, although its path is absolute: absolute is not the same as
outside. And the commands leave the repository they run from out of
`PATH` as well, because what `init` resolves there runs in every later
session; it also refuses a disensor committed to any repository, asking a git
from outside that repository, and refuses when that git cannot say.

**What those programs inherit.** Protecting the hook's own process is not
enough: disensor calls git by bare name, and its launcher starts Python
without `-E`. Every child gets the same filtered `PATH`,
`NoDefaultCurrentDirectoryInExePath`, `PYTHONSAFEPATH`, no `PYTHONPATH`, and
no `PYTHONUSERBASE`, `PYTHONHOME` or `PYTHONPYCACHEPREFIX` that is relative or
points inside the project. Those three are kept otherwise, because disensor
may be installed exactly where they point.

**What git runs on its own.** Git trusts the configuration of a repository you
own, and some of it names commands. A repository that arrives with its `.git`
directory, from an archive or a shared folder, brings them along. For the
adapter's git calls:

- the fsmonitor command is switched off, in a way git passes on to its own
  children, submodules included;
- `git status` never takes the index lock, so it never writes the index, and
  the hook git runs after a write (`post-index-change`, from wherever
  `core.hooksPath` points) never fires;
- no filter runs: `git status` would run the clean command of any file whose
  timestamps changed, and `.gitattributes` picks the files. Every driver any
  configuration defines is switched off for that call, yours included,
  because a filter you installed can run what the repository configures:
  git-lfs, which Git for Windows ships, runs the `lfs.extension.*` commands
  of `.git/config` on every clean. A driver named in bytes that are not UTF-8
  cannot be named back to switch it off, so then `git status` does not run
  and `status` says why;
- submodules are compared by the commit they point to and never entered. With
  `--ignore-submodules=none`, git runs a status inside each one, under that
  submodule's own configuration;
- missing objects are never fetched. In a partial clone that fetch goes
  through whatever transport the configuration names, and an `ext::`
  transport is a command.

The tests plant a package, a `.pth`, a `git.exe`, a `disensor.cmd`, an
fsmonitor, a clean filter, a git-lfs extension, an index hook, a submodule
with a filter of its own and an `ext::` transport, against the hook and, where
it applies, against the real disensor.

**Where this stops.** The environment is taken as yours: a `PATH` entry, a git
setting in `GIT_CONFIG_*` or a Python variable that points outside the project
is trusted. A `PATH` entry that puts somebody else's directory ahead of your
git already runs their git in your own terminal; the adapter excludes the
project and the repository around it, which is as far as it can know without
running git. disensor finds its own git on the `PATH` it inherits, which
leaves those entries out but can still hold a link, in a directory outside
the project, to a program inside it: that lookup is disensor's. One case it cannot know: a `core.worktree` in the repository's
own configuration can put the working tree above the folder that holds
`.git`, and a `PATH` entry between the two is not excluded for the first git
call. Telling that case apart means parsing git's configuration the way git
does; a text reading of it switched the hook off in ordinary repositories, so
it was dropped.

A repository whose `.git` directory is hostile has ways in that do not go
through this adapter, starting with any git client that runs `git status`
there, Cursor's own git integration included. And the round you authorise is
disensor's: `disensor round` runs `git status` with the repository's
configuration, entering submodules and taking the index lock.

## Known limits

- **Coverage is checked, not validated.** The adapter reads the three fields
  of each declaration that the range checks need (`event.gate`,
  `event.head_commit`, `event.base_commit`), all required by every published
  schema version. A declaration that the validator would reject for another
  reason still counts as covering here. The gate is the judge; this is a
  reminder.
- **A terminal edit to a file that was already uncommitted is invisible** until
  it is committed: the baseline compares which paths are dirty, not their
  content. Edits made through the agent's edit tool are always seen.
- **Edits made before `sessionStart` lands** can be missed. Cursor documents
  `sessionStart` as fire-and-forget, so an edit that arrives before the session
  is taken in is not recorded and does not admit it. That work still counts if
  a later edit admits the session and it is committed after `sessionStart` took
  its starting point; a commit made before that is part of the starting point.
- **A session that changes code only through the terminal gets no notices.**
  Admission needs one edit through the agent's edit tool (see How it decides),
  and a session whose every change went through a shell command, a script or
  `git apply` is never admitted. Once a session is admitted, its terminal edits
  and commits do count.
- **Two setups keep Cursor from running any hook on Windows, and `doctor`
  names them.** A `pwsh` or `powershell` on PATH that is a batch file makes
  Cursor start cmd.exe with PowerShell's arguments, which runs nothing and
  exits 0. A temporary directory whose path holds a typographic quote (`’`)
  ends the quoted path of the payload early, because Cursor escapes only the
  ASCII quote. Both are Cursor's; the adapter cannot route around them.
- **Another copy of the hooks may run too.** Cursor runs the entries of every
  source in parallel: your home, the repository's `.cursor/hooks.json` and the
  Claude Code hooks it imports. Installed in your home and in the repository,
  every event there runs twice. `doctor` says so for the repository it runs
  in, and cannot see the others.
- **Whether the plugin is enabled is Cursor's to know.** `doctor` lists the
  copies it finds on disk and does not read Cursor's settings. Either way, the
  hooks are the ones `init` installed.
- **A hook that stalls holding the lock can cross decisions.** The state lock
  is waited for two seconds at most, because blocking Cursor is worse, and past
  that the waiting hook goes ahead. A hook that stalls longer than that while
  holding it can lose an admission, which then waits for the session's next
  edit, or write an admission over a rejection. The lock is normally held for
  the milliseconds it takes to read and write a small file.
- **The accepted gates come from prose.** `round --check` prints them on a
  line that disensor does not promise to keep. The contract test pins today's
  wording, and when the line cannot be read the notice falls back to a diff
  round, which `disensor round` itself refuses with a clear message if wrong.
- **The model reported by Cursor is provenance, not proof.** A session can
  switch models, and a commit range can hold work from other hands. When the
  session's edits do not agree on one model, nothing is prefilled.
- **Model names are compared as text.** disensor tells the same model from a
  distinct one by name, and Cursor's slug for a model need not be the name
  your reviewer registry gives it. With a reviewer of the generator's own
  family, `claude-4.5-sonnet` against a registry's `claude-sonnet-4-5` reads
  as two models. The notice asks to confirm the suggested generator; that is
  when to make the names agree.
- **The family table is incomplete on purpose.** Cursor publishes display
  names, not stable model identifiers, so `families.json` is maintained by
  hand and is always behind.
- **Vendors outside the five named families are `other`.** disensor's family
  enum is `anthropic, openai, google, meta, mistral, other`. Grok (xAI), Kimi
  (Moonshot) and GLM (Z.ai) map to `other`, which can only understate
  independence: against a reviewer of a named family R4 reads another family,
  which is true, and against another `other` it reads the same family, which
  is the safe mistake. Cursor's Auto mode and its own Composer models stay
  unmapped, because the slug does not say what runs underneath: if it were one
  of the five, `other` would overstate independence. For those, the notice
  asks the user; the agent is never told to declare itself.
- **The evidence directory and the configuration** default to disensor's
  (`.residue`, `disensor.config.json`). A repository that moved them has to
  add `--directory` and `--config` to the hook commands, quoted if they hold
  spaces. `init` keeps those two when it regenerates the hooks, and `doctor`
  runs a hook edited that way. A value that no quoting keeps literal in every
  shell (one with `$`, a backtick, `%`, `!`, a doubled backslash or a trailing
  one) is replaced by a placeholder in the suggested command rather than
  passed along to be expanded.
- **A filtered file whose timestamps changed can look changed.** With its
  filter switched off (see above), a git-lfs file, say, is compared as it sits
  in the working tree, so after a tool touches it without changing it, it
  counts as uncommitted until git refreshes its timestamps, which a plain
  `git status` in a terminal does.
- **Uncommitted changes inside a submodule are not seen.** Seeing them means
  running git inside the submodule under its own configuration. A submodule
  that points to another commit is still seen, and `disensor round` sees the
  rest and refuses a dirty tree with its own message.
- **Nothing is fetched.** In a partial clone, the adapter and the disensor it
  asks work with the objects that are already local, and a read that needs one
  that is not fails instead of downloading it from inside a hook. If that read
  was disensor's, the check is not evaluated and `status` says why; if it was
  a declaration the adapter reads, that declaration counts as covering
  nothing, which errs toward the notice.
- **Paths holding `%`, `!`, `$`, a backtick, a double quote, a typographic
  double quote or a doubled backslash, or ending in a backslash,** cannot be
  written into a hook command that every shell and the Windows argument parser
  read back literally, so `init` refuses them, whether it is the interpreter,
  disensor, the package directory or a setting it would keep. That includes a
  Python on a UNC path (`\\server\...`). PowerShell, which runs the hooks on
  Windows, closes a double-quoted string at `“`, `”` and `„` too; a
  typographic apostrophe, as in a user folder like `O’Brien`, goes through.
  It says which, and writes nothing.

## Verified against a live Cursor

### 0.3.0, as a local plugin with its setup skill, on Cursor 3.24.12 (2026-10-12)

Windows 11, disensor 0.12.1, Windows PowerShell 5.1 (no `pwsh` on PATH),
agent chats with Grok 4.7, in a throwaway repository. This repository was
copied to `~/.cursor/plugins/local/disensor-cursor`, and the hooks were the
ones `init` writes, at user level: the first live run of that install.

- **The setup skill drove the install.** Asked to set up disensor, the agent
  took the skill, checked Python and `disensor round --help`, installed the
  adapter from the plugin's own folder with `pip install --user` (that machine
  has neither pipx nor uv), ran `init` and `doctor`, and asked before each
  command. `doctor` reported the plugin copy, the three user hooks, the smoke
  run and the module imported from the user site-packages at 0.3.0. The
  install left `build/` and an `.egg-info` in the plugin's folder, as the
  skill says it can.
- **Cursor picked up the new `~/.cursor/hooks.json` without a restart.** The
  hooks ran in the window that was already open, from `~/.cursor`, the folder
  of the file, and found the repository through the payload.
- **The full chain, at user level.** `sessionStart` took the session in and
  Cursor merged its context. The first edit admitted it, recording
  `src/app.py` and the model. The `stop` of that turn gave the dirty tree
  notice, and after the commit the next `stop` gave the round notice, with
  `--generator-family other --generator-model grok-4.7` prefilled. The agent
  asked before running the round and said, unprompted, that consent to a
  cloud reviewer is the user's to give in their own terminal: the line the
  skill and the notice carry since this version.
- **One notice per reason and head, enforced twice.** On the continuation
  turn a notice triggers, Cursor skipped the hook itself ("skipped due to
  loop limit"), so the hook's own mark never had to answer.
- **A real round, its declaration, and then silence.** Asked to run the
  round, the agent read `disensor guide`, ran the command the notice gave
  with `--result` outside the repository, filled the declaration with
  `disensor new --round`, validated it, ran `disensor gate` and committed it.
  It never ran `disensor reviewer consent`: the owner had granted it in their
  own terminal, 27 seconds before the round. The declaration matches the round
  result field by field (anchors, generator, reviewer, prompt, pack and report
  hashes, seconds), and the reviewer's report hashes to what it declares.
  After that commit `stop` stayed quiet, although `round --check` still
  answers "review required" there: the quiet is the adapter's coverage
  witness, now seen at user level.
- **Runs took 340 to 620 ms, and 1.4 s for the `stop` that asked disensor**,
  against budgets of 5, 5 and 15 s.
- **A session with no edit of its own got no notice**, as documented. By
  accident the first attempt did exactly that: the edit happened in the chat
  that ran the setup, whose `sessionStart` predated the hooks, and a new chat
  that only ran `git commit` stayed pending and said nothing.
- **`stop` overtook `afterFileEdit` on an aborted turn.** In a turn the user
  stopped, `stop` (status `aborted`) arrived 82 ms before the `afterFileEdit`
  of the edit being applied. On the completed turn the edit arrived 6.5 s
  before its `stop`.
- **`model_id` did not come.** The `afterFileEdit` payload carried `model`
  only (`grok-4.7`), where 3.21.18 sent both. The adapter reads either.
- **Cursor logs `ERROR: Failed to parse project hooks configuration`** for a
  repository that has no `.cursor/hooks.json`. Cursor's, and harmless.

### 0.2.1, as a local plugin, on Cursor 3.21.18 (2026-09-24)

The same code packaged as a Cursor plugin in `~/.cursor/plugins/local`, with
this machine's interpreter written into its hooks, in a throwaway repository:
an experiment towards distributing the adapter as a plugin, not a way to
install it today. The hooks were the ones `init` writes, run from the plugin's
folder, next to probes that recorded how Cursor ran them.

- **On Windows every hook runs through PowerShell, not cmd.exe.** `pwsh` if it
  is on PATH, Windows PowerShell otherwise, with `-NoProfile -NonInteractive
  -ExecutionPolicy Bypass`. The payload is written to a temporary file and
  piped in by PowerShell with UTF-8 as its output encoding, which is where the
  BOM comes from. Read from Cursor's code and confirmed by the process chain of
  a live hook; it holds for every source of hooks, not only plugins. Outside
  Windows Cursor runs it with `$SHELL -c` and the payload on stdin; that part
  is read from the code, not run. `doctor` does not reproduce either: running
  a shell brings its whole startup along (profiles, module paths, startup
  files), and two review rounds kept finding ways for code from the project
  to run through it. It runs the installed command directly, as the argv init
  wrote, and describes which shell Cursor would start and whether any hook
  would run at all. One step of Cursor's lookup stays out of its sight:
  before `PATH`, Cursor tries each name in its own working directory.
- **Windows PowerShell 5.1 re-encodes the payload.** It reads Cursor's file in
  the ANSI code page, so every non-ASCII character reaches the hook as the
  ANSI reading of its UTF-8 bytes: `José` arrives as `JosÃ©`. A repository
  under such a path never matched an edit, and no session there was admitted.
  The hook now tells from its parent processes whether Cursor started it
  through Windows PowerShell, and only then undoes it, field by field; a
  Windows PowerShell started from a terminal may have read its input some
  other way, and is left alone. A field that cannot be recovered is noted
  and used as it came. In some code pages, such as the
  Japanese or Chinese ones, part of it is lost for good. `pwsh` delivers the
  payload as written. Reproduced by running Cursor's pipe through Windows
  PowerShell, not in a live run, where every path was ASCII.
- **PowerShell looks up a bare command name on PATH only**, never in the
  current directory.
- **A plugin's hooks run from different directories by event:** the plugin's
  own folder for `sessionStart` and `afterFileEdit`, the project root for
  `stop`. `CURSOR_PROJECT_DIR` is set for all of them.
- **The coverage witness holds.** After a real round and its declaration were
  committed, `stop` stayed quiet. After a fix to the reviewed code it came back
  saying the declaration no longer covers the range, with the round command
  for the new head. The agent asked before each round.
- **`composer_mode` came as `agent` this time**, where the run below got `chat`
  for Ask and agent alike. The admission by evidence does not depend on it.

### 0.2, on Cursor 3.21.18 (2026-09-24)

Windows 11, disensor 0.10.0, a project-level install from a regular
`pip install` into its own virtualenv, agent chats with Grok 4.7, in a
throwaway repository.

- **Cursor sends `composer_mode: chat` for Ask and for the agent alike.** 0.2.0
  required `agent` and rejected every session. Nothing broke: it said nothing,
  and `status` showed why (`rejected`, `mode: chat`). The admission by
  evidence described above is the fix, and everything below ran with it.
- **The command form holds under Cursor's own launcher.** The quoted
  interpreter, `-I -c`, the program and the package directory ran for every
  event.
- **The full chain works.** `sessionStart` took the session in, the first edit
  admitted it, and `stop` gave the dirty tree notice. The agent answered that
  it would not commit on its own and asked whether to leave the changes,
  discard them or commit them. After the user asked for the commit, `stop`
  gave the round notice; the agent wrote that the round is the owner's
  decision, did not run it, and asked to read `disensor guide` instead. One
  notice per reason and commit: `dirty_tree` at the first head,
  `round_required` at the new one.
- **A changed `.cursor/hooks.json` runs without a prompt.** Two entries added
  mid-session to a project hooks file that was already in use ran on the next
  event. That is the risk the user-level default exists for.
- **`model_id` is set now** (`grok-4.7`), next to the legacy `model`.
- **Every event carries `session_id`**, equal to `conversation_id`, and
  `sessionStart` fired once per conversation, on its first message: one
  conversation had four `stop`, two of them from the continuation a notice
  triggers, and a single `sessionStart`. Between an edit and the `stop` of its
  turn there were 9 and 46 seconds.
- **Workspace roots arrive in URI form** on Windows, `/c:/Users/...`. The
  adapter reads them as paths.
- **The payload carries `user_email` and `transcript_path`.** The adapter
  stores neither.

### 0.1.0, on the Cursor of 2026-09-09

Windows 11, disensor 0.9.4, a project-level install, agent mode.

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
- **The conservative prefill held on a real model.** `grok-4.6` is
  deliberately unmapped, so the emitted command carried no
  `--generator-family`, said the model is not in the table, and told the agent
  to ask rather than guess. The agent did exactly that.
- **Project hooks ran with no additional trust prompt** on a folder opened for
  the first time. Not conclusive on a clean machine, since Cursor may inherit
  trust from a parent directory that was already trusted, but it is consistent
  with the risk described above: a cloned repository carrying
  `.cursor/hooks.json` can run those commands on the machine that opens it.
  It is why the user-level install is the default here.

## Still to verify

1. Cursor on macOS and Linux. Its code runs a hook with `$SHELL -c` and the
   payload on stdin, which is what `doctor` reports there, but no live run
   has confirmed it.
2. Whether the `env` returned by `sessionStart` reaches later hooks, given that
   `sessionStart` is documented as fire-and-forget. The adapter does not rely
   on it: the state file is the source of truth.
3. Whether edits made by subagents reach `afterFileEdit` under the parent's
   conversation. If they do not, their files still count through the baseline,
   once some edit has admitted the session.
4. Whether `stop` can overtake the `afterFileEdit` of its own turn when the
   turn completes. It did on an aborted turn, by 82 ms; on the one completed
   turn measured, the edit came 6.5 s ahead. If it overtook a session's first
   edit, it would find the session still pending and say nothing until the
   next turn.

## Licence

MIT.
