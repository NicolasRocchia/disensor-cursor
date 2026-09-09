"""The slug to family table: incomplete on purpose, never inventive."""
from __future__ import annotations

import pytest

from disensor_cursor import families


@pytest.mark.parametrize("slug, family", [
    ("claude-4.5-sonnet", "anthropic"),
    ("CLAUDE-OPUS-5", "anthropic"),
    ("gpt-5.4", "openai"),
    ("o3-mini", "openai"),
    ("gemini-3-pro", "google"),
    ("llama-4", "meta"),
    ("mistral-large", "mistral"),
])
def test_known_slugs_map(slug, family):
    assert families.family_of(slug) == family


@pytest.mark.parametrize("slug", [
    "auto",           # Cursor elige: el slug no dice cual corrio
    "composer-2.5",   # modelos propios de Cursor
    "grok-4.6",       # xAI no tiene familia propia en el enum
    "kimi-k2",
    "glm-5.2",
    "claudette-1",    # el prefijo tiene que terminar en un limite
    "gptx",
    "",
    None,
])
def test_everything_else_stays_unmapped(slug):
    """Unmapped is NOT `other`. `other` is a family like any other in the enum,
    and claiming it would let R4 compare two unrelated vendors as correlated."""
    assert families.family_of(slug) is None


def test_the_flags_come_in_pairs_or_not_at_all():
    """Family alone makes `disensor round` derive a false independence: with an
    empty generator model, a reviewer of the same family whose model is set is
    classified same_family_distinct_model even when it is the same model."""
    flags = families.generator_flags("claude-4.5-sonnet", None)
    assert flags == [
        "--generator-family", "anthropic",
        "--generator-model", "claude-4.5-sonnet",
    ]
    assert families.generator_flags("composer-2.5", None) == []
    assert families.generator_flags(None, None) == []


def test_model_id_wins_over_the_legacy_slug():
    assert families.generator_flags("auto", "gpt-5.4")[1] == "openai"


def test_every_mapped_family_exists_in_the_schema_enum():
    """A family disensor's schema does not know would be rejected by the
    validator after the round already ran."""
    table = families._table()
    valid = set(table["families"])
    assert set(table["prefixes"].values()) <= valid
