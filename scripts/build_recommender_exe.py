"""Build the self-updating Windows x64 app, ZIP and verified data manifest."""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

from aram_nn.desktop.release import create_manifest, export_data, zip_data
from aram_nn.desktop.update import DATA_FILES, atomic_json, validate_data, validate_manifest, version_key

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ARAMRecommender"
EXCLUDE_MODULES = ["torch", "sklearn", "scipy", "pandas", "polars", "pyarrow",
                   "fastapi", "uvicorn", "matplotlib", "pytest"]


def build(*, version: str, input_root: Path, region: str | None, time_cutoff: str | None,
          data_dir: Path | None = None) -> Path:
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Build this release with 64-bit Python on Windows")
    version_key(version)
    tag = f"recommender-v{version}"
    dist = ROOT / "dist" / version
    resources = ROOT / "build" / "desktop-data"
    if data_dir:
        metadata = validate_data(data_dir)
        resources.mkdir(parents=True, exist_ok=True)
        for name in DATA_FILES:
            shutil.copyfile(data_dir / name, resources / name)
        validate_data(resources)
    else:
        if not region or not time_cutoff:
            raise ValueError("Local model export requires region and actual training cutoff")
        metadata = export_data(input_root / "models/composition_lr_pooled_recency_7d",
                               input_root / "data/cache/champion_abilities.json",
                               input_root / "docs/api/tier-list.json", resources,
                               region=region, time_cutoff=time_cutoff)
    atomic_json(resources / "app.json", {"version": version})
    icon = input_root / "docs/recommender-app-icon.ico"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onefile",
           "--name", APP_NAME, "--distpath", str(dist), "--workpath", str(ROOT / "build/pyinstaller"),
           "--specpath", str(ROOT / "build/pyinstaller"), "--paths", str(ROOT / "src"),
           "--icon", str(icon), "--add-data", f"{resources};desktop-data",
           "--add-data", f"{icon};docs"]
    for module in EXCLUDE_MODULES:
        cmd.extend(["--exclude-module", module])
    cmd.append(str(ROOT / "scripts/recommend_gui.py"))
    subprocess.run(cmd, cwd=ROOT, check=True)
    executable = dist / f"{APP_NAME}.exe"
    data_archive = dist / "recommender-data.zip"
    zip_data(resources, data_archive)
    manifest = create_manifest(executable, data_archive, version, tag, metadata)
    final = dist / f"recommender-data-{manifest['data']['version']}.zip"
    data_archive.replace(final)
    data_archive = final
    manifest["data"]["url"] = manifest["data"]["url"].replace("recommender-data.zip", data_archive.name)
    validate_manifest(manifest)
    atomic_json(dist / "recommender-manifest.json", manifest)
    with zipfile.ZipFile(dist / f"{APP_NAME}-windows.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, f"{APP_NAME}/{APP_NAME}.exe")
        archive.writestr(f"{APP_NAME}/README.txt", "Windows 10/11 x64\n執行 ARAMRecommender.exe，自動下載最新程式與資料。\n"
                         "不需安裝 Python；首次啟動可能較久。關閉更新視窗可取消，下次續傳。\n"
                         "離線／更新失敗會使用上次完整資料或內附資料。\n"
                         "未簽章：Windows SmartScreen 可能顯示提示。\n")
    return dist


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release version, e.g. 2026.10.09.1")
    parser.add_argument("--input-root", type=Path, default=ROOT)
    parser.add_argument("--data-dir", type=Path, help="Verified public JSON dataset for a clean CI build")
    parser.add_argument("--region")
    parser.add_argument("--time-cutoff", help="Actual training dataset cutoff, with timezone")
    parser.add_argument("--onefile", action="store_true", help="Compatibility flag; releases are single-file")
    args = parser.parse_args()
    print(build(version=args.version, input_root=args.input_root, region=args.region,
                time_cutoff=args.time_cutoff, data_dir=args.data_dir))


if __name__ == "__main__":
    main()
