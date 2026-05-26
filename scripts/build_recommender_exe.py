"""Build a downloadable Windows package for the ARAM recommender GUI.

The default output is a PyInstaller one-folder app zipped as:
  dist/ARAMRecommender-windows.zip

Users unzip it and run ARAMRecommender.exe.  One-folder is intentional:
it starts faster than --onefile and keeps Tk/httpx/numpy dependencies more
reliable on Windows while still being easy to download as a single zip.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ARAMRecommender"
ICON_FILE = ROOT / "docs" / "recommender-app-icon.ico"

DATA_FILES = [
    ("models/tier2_mayhem/lr_weights.json", "models/tier2_mayhem"),
    ("models/tier2_mayhem/tier2_checkpoint.champ_to_idx.json", "models/tier2_mayhem"),
    ("models/pair_synergy_16_10.json", "models"),
    (
        "models/composition_lr_16_10_2026_05_21_dual_roles/model.pkl",
        "models/composition_lr_16_10_2026_05_21_dual_roles",
    ),
    (
        "models/composition_lr_16_10_2026_05_21_dual_roles/single_team_calibration.json",
        "models/composition_lr_16_10_2026_05_21_dual_roles",
    ),
    ("data/cache/champion_abilities.json", "data/cache"),
    ("docs/recommender-app-icon.ico", "docs"),
]

EXCLUDE_MODULES = [
    "torch",
    "sklearn",
    "scipy",
    "pandas",
    "polars",
    "pyarrow",
    "fastapi",
    "uvicorn",
    "matplotlib",
    "pytest",
]


def _add_data_value(src: Path, dest: str) -> str:
    sep = ";" if sys.platform == "win32" else ":"
    return f"{src}{sep}{dest}"


def _zip_dir(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in source_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=Path(APP_NAME) / path.relative_to(source_dir))


def build(onefile: bool = False, skip_zip: bool = False) -> Path:
    missing = [str(ROOT / rel) for rel, _ in DATA_FILES if not (ROOT / rel).exists()]
    if missing:
        raise SystemExit("Missing package data:\n" + "\n".join(f"  {path}" for path in missing))
    if not ICON_FILE.exists():
        raise SystemExit(f"Missing app icon: {ICON_FILE}")

    dist_dir = ROOT / "dist"
    work_dir = ROOT / "build" / "pyinstaller"
    spec_dir = ROOT / "build" / "pyinstaller"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--paths",
        str(ROOT / "src"),
        "--icon",
        str(ICON_FILE),
    ]

    if onefile:
        cmd.append("--onefile")

    for rel, dest in DATA_FILES:
        cmd.extend(["--add-data", _add_data_value(ROOT / rel, dest)])

    for module in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", module])

    cmd.append(str(ROOT / "scripts" / "recommend_gui.py"))

    subprocess.run(cmd, cwd=ROOT, check=True)

    app_path = dist_dir / (f"{APP_NAME}.exe" if onefile else APP_NAME)
    if onefile or skip_zip:
        return app_path

    zip_path = dist_dir / f"{APP_NAME}-windows.zip"
    _zip_dir(app_path, zip_path)
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onefile", action="store_true", help="Build a single exe instead of the default zip.")
    parser.add_argument("--no-zip", action="store_true", help="Keep only the one-folder app directory.")
    args = parser.parse_args()

    artifact = build(onefile=args.onefile, skip_zip=args.no_zip)
    print(f"Built: {artifact}")


if __name__ == "__main__":
    main()
