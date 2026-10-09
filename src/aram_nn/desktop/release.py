"""Export a public, JSON-only desktop dataset from a verified local model."""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .update import DATA_FILES, REPO, SCHEMA, atomic_json, read_json, validate_data


def resolve_release_tag(tag: str) -> str:
    """Use the same latest channel as installed clients, excluding incomplete releases."""
    if tag != "latest":
        return tag
    result = subprocess.run(["gh", "api", f"repos/{REPO}/releases/latest"],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    release = json.loads(result.stdout)
    required = {"ARAMRecommender.exe", "ARAMRecommender-windows.zip", "recommender-manifest.json"}
    if (release.get("draft") or release.get("prerelease")
            or not release.get("tag_name", "").startswith("recommender-v")
            or not required <= {a["name"] for a in release.get("assets", [])}):
        raise ValueError("Latest release is not a complete stable desktop application")
    return release["tag_name"]


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")), encoding="utf-8")


def export_data(model_dir: Path, names_path: Path, tier_path: Path, destination: Path,
                *, region: str, time_cutoff: str) -> dict:
    """Read local pickle once; whitelist only numeric inference fields publicly.

    Model rows, patch pool and held-out evidence are taken from the source model,
    not inferred from the website's freshness or from a directory name.
    """
    from aram_nn.recommend import load_composition_lr, load_synergy
    from aram_nn.role_synergy import RoleSynergyStats, save_role_synergy
    sources = [model_dir / name for name in ("summary.json", "model.pkl", "role_synergy.json", "lr_weights.json",
                                             "champ_to_idx.json", "single_team_calibration.json")]
    sources.extend([names_path, tier_path])
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in sources}
    summary = read_json(model_dir / "summary.json")
    current_patch = summary["current_patch"]
    if not region or datetime.fromisoformat(time_cutoff).tzinfo is None:
        raise ValueError("Explicit region and timezone-aware model cutoff are required")
    model = load_composition_lr(model_dir / "model.pkl")
    synergy = load_synergy(model_dir / "role_synergy.json")
    if not isinstance(synergy, RoleSynergyStats) or synergy.queue_id != 2400:
        raise ValueError("Only queue 2400 role synergy may be published")
    if current_patch not in synergy.patch_prefix.split(","):
        raise ValueError("Model and synergy patch pools differ")
    if synergy.total_matches != summary["shipped_model_rows"]:
        raise ValueError("Model and synergy row counts differ")
    evidence = summary["results"]["pooled_recency_7d"]["all"]
    if (not 0 < float(evidence["acc"]) < 0.65 or evidence["n"] <= 0
            or summary["candidate_minus_baseline_logloss"] > 0):
        raise ValueError("Model has no valid held-out verification")
    destination.mkdir(parents=True, exist_ok=True)
    _write(destination / "composition.json", {
        "schema_version": SCHEMA, "coef": model.coef.tolist(), "intercept": model.intercept,
        "feature_names": model.feature_names, "champ_to_idx": model.champ_to_idx,
        "champion_profiles": {str(cid): {
            "cid": p.cid, "scores": p.scores, "roles": p.roles,
            "physical_dpm": p.physical_dpm, "magic_dpm": p.magic_dpm, "true_dpm": p.true_dpm,
        } for cid, p in model.profiles.items()},
    })
    lr = read_json(model_dir / "lr_weights.json")
    _write(destination / "lr_weights.json", {"coef": lr["coef"], "intercept": lr["intercept"]})
    _write(destination / "champ_to_idx.json", read_json(model_dir / "champ_to_idx.json"))
    _write(destination / "single_team_calibration.json", {"single_team_intercept": model.single_team_intercept})
    save_role_synergy(synergy, destination / "role_synergy.json")
    names = read_json(names_path)
    _write(destination / "champion_names.json", {"champions": [
        {"champion_id": row["champion_id"], "alias": row.get("alias") or row.get("name_en")}
        for row in names["champions"]
    ]})
    # Only fields used by the GUI's champion win-rate sharing. Augment bridge is
    # disabled; do not distribute the full website payload or unrelated records.
    tier = read_json(tier_path)
    _write(destination / "tier-list.json", {"champs": {
        cid: {k: row[k] for k in ("name", "name_zh", "name_en", "wr", "g") if k in row}
        for cid, row in tier["champs"].items()
    }, "augs": {}})
    metadata = {
        "schema_version": SCHEMA, "queue_id": 2400, "current_patch": current_patch,
        "patches": synergy.patch_prefix.split(","), "region": region,
        "source": "LCU pooled composition LR; public aggregate inference data",
        "time_cutoff": time_cutoff, "row_count": summary["shipped_model_rows"],
        "half_life_days": summary["half_life_days"], "held_out": evidence,
        "exclusions": "Non-Mayhem queues and structurally invalid games excluded by training pipeline",
        "generated_at": time_cutoff,
        "tier_patch": tier.get("patch") or tier.get("patch_prefix") or tier.get("ddv"),
        "files": {name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
                  for name in sorted(DATA_FILES - {"metadata.json"})},
    }
    _write(destination / "metadata.json", metadata)
    validate_data(destination)
    if before != {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in sources}:
        raise ValueError("Source files changed during export; retry after model refresh completes")
    return metadata


def zip_data(directory: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(DATA_FILES):
            # Fixed ZIP timestamps make repeated exports reproducible apart from metadata.
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (directory / name).read_bytes())


def asset_info(path: Path, tag: str) -> dict:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"url": f"https://github.com/{REPO}/releases/download/{tag}/{path.name}",
            "sha256": digest, "size": path.stat().st_size}


def create_manifest(app: Path, data: Path, version: str, tag: str, metadata: dict) -> dict:
    asset = asset_info(data, tag)
    return {"schema_version": SCHEMA, "platform": "windows-x64",
            "app": {**asset_info(app, tag), "version": version},
            "data": {**asset, "version": asset["sha256"][:16], "schema_version": SCHEMA,
                     "queue_id": 2400, "current_patch": metadata["current_patch"],
                     "time_cutoff": metadata["time_cutoff"]}}


def publish_data(model_dir: Path, names: Path, tier: Path, output: Path, *, region: str,
                 time_cutoff: str, tag: str) -> dict:
    """Upload immutable data first; switch the manifest only after upload succeeds.

    The release already owns a built application. A failed data upload cannot
    invalidate its previous data manifest. Old data assets remain available.
    """
    channel = tag
    tag = resolve_release_tag(tag)
    result = subprocess.run(["gh", "release", "view", tag, "--repo", REPO, "--json", "assets"],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    assets = json.loads(result.stdout)["assets"]
    if not any(a["name"] == "recommender-manifest.json" for a in assets):
        raise ValueError("Publish a complete desktop release before updating data")
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "release", "download", tag, "--repo", REPO, "--pattern",
                    "recommender-manifest.json", "--dir", str(output), "--clobber"], check=True)
    manifest = read_json(output / "recommender-manifest.json")
    metadata = export_data(model_dir, names, tier, output / "dataset", region=region, time_cutoff=time_cutoff)
    archive = output / "data.zip"
    zip_data(output / "dataset", archive)
    info = asset_info(archive, tag)
    final = output / f"recommender-data-{info['sha256'][:16]}.zip"
    archive.replace(final)
    archive = final
    info = asset_info(archive, tag)
    if manifest["data"]["sha256"] == info["sha256"]:
        return manifest
    manifest["data"] = {**info, "version": info["sha256"][:16], "schema_version": SCHEMA,
                        "queue_id": 2400, "current_patch": metadata["current_patch"], "time_cutoff": time_cutoff}
    existing = next((a for a in assets if a["name"] == archive.name), None)
    if existing:
        if existing.get("digest") != f"sha256:{info['sha256']}" or existing.get("size") != info["size"]:
            raise ValueError("An immutable data asset with different content already exists")
    else:
        subprocess.run(["gh", "release", "upload", tag, str(archive), "--repo", REPO], check=True)
    if channel == "latest" and resolve_release_tag(channel) != tag:
        raise ValueError("Latest application changed during data upload; retry next cycle")
    atomic_json(output / "recommender-manifest.json", manifest)
    subprocess.run(["gh", "release", "upload", tag, str(output / "recommender-manifest.json"),
                    "--repo", REPO, "--clobber"], check=True)
    return manifest


def publish_refreshed_models(*, model_dir: Path, parquet: Path, tag: str,
                             region: str, output: Path, names: Path, tier: Path) -> bool:
    """Retry independent public publishing even when the training gate is closed.

    A local refresh remains successful if GitHub is temporarily unavailable.
    The publish watermark moves only after the manifest is uploaded.
    """
    import pyarrow.parquet as pq
    channel = tag
    tag = resolve_release_tag(tag)
    inputs = [model_dir / name for name in ("model.pkl", "summary.json", "lr_weights.json", "champ_to_idx.json",
                                            "role_synergy.json", "single_team_calibration.json")]
    inputs.extend([names, tier])
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    identity = digest.hexdigest()
    state_path = output / "publish-state.json"
    try:
        previous = read_json(state_path)
        if previous.get("source_hash") == identity and previous.get("tag") == tag:
            return False
    except (OSError, ValueError):
        pass
    summary = read_json(model_dir / "summary.json")
    file = pq.ParquetFile(parquet)
    if file.metadata.num_rows != summary["shipped_model_rows"]:
        raise ValueError("Parquet and shipped model row counts differ")
    index = file.schema.names.index("game_creation_ms")
    maxima = []
    for group in range(file.metadata.num_row_groups):
        statistics = file.metadata.row_group(group).column(index).statistics
        if not statistics or not statistics.has_min_max:
            raise ValueError("Cannot establish actual training dataset cutoff")
        maxima.append(statistics.max)
    cutoff = datetime.fromtimestamp(max(maxima) / 1000, timezone.utc).isoformat()
    publish_data(model_dir, names, tier, output, region=region, time_cutoff=cutoff, tag=tag)
    if channel == "latest" and resolve_release_tag(channel) != tag:
        raise ValueError("Latest application changed during publication; retry next cycle")
    atomic_json(state_path, {"source_hash": identity, "time_cutoff": cutoff, "tag": tag})
    return True
