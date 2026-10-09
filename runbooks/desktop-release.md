# Windows desktop release

## Contract

- Target Windows 10/11 x64. Distribute a self-contained `ARAMRecommender.exe`; ZIP contains that same EXE and a short README. No Python or admin installation is required.
- The packaged app checks the public GitHub latest stable release's `recommender-manifest.json` at startup. Source GUI keeps its existing local model behavior.
- The manifest declares schema, Windows architecture, app version, independent data version, byte sizes and SHA-256. Downloads are HTTPS assets in this repository only. No GitHub token is shipped.
- Data packages contain a flat JSON allowlist; downloaded pickle, executable data and paths outside the installation are rejected. Composition JSON must reproduce local pickle inference exactly.
- App and data install under `%LOCALAPPDATA%/ARAMRecommender`. Immutable files are verified before atomically switching pointers. Partial downloads resume on the next launch; corrupt, incompatible or interrupted updates preserve the previous complete data. The original download can launch a newer cached app even offline.
- Closing startup cancels this launch; it does not activate partial files. Update failures appear in the GUI and `%LOCALAPPDATA%/ARAMRecommender/last-update.json`. `--no-update` deliberately uses the bundled model for diagnostics.
- Queue 2400, region, patch pool, actual training dataset cutoff, row count and held-out evidence remain attached to the data. A newer website does not establish a newer model cutoff.
- EXEs remain unsigned until a code-signing identity is configured. Do not disable SmartScreen or antivirus to make a test pass.

## Build and publish

Work in a task worktree. Read ignored inputs via `--input-root`, never link a model directory or move live DB files. First obtain the actual `game_creation_ms` maximum from the pooled parquet used to fit the shipped model and check its row count against `summary.json`.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python scripts/build_recommender_exe.py --version 2026.10.09.2 --input-root D:/Projects/CODING/aram-winrate-nn --region TW --time-cutoff '2026-10-08T21:26:28.219000+00:00'
```

This validates the existing held-out model, snapshots whitelisted public JSON, builds a Windows x64 single-file executable, creates its convenience ZIP and data archive, and writes the matching manifest. Never upload a manifest referring to an incomplete asset set. Create the release as a draft, upload all four assets, then publish it as latest only after local portable smoke tests pass. Tag the exact source commit used for the build; preserve old release history.

Required verification: updater tests (resume/restart/digest/corrupt file/schema/queue/path traversal/atomic fallback/concurrency/offline cache), source-to-JSON numerical parity, EXE `--self-test --no-update` in an empty Chinese-and-space directory with Python env/PATH removed, then public `--self-test` with a fresh LocalAppData. Run `Windows desktop download smoke` on the release source branch for hosted clean Windows checks. Windows Server runners are compatibility evidence, not a substitute for Windows 10/11 player hardware or live LCU tests.

## Publish new data without rebuilding

`publish_recommender_data.py` exports JSON from a completed local model, uploads a content-addressed ZIP to the existing app release, then replaces the small manifest. Older data assets stay available. Manifest replacement can briefly return 404; clients keep their prior complete model and retry next launch.

```powershell
python scripts/publish_recommender_data.py --input-root D:/Projects/CODING/aram-winrate-nn --tag recommender-v2026.10.09.2 --region TW --time-cutoff '<actual training cutoff with timezone>'
```

The existing refresher supports opt-in `--desktop-release-tag recommender-v2026.10.09.2 --desktop-region TW`. It derives the cutoff from the matching parquet, compares source fingerprints, and retries public publishing independently on each watch cycle. This flag requires the repository owner's GitHub credentials on the publisher machine, never on users' computers. No publication occurs under `--dry-run` or `--check-only`.

Opt-in source support does not enable a running production process. Deploy the reviewed source, preserve the production wrapper's existing argv, add these two flags to the refresher child, and verify its next `[desktop-publish]` log before claiming future model refreshes publish automatically. Changing the desktop release tag requires updating this publisher target too.

An old May-2026 EXE has no updater. Existing users must download this new updater-capable EXE once; subsequent app and data updates are automatic at startup.
