"""Clean-runner release tooling: consume public JSON; publish only verified assets."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .release import asset_info
from .update import (DATA_FILES, REPO, atomic_json, download, fetch_manifest, install_data,
                     read_json, validate_data, validate_manifest, version_key)


def next_version(current: str, run_number: int, today: str | None = None) -> str:
    date = today or datetime.now(timezone.utc).strftime("%Y.%m.%d")
    previous = version_key(current)
    date = max(date, current.rsplit(".", 1)[0])
    serial = max(run_number, previous[-1] + 1 if date == current.rsplit(".", 1)[0] else 1)
    return f"{date}.{serial}"


def prepare(destination: Path, run_number: int) -> str:
    manifest = fetch_manifest()
    archive = download(manifest["data"], destination / "downloads")
    data = install_data(archive, destination, manifest["data"])
    public = destination / "public-data"
    public.mkdir(parents=True, exist_ok=True)
    for name in DATA_FILES:
        shutil.copyfile(data / name, public / name)
    validate_data(public)
    return next_version(manifest["app"]["version"], run_number)


def publish(dist: Path, source: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("Release must target an exact reviewed source commit")
    manifest = validate_manifest(read_json(dist / "recommender-manifest.json"))
    version = manifest["app"]["version"]
    tag = f"recommender-v{version}"
    executable = dist / "ARAMRecommender.exe"
    if asset_info(executable, tag) != {k: manifest["app"][k] for k in ("url", "size", "sha256")}:
        raise ValueError("Built executable differs from its manifest")
    # Model refresh can finish during a Windows build. Carry its newest verified
    # data into the new release; a slow build must not roll the public channel back.
    current = fetch_manifest()
    if version_key(version) <= version_key(current["app"]["version"]):
        raise ValueError("A newer application already exists; rebuild against latest")
    archive = download(current["data"], dist / "downloads")
    install_data(archive, dist / "verified", current["data"])
    data = dist / f"recommender-data-{current['data']['sha256'][:16]}.zip"
    shutil.copyfile(archive, data)
    manifest["data"] = {**current["data"], **asset_info(data, tag)}
    atomic_json(dist / "recommender-manifest.json", validate_manifest(manifest))
    notes = dist / "release-notes.md"
    notes.write_text(
        f"Windows 10/11 x64，免安裝 Python。啟動時自動檢查最新程式與資料。\n\n"
        f"Queue 2400 · patch {manifest['data']['current_patch']} · "
        f"資料截止 {manifest['data']['time_cutoff']}。\n\n"
        "下載 ARAMRecommender.exe 直接執行，或下載 Windows ZIP 解壓縮。"
        "程式尚未簽章，SmartScreen 可能顯示提示；確認檔案來自本專案後，"
        "可選「其他資訊 → 仍要執行」。\n\n"
        f"Source: {source}\n", encoding="utf-8")
    command = ["gh", "release", "view", tag, "--repo", REPO, "--json", "isDraft,targetCommitish"]
    existing = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if existing.returncode == 0:
        release = json.loads(existing.stdout)
        if not release["isDraft"] or release["targetCommitish"] != source:
            raise ValueError("Release tag is already owned by another build")
    else:
        subprocess.run(["gh", "release", "create", tag, "--repo", REPO, "--draft", "--target", source,
                        "--title", f"ARAM Recommender {version}", "--notes-file", str(notes)], check=True)
    assets = [executable, dist / "ARAMRecommender-windows.zip", data, dist / "recommender-manifest.json"]
    subprocess.run(["gh", "release", "upload", tag, *map(str, assets), "--repo", REPO, "--clobber"], check=True)
    result = subprocess.run(["gh", "release", "view", tag, "--repo", REPO, "--json", "assets"],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    uploaded = {a["name"]: a for a in json.loads(result.stdout)["assets"]}
    for path in assets:
        expected = asset_info(path, tag)
        actual = uploaded[path.name]
        if actual["size"] != expected["size"] or actual.get("digest") != f"sha256:{expected['sha256']}":
            raise ValueError("Uploaded release asset failed verification")
    latest = fetch_manifest()
    if latest["app"] != current["app"] or latest["data"] != current["data"]:
        raise ValueError("Stable channel changed during upload; rerun instead of rolling it back")
    subprocess.run(["gh", "release", "edit", tag, "--repo", REPO, "--draft=false", "--latest"], check=True)
    return tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preparation = sub.add_parser("prepare")
    preparation.add_argument("--output", type=Path, required=True)
    preparation.add_argument("--run-number", type=int, required=True)
    publication = sub.add_parser("publish")
    publication.add_argument("--dist", type=Path, required=True)
    publication.add_argument("--source", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        value = prepare(args.output, args.run_number)
        key = "version"
    else:
        value = publish(args.dist, args.source)
        key = "tag"
    print(value)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
