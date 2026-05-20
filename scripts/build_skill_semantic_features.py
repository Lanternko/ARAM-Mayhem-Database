"""Build reviewable skill-level semantic feature table.

Inputs:
  data/cache/champion_abilities.json from scripts/fetch_champion_abilities.py

Outputs:
  data/cache/skill_semantic_features.csv
  data/cache/skill_semantic_features.json

This table is the repo's intermediate "skill truth-ish" layer:

  Riot Data Dragon raw arrays + reviewed semantic fields + source notes

It is not full ground truth yet, but it is a cleaner contract than letting the
scoring script infer everything directly from free-form tooltip text.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import click
import httpx

import build_semantic_ability_scores as semantic

CACHE_DIR = Path("data/cache/cdragon_bin")
CDRAGON_BIN_URL = "https://raw.communitydragon.org/latest/game/data/characters/{slug}/{slug}.bin.json"


def cdragon_slug(alias: str) -> str:
    return alias.lower()


def cdragon_cache_path(alias: str) -> Path:
    return CACHE_DIR / f"{cdragon_slug(alias)}.json"


def load_cdragon_bin(alias: str) -> dict[str, Any]:
    cache_path = cdragon_cache_path(alias)
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    url = CDRAGON_BIN_URL.format(slug=cdragon_slug(alias))
    r = httpx.get(url, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def iter_spell_objects(bin_data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for value in bin_data.values():
        if isinstance(value, dict) and isinstance(value.get("mSpell"), dict):
            out.append(value)
    return out


def find_spell_object(bin_data: dict[str, Any], spell_id: str) -> dict[str, Any] | None:
    spell_id_l = spell_id.lower()
    best: dict[str, Any] | None = None
    for obj in iter_spell_objects(bin_data):
        names = [
            str(obj.get("mScriptName") or ""),
            str(obj.get("ObjectName") or ""),
            str((obj.get("mSpell") or {}).get("mAlternateName") or ""),
        ]
        if any(name.lower() == spell_id_l for name in names if name):
            return obj
        if best is None and any(spell_id_l in name.lower() for name in names if name):
            best = obj
    return best


def cdragon_data_values_map(spell_obj: dict[str, Any]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    spell = spell_obj.get("mSpell") or {}
    mana = number_list(spell.get("mana"))
    expected_len = 5
    if mana:
        if len(mana) >= 6:
            expected_len = 5
        else:
            expected_len = len(mana)
    for entry in spell.get("DataValues") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        raw_values = number_list(entry.get("values"))
        values = rank_series(raw_values, expected_len) or raw_values
        if name and values:
            out[name] = values
    return out


def scaling_stat_from_part(part: dict[str, Any], data_value_name: str, tags: set[str]) -> str:
    name = data_value_name.lower()
    if "bonusad" in name or "bonus_ad" in name:
        return "bonus_ad"
    if "totalad" in name or "total_ad" in name or "adratio" in name:
        return "total_ad"
    if "apratio" in name or name.endswith("ap") or "spelldamage" in name:
        return "ap"
    if "health" in name or "maxhp" in name or "max_health" in name:
        return "max_hp"
    if "armor" in name:
        return "armor"
    if "magicresist" in name or "spellblock" in name or "mr" in name:
        return "mr"
    mstat = part.get("mStat")
    if mstat == 2:
        return "bonus_ad" if "Mage" not in tags else "total_ad"
    return "ap" if "Mage" in tags else "bonus_ad"


def flatten_formula_parts(calc: dict[str, Any]) -> list[dict[str, Any]]:
    return [part for part in (calc.get("mFormulaParts") or []) if isinstance(part, dict)]


def choose_damage_calc(spell_obj: dict[str, Any], maxrank: int) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    spell = spell_obj.get("mSpell") or {}
    dv_map = cdragon_data_values_map(spell_obj)
    best_name: str | None = None
    best_calc: dict[str, Any] | None = None
    best_score = -1
    for calc_name, calc in (spell.get("mSpellCalculations") or {}).items():
        if not isinstance(calc, dict):
            continue
        score = 0
        lowered = str(calc_name).lower()
        if "damage" in lowered:
            score += 4
        parts = flatten_formula_parts(calc)
        for part in parts:
            dtype = str(part.get("__type") or "")
            if dtype == "NamedDataValueCalculationPart":
                dv_name = str(part.get("mDataValue") or "")
                dv_vals = dv_map.get(dv_name) or []
                if dv_vals[:maxrank] and "damage" in dv_name.lower():
                    score += 5
                elif dv_vals[:maxrank]:
                    score += 2
            elif "StatBy" in dtype:
                score += 3
        if score > best_score:
            best_name = str(calc_name)
            best_calc = calc
            best_score = score
    return best_name, best_calc


def extract_primary_base_damage(spell_obj: dict[str, Any], maxrank: int) -> list[float]:
    dv_map = cdragon_data_values_map(spell_obj)
    _calc_name, calc = choose_damage_calc(spell_obj, maxrank)
    if isinstance(calc, dict):
        for part in flatten_formula_parts(calc):
            if str(part.get("__type") or "") != "NamedDataValueCalculationPart":
                continue
            dv_name = str(part.get("mDataValue") or "")
            values = dv_map.get(dv_name) or []
            if len(values) >= maxrank and "damage" in dv_name.lower():
                return [round(v, 3) for v in values[:maxrank]]
    preferred_names = [name for name in dv_map if "damage" in name.lower()]
    preferred_names.sort(key=lambda n: ("base" not in n.lower(), "damage" not in n.lower(), n))
    for name in preferred_names:
        values = dv_map[name]
        if len(values) >= maxrank:
            return [round(v, 3) for v in values[:maxrank]]
    return []


def extract_scaling_coeffs(spell_obj: dict[str, Any], maxrank: int, tags: set[str]) -> list[dict[str, Any]]:
    dv_map = cdragon_data_values_map(spell_obj)
    _calc_name, calc = choose_damage_calc(spell_obj, maxrank)
    if not isinstance(calc, dict):
        return []
    out: list[dict[str, Any]] = []
    for part in flatten_formula_parts(calc):
        ptype = str(part.get("__type") or "")
        if ptype == "StatByNamedDataValueCalculationPart":
            dv_name = str(part.get("mDataValue") or "")
            values = dv_map.get(dv_name) or []
            if values:
                out.append(
                    {
                        "stat": scaling_stat_from_part(part, dv_name, tags),
                        "values": [round(v, 4) for v in values[:maxrank]],
                        "source": dv_name,
                    }
                )
        elif ptype == "StatByCoefficientCalculationPart":
            coeff = part.get("mCoefficient")
            if isinstance(coeff, (int, float)):
                out.append(
                    {
                        "stat": "ap" if "Mage" in tags else "bonus_ad",
                        "values": [round(float(coeff), 4)] * maxrank,
                        "source": "mCoefficient",
                    }
                )
    seen: set[tuple[str, tuple[float, ...]]] = set()
    deduped: list[dict[str, Any]] = []
    for row in out:
        key = (str(row["stat"]), tuple(float(x) for x in row["values"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def cdragon_targeting_type(spell_obj: dict[str, Any]) -> str:
    spell = spell_obj.get("mSpell") or {}
    t = str(((spell.get("mTargetingTypeData") or {}).get("__type")) or "")
    if t == "Location":
        return "line"
    if t == "Area":
        return "circle"
    if t == "Self":
        return "self"
    if t == "Target":
        return "targeted"
    return ""


def enrich_from_cdragon(alias: str, spell_id: str, maxrank: int, tags: set[str]) -> dict[str, Any]:
    try:
        bin_data = load_cdragon_bin(alias)
    except Exception:
        return {}
    spell_obj = find_spell_object(bin_data, spell_id)
    if not isinstance(spell_obj, dict):
        return {}
    spell = spell_obj.get("mSpell") or {}
    out: dict[str, Any] = {}
    base_damage = extract_primary_base_damage(spell_obj, maxrank)
    scaling = extract_scaling_coeffs(spell_obj, maxrank, tags)
    if base_damage:
        out["base_damage_by_rank"] = base_damage
    if scaling:
        out["scaling_coeff_by_rank"] = scaling
    cast_range = number_list(spell.get("castRangeDisplayOverride")) or number_list(spell.get("castRange"))
    if cast_range:
        out["cast_range"] = float(cast_range[0])
        out["range"] = float(cast_range[0])
    line_width = spell.get("mLineWidth")
    if isinstance(line_width, (int, float)):
        out["width"] = float(line_width)
    cast_radius = number_list(spell.get("castRadius"))
    if cast_radius:
        out["radius"] = float(cast_radius[0])
    missile_speed = spell.get("missileSpeed")
    if isinstance(missile_speed, (int, float)):
        out["speed"] = float(missile_speed)
    move_speed = (((spell.get("mMissileSpec") or {}).get("movementComponent") or {}).get("mSpeed"))
    if "speed" not in out and isinstance(move_speed, (int, float)):
        out["speed"] = float(move_speed)
    cast_time = spell.get("mCastTime")
    if isinstance(cast_time, (int, float)):
        out["cast_time"] = float(cast_time)
    targeting_type = cdragon_targeting_type(spell_obj)
    if targeting_type:
        out["targeting_type"] = targeting_type
        out["shape"] = targeting_type
    out["source_priority"] = "cdragon_spellcalc_plus_ddragon"
    out["source_note"] = "CommunityDragon bin spell data for base/scaling/geometry, with DDragon text fallback."
    return out


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def number_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            out.append(float(item))
    return out


def rank_series(values: list[float], expected_len: int) -> list[float]:
    if len(values) < expected_len:
        return []
    if len(values) >= expected_len + 1:
        trimmed = values[1 : 1 + expected_len]
    else:
        trimmed = values[:expected_len]
    if all(v == 0.0 for v in trimmed):
        return []
    return trimmed


def best_base_damage_series(ability: dict[str, Any]) -> list[float]:
    maxrank = int(ability.get("maxrank") or 0)
    candidates: list[list[float]] = []
    for raw in ability.get("effect") or []:
        nums = rank_series(number_list(raw), maxrank)
        if not nums:
            continue
        peak = max(nums)
        low = min(nums)
        if 20.0 <= peak <= 450.0 and peak > low:
            candidates.append(nums)
    if not candidates:
        return []
    candidates.sort(key=lambda xs: (max(xs), sum(xs)), reverse=True)
    return [round(v, 3) for v in candidates[0]]


def raw_text(ability: dict[str, Any]) -> str:
    return " ".join(
        str(ability.get(field) or "")
        for field in (
            "description_en_clean",
            "tooltip_en_clean",
            "description_en",
            "tooltip_en",
        )
    ).strip()


def infer_scaling_stat(alias: str, tags: set[str], ability: dict[str, Any]) -> str:
    slot = str(ability.get("slot") or "")
    text = raw_text(ability).lower()
    override = semantic.ability_stat_override(alias, slot, "scaling_stat")
    if isinstance(override, str) and override:
        return override
    ap_weight, ad_weight = semantic.infer_scaling_mix(alias, slot, text, tags)
    if ap_weight <= 0.0 and ad_weight <= 0.0:
        return "none"
    if ap_weight >= 0.75:
        return "ap"
    if ad_weight >= 0.75:
        return "bonus_ad"
    return "mixed"


def row_for_ability(champion: dict[str, Any], ability: dict[str, Any], version: str) -> dict[str, Any]:
    alias = str(champion.get("alias") or "")
    slot = str(ability.get("slot") or "")
    tags = set(champion.get("tags") or [])
    ctx = semantic.ability_context(champion, ability)
    text = str(ctx["text"])
    raw = str(ctx["raw_text"])
    rng = float(ctx["range"])
    cast_range = float(ctx["cast_range"])
    shape = str(ctx["shape"])
    width = float(ctx["width"])
    radius = float(ctx["radius"])
    cast_state = str(ctx["cast_state"])
    target_domain = str(ctx["target_domain"])
    build_record = semantic.build_profile_record(alias, tags)
    cdragon = enrich_from_cdragon(alias, str(ability.get("spell_id") or ""), int(ability.get("maxrank") or 0), tags)
    cc_type = semantic.infer_cc_type(alias, slot, text, bool(ctx["aoe"]), target_domain)
    targeting_bonus = float(semantic.infer_targeting_bonus(text, shape, rng))
    damage_series = list(cdragon.get("base_damage_by_rank") or best_base_damage_series(ability))
    scaling_coeffs = list(cdragon.get("scaling_coeff_by_rank") or [])
    scaling_stat = infer_scaling_stat(alias, tags, ability)
    if scaling_coeffs and isinstance(scaling_coeffs[0], dict):
        scaling_stat = str(scaling_coeffs[0].get("stat") or scaling_stat)
    condition_penalty = semantic.ability_stat_override(alias, slot, "condition_penalty")
    condition_penalty_value = (
        float(condition_penalty) if isinstance(condition_penalty, (int, float)) else 0.0
    )
    persistence = float(semantic.infer_persistence(alias, slot, text))
    self_commit = float(semantic.infer_self_commit(tags, text, shape, rng))

    return {
        "schema_version": "v1",
        "source_version": version,
        "champion_id": int(champion["champion_id"]),
        "champion_alias": alias,
        "champion_name_en": champion.get("name_en", ""),
        "champion_name_zh": champion.get("name_zh", ""),
        "champion_tags": list(champion.get("tags") or []),
        "spell_slot": slot,
        "spell_id": ability.get("spell_id", ""),
        "spell_name_en": ability.get("name_en", ""),
        "spell_name_zh": ability.get("name_zh", ""),
        "targeting_type": str(cdragon.get("targeting_type") or shape),
        "shape": str(cdragon.get("shape") or shape),
        "target_domain": target_domain,
        "effect_scope": str(ctx["effect_scope"]),
        "cast_state": cast_state,
        "range": round(float(cdragon.get("range") or rng), 3),
        "cast_range": round(float(cdragon.get("cast_range") or cast_range), 3),
        "width": round(float(cdragon.get("width") or width), 3),
        "radius": round(float(cdragon.get("radius") or radius), 3),
        "speed": round(float(cdragon.get("speed") or ctx["speed"]), 3),
        "cast_time": round(float(cdragon.get("cast_time") or ctx["cast_time"]), 3),
        "cooldown_by_rank": list(number_list(ability.get("cooldown"))),
        "cost_by_rank": list(number_list(ability.get("cost"))),
        "range_by_rank": list(number_list(ability.get("range"))),
        "base_damage_by_rank": damage_series,
        "scaling_stat": scaling_stat,
        "scaling_coeff_by_rank": scaling_coeffs,
        "cc_type": cc_type,
        "cc_duration": round(float(semantic.infer_cc_duration(alias, slot, cc_type, text)), 3),
        "expected_targets": round(float(semantic.infer_expected_targets(alias, slot, text, bool(ctx["aoe"]), shape)), 3),
        "entry_followthrough": round(float(semantic.infer_entry_followthrough(alias, slot, text, cc_type, bool(ctx["mobility"]), rng, cast_state)), 3),
        "certainty": round(float(semantic.infer_certainty(alias, slot, text, shape, targeting_bonus, cast_state)), 3),
        "targeting_bonus": round(targeting_bonus, 3),
        "condition_penalty": round(condition_penalty_value, 3),
        "pierce_bounce": round(float(semantic.infer_pierce_bounce(alias, slot, text, shape)), 3),
        "persistence": round(persistence, 3),
        "self_commit": round(self_commit, 3),
        "wave_reliability": round(float(semantic.infer_wave_reliability(alias, slot, text, shape, cast_range, target_domain, cast_state)), 3),
        "prep_bonus": round(float(semantic.infer_wave_prep_bonus(text, shape, cast_range, cast_state, persistence, self_commit, slot)), 3),
        "build_profile": build_record["profile"],
        "build_items": list(build_record["items"]),
        "build_ap": float(build_record["ap"]),
        "build_bonus_ad": float(build_record["bonus_ad"]),
        "raw_effect": ability.get("effect") or [],
        "raw_effect_burn": ability.get("effect_burn") or [],
        "raw_vars": ability.get("vars") or [],
        "raw_description_en": ability.get("description_en_clean", ""),
        "raw_tooltip_en": ability.get("tooltip_en_clean", ""),
        "source_priority": str(cdragon.get("source_priority") or "riot_ddragon_plus_reviewed_semantics"),
        "source_note": str(cdragon.get("source_note") or "DDragon arrays plus current reviewed semantic overrides; scaling coeffs still unparsed."),
    }


def build_feature_rows(ability_json: Path) -> list[dict[str, Any]]:
    raw = json.loads(ability_json.read_text(encoding="utf-8"))
    version = str(raw.get("version") or "")
    rows: list[dict[str, Any]] = []
    for champion in raw.get("champions", []):
        for ability in champion.get("abilities") or []:
            rows.append(row_for_ability(champion, ability, version))
    rows.sort(key=lambda row: (int(row["champion_id"]), str(row["spell_slot"])))
    return rows


def csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        flat = dict(row)
        for key in (
            "champion_tags",
            "cooldown_by_rank",
            "cost_by_rank",
            "range_by_rank",
            "base_damage_by_rank",
            "scaling_coeff_by_rank",
            "build_items",
            "raw_effect",
            "raw_effect_burn",
            "raw_vars",
        ):
            flat[key] = compact_json(flat.get(key))
        out.append(flat)
    return out


def write_csv(rows: list[dict[str, Any]], out_csv: Path) -> None:
    if not rows:
        raise click.ClickException("No skill semantic feature rows to write")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@click.command()
@click.option(
    "--ability-json",
    type=click.Path(exists=True, path_type=Path),
    default=Path("data/cache/champion_abilities.json"),
    show_default=True,
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=Path("data/cache/skill_semantic_features.json"),
    show_default=True,
)
@click.option(
    "--out-csv",
    type=click.Path(path_type=Path),
    default=Path("data/cache/skill_semantic_features.csv"),
    show_default=True,
)
def main(ability_json: Path, out_json: Path, out_csv: Path) -> None:
    rows = build_feature_rows(ability_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_rows(rows), out_csv)
    click.echo(f"[skill-semantic] wrote {len(rows)} skill rows")
    click.echo(f"[skill-semantic] json: {out_json}")
    click.echo(f"[skill-semantic] csv : {out_csv}")


if __name__ == "__main__":
    main()
