from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

from integrations.common.model_routing import RoutingError, validate_model_id


MAX_REGISTRY_BYTES = 8 * 1024 * 1024
MAX_CONTEXT_LIMIT = 2_000_000
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class HeadroomModelsError(ValueError):
    pass


def _bounded_text(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise HeadroomModelsError(f"{label} is invalid")
    return value


def _limit(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CONTEXT_LIMIT:
        raise HeadroomModelsError(
            f"{label} must be an integer from 1 through {MAX_CONTEXT_LIMIT}"
        )
    return value


def build_catalog(
    registry: object,
    repository: str,
    tag: str,
    version: str,
    registry_sha256: str,
) -> dict[str, object]:
    repository = _bounded_text(
        repository, REPOSITORY_PATTERN, "source repository"
    )
    tag = _bounded_text(tag, TAG_PATTERN, "source tag")
    version = _bounded_text(version, VERSION_PATTERN, "source version")
    registry_sha256 = _bounded_text(
        registry_sha256, SHA256_PATTERN, "registry SHA-256"
    )
    if tag not in {version, f"v{version}"}:
        raise HeadroomModelsError("source tag does not match source version")
    if not isinstance(registry, Mapping):
        raise HeadroomModelsError("CLIProxyAPI registry must be an object")

    limits: dict[str, int] = {}
    provider_arrays = 0
    for provider, records in registry.items():
        if not isinstance(provider, str) or not isinstance(records, list):
            continue
        provider_arrays += 1
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise HeadroomModelsError(
                    f"registry provider {provider!r} entry {index} is not an object"
                )
            if "id" not in record:
                continue
            try:
                model_id = validate_model_id(
                    record["id"], f"registry provider {provider} entry {index}"
                )
            except RoutingError as error:
                raise HeadroomModelsError(str(error)) from error
            declared = []
            for field in ("context_length", "inputTokenLimit"):
                if field in record:
                    declared.append(
                        _limit(record[field], f"{model_id} {field}")
                    )
            if not declared:
                continue
            candidate = min(declared)
            limits[model_id] = min(candidate, limits.get(model_id, candidate))

    if provider_arrays == 0 or not limits:
        raise HeadroomModelsError(
            "CLIProxyAPI registry contains no bounded context metadata"
        )
    return {
        "schemaVersion": 1,
        "source": {
            "repository": repository,
            "tag": tag,
            "version": version,
            "registrySha256": registry_sha256,
        },
        "anthropic": {"context_limits": dict(sorted(limits.items()))},
    }


def validate_catalog(
    catalog: object,
    expected_repository: str | None = None,
    expected_version: str | None = None,
) -> dict[str, int]:
    if not isinstance(catalog, Mapping) or set(catalog) != {
        "schemaVersion",
        "source",
        "anthropic",
    }:
        raise HeadroomModelsError("Headroom model catalogue has invalid keys")
    if (
        type(catalog["schemaVersion"]) is not int
        or catalog["schemaVersion"] != 1
    ):
        raise HeadroomModelsError("Headroom model catalogue schema must be 1")
    source = catalog["source"]
    anthropic = catalog["anthropic"]
    if not isinstance(source, Mapping) or set(source) != {
        "repository",
        "tag",
        "version",
        "registrySha256",
    }:
        raise HeadroomModelsError("Headroom model catalogue source is invalid")
    if not isinstance(anthropic, Mapping) or set(anthropic) != {
        "context_limits"
    }:
        raise HeadroomModelsError("Headroom anthropic catalogue is invalid")
    repository = _bounded_text(
        source["repository"], REPOSITORY_PATTERN, "source repository"
    )
    tag = _bounded_text(source["tag"], TAG_PATTERN, "source tag")
    version = _bounded_text(
        source["version"], VERSION_PATTERN, "source version"
    )
    _bounded_text(
        source["registrySha256"], SHA256_PATTERN, "registry SHA-256"
    )
    if tag not in {version, f"v{version}"}:
        raise HeadroomModelsError("source tag does not match source version")
    if expected_repository is not None and repository != expected_repository:
        raise HeadroomModelsError("source repository does not match")
    if expected_version is not None and version != expected_version:
        raise HeadroomModelsError("source version does not match")
    raw_limits = anthropic["context_limits"]
    if not isinstance(raw_limits, Mapping) or not raw_limits:
        raise HeadroomModelsError("Headroom context limits are empty")
    limits: dict[str, int] = {}
    for raw_model, raw_limit in raw_limits.items():
        try:
            model = validate_model_id(raw_model, "Headroom model")
        except RoutingError as error:
            raise HeadroomModelsError(str(error)) from error
        limits[model] = _limit(raw_limit, f"{model} context limit")
    return dict(sorted(limits.items()))


def _read_json(path: Path) -> tuple[object, bytes]:
    try:
        with path.open("rb") as handle:
            payload = handle.read(MAX_REGISTRY_BYTES + 1)
    except OSError as error:
        raise HeadroomModelsError(f"could not read {path}") from error
    if len(payload) > MAX_REGISTRY_BYTES:
        raise HeadroomModelsError(f"{path} exceeds the size limit")
    try:
        return json.loads(payload.decode("utf-8")), payload
    except (UnicodeError, json.JSONDecodeError) as error:
        raise HeadroomModelsError(f"{path} is not valid UTF-8 JSON") from error


def _write_private_new(path: Path, catalog: Mapping[str, object]) -> None:
    if path.is_symlink():
        raise HeadroomModelsError("output path must not be a symlink")
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(catalog, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.chmod(path, 0o600)
    except OSError as error:
        raise HeadroomModelsError(f"could not write {path}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="headroom-models")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--registry", type=Path, required=True)
    generate.add_argument("--repository", required=True)
    generate.add_argument("--tag", required=True)
    generate.add_argument("--version", required=True)
    generate.add_argument("--output", type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--catalog", type=Path, required=True)
    validate.add_argument("--expected-repository")
    validate.add_argument("--expected-version")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "generate":
            registry, payload = _read_json(arguments.registry)
            catalog = build_catalog(
                registry,
                arguments.repository,
                arguments.tag,
                arguments.version,
                hashlib.sha256(payload).hexdigest(),
            )
            validate_catalog(
                catalog,
                expected_repository=arguments.repository,
                expected_version=arguments.version,
            )
            _write_private_new(arguments.output, catalog)
        else:
            catalog, _ = _read_json(arguments.catalog)
            validate_catalog(
                catalog,
                expected_repository=arguments.expected_repository,
                expected_version=arguments.expected_version,
            )
        return 0
    except HeadroomModelsError as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
