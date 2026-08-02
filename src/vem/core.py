"""Storage, filesystem and integrity primitives for vem."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
STATES = {"ok", "orphan", "creating", "missing-environment", "broken"}


class VemError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def normalize(path: str | os.PathLike[str]) -> Path:
    value = os.path.expandvars(os.path.expanduser(os.fspath(path)))
    return Path(os.path.abspath(os.path.normpath(value)))


def path_key(path: Path) -> str:
    value = os.path.normpath(os.fspath(path))
    return os.path.normcase(value) if os.name == "nt" else value


def default_house() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise VemError("LOCALAPPDATA is not set; specify --house", 2)
        return normalize(Path(root) / "vem")
    if sys.platform == "darwin":
        return normalize(Path.home() / "Library" / "Application Support" / "vem")
    root = os.environ.get("XDG_DATA_HOME") or os.fspath(Path.home() / ".local" / "share")
    return normalize(Path(root) / "vem")


def resolve_house(option: str | None) -> Path:
    return normalize(option or os.environ.get("VEM_HOUSE") or default_house())


def _python_candidates(value: str) -> tuple[str, ...]:
    if os.name == "nt" and not value.lower().endswith(".exe"):
        return value, f"{value}.exe"
    return (value,)


def resolve_python(value: str) -> Path | None:
    """Resolve a Python command or path, including commands found on PATH."""
    expanded = os.path.expandvars(os.path.expanduser(value))
    for candidate in _python_candidates(expanded):
        executable = shutil.which(candidate)
        if executable is not None:
            return normalize(executable)
    return None


def validate_name(name: str | None) -> None:
    if name is None:
        return
    if not name or len(name) > 255 or name != name.strip() or name in {".", ".."}:
        raise VemError("invalid environment name", 2)
    if "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise VemError("invalid environment name", 2)


def validate_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise VemError(f"invalid environment ID: {value}", 2) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise VemError(f"invalid UUID version 4 environment ID: {value}", 2)
    return value


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise VemError(f"cannot write management JSON {path}: {exc}", 6) from exc


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise VemError(f"cannot read management JSON {path}: {exc}", 6) from exc
    if not isinstance(value, dict):
        raise VemError(f"management JSON is not an object: {path}", 6)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise VemError(f"unsupported schema version in {path}", 11)
    if not isinstance(value.get("revision"), int) or value["revision"] < 0:
        raise VemError(f"invalid revision in {path}", 6)
    return value


class House:
    def __init__(self, path: Path):
        self.path = path
        self.environments = path / "environments"
        self.tmp = path / "tmp"
        self.registry_path = path / "registry.json"

    def initialize(self) -> None:
        try:
            self.environments.mkdir(parents=True, exist_ok=True)
            self.tmp.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VemError(f"cannot initialize house {self.path}: {exc}", 5) from exc
        if not self.registry_path.exists():
            atomic_json(self.registry_path, self.empty_registry())

    @staticmethod
    def empty_registry() -> dict[str, Any]:
        return {"schema_version": 1, "revision": 0, "updated_at": now(), "environments": {}}

    @contextlib.contextmanager
    def lock(self, exclusive: bool = True, timeout: float = 10.0) -> Iterator[None]:
        self.path.mkdir(parents=True, exist_ok=True)
        stream = (self.path / "lock").open("a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(stream, mode | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    stream.close()
                    raise VemError(f"timed out acquiring house lock: {self.path}", 9)
                time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()

    def registry(self) -> dict[str, Any]:
        return load_json(self.registry_path)

    def save_registry(self, registry: dict[str, Any]) -> None:
        registry["revision"] += 1
        registry["updated_at"] = now()
        atomic_json(self.registry_path, registry)

    def metadata_path(self, env_id: str) -> Path:
        return self.environments / env_id / "metadata.json"

    def metadata(self, env_id: str) -> dict[str, Any]:
        value = load_json(self.metadata_path(env_id))
        if value.get("id") != env_id:
            raise VemError(f"metadata ID does not match directory: {env_id}", 7)
        return value

    def save_metadata(self, metadata: dict[str, Any]) -> None:
        metadata["revision"] += 1
        metadata["updated_at"] = now()
        atomic_json(self.metadata_path(metadata["id"]), metadata)

    def find(self, *, name: str | None = None, env_id: str | None = None) -> tuple[str, dict[str, Any]]:
        registry = self.registry()
        entries = registry.get("environments")
        if not isinstance(entries, dict):
            raise VemError("invalid environments object in registry", 6)
        if env_id is not None:
            validate_uuid(env_id)
            if env_id not in entries:
                raise VemError(f"environment not found: {env_id}", 3)
            return env_id, self.metadata(env_id)
        folded = (name or "").casefold()
        matches = [key for key, val in entries.items() if isinstance(val, dict) and isinstance(val.get("name"), str) and val["name"].casefold() == folded]
        if not matches:
            raise VemError(f"environment not found: {name}", 3)
        return matches[0], self.metadata(matches[0])


def ensure_under(path: Path, parent: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError as exc:
        raise VemError(f"unsafe managed path outside house: {path}", 7) from exc


def venv_path(house: House, metadata: dict[str, Any]) -> Path:
    rel = metadata.get("venv", {}).get("path")
    if rel != "venv":
        raise VemError("unsafe or unsupported venv.path", 7)
    result = house.environments / metadata["id"] / rel
    ensure_under(result, house.environments / metadata["id"])
    return result


def link_type(requested: str) -> str:
    if requested == "junction" and os.name != "nt":
        raise VemError("junction links are only available on Windows", 2)
    return ("junction" if os.name == "nt" else "symlink") if requested == "auto" else requested


def create_link(path: Path, target: Path, kind: str) -> None:
    if os.path.lexists(path):
        raise VemError(f"link destination already exists: {path}", 4)
    if not path.parent.is_dir():
        raise VemError(f"link parent directory does not exist: {path.parent}", 5)
    try:
        if kind == "symlink":
            path.symlink_to(target, target_is_directory=True)
        elif os.name == "nt" and kind == "junction":
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", os.fspath(path), os.fspath(target)], capture_output=True, text=True)
            if result.returncode:
                raise OSError(result.stderr.strip() or result.stdout.strip())
        else:
            raise VemError(f"unsupported link type: {kind}", 2)
    except OSError as exc:
        raise VemError(f"cannot create {kind} {path}: {exc}", 5) from exc
    if inspect_link(path, target, kind)["state"] != "ok":
        delete_link(path, kind)
        raise VemError(f"created link failed validation: {path}", 7)


def actual_link_type(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if os.name == "nt" and os.path.lexists(path) and path.is_dir():
        import stat
        try:
            if os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return "junction"
        except OSError:
            return None
    return None


def inspect_link(path: Path, expected: Path, registered: str) -> dict[str, Any]:
    base = {"path": os.fspath(path), "registered_type": registered, "actual_type": None,
            "state": "ok", "expected_target": os.fspath(expected), "actual_target": None}
    if not os.path.lexists(path):
        base["state"] = "stale-link"
        return base
    actual = actual_link_type(path)
    base["actual_type"] = actual
    if actual is None:
        base["state"] = "not-a-link"
        return base
    try:
        target = path.resolve(strict=False)
        base["actual_target"] = os.fspath(target)
    except OSError:
        base["state"] = "unreadable"
        return base
    if path_key(target) != path_key(expected.resolve(strict=False)):
        base["state"] = "wrong-target"
    elif actual != registered:
        base["state"] = "wrong-type"
    return base


def delete_link(path: Path, kind: str) -> None:
    try:
        if kind == "junction":
            os.rmdir(path)
        else:
            path.unlink()
    except OSError as exc:
        raise VemError(f"cannot delete link {path}: {exc}", 5) from exc


def python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def probe_python(executable: Path) -> tuple[str | None, str]:
    script = "import json,sys;print(json.dumps({'base':getattr(sys,'_base_executable',None),'version':'.'.join(map(str,sys.version_info[:3]))}))"
    try:
        result = subprocess.run([os.fspath(executable), "-c", script], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VemError(f"cannot execute Python {executable}: {exc}", 10) from exc
    if result.returncode:
        raise VemError(f"Python validation failed: {result.stderr.strip()}", 10)
    try:
        info = json.loads(result.stdout)
        return info.get("base"), info["version"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise VemError("Python returned invalid validation data", 10) from exc
