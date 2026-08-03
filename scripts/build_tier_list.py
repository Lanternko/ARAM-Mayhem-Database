"""ARAM/Mayhem tier-list site builder — CLI entry point.

Compute engine lives in tierlist_engine.py, rendering in tierlist_render.py;
this module wires them behind the click CLI.  Kept importable as `build_tier_list`
(re-exports every engine/render symbol) for existing callers.
"""
from __future__ import annotations
import os as _os, subprocess as _subprocess, sys as _sys
_SCRIPT_DIR = _os.path.dirname(_os.path.abspath(__file__))
_ROOT_DIR = _os.path.dirname(_SCRIPT_DIR)
_sys.path.insert(0, _os.path.join(_ROOT_DIR, "src"))
_sys.path.insert(0, _SCRIPT_DIR)
import tierlist_engine as _eng  # noqa: E402,F401
import tierlist_render as _rnd  # noqa: E402,F401
for _m in (_eng, _rnd):
    globals().update({_k: _v for _k, _v in vars(_m).items() if not _k.startswith('__')})


PRODUCTION_SITE_URL = "https://arammeta.com"
PRODUCTION_META_PICK_API_URL = "https://api.arammeta.com"


def refresh_empirical_champion_profiles(
    *, db: Path, queue_id: int, patch_prefix: str | None
) -> bool:
    """Refresh the comp capability vectors before per-patch table training.

    The composition categories depend on empirical damage/frontline/sustain
    profiles.  Rebuilding only the residual tables while leaving those vectors
    on an older patch would still be a partial stale-model bug.
    """
    if not patch_prefix:
        return False
    command = [
        _sys.executable,
        _os.path.join(_SCRIPT_DIR, "build_empirical_champion_scores.py"),
        "--db",
        str(db),
        "--queue",
        str(queue_id),
        "--patch-prefix",
        str(patch_prefix),
    ]
    result = _subprocess.run(command, check=False)
    if result.returncode != 0:
        click.echo(
            "[tierlist] WARN: empirical champion-profile refresh failed; "
            "using the previous profile artifact"
        )
        return False
    click.echo(f"[tierlist] refreshed empirical champion profiles for patch {patch_prefix}")
    return True


def team_score_artifact_path(*, queue_id: int, patch_prefix: str | None) -> Path:
    artifact_patch = str(patch_prefix or "all").replace("/", "_").replace("\\", "_")
    return Path("data/cache") / f"team_score_{queue_id}_{artifact_patch}.json"


def load_team_score_artifact(*, queue_id: int, patch_prefix: str | None) -> dict | None:
    artifact = team_score_artifact_path(queue_id=queue_id, patch_prefix=patch_prefix)
    if not artifact.exists():
        return None
    try:
        return json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        click.echo(f"[tierlist] WARN: cached team-score artifact unreadable: {exc}")
        return None


def train_and_save_team_score_bundle(
    *,
    db: Path,
    queue_id: int,
    patch_prefix: str | None,
    champ_profiles: dict,
    champ_meta: dict,
    min_synergy_games: int,
) -> dict:
    from aram_nn.site.team_score_training import train_team_score_bundle

    bundle = train_team_score_bundle(
        db,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        champ_profiles=champ_profiles,
        champ_meta=champ_meta,
        lack_thresholds=COMPOSITION_LACK_THRESHOLDS,
        table_weights=RECOMMENDATION_COMPOSITION_TABLE_WEIGHTS,
        composition_clamp=RECOMMENDATION_COMPOSITION_CLAMP,
        min_synergy_games=min_synergy_games,
    )
    score_meta = bundle["team_score"]
    score_metrics = score_meta["metrics"]["test"]
    click.echo(
        "[tierlist] team score retrained "
        f"patch={patch_prefix} games={score_meta['trained_games']:,} "
        f"pair_prior={score_meta['pair_prior_games']:g} "
        f"pair_logit={score_meta['pair_logit_weight']:.3f} "
        f"composition_logit={score_meta['composition_logit_weight']:.3f} "
        f"test_auc={score_metrics['base']['auc']:.4f}->{score_metrics['full']['auc']:.4f} "
        f"test_ll={score_metrics['base']['log_loss']:.5f}->{score_metrics['full']['log_loss']:.5f}"
    )
    artifact = team_score_artifact_path(queue_id=queue_id, patch_prefix=patch_prefix)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    click.echo(f"[tierlist] wrote {artifact}")
    return bundle


def resolve_meta_pick_api_url(site_url: str, meta_pick_api_url: str) -> str:
    """Keep production builds wired to the one public leaderboard API."""
    normalized_site = site_url.strip().rstrip("/")
    normalized_api = meta_pick_api_url.strip().rstrip("/")
    if normalized_site != PRODUCTION_SITE_URL:
        return normalized_api
    if not normalized_api:
        return PRODUCTION_META_PICK_API_URL
    if normalized_api != PRODUCTION_META_PICK_API_URL:
        raise click.ClickException(
            "production site requires --meta-pick-api-url "
            f"{PRODUCTION_META_PICK_API_URL!r}; got {normalized_api!r}"
        )
    return normalized_api




@click.command()
@click.option("--db", type=click.Path(path_type=Path), default=Path("data/lcu/games.db"))
@click.option("--queue", "queue_id", type=int, default=2400, help="450=ARAM, 2400=Mayhem")
@click.option("--patch-prefix", default="auto", help='"auto" detects from DB; explicit e.g. "16.11"; "" for all patches')
@click.option("--ddragon-version", default=None, help="Override Data Dragon version (default: latest)")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=Path("docs/index.html"),
              help="Output HTML path (default: docs/index.html — the only non-root folder GitHub Pages serves from)")
@click.option("--min-games", type=int, default=50, help="Drop champions below this game count")
@click.option("--min-pair-games", type=int, default=15, help="Min games per (champ, augment) pair")
# 10, lowered from 40.  The old floor existed to hide raw lifts that were wild
# below ~40 games; the shrinkage at PAIR_PREV_PATCH_PRIOR_GAMES removes that noise
# instead of hiding it, so the floor now only controls coverage.  Replaying 16.14 at
# a 10,000-game patch: floor 40 shows 500 pairs / 73 of 173 champions, floor 10 shows
# 8,101 pairs / 169 champions at 2.12pp RMSE -- still 3x better than the 7.19pp the
# old raw-lift-at-floor-40 build shipped.
@click.option("--min-synergy-games", type=int, default=10,
              help="Min games per same-team champion pair for synergy / recommendation ranking")
@click.option("--top-n", type=int, default=16,
              help="Max best augments per rarity (default 16; 0 keeps all — bloats payload)")
@click.option("--bot-n", type=int, default=12,
              help="Max worst augments per rarity (default 12; 0 keeps all — bloats payload)")
@click.option("--site-url", default="",
              help="Canonical URL (used for OG og:url + <link rel=canonical>), e.g. https://user.github.io/repo/")
@click.option("--og-image", default="",
              help="Override the og:image URL (default: generated og-image.png under --site-url)")
@click.option("--build-date", default="",
              help="Date stamp shown in footer (default: today, YYYY-MM-DD)")
@click.option("--cloudflare-analytics-token", envvar="CLOUDFLARE_ANALYTICS_TOKEN", default="",
              help="Cloudflare Web Analytics token; can also be set via CLOUDFLARE_ANALYTICS_TOKEN")
@click.option("--ga-measurement-id", envvar="GA_MEASUREMENT_ID", default="",
              help="GA4 measurement id, e.g. G-XXXXXXXXXX; can also be set via GA_MEASUREMENT_ID")
@click.option("--payload-out", type=click.Path(path_type=Path), default=None,
              help="Write the frontend DATA payload as JSON for split frontend/backend deployment.")
@click.option("--payload-url", default="",
              help="Have the generated HTML fetch DATA from this URL instead of embedding it inline.")
@click.option("--shell-only", is_flag=True, default=False,
              help="Fast frontend-preview rebuild: regenerate ONLY index.html from the existing "
                   "tier-list.json payload, skipping all win-rate / augment / affinity compute "
                   "(seconds vs the full multi-minute build). Champ grid + CSS/JS render as in "
                   "production and search still works (the client rebuilds its search index from "
                   "the payload on load). Use while iterating on site.css / site.js / copy; run a "
                   "normal build to refresh the data.")
@click.option("--reuse-model-artifacts", is_flag=True, default=False,
              help="Refresh current-patch statistics while reusing cached empirical profiles "
                   "and team-score artifacts; useful when only the data payload changed.")
@click.option("--skip-patch-comparison", is_flag=True, default=False,
              help="Skip the expensive item/augment version-comparison tables for a data-only refresh.")
@click.option(
    "--meta-pick-api-url",
    envvar="ARAM_META_PICK_API_URL",
    default="",
    help="Base URL for Meta Pick leaderboard API (e.g. https://api.example.com). "
         "Empty disables remote submit/board. Injected into site.js as __META_PICK_API_BASE__.",
)
def main(
    db: Path,
    queue_id: int,
    patch_prefix: str,
    ddragon_version: str | None,
    out_path: Path,
    min_games: int,
    min_pair_games: int,
    min_synergy_games: int,
    top_n: int,
    bot_n: int,
    site_url: str,
    og_image: str,
    build_date: str,
    cloudflare_analytics_token: str,
    ga_measurement_id: str,
    payload_out: Path | None,
    payload_url: str,
    shell_only: bool,
    reuse_model_artifacts: bool,
    skip_patch_comparison: bool,
    meta_pick_api_url: str,
) -> None:
    meta_pick_api_url = resolve_meta_pick_api_url(site_url, meta_pick_api_url)
    if site_url.strip().rstrip("/") == PRODUCTION_SITE_URL:
        click.echo(f"[tierlist] production Meta Pick API: {meta_pick_api_url}")

    if patch_prefix == "auto":
        from aram_nn.site.db import SITE_PATCH_MIN_GAMES, latest_patch_prefix as _latest
        # Same maturity floor the publisher uses (SITE_PATCH_MIN_GAMES): a patch that
        # is too thin to fill the synergy / item sections must not win "auto" here
        # either.  fallback_latest stays on so a fresh DB with no mature patch still
        # builds something rather than hard-failing.
        patch_prefix = _latest(db, queue_id=queue_id, min_games=SITE_PATCH_MIN_GAMES)
        if not patch_prefix:
            click.echo("[tierlist] error: could not auto-detect patch from DB", err=True)
            raise SystemExit(1)
        click.echo(
            f"[tierlist] auto-detected patch prefix: {patch_prefix} "
            f"(maturity floor {SITE_PATCH_MIN_GAMES:,} games)"
        )
    else:
        patch_prefix = patch_prefix or None
    click.echo(f"[tierlist] db={db}  queue={queue_id}  patch_prefix={patch_prefix}")

    if shell_only:
        _run_shell_only(
            out_path=out_path, db=db, queue_id=queue_id, patch_prefix=patch_prefix,
            payload_out=payload_out, payload_url=payload_url, site_url=site_url,
            og_image=og_image, build_date=build_date,
            cloudflare_analytics_token=cloudflare_analytics_token,
            ga_measurement_id=ga_measurement_id, min_pair_games=min_pair_games,
            min_synergy_games=min_synergy_games,
            meta_pick_api_url=meta_pick_api_url,
        )
        return

    version, champ_meta = load_champion_metadata(ddragon_version)
    click.echo(f"[tierlist] data dragon version: {version}")

    aug_meta = load_augment_metadata(cache_dir=Path("data/cache"))
    desc_n = sum(1 for v in aug_meta.values() if v.get("desc"))
    click.echo(
        f"[tierlist] augment catalogue: {len(aug_meta)} entries "
        f"({desc_n} with zh-TW description)"
    )
    item_meta = load_item_metadata(cache_dir=Path("data/cache"), ddragon_version=version)
    click.echo(f"[tierlist] item catalogue: {len(item_meta)} entries")
    spell_meta = load_summoner_spell_metadata(version)
    click.echo(f"[tierlist] summoner-spell catalogue: {len(spell_meta)} entries")

    # The previous patch is scanned for the version-comparison view and, while the
    # current patch is still small, as an early-patch prior.  A mature current patch
    # is deliberately published from its own data only; otherwise a fixed prior can
    # hide a real balance shift in the headline number.
    baseline_patch_prefix = previous_patch_prefix(patch_prefix)
    current_total_games = count_patch_games(db, queue_id, patch_prefix)
    use_prev_patch_blend = current_total_games < CURRENT_PATCH_MATURE_GAMES
    click.echo(
        f"[tierlist] current patch games={current_total_games:,}; "
        + (
            f"early-patch prior enabled (< {CURRENT_PATCH_MATURE_GAMES:,})"
            if use_prev_patch_blend
            else f"mature-patch cutoff reached (>= {CURRENT_PATCH_MATURE_GAMES:,}); current data only"
        )
    )
    baseline_champ_records: list[dict] = []
    baseline_champ_aug: list[dict] = []
    prev_wr_by_champ: dict[int, float] = {}
    prev_pair_lift: dict[tuple[int, int], tuple[float, int]] = {}
    if baseline_patch_prefix:
        baseline_champ_records, baseline_champ_aug, baseline_champ_pairs = compute_winrates(
            db, queue_id, baseline_patch_prefix
        )
        if use_prev_patch_blend:
            prev_wr_by_champ = {
                int(r["champion_id"]): float(r["raw_wr"])
                for r in baseline_champ_records
                if int(r["games"]) >= CHAMP_PREV_PATCH_MIN_GAMES
            }
            # Raw (unshrunk) baseline lifts: compute_winrates shrinks them on the way
            # in, not on the way out, so the prior must stay raw here.
            prev_pair_lift = {
                (int(r["champion_id"]), int(r["teammate_id"])): (
                    float(r["lift"]),
                    int(r["games"]),
                )
                for r in baseline_champ_pairs
            }
            click.echo(
                f"[tierlist] prev-patch WR prior: {len(prev_wr_by_champ)} champions from "
                f"{baseline_patch_prefix} (k={CHAMP_PREV_PATCH_PRIOR_GAMES:g} pseudo-games, "
                f"min {CHAMP_PREV_PATCH_MIN_GAMES} games)"
            )
            click.echo(
                f"[tierlist] prev-patch synergy prior: {len(prev_pair_lift):,} pairs from "
                f"{baseline_patch_prefix} (k={PAIR_PREV_PATCH_PRIOR_GAMES:g}, "
                f"prior shrink {PAIR_PREV_PATCH_SHRINK_GAMES:g})"
            )
        else:
            click.echo(
                f"[tierlist] skipping previous-patch smoothing for current stats; "
                f"{baseline_patch_prefix} remains comparison-only"
            )

    all_champ_records, champ_aug, champ_pairs = compute_winrates(
        db,
        queue_id,
        patch_prefix,
        prev_wr_by_champ=prev_wr_by_champ or None,
        prev_pair_lift=prev_pair_lift if baseline_patch_prefix else None,
    )
    total_games = sum(r["games"] for r in all_champ_records) // 10
    # Games that actually carry augment data — the denominator for the augment
    # board's per-game appearance rate (see build_augment_global_stats).
    aug_appearance_games = count_participant_games(db, queue_id, patch_prefix)
    champ_records = [r for r in all_champ_records if r["games"] >= min_games]
    click.echo(f"[tierlist] {len(champ_records)} champions after min_games={min_games}")
    click.echo(f"[tierlist] {len(champ_aug):,} (champ, augment) pairs total")
    click.echo(f"[tierlist] {len(champ_pairs):,} ordered same-team champion pairs total")

    patch_changes = None
    new_aug_ids: frozenset[int] = frozenset()
    if baseline_patch_prefix and not skip_patch_comparison:
        # baseline_champ_records / baseline_champ_aug were already scanned above to
        # build the prev-patch WR prior; reuse them rather than rescanning the patch.
        baseline_total_games = sum(r["games"] for r in baseline_champ_records) // 10
        new_aug_ids = derive_recent_augment_ids(
            db,
            queue_id,
            patch_prefix,
            champ_aug,
            baseline_prefix=baseline_patch_prefix,
            baseline_champ_aug=baseline_champ_aug,
        )
        click.echo(
            f"[tierlist] {len(new_aug_ids)} augments new within last "
            f"{NEW_AUGMENT_PATCH_WINDOW} patches (through {patch_prefix}); "
            f"flagged with the 新 category chip"
        )
        if baseline_total_games:
            patch_changes = compute_patch_changes(
                db,
                queue_id,
                patch_prefix,
                baseline_patch_prefix,
                item_meta,
                champ_meta,
                all_champ_records,
                baseline_champ_records,
                champ_aug_records=champ_aug,
                baseline_champ_aug=baseline_champ_aug,
                aug_meta=aug_meta,
            )
            if patch_changes:
                click.echo(
                    f"[tierlist] patch changes: {baseline_patch_prefix} -> {patch_prefix} "
                    f"({baseline_total_games:,} vs {total_games:,} games)"
                )
    elif baseline_patch_prefix and skip_patch_comparison:
        click.echo("[tierlist] skipped cross-patch augment discovery and comparison tables")

    aug_prior_strength = estimate_augment_prior_strength(champ_aug)
    click.echo(
        f"[tierlist] augment EB prior strength k={aug_prior_strength:.1f} "
        f"(posterior q={AUGMENT_POSTERIOR_Q:.2f}, pick_weight={AUGMENT_PICK_LIFT_WEIGHT:g})"
    )
    affinity_min_games = max(min_pair_games * 3, 45)
    item_style_min_games = max(affinity_min_games, ITEM_STYLE_MIN_GAMES)
    augment_type_min_games = max(affinity_min_games, AUGMENT_TYPE_MIN_GAMES)
    if reuse_model_artifacts:
        click.echo("[tierlist] reusing cached empirical profiles and team-score artifact")
    else:
        refresh_empirical_champion_profiles(
            db=db,
            queue_id=queue_id,
            patch_prefix=patch_prefix,
        )
    champ_profiles = load_champion_pick_profiles(champ_meta)
    if reuse_model_artifacts:
        team_score_bundle = load_team_score_artifact(
            queue_id=queue_id,
            patch_prefix=patch_prefix,
        )
        if team_score_bundle is None:
            click.echo("[tierlist] WARN: no cached team-score artifact; using legacy fallback")
    else:
        team_score_bundle = None
        try:
            team_score_bundle = train_and_save_team_score_bundle(
                db=db,
                queue_id=queue_id,
                patch_prefix=patch_prefix,
                champ_profiles=champ_profiles,
                champ_meta=champ_meta,
                min_synergy_games=min_synergy_games,
            )
        except ValueError as exc:
            click.echo(f"[tierlist] WARN: {exc}; using explicit legacy team-score fallback")
        except Exception as exc:
            click.echo(f"[tierlist] WARN: team-score retraining failed: {exc}; using legacy fallback")
    set_affinity, item_style_affinity, augment_type_affinity = compute_champ_category_affinities(
        db,
        queue_id,
        patch_prefix,
        aug_meta,
        item_meta,
        champ_records,
        champ_profiles,
        min_set_games=affinity_min_games,
        min_item_games=item_style_min_games,
        min_augtype_games=augment_type_min_games,
    )
    click.echo(
        f"[tierlist] {len(set_affinity)} champions have >= 1 augment-set affinity row "
        f"(games >= {affinity_min_games})"
    )
    click.echo(
        f"[tierlist] {len(item_style_affinity)} champions have >= 1 item-style affinity row "
        f"(games >= {item_style_min_games})"
    )
    item_pair_affinity = compute_champ_item_pair_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=ITEM_PAIR_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(item_pair_affinity)} champions have >= 1 core item-pair row "
        f"(games >= {ITEM_PAIR_MIN_GAMES}, pick >= {ITEM_PAIR_TOP_MIN_PICK_RATE:.1%} of champ games, "
        f"top_lift >= {ITEM_PAIR_TOP_MIN_LIFT:.1%})"
    )
    single_item_affinity = compute_champ_single_item_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=SINGLE_ITEM_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(single_item_affinity)} champions have >= 1 single-item row "
        f"(games >= {SINGLE_ITEM_MIN_GAMES}, pick >= {SINGLE_ITEM_TOP_MIN_PICK_RATE:.1%} of champ games, "
        f"top_lift >= {SINGLE_ITEM_TOP_MIN_LIFT:.1%})"
    )
    boot_item_affinity = compute_champ_boot_item_affinities(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        min_games=BOOT_ITEM_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(boot_item_affinity)} champions have >= 1 boot row "
        f"(games >= {BOOT_ITEM_MIN_GAMES}, pick >= {BOOT_ITEM_TOP_MIN_PICK_RATE:.1%} of champ games, "
        f"top_lift >= {BOOT_ITEM_TOP_MIN_LIFT:.1%})"
    )
    spell_affinity = compute_champ_spell_affinities(
        db,
        queue_id,
        patch_prefix,
        spell_meta,
        champ_records,
        min_games=SPELL_MIN_GAMES,
    )
    click.echo(
        f"[tierlist] {len(spell_affinity)} champions have >= 1 summoner-spell row "
        f"(games >= {SPELL_MIN_GAMES}, top_lift >= {SPELL_TOP_MIN_LIFT:.1%})"
    )
    item_build_clusters = compute_champ_item_build_clusters(
        db,
        queue_id,
        patch_prefix,
        item_meta,
        champ_records,
        single_item_affinity,
    )
    click.echo(
        f"[tierlist] {len(item_build_clusters)} champions have >= 1 core-2 build route "
        f"(core_games >= {ITEM_CORE_BUILD_MIN_GAMES}, pairing item games >= {ITEM_CORE_BUILD_OPTION_MIN_GAMES} "
        f"& (pick >= {ITEM_CORE_BUILD_OPTION_MIN_PICK:.1%} or LCB-lift > {ITEM_CORE_BUILD_WINRATE_MIN_LCB:+.0%} on >= {ITEM_CORE_BUILD_WINRATE_MIN_GAMES} games), "
        f"max_items={ITEM_CLUSTER_MAX_ITEMS}); up to {ITEM_CORE_BUILD_OPTION_TOP_N} pairing items per core-2"
    )
    click.echo(
        f"[tierlist] {len(augment_type_affinity)} champions have >= 1 augment-type affinity row "
        f"(games >= {augment_type_min_games})"
    )
    dual_role_count = sum(1 for meta in champ_meta.values() if len(meta.get("tags") or []) > 1)
    click.echo(
        f"[tierlist] using fixed site role tags from scripts/champion_roles.py "
        f"({dual_role_count} dual-role champions)"
    )
    role_spec_path = out_path.parent / "champion-roles.json"
    write_role_definitions_json(
        role_spec_path,
        champ_meta=champ_meta,
        data_dragon_version=version,
        patch_prefix=patch_prefix,
    )
    click.echo(f"[tierlist] wrote {role_spec_path}")

    picks = build_champ_augment_picks(
        champ_aug,
        aug_meta,
        champ_profiles,
        min_games_per_pair=min_pair_games,
        top_n=top_n,
        bot_n=bot_n,
        prior_strength=aug_prior_strength,
    )
    click.echo(
        f"[tierlist] {len(picks)} champions have >= 1 rarity-bucketed pair "
        f"(games >= {min_pair_games})"
    )
    # prior_games=0: this ranks rows for payload slimming, and `lift` already
    # carries its shrinkage (PAIR_PREV_PATCH_PRIOR_GAMES).  Re-applying the
    # trained pair prior here would shrink a second time and bias slimming toward
    # high-sample partners -- the same double-shrink _team_score_for_payload
    # disables on the client side.  Keep the two in step.
    synergy = build_champ_synergy_index(
        champ_pairs,
        min_games=min_synergy_games,
        prior_games=0.0,
    )
    click.echo(
        f"[tierlist] {len(synergy)} champions have >= 1 teammate synergy row "
        f"(games >= {min_synergy_games})"
    )

    if not build_date:
        build_date = _dt.date.today().isoformat()

    if cloudflare_analytics_token:
        click.echo("[tierlist] Cloudflare Web Analytics enabled")
    if ga_measurement_id:
        click.echo(f"[tierlist] GA4 enabled: {ga_measurement_id}")

    if not og_image:
        og_asset_path = out_path.parent / "og-image.png"
        try:
            write_og_image(
                og_asset_path,
                champ_records,
                champ_meta,
                queue_id=queue_id,
                patch_prefix=patch_prefix,
                total_games=total_games,
            )
            click.echo(f"[tierlist] wrote {og_asset_path}  ({og_asset_path.stat().st_size:,} bytes)")
            if site_url:
                # Content hash of the just-written thumbnail, not the date, so the
                # cache-bust changes iff the image did (see og_cache_bust).
                og_version = og_cache_bust(og_asset_path, build_date)
                og_image = site_url.rstrip("/") + "/" + og_asset_path.name + f"?v={og_version}"
        except Exception as exc:
            click.echo(f"[tierlist] WARN: og image generation failed: {exc}")

    favicon_outputs = write_favicon_assets(out_path.parent)
    for asset_path in favicon_outputs:
        click.echo(f"[tierlist] wrote {asset_path}  ({asset_path.stat().st_size:,} bytes)")

    aug_global = build_augment_global_stats(
        champ_aug,
        aug_meta,
        appearance_games=aug_appearance_games,
        prev_champ_aug_records=baseline_champ_aug if use_prev_patch_blend else None,
    )
    blended_n = sum(1 for v in aug_global.values() if v.get("prevMix", 0))
    click.echo(
        f"[tierlist] augment tier: {len(aug_global)} augments rolled up to a global win-rate "
        + (
            f"(current {patch_prefix}; {blended_n} thin augments topped up from "
            f"{baseline_patch_prefix or 'n/a'} below {AUGMENT_CURRENT_MIN_GAMES} games)"
            if use_prev_patch_blend
            else f"(current {patch_prefix}; current-patch data only)"
        )
    )
    html = render_html(
        champ_records,
        champ_meta,
        champ_profiles,
        picks,
        set_affinity,
        item_pair_affinity,
        single_item_affinity,
        boot_item_affinity,
        spell_affinity,
        item_build_clusters,
        augment_type_affinity,
        synergy,
        aug_meta,
        patch_changes,
        new_aug_ids=new_aug_ids,
        queue_id=queue_id,
        patch_prefix=patch_prefix,
        ddragon_version=version,
        total_games=total_games,
        min_games_per_pair=min_pair_games,
        min_synergy_games=min_synergy_games,
        site_url=site_url,
        og_image=og_image,
        build_date=build_date,
        cloudflare_analytics_token=cloudflare_analytics_token,
        ga_measurement_id=ga_measurement_id,
        payload_out_path=payload_out,
        payload_url=payload_url,
        icon_assets_dir=out_path.parent / "assets" / "icons",
        aug_global=aug_global,
        script_assets_dir=out_path.parent / "assets",
        meta_pick_api_url=meta_pick_api_url,
        team_score_bundle=team_score_bundle,
    )
    if payload_out is not None:
        click.echo(f"[tierlist] wrote {payload_out}  ({payload_out.stat().st_size:,} bytes)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    click.echo(f"[tierlist] wrote {out_path}  ({out_path.stat().st_size:,} bytes)")
    mirrors = write_spa_path_shells(out_path, site_url=site_url, og_image=og_image)
    if mirrors:
        click.echo(f"[tierlist] wrote {len(mirrors)} clean-path deep-link stubs (+ 404.html)")
    info_pages = write_site_info_pages(
        out_path,
        site_url=site_url,
        build_date=build_date,
    )
    if info_pages:
        click.echo(f"[tierlist] wrote {len(info_pages)} site information file(s)")

    # Hand-made article cover banners live (committed) under docs/assets/covers
    # and are referenced as assets/covers/<file>.  Mirror them into the build
    # output dir so non-docs builds (e.g. the outputs/ preview) resolve the same
    # relative URLs.  No-op for a docs/ build (src == dest) or if none exist yet.
    covers_src = Path(__file__).resolve().parent.parent / "docs" / "assets" / "covers"
    covers_dest = out_path.parent / "assets" / "covers"
    if covers_src.is_dir() and covers_src.resolve() != covers_dest.resolve():
        shutil.copytree(covers_src, covers_dest, dirs_exist_ok=True)
        n_cov = sum(1 for p in covers_dest.iterdir() if p.is_file())
        click.echo(f"[tierlist] mirrored {n_cov} cover image(s) -> {covers_dest}")

    # GitHub Pages: prevent Jekyll preprocessing (we don't have any _-prefixed
    # files today, but adding the marker keeps it that way as we evolve).
    nojekyll = out_path.parent / ".nojekyll"
    if not nojekyll.exists():
        nojekyll.write_text("", encoding="utf-8")
        click.echo(f"[tierlist] wrote {nojekyll}")

if __name__ == "__main__":
    main()
