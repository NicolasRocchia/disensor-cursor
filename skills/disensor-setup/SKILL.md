---
name: disensor-setup
description: Install, update, check or remove the disensor-cursor hooks, which remind the agent to close the adversarial review round with disensor. Use when the user asks to set up disensor in Cursor, when `disensor-cursor` is not installed, or when its reminders never show up.
---

# Set up the disensor-cursor hooks

This plugin brings no hooks of its own. The `disensor-cursor` command installs
them in the user's home (`~/.cursor/hooks.json`) with the absolute path of the
Python interpreter that installed it, so nothing from a repository chooses what
runs. It also means that disabling this plugin does not remove them:
`disensor-cursor uninstall` does.

Every step below that installs or changes something needs the user's approval
first. Show the command and ask.

## Never do this on your own

- Never run `disensor round`. It sends the material under review to an external
  reviewer, and that is the user's decision.
- Never run `disensor reviewer consent`. Consent to send a repository's material
  to a cloud reviewer is the user's to give, in their own terminal, even after
  they approve a round.
- Never install with `--project`, or edit a repository's `.cursor/hooks.json`,
  unless the user asks for it: those are executable commands that travel with
  the repository.

## Check what is there

1. Python 3.10 or newer. On Windows try `py -3 --version`, then
   `python --version`. On macOS and Linux, `python3 --version`. The Python that
   comes with the macOS Command Line Tools is 3.9, which is too old.
2. disensor 0.9.0 or newer. Run `disensor round --help` and look for
   `--check` in what it prints. If the command is not found, disensor is
   missing; if it runs and `--check` is not there, it is older than 0.9.0. Do
   not decide with `disensor --version`, which only exists since 0.9.5. To
   install or update it, propose `pipx install disensor` (`pipx upgrade
   disensor` if pipx already has it), or the `uv tool` or
   `python -m pip install --user` equivalent.
3. `disensor-cursor --version`, compared with the `version` in this plugin's
   `.cursor-plugin/plugin.json`. Compare them as version numbers, part by
   part, not as text: 0.10.0 is newer than 0.3.0.
   - not found: go to "Install the adapter";
   - older than the plugin: reinstall it from this plugin's folder first (see
     "Update"), because an older `init` writes older hooks;
   - the same or newer: go to "Install the hooks".

## Install the adapter

Install it from this plugin's own folder: the one holding `pyproject.toml`, two
folders up from this file. It is under `~/.cursor/plugins/local/` or
`~/.cursor/plugins/cache/`. Propose one of these, in this order:

- `pipx install <that folder>`
- `uv tool install <that folder>`
- `python -m pip install --user <that folder>` (`py -m pip` on Windows)

Not from a package index: the adapter is not published there, and a package
with that name could belong to somebody else. Never with `pip install -e`:
`disensor-cursor init` refuses a package imported from a checkout, because
every later edit to it would run inside the hooks. Installing builds the
package inside that folder, which can leave `build/` and an `.egg-info`
directory there; they are harmless.

If `disensor-cursor` is still not found afterwards, the folder those tools put
commands in is not on PATH yet: `pipx ensurepath` or `uv tool update-shell`,
then a new terminal.

## Install the hooks

1. `disensor-cursor init`
2. `disensor-cursor doctor`, and read the whole report back to the user. A line
   saying "NO HOOK WOULD RUN", "FAILED" or "WRONG" is a problem to solve before
   anything else.

The hooks only act inside repositories whose root holds a
`disensor.config.json`. Everywhere else they do nothing. Whether a repository
adopts disensor (`disensor init` in it) is the user's call; `disensor guide`
explains the method.

## Update

1. Reinstall from this plugin's folder once it has the new version:
   `pipx install --force <that folder>`, or the `uv tool install --force` or
   `pip install --user --upgrade` equivalent.
2. `disensor-cursor init` again. It replaces its own entries and keeps the rest
   of the file, including the `--config` and `--directory` settings the user
   added to them.
3. `disensor-cursor doctor`.

## Remove

1. `disensor-cursor uninstall` removes the entries init added and nothing else.
   With `--purge` it also deletes the session state in `~/.disensor-cursor`.
2. Then the package (`pipx uninstall disensor-cursor`) and, if the user wants,
   this plugin.

## When the reminders never show up

Run `disensor-cursor doctor` from the repository in question, then
`disensor-cursor status`. It says what the hook decided for each session, and
why it stayed quiet when it did.
