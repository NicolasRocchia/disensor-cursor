"""Model slug to declared family, conservatively and incompletely.

Cursor does not publish stable model identifiers: its models page lists display
names. Whatever mapping lives here is maintained by hand against a third
party's release calendar, so the only safe default is to map nothing.

`other` is claimed only for vendors known to be none of the five named
families: xAI's Grok, Moonshot's Kimi, Z.ai's GLM. That can only understate
independence. Against a reviewer of a named family R4 reads another family,
which is true; against another `other` it reads the same family, which is the
safe mistake. A slug whose vendor is not known, Cursor's Auto or its own
Composer, stays unmapped: if it runs on one of the five, `other` would
overstate independence. Unmapped means the generator is not prefilled and the
user is asked.
"""
from __future__ import annotations

import json
import re
from importlib import resources

# Un slug que no tiene esta forma no se prellena: viaja dentro de un comando
# que el agente puede copiar y correr, y un `;` ahi dentro seria otro comando.
SAFE_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


def _table() -> dict:
    raw = resources.files("disensor_cursor").joinpath("families.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def family_of(slug: str | None) -> str | None:
    """The family this slug belongs to, or None when it is not known.

    The prefix has to end at a boundary. Matching `claude` inside `claudette`
    would be inventing a vendor out of a substring, which is the exact failure
    this table exists to avoid.
    """
    if not slug or not isinstance(slug, str):
        return None
    text = slug.strip().lower()
    if not text:
        return None
    prefixes = _table().get("prefixes", {})
    if not isinstance(prefixes, dict):
        return None
    best: tuple[int, str] | None = None
    for prefix, family in prefixes.items():
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        if rest and (rest[0].isalnum()):
            continue
        # El prefijo mas largo gana, para que `o1` no le robe un slug a un
        # prefijo mas especifico que se agregue despues.
        if best is None or len(prefix) > best[0]:
            best = (len(prefix), family)
    return best[1] if best else None


def generator_flags(model: str | None, model_id: str | None) -> list[str]:
    """The `--generator-*` flags to suggest, or an empty list.

    Family alone is never enough. `disensor round` derives independence from
    family AND model: with an empty generator model, a reviewer of the same
    family whose model is set gets classified `same_family_distinct_model` even
    when it is literally the same model. Prefilling half the pair would make
    the round declare an independence that did not happen.
    """
    slug = model_id or model
    if not isinstance(slug, str) or not SAFE_SLUG.fullmatch(slug):
        return []
    family = family_of(slug)
    if not family:
        return []
    return ["--generator-family", family, "--generator-model", slug]
