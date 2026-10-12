"""The Cursor plugin: a manifest and a setup skill, and no hooks of its own."""
from __future__ import annotations

import json
import re
from pathlib import Path

from disensor_cursor import __version__, cli

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".cursor-plugin" / "plugin.json"
SKILL = ROOT / "skills" / "disensor-setup" / "SKILL.md"

# Las propiedades del esquema oficial del manifiesto (cursor/plugins,
# schemas/plugin.schema.json), que rechaza cualquier otra.
SCHEMA_PROPERTIES = {
    "agents", "author", "category", "commands", "description", "displayName", "homepage",
    "hooks", "keywords", "license", "logo", "mcpServers", "minClientVersions", "name",
    "publisher", "repository", "rules", "skills", "tags", "variables", "version",
}


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_only_uses_what_the_schema_allows():
    data = manifest()
    assert set(data) <= SCHEMA_PROPERTIES
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", data["name"])
    assert data["description"]


def test_the_manifest_names_the_plugin_doctor_looks_for():
    assert manifest()["name"] == cli.PLUGIN_NAME


def test_the_plugin_version_is_the_package_version():
    assert manifest()["version"] == __version__


def test_the_license_is_there_and_is_the_declared_one():
    assert manifest()["license"] == "MIT"
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


def test_the_plugin_brings_no_hooks_of_its_own():
    """The hooks are the ones init installs, with the absolute interpreter.
    Hooks inside the plugin would have to find Python by name, which two
    Codex rounds showed a repository can steer, and next to init's they would
    run every event twice."""
    assert "hooks" not in manifest()
    assert not (ROOT / "hooks").exists()


def test_the_skill_has_the_frontmatter_cursor_reads():
    text = SKILL.read_text(encoding="utf-8")
    front = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert front, "SKILL.md needs a frontmatter block"
    fields = dict(line.split(": ", 1) for line in front.group(1).splitlines())
    assert fields["name"] == SKILL.parent.name
    assert fields["description"]


def test_the_skill_leaves_the_owners_decisions_to_the_owner():
    text = SKILL.read_text(encoding="utf-8")
    assert "Never run `disensor round`" in text
    assert "Never run `disensor reviewer consent`" in text


def test_the_skill_never_sends_the_agent_to_an_unclaimed_package_name():
    """The adapter is not published on any index. Pointing the agent at
    `pipx install disensor-cursor` would install whatever somebody else
    publishes under that name first."""
    text = SKILL.read_text(encoding="utf-8")
    assert not re.search(r"(?<!un)install (--\S+ )*disensor-cursor\b", text)
    assert not re.search(r"upgrade disensor-cursor\b", text)


def test_the_skill_asks_disensor_what_the_adapter_uses():
    """Codex, round on the 0.3.0 diff: `disensor --version` only exists since
    0.9.5, and 0.9.0 to 0.9.4 are supported. The adapter's own probe asks the
    help of `round` for `--check`; the skill has to ask the same."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "Run `disensor round --help` and look for `--check`" in text
    assert "Do not decide with `disensor --version`" in text


def test_the_skill_updates_an_older_adapter_before_init():
    """Codex, same round: with any version installed, the skill went straight
    to `init`, and an older init writes older hooks."""
    text = " ".join(SKILL.read_text(encoding="utf-8").split())
    assert "compared with the `version` in this plugin's `.cursor-plugin/plugin.json`" in text
    assert "older than the plugin: reinstall it from this plugin's folder first" in text
    # Codex, segunda ronda: comparadas como texto, 0.10.0 queda antes que 0.3.0.
    assert "as version numbers, part by part, not as text: 0.10.0 is newer than 0.3.0" in text


def test_no_em_dashes_in_what_the_plugin_shows():
    for text in (SKILL.read_text(encoding="utf-8"), manifest()["description"]):
        assert "\u2014" not in text
