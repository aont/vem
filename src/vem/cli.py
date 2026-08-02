"""Command-line interface for vem."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .core import (House, VemError, atomic_json, create_link, delete_link,
                   inspect_link, link_type, normalize, now, path_key,
                   probe_python, python_in, resolve_house, resolve_python,
                   validate_name, validate_uuid, venv_path)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vem", description="Manage centralized Python virtual environments")
    p.add_argument("--house", metavar="PATH")
    p.add_argument("--no-color", action="store_true", help="disable color output")
    p.add_argument("--version", action="version", version=f"vem {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create an environment and link")
    create.add_argument("--name")
    create.add_argument("--python", default=sys.executable,
                        help="Python executable path or command name (searched on PATH)")
    add_link_type(create)
    create.add_argument("link_path")

    link = sub.add_parser("link", help="add a link to an environment")
    add_selector(link)
    add_link_type(link)
    link.add_argument("link_path")

    move = sub.add_parser("move", help="move a managed link")
    move.add_argument("source_link")
    move.add_argument("destination_link")

    unlink = sub.add_parser("unlink", help="delete a managed link")
    unlink.add_argument("--delete-if-orphan", action="store_true")
    unlink.add_argument("link_path")

    remove = sub.add_parser("remove", help="delete an environment")
    add_selector(remove)
    remove.add_argument("--force", action="store_true")

    status = sub.add_parser("status", help="show managed environments")
    filters = status.add_mutually_exclusive_group()
    filters.add_argument("--name")
    filters.add_argument("--id")
    filters.add_argument("--path")
    status.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="inspect house consistency")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--rebuild-registry", action="store_true")
    return p


def add_selector(p: argparse.ArgumentParser) -> None:
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--name")
    selection.add_argument("--id")


def add_link_type(p: argparse.ArgumentParser) -> None:
    p.add_argument("--link-type", choices=("auto", "junction", "symlink"), default="auto")


def entry(metadata: dict[str, Any]) -> dict[str, Any]:
    env_id = metadata["id"]
    return {"name": metadata.get("name"), "metadata_path": f"environments/{env_id}/metadata.json"}


def registered_links(house: House) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    result = []
    registry = house.registry()
    for env_id in registry.get("environments", {}):
        metadata = house.metadata(env_id)
        for item in metadata.get("links", []):
            result.append((env_id, metadata, item))
    return result


def assert_unique_name(registry: dict[str, Any], name: str | None) -> None:
    if name is None:
        return
    for item in registry.get("environments", {}).values():
        existing = item.get("name") if isinstance(item, dict) else None
        if isinstance(existing, str) and existing.casefold() == name.casefold():
            raise VemError(f"environment name already exists: {name}", 4)


def command_create(house: House, args: argparse.Namespace) -> None:
    validate_name(args.name)
    destination = normalize(args.link_path)
    requested = resolve_python(args.python)
    kind = link_type(args.link_type)
    with house.lock():
        house.initialize()
        registry = house.registry()
        assert_unique_name(registry, args.name)
        if os.path.lexists(destination):
            raise VemError(f"link destination already exists: {destination}", 4)
        if not destination.parent.is_dir():
            raise VemError(f"link parent directory does not exist: {destination.parent}", 5)
        if requested is None or not requested.is_file():
            raise VemError(f"Python executable does not exist: {args.python}", 10)
        env_id = str(uuid.uuid4())
        while env_id in registry["environments"] or (house.environments / env_id).exists():
            env_id = str(uuid.uuid4())
        temporary = house.tmp / f"{env_id}.creating"
        final = house.environments / env_id
        timestamp = now()
        metadata = {"schema_version": 1, "revision": 0, "id": env_id, "name": args.name,
                    "state": "creating", "created_at": timestamp, "updated_at": timestamp,
                    "last_error": None, "python": {"requested_executable": os.fspath(requested),
                    "base_executable": None, "version": None}, "venv": {"path": "venv"}, "links": []}
        try:
            temporary.mkdir()
            atomic_json(temporary / "metadata.json", metadata)
            result = subprocess.run([os.fspath(requested), "-m", "venv", os.fspath(temporary / "venv")])
            if result.returncode:
                raise VemError(f"Python venv creation failed with exit status {result.returncode}", 10)
            base, version = probe_python(python_in(temporary / "venv"))
            metadata["python"]["base_executable"] = base
            metadata["python"]["version"] = version
            temporary.rename(final)
            create_link(destination, final / "venv", kind)
            timestamp = now()
            metadata["links"].append({"path": os.fspath(destination), "type": kind,
                                      "created_at": timestamp, "updated_at": timestamp})
            metadata["state"] = "ok"
            house.save_metadata(metadata)
            registry["environments"][env_id] = entry(metadata)
            house.save_registry(registry)
        except Exception:
            with contextlib.suppress(Exception):
                if os.path.lexists(destination):
                    delete_link(destination, kind)
            shutil.rmtree(temporary, ignore_errors=True)
            shutil.rmtree(final, ignore_errors=True)
            raise
    print(f"Created environment:\n  id:   {env_id}\n  name: {args.name or '-'}\n  env:  {final / 'venv'}\n  link: {destination}\n  type: {kind}")


def command_link(house: House, args: argparse.Namespace) -> None:
    destination = normalize(args.link_path)
    kind = link_type(args.link_type)
    with house.lock():
        env_id, metadata = house.find(name=args.name, env_id=args.id)
        target = venv_path(house, metadata)
        if metadata.get("state") not in {"ok", "orphan"} or not target.is_dir():
            raise VemError(f"environment {env_id} does not permit link creation", 7)
        if any(path_key(normalize(item["path"])) == path_key(destination) for item in metadata.get("links", [])):
            raise VemError(f"link path is already registered: {destination}", 4)
        create_link(destination, target, kind)
        timestamp = now()
        metadata["links"].append({"path": os.fspath(destination), "type": kind, "created_at": timestamp, "updated_at": timestamp})
        metadata["state"] = "ok"
        try:
            house.save_metadata(metadata)
        except Exception:
            delete_link(destination, kind)
            raise
    print(f"Linked {destination} to environment {env_id}")


def locate_link(house: House, path: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    matches = [value for value in registered_links(house) if path_key(normalize(value[2]["path"])) == path_key(path)]
    if not matches:
        raise VemError(f"managed link not found: {path}", 3)
    if len(matches) > 1:
        raise VemError(f"link is registered more than once: {path}", 7)
    return matches[0]


def command_move(house: House, args: argparse.Namespace) -> None:
    source, destination = normalize(args.source_link), normalize(args.destination_link)
    if path_key(source) == path_key(destination):
        raise VemError("source and destination are the same path", 2)
    with house.lock():
        env_id, metadata, item = locate_link(house, source)
        target = venv_path(house, metadata)
        state = inspect_link(source, target, item["type"])
        if state["state"] != "ok":
            raise VemError(f"source link is inconsistent: {state['state']}", 7)
        create_link(destination, target, item["type"])
        try:
            delete_link(source, item["type"])
        except Exception:
            with contextlib.suppress(Exception):
                delete_link(destination, item["type"])
            raise
        item["path"] = os.fspath(destination)
        item["updated_at"] = now()
        house.save_metadata(metadata)
    print(f"Moved link {source} to {destination} for environment {env_id}")


def delete_environment(house: House, env_id: str, metadata: dict[str, Any], registry: dict[str, Any]) -> None:
    directory = house.environments / env_id
    if directory.name != env_id or metadata.get("id") != env_id:
        raise VemError("unsafe environment deletion target", 7)
    venv_path(house, metadata)
    shutil.rmtree(directory)
    registry["environments"].pop(env_id, None)
    house.save_registry(registry)


def command_unlink(house: House, args: argparse.Namespace) -> None:
    path = normalize(args.link_path)
    with house.lock():
        env_id, metadata, item = locate_link(house, path)
        state = inspect_link(path, venv_path(house, metadata), item["type"])
        if state["state"] != "ok":
            raise VemError(f"refusing to delete inconsistent link: {state['state']}", 7)
        delete_link(path, item["type"])
        metadata["links"].remove(item)
        metadata["state"] = "ok" if metadata["links"] else "orphan"
        house.save_metadata(metadata)
        if args.delete_if_orphan and not metadata["links"]:
            delete_environment(house, env_id, metadata, house.registry())
    print(f"Unlinked {path}" + (" and deleted orphan environment" if args.delete_if_orphan and not metadata["links"] else ""))


def command_remove(house: House, args: argparse.Namespace) -> None:
    with house.lock():
        env_id, metadata = house.find(name=args.name, env_id=args.id)
        links = metadata.get("links", [])
        if links and not args.force:
            raise VemError(f"environment {env_id} still has registered links; use --force", 7)
        target = venv_path(house, metadata)
        for item in links:
            path = normalize(item["path"])
            state = inspect_link(path, target, item["type"])
            if state["state"] != "ok":
                raise VemError(f"refusing to delete inconsistent link {path}: {state['state']}", 7)
        for item in links:
            delete_link(normalize(item["path"]), item["type"])
        delete_environment(house, env_id, metadata, house.registry())
    print(f"Removed environment {env_id}")


def status_record(house: House, env_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    target = venv_path(house, metadata)
    links = [inspect_link(normalize(item["path"]), target, item["type"]) for item in metadata.get("links", [])]
    exists = target.is_dir() and python_in(target).is_file()
    state = metadata.get("state", "unknown")
    if not exists:
        state = "missing-environment"
    elif not links:
        state = "orphan"
    elif any(item["state"] != "ok" for item in links):
        state = "broken"
    elif state not in {"creating", "broken"}:
        state = "ok"
    return {"id": env_id, "name": metadata.get("name"), "state": state,
            "metadata_state": metadata.get("state", "unknown"), "venv_path": os.fspath(target),
            "venv_exists": exists, "python": {"executable": os.fspath(python_in(target)),
            "version": metadata.get("python", {}).get("version")}, "created_at": metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"), "metadata_revision": metadata.get("revision"), "links": links}


def command_status(house: House, args: argparse.Namespace) -> None:
    with house.lock(exclusive=False):
        registry = house.registry()
        records = []
        wanted_path = normalize(args.path) if args.path else None
        for env_id in registry.get("environments", {}):
            if args.id and env_id != validate_uuid(args.id):
                continue
            metadata = house.metadata(env_id)
            if args.name and (metadata.get("name") or "").casefold() != args.name.casefold():
                continue
            if wanted_path and not any(path_key(normalize(x["path"])) == path_key(wanted_path) for x in metadata.get("links", [])):
                continue
            records.append(status_record(house, env_id, metadata))
        if (args.name or args.id or args.path) and not records:
            raise VemError("no matching environment", 3)
    if args.json:
        print(json.dumps({"schema_version": 1, "house": os.fspath(house.path), "environments": records}, ensure_ascii=False, indent=2))
    else:
        if not records:
            print("No managed environments.")
        for record in records:
            print(f"{record['id']}  {record['name'] or '-'}  {record['state']}\n  env: {record['venv_path']}\n  python: {record['python']['version'] or '-'}\n  links: {len(record['links'])}")
            for item in record["links"]:
                print(f"    {item['path']}  {item['state']} ({item['registered_type']})")


def scan_metadata(house: House) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    found, issues, names, paths = {}, [], {}, {}
    if not house.environments.exists():
        return found, issues
    for directory in house.environments.iterdir():
        if not directory.is_dir():
            issues.append({"code": "unexpected-entry", "path": os.fspath(directory), "message": "unexpected environments entry"})
            continue
        try:
            validate_uuid(directory.name)
            metadata = house.metadata(directory.name)
            found[directory.name] = metadata
            name = metadata.get("name")
            if isinstance(name, str):
                validate_name(name)
                if name.casefold() in names:
                    issues.append({"code": "duplicate-name", "path": os.fspath(directory), "message": f"duplicate name: {name}"})
                names[name.casefold()] = directory.name
            record = status_record(house, directory.name, metadata)
            if not record["venv_exists"]:
                issues.append({"code": "missing-environment", "path": record["venv_path"], "message": "actual environment is missing"})
            for link in record["links"]:
                key = path_key(normalize(link["path"]))
                if key in paths:
                    issues.append({"code": "duplicate-link", "path": link["path"], "message": "link is registered more than once"})
                paths[key] = directory.name
                if link["state"] != "ok":
                    issues.append({"code": link["state"], "path": link["path"], "message": "registered link is inconsistent"})
        except VemError as exc:
            issues.append({"code": "invalid-metadata", "path": os.fspath(directory), "message": str(exc)})
    return found, issues


def command_doctor(house: House, args: argparse.Namespace) -> bool:
    with house.lock(exclusive=args.rebuild_registry):
        found, issues = scan_metadata(house)
        try:
            registry = house.registry()
            entries = registry.get("environments", {})
            if not isinstance(entries, dict):
                raise VemError("registry environments is not an object", 6)
            for env_id, item in entries.items():
                expected = f"environments/{env_id}/metadata.json"
                if env_id not in found:
                    issues.append({"code": "missing-metadata", "path": str(item), "message": f"registry metadata is missing: {env_id}"})
                elif not isinstance(item, dict) or item.get("metadata_path") != expected:
                    issues.append({"code": "unsafe-metadata-path", "path": str(item), "message": "invalid metadata_path"})
            for env_id in found.keys() - entries.keys():
                issues.append({"code": "unregistered-environment", "path": env_id, "message": "environment is absent from registry"})
        except VemError as exc:
            registry = None
            issues.append({"code": "invalid-registry", "path": os.fspath(house.registry_path), "message": str(exc)})
        if house.tmp.exists():
            for item in house.tmp.iterdir():
                issues.append({"code": "temporary-entry", "path": os.fspath(item), "message": "temporary entry remains"})
        if args.rebuild_registry:
            fatal = [x for x in issues if x["code"] in {"invalid-metadata", "duplicate-name", "duplicate-link"}]
            if fatal:
                raise VemError("registry rebuild rejected because metadata is inconsistent", 7)
            rebuilt = House.empty_registry()
            rebuilt["environments"] = {env_id: entry(metadata) for env_id, metadata in found.items()}
            # A rebuild is itself the first persisted registry revision.
            house.save_registry(rebuilt)
            issues = [x for x in issues if x["code"] not in {"invalid-registry", "missing-metadata", "unsafe-metadata-path", "unregistered-environment"}]
    report = {"schema_version": 1, "house": os.fspath(house.path), "ok": not issues, "issues": issues}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if not issues:
            print("No problems found." + (" Registry rebuilt." if args.rebuild_registry else ""))
        for issue in issues:
            print(f"[{issue['code']}] {issue['message']}: {issue['path']}")
    return bool(issues)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        house = House(resolve_house(args.house))
        if args.command in {"create", "link", "move", "unlink", "remove"}:
            # Create initializes; other modification commands require a house.
            if args.command != "create" and not house.registry_path.exists():
                raise VemError(f"house does not exist: {house.path}", 3)
            globals()[f"command_{args.command}"](house, args)
        elif args.command == "status":
            if not house.registry_path.exists():
                raise VemError(f"house does not exist: {house.path}", 3)
            command_status(house, args)
        else:
            if not house.registry_path.exists():
                raise VemError(f"house does not exist: {house.path}", 3)
            return 7 if command_doctor(house, args) else 0
        return 0
    except VemError as exc:
        print(f"vem: error: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, shutil.Error) as exc:
        print(f"vem: filesystem error: {exc}", file=sys.stderr)
        return 5
