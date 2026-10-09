"""Verified, resumable HTTPS downloads; immutable installs with atomic pointers.

Only the packaged Windows app calls this automatically. No API token, Python
installation, administrator privileges, or access to the collector DB is needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = "Lanternko/ARAM-Mayhem-Database"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/recommender-manifest.json"
SCHEMA = 1
DATA_FILES = frozenset({"metadata.json", "composition.json", "lr_weights.json", "champ_to_idx.json",
                        "role_synergy.json", "single_team_calibration.json", "champion_names.json", "tier-list.json"})
Progress = Callable[[str], None]


class UpdateError(ValueError):
    pass


def cache_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / "ARAMRecommender"


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise UpdateError("Expected a JSON object")
    return value


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def version_key(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d+", value):
        raise UpdateError("Invalid app version")
    return tuple(map(int, value.split(".")))


def check_url(url: str) -> None:
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or not parsed.path.startswith(f"/{REPO}/releases/download/")):
        raise UpdateError("Download must be a release asset in the official repository")


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit
        p = urlsplit(newurl)
        if p.scheme != "https" or p.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
            raise UpdateError("Unexpected download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": "ARAMRecommender/1", **(headers or {})})
    return urllib.request.build_opener(_HTTPSRedirect()).open(request, timeout=20)


def validate_manifest(value: dict, *, require_data_schema: bool = True) -> dict:
    from datetime import datetime
    # Manifest protocol remains version 1 even when an app's data schema evolves.
    if value.get("schema_version") != 1 or value.get("platform") != "windows-x64":
        raise UpdateError("This release requires a different updater or platform")
    for kind in ("app", "data"):
        asset = value.get(kind, {})
        if not isinstance(asset, dict):
            raise UpdateError("Invalid release asset")
        check_url(asset.get("url", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256", ""))):
            raise UpdateError("Missing SHA-256")
        size = asset.get("size")
        if type(size) is not int or not 0 < size <= (150_000_000 if kind == "app" else 25_000_000):
            raise UpdateError("Invalid asset size")
    version_key(value["app"].get("version", ""))
    data = value["data"]
    if (type(data.get("schema_version")) is not int or data["schema_version"] < 1
            or (require_data_schema and data["schema_version"] != SCHEMA) or data.get("queue_id") != 2400):
        raise UpdateError("Incompatible recommendation data")
    if not re.fullmatch(r"[0-9a-f]{16}", str(data.get("version", ""))):
        raise UpdateError("Invalid data version")
    if not re.fullmatch(r"\d+\.\d+", str(data.get("current_patch", ""))):
        raise UpdateError("Missing data patch")
    if datetime.fromisoformat(data.get("time_cutoff", "")).tzinfo is None:
        raise UpdateError("Data cutoff must contain a timezone")
    return value


def fetch_manifest() -> dict:
    with open_url(MANIFEST_URL, {"Cache-Control": "no-cache"}) as response:
        body = response.read(512_001)
    if len(body) > 512_000:
        raise UpdateError("Manifest is too large")
    return validate_manifest(json.loads(body))


def verified(path: Path, asset: dict) -> bool:
    if not path.is_file() or path.stat().st_size != asset["size"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"]


def download(asset: dict, directory: Path, progress: Progress = lambda _: None,
             *, opener=open_url, attempts: int = 3) -> Path:
    """Resume partial bytes only against an immutable URL + expected SHA-256.

    A server that ignores Range causes a full restart. A digest mismatch never
    reaches an installation pointer. Partial downloads survive the next launch.
    """
    check_url(asset["url"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (asset["sha256"] + ".download")
    if verified(path, asset):
        return path
    deadline = time.monotonic() + 180
    last_error: Exception = UpdateError("Download did not complete")
    for _ in range(attempts):
        try:
            offset = path.stat().st_size if path.exists() else 0
            if offset >= asset["size"]:
                path.unlink()
                offset = 0
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            with opener(asset["url"], headers) as response:
                status = response.status
                if status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    if not content_range.startswith(f"bytes {offset}-") or not content_range.endswith(f"/{asset['size']}"):
                        raise UpdateError("Unexpected resume offset")
                elif status == 200:
                    offset = 0
                else:
                    raise UpdateError(f"Download returned HTTP {status}")
                with path.open("ab" if offset else "wb") as stream:
                    total = offset
                    while True:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Download timed out; it will resume next launch")
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > asset["size"]:
                            raise UpdateError("Download exceeds declared size")
                        stream.write(chunk)
                        progress(f"下載中 {total / asset['size']:.0%} · {total / 1_000_000:.1f} / {asset['size'] / 1_000_000:.1f} MB")
            if not verified(path, asset):
                if path.stat().st_size == asset["size"]:
                    path.unlink()
                raise UpdateError("Download is incomplete or SHA-256 differs")
            return path
        except (OSError, urllib.error.URLError, UpdateError) as exc:
            last_error = exc
            progress("連線中斷，正在嘗試續傳…")
            if time.monotonic() > deadline:
                break
    raise UpdateError(str(last_error))


def validate_data(directory: Path) -> dict:
    """Validate the files together before making this dataset active."""
    import math
    metadata = read_json(directory / "metadata.json")
    if metadata.get("schema_version") != SCHEMA or metadata.get("queue_id") != 2400:
        raise UpdateError("Wrong data scope or schema")
    if not re.fullmatch(r"\d+\.\d+", str(metadata.get("current_patch", ""))):
        raise UpdateError("Missing data patch")
    if not metadata.get("region") or not metadata.get("time_cutoff") or not metadata.get("row_count"):
        raise UpdateError("Missing data provenance")
    if set(metadata.get("files", {})) != DATA_FILES - {"metadata.json"}:
        raise UpdateError("Missing per-file integrity checks")
    for name, expected in metadata["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise UpdateError(f"Installed file is damaged: {name}")
    from aram_nn.recommend import load_composition_lr, load_lr, load_synergy
    lr = load_lr(directory / "lr_weights.json", directory / "champ_to_idx.json")
    comp = load_composition_lr(directory / "composition.json")
    synergy = load_synergy(directory / "role_synergy.json")
    if (lr.champ_to_idx != comp.champ_to_idx or sorted(lr.champ_to_idx.values()) != list(range(lr.n_champs))
            or len(lr.coef) != lr.n_champs or not comp.profiles
            or synergy.queue_id != 2400 or metadata["current_patch"] not in synergy.patch_prefix.split(",")):
        raise UpdateError("Model files disagree")
    if not all(math.isfinite(float(x)) for x in [*lr.coef, lr.intercept, *comp.coef, comp.intercept,
                                                comp.single_team_intercept]):
        raise UpdateError("Model contains non-finite values")
    for path in DATA_FILES:
        read_json(directory / path)
    return metadata


def install_data(archive: Path, root: Path, asset: dict) -> Path:
    if not verified(archive, asset):
        raise UpdateError("Archive verification failed")
    target = root / "datasets" / asset["sha256"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        validate_data(target)
        return target
    with tempfile.TemporaryDirectory(prefix="install-", dir=target.parent) as temp:
        staged = Path(temp) / "data"
        staged.mkdir()
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            if len(entries) != len(DATA_FILES) or {e.filename for e in entries} != DATA_FILES:
                raise UpdateError("Unexpected or duplicate files in data archive")
            if sum(e.file_size for e in entries) > 40_000_000:
                raise UpdateError("Expanded data exceeds limit")
            for entry in entries:
                # Exact flat allowlist excludes path traversal, symlinks and executable code.
                if entry.external_attr >> 16 & 0o170000 == 0o120000:
                    raise UpdateError("Symlinks are not allowed")
                (staged / entry.filename).write_bytes(z.read(entry))
        validate_data(staged)
        try:
            staged.rename(target)
        except FileExistsError:  # another instance installed the same verified bundle
            validate_data(target)
    return target


def install_app(path: Path, root: Path, asset: dict) -> Path:
    if not verified(path, asset):
        raise UpdateError("Executable verification failed")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise UpdateError("Not a Windows executable")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise UpdateError("Executable is not Windows x64")
    target = root / "apps" / asset["sha256"] / "ARAMRecommender.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not verified(target, asset):
        import shutil
        fd, temp = tempfile.mkstemp(dir=target.parent, suffix=".exe")
        os.close(fd)
        try:
            shutil.copyfile(path, temp)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return target


@dataclass
class UpdateResult:
    data_dir: Path | None = None
    app_path: Path | None = None
    message: str = ""


def _update(root: Path, current_version: str, progress: Progress = lambda _: None,
           *, manifest_loader=fetch_manifest, downloader=download) -> UpdateResult:
    result = UpdateResult()
    newest_app = version_key(current_version)
    try:
        state = read_json(root / "active.json")
    except (OSError, ValueError):
        state = {}
    # Only pointers to paths derived from validated digests are trusted.
    try:
        saved = validate_manifest(state)
        data_dir = root / "datasets" / saved["data"]["sha256"]
        validate_data(data_dir)
        result.data_dir = data_dir
    except (OSError, ValueError, KeyError, TypeError):
        saved = None
    try:
        app = read_json(root / "app-active.json")
        version_key(app["version"])
        check_url(app["url"])
        if not re.fullmatch(r"[0-9a-f]{64}", app["sha256"]):
            raise UpdateError("Invalid cached application")
        cached = root / "apps" / app["sha256"] / "ARAMRecommender.exe"
        if version_key(app["version"]) > version_key(current_version) and verified(cached, app):
            result.app_path = cached
            newest_app = version_key(app["version"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        progress("檢查最新 Windows 程式與 Mayhem 資料…")
        manifest = validate_manifest(manifest_loader(), require_data_schema=False)
        if version_key(manifest["app"]["version"]) > newest_app:
            progress("下載最新程式…")
            app = downloader(manifest["app"], root / "downloads", progress)
            result.app_path = install_app(app, root, manifest["app"])
            atomic_json(root / "app-active.json", manifest["app"])
        if manifest["data"]["schema_version"] != SCHEMA:
            if result.app_path:
                result.message = "啟動新版程式以更新推薦資料"
                return result
            raise UpdateError("New data requires a newer application")
        from datetime import datetime
        if saved and datetime.fromisoformat(manifest["data"]["time_cutoff"]) < datetime.fromisoformat(saved["data"]["time_cutoff"]):
            result.message = "目前資料比發布端更新，保留目前版本"
            return result
        if saved and saved["data"]["sha256"] == manifest["data"]["sha256"]:
            result.message = "已是最新資料"
            return result
        progress("下載最新推薦資料…")
        archive = downloader(manifest["data"], root / "downloads", progress)
        directory = install_data(archive, root, manifest["data"])
        metadata = read_json(directory / "metadata.json")
        if (metadata["current_patch"] != manifest["data"]["current_patch"]
                or metadata["time_cutoff"] != manifest["data"]["time_cutoff"]):
            raise UpdateError("Manifest and dataset scope differ")
        atomic_json(root / "active.json", manifest)
        result.data_dir = directory
        result.message = "已更新至最新資料"
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        result.message = "更新暫時失敗，使用上次完整資料" if result.data_dir else "更新暫時失敗，使用內附資料"
        try:
            atomic_json(root / "last-update.json", {"error": str(exc), "time": time.time()})
        except OSError:
            pass
        progress(result.message)
    return result


def update(root: Path, current_version: str, progress: Progress = lambda _: None,
           *, manifest_loader=fetch_manifest, downloader=download) -> UpdateResult:
    """Serialize updates across Windows processes; an existing app stays usable."""
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        lock = (root / "update.lock").open("a+b")
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if "lock" in locals():
            lock.close()
        def unavailable():
            raise UpdateError("Another updater is running, or the cache is not writable")
        return _update(root, current_version, progress, manifest_loader=unavailable, downloader=downloader)
    try:
        return _update(root, current_version, progress, manifest_loader=manifest_loader, downloader=downloader)
    finally:
        lock.close()
