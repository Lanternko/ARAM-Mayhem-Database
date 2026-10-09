# Windows desktop release

## Contract

- Target Windows 10/11 x64. Distribute a self-contained `ARAMRecommender.exe`; ZIP contains that same EXE and a short README. No Python or admin installation is required.
- The packaged app checks the public GitHub latest stable release's `recommender-manifest.json` at startup. Source GUI keeps its existing local model behavior.
- The manifest declares schema, Windows architecture, app version, independent data version, byte sizes and SHA-256. Downloads are HTTPS assets in this repository only. No GitHub token is shipped.
- Data packages contain a flat JSON allowlist; downloaded pickle, executable data and paths outside the installation are rejected. Composition JSON must reproduce local pickle inference exactly.
- App and data install under `%LOCALAPPDATA%/ARAMRecommender`. Immutable files are verified before atomically switching pointers. Partial downloads resume on the next launch; corrupt, incompatible or interrupted updates preserve the previous complete data. The original download can launch a newer cached app even offline.
- Downloads allow 15 minutes per asset with 20-second socket idle timeouts and up to three resume attempts. This accommodates measured 70 KB/s player connections; closing startup still cancels the launch. Earlier updater EXEs used a three-minute deadline and may need another launch to finish a large app upgrade before receiving this fix.
- Closing startup cancels this launch; it does not activate partial files. Update failures appear in the GUI and `%LOCALAPPDATA%/ARAMRecommender/last-update.json`. `--no-update` deliberately uses the bundled model for diagnostics.
- Queue 2400, region, patch pool, actual training dataset cutoff, row count and held-out evidence remain attached to the data. A newer website does not establish a newer model cutoff.
- EXEs remain unsigned until a code-signing identity is configured. Do not disable SmartScreen or antivirus to make a test pass.

## Build and publish

Normal app releases are automated by `.github/workflows/recommender-release.yml`. A push to `main` touching desktop application sources starts a Windows x64 build; pull requests run the same candidate build and tests without publication. `workflow_dispatch` on `main` can retry a failed release. Site-only edits and model data uploads do not rebuild the EXE.

Clean runners download and validate the current public JSON dataset, not the private DB or pickled models. Versions increase relative to the current manifest. The candidate ZIP and EXE must pass SHA-256, extraction, Tk and inference checks on both `windows-2022` and `windows-2025` without Python paths. Only then can a job with `contents: write` create a draft at the exact source SHA, upload and verify four assets, and promote it to latest. Newest verified data is read again before upload; if the stable channel changes during upload, publication stops rather than rolling it back. A failed publish leaves the old latest intact; inspect the Actions error and rerun the workflow. Public download and old-EXE upgrade tests run in the same workflow after publication because releases created with `GITHUB_TOKEN` do not trigger another release workflow.

The manual build below remains available for an initial release, model/schema migration or recovery.

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
python scripts/publish_recommender_data.py --input-root D:/Projects/CODING/aram-winrate-nn --tag latest --region TW --time-cutoff '<actual training cutoff with timezone>'
```

The refresher supports `--desktop-release-tag latest --desktop-region TW`. It resolves the same stable release as clients on every watch cycle, derives the cutoff from the matching parquet, compares source fingerprints plus the resolved tag, and retries publishing independently of the training growth gate. Each cycle first publishes the existing verified model, before entering training's memory admission; after a successful refresh it publishes the new model in the same cycle. Training retains its resource guard and publication failures do not stop training. New app releases receive the current verified model even if it has not been retrained. Unrelated, draft or incomplete latest releases are rejected. This flag requires the repository owner's GitHub credentials on the publisher machine, never on users' computers. No publication occurs under `--dry-run` or `--check-only`.

The tracked production `watchdog_keepalive.ps1` forwards these two flags through the watchdog to the refresher child. Source integration does not change an already-running daemon's argv. For an authorized production cutover, wait until the refresher has no training descendants, then replace only the watchdog parent and refresher daemon; retain collector workers and static publisher. Keepalive starts the new parent, which adopts those existing children. Verify parent and refresher argv, unchanged worker PIDs, `[desktop-publish] published`, `outputs/desktop-publish/publish-state.json` resolved tag, public manifest and EXE startup. Never kill an in-progress training pipeline or move SQLite files.

A game patch does not create a trustworthy model immediately. The current production gate still requires at least 15,000 games on the new patch and successful held-out validation. Until then the app displays the previous verified patch and cutoff; automatic publication does not bypass these evidence gates. Signing and managed-PC SmartScreen policies remain separate from packaging automation.

An old May-2026 EXE has no updater. Existing users must download this new updater-capable EXE once; subsequent app and data updates are automatic at startup.
