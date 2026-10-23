#!/usr/bin/env python3
"""Import repositories from meta/next.list into reviewed XBPS recipes."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NEXT_LIST = ROOT / "meta" / "next.list"
RECIPES = ROOT / "meta" / "tarballs.tsv"
GENERATOR_PATH = ROOT / "scripts" / "generate-xbps-templates.py"

SPEC = importlib.util.spec_from_file_location("neko_xbps_generator", GENERATOR_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"no se pudo cargar {GENERATOR_PATH}")
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

HOST_DEPS = {
    "cmake": "cmake ninja pkg-config",
    "meson": "meson pkg-config",
    "gnu-configure": "pkg-config",
    "gnu-makefile": "pkg-config",
}
SOURCE_STYLE_ORDER = ("meson", "cmake", "gnu-configure", "gnu-makefile")
UNKNOWN_LICENSE = "custom:unknown"


@dataclass(frozen=True)
class Candidate:
    raw_line: str
    repo: str
    pkgname: str
    requested_kind: str
    requested_target: str
    ignore_license: bool


def log(message: str) -> None:
    print(message, file=sys.stderr)


def read_candidates(path: Path) -> tuple[list[str], list[Candidate]]:
    comments: list[str] = []
    candidates: list[Candidate] = []
    if not path.exists():
        return comments, candidates
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            comments.append(raw)
            continue
        fields = stripped.split()
        ignore_license = False
        positional: list[str] = []
        for field in fields:
            if field == "--ignore-license":
                if ignore_license:
                    raise generator.GeneratorError(
                        f"{path}:{line_number}: --ignore-license esta repetido"
                    )
                ignore_license = True
            elif field.startswith("--"):
                raise generator.GeneratorError(
                    f"{path}:{line_number}: opcion desconocida: {field!r}"
                )
            else:
                positional.append(field)
        if not positional:
            raise generator.GeneratorError(
                f"{path}:{line_number}: falta el repositorio GitHub"
            )
        if len(positional) > 4:
            raise generator.GeneratorError(
                f"{path}:{line_number}: se esperaban como maximo 4 campos posicionales"
            )
        repo = generator.normalize_repo(positional[0])
        default_name = repo.rsplit("/", 1)[1].lower()
        pkgname = positional[1] if len(positional) >= 2 else default_name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", pkgname):
            raise generator.GeneratorError(
                f"{path}:{line_number}: pkgname invalido: {pkgname!r}"
            )
        requested_kind = positional[2].lower() if len(positional) >= 3 else "auto"
        if requested_kind not in {"auto", "binary", "source"}:
            raise generator.GeneratorError(
                f"{path}:{line_number}: tipo invalido: {requested_kind!r}"
            )
        requested_target = positional[3] if len(positional) >= 4 else ""
        if requested_kind == "source" and requested_target and requested_target not in HOST_DEPS:
            raise generator.GeneratorError(
                f"{path}:{line_number}: build_style no soportado: {requested_target!r}"
            )
        candidates.append(
            Candidate(
                raw,
                repo,
                pkgname,
                requested_kind,
                requested_target,
                ignore_license,
            )
        )
    return comments, candidates


def sanitize_description(value: object, pkgname: str) -> str:
    text = " ".join(str(value or "").replace("\t", " ").split())
    return text or f"Paquete {pkgname}"


def github_license(repo_data: dict, ignore_missing: bool = False) -> str:
    license_data = repo_data.get("license") or {}
    spdx = str(license_data.get("spdx_id") or "").strip()
    if not spdx or spdx in {"NOASSERTION", "OTHER"}:
        if ignore_missing:
            log(
                "  AVISO: licencia SPDX no verificada; "
                f"se usara {UNKNOWN_LICENSE}"
            )
            return UNKNOWN_LICENSE
        raise generator.GeneratorError(
            "GitHub no informa una licencia SPDX; use --ignore-license "
            "solo despues de revisar la licencia manualmente"
        )
    return spdx


def release_assets(release_data: dict) -> list[generator.ReleaseAsset]:
    assets: list[generator.ReleaseAsset] = []
    for item in release_data.get("assets") or []:
        name = str(item.get("name") or "")
        url = str(item.get("browser_download_url") or "")
        if name and url:
            assets.append(
                generator.ReleaseAsset(name, url, int(item.get("size") or 0))
            )
    return assets


def select_binary_asset(candidate: Candidate, assets: list[generator.ReleaseAsset]):
    recipe = generator.Recipe(
        pkgname=candidate.pkgname,
        repo=candidate.repo,
        asset_pattern=".*",
        kind="binary",
        executable=candidate.requested_target or candidate.pkgname,
        build_style="-",
        license="MIT",
        short_desc=candidate.pkgname,
    )
    return generator.select_asset(recipe, assets)


def is_x86_64_elf(header: bytes) -> bool:
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
        return False
    byteorder = "little" if header[5] == 1 else "big" if header[5] == 2 else ""
    return bool(byteorder) and int.from_bytes(header[18:20], byteorder) == 62


def discover_executable(asset_path: Path, asset_name: str, preferred: str, pkgname: str) -> str:
    with tempfile.TemporaryDirectory(prefix="neko-next-inspect-") as tmp_name:
        tar_path = generator.prepare_tar(asset_path, asset_name, Path(tmp_name))
        try:
            archive = tarfile.open(tar_path, mode="r:*")
        except (tarfile.TarError, OSError) as exc:
            raise generator.GeneratorError(f"tarball corrupto: {exc}") from exc
        candidates: list[
            tuple[tuple[int, int, int, str], str, Path]
        ] = []
        with archive:
            members = archive.getmembers()
            if not members or len(members) > generator.MAX_MEMBERS:
                raise generator.GeneratorError("tarball vacio o con demasiadas entradas")
            unpacked = 0
            for member in members:
                path = generator.safe_member_path(member.name)
                if member.isdev() or member.isfifo():
                    raise generator.GeneratorError(
                        f"entrada especial no permitida: {member.name!r}"
                    )
                if member.issym() or member.islnk():
                    generator.validate_link(member)
                if not member.isfile():
                    continue
                unpacked += member.size
                if unpacked > generator.MAX_UNPACKED_BYTES:
                    raise generator.GeneratorError("contenido descomprimido demasiado grande")
                stream = archive.extractfile(member)
                if stream is None or not is_x86_64_elf(stream.read(64)):
                    continue
                lower_parts = {part.lower() for part in path.parts[:-1]}
                if lower_parts.intersection(
                    {"lib", "lib64", "debug", "tests", "test", "examples", "example"}
                ):
                    continue
                basename = path.name
                if ".so" in basename or basename.endswith((".a", ".o")):
                    continue
                wanted = {preferred, pkgname, pkgname.removesuffix("-bin")}
                name_rank = 0 if basename in wanted else 1 if any(
                    basename.startswith(name) for name in wanted if name
                ) else 2
                exec_rank = 0 if member.mode & 0o111 else 1
                score = (name_rank, exec_rank, len(path.parts), basename)
                candidates.append((score, basename, Path(*path.parts)))
        if preferred:
            exact = [item for item in candidates if item[1] == preferred]
            if not exact:
                raise generator.GeneratorError(
                    f"no se encontro el ELF solicitado {preferred!r}"
                )
            candidates = exact
        if not candidates:
            raise generator.GeneratorError("no se encontro un ejecutable ELF x86_64")
        candidates.sort(key=lambda item: item[0])
        if len(candidates) > 1 and candidates[0][0][:-1] == candidates[1][0][:-1]:
            names = ", ".join(sorted({item[1] for item in candidates}))
            raise generator.GeneratorError(
                f"ejecutable ambiguo ({names}); indiquelo en next.list"
            )
        selected_path = candidates[0][2]
        if any(part.lower().endswith(".app") for part in selected_path.parts[:-1]):
            raise generator.GeneratorError(
                "el tarball es un bundle de aplicacion; necesita una receta manual "
                "o soporte de instalacion de bundles"
            )
        return candidates[0][1]


def generated_asset_pattern(asset_name: str, version: str) -> str:
    escaped = re.escape(asset_name)
    replaced = escaped.replace(re.escape(version), ".*") if version else escaped
    if replaced == escaped:
        replaced = re.sub(r"\\d\+(?:\\\.\\d\+)+", ".*", escaped, count=1)
    return f"^{replaced}$"


def source_recipe(
    candidate: Candidate,
    version: str,
    raw_tag: str,
    license_id: str,
    description: str,
) -> generator.Recipe:
    repo_name = candidate.repo.rsplit("/", 1)[1]
    asset = generator.ReleaseAsset(
        f"{repo_name}-{version}.tar.gz",
        f"https://github.com/{candidate.repo}/archive/refs/tags/{raw_tag}.tar.gz",
        0,
    )
    with tempfile.TemporaryDirectory(prefix="neko-next-source-") as tmp_name:
        asset_path = Path(tmp_name) / asset.name
        generator.download_asset(asset, asset_path)
        styles = (
            (candidate.requested_target,)
            if candidate.requested_target
            else SOURCE_STYLE_ORDER
        )
        failures: list[str] = []
        for style in styles:
            recipe = generator.Recipe(
                candidate.pkgname,
                candidate.repo,
                "TARBALL",
                "source",
                "-",
                style,
                license_id,
                description,
                hostmakedepends=HOST_DEPS[style],
            )
            try:
                generator.inspect_archive(asset_path, asset.name, recipe)
                return recipe
            except generator.GeneratorError as exc:
                failures.append(f"{style}: {exc}")
    raise generator.GeneratorError(
        "no se detecto un sistema de compilacion soportado; " + "; ".join(failures)
    )


def recipe_to_tsv(recipe: generator.Recipe) -> str:
    fields = (
        recipe.pkgname,
        recipe.repo,
        recipe.asset_pattern,
        recipe.kind,
        recipe.executable,
        recipe.build_style,
        recipe.license,
        recipe.short_desc,
        recipe.maintainer if recipe.maintainer != generator.DEFAULT_MAINTAINER else "",
        recipe.hostmakedepends,
        recipe.makedepends,
        recipe.depends,
        recipe.configure_args,
    )
    return "\t".join(fields).rstrip("\t")


def import_candidate(candidate: Candidate, token: str) -> generator.Recipe:
    log(f"Analizando {candidate.repo}...")
    repo_url = f"https://api.github.com/repos/{candidate.repo}"
    repo_data = generator.github_json(repo_url, token)
    release_data = generator.github_json(f"{repo_url}/releases/latest", token)
    raw_tag = str(release_data.get("tag_name") or "").strip()
    if not raw_tag:
        raise generator.GeneratorError("la ultima release no tiene tag_name")
    version = raw_tag[1:] if raw_tag.startswith("v") else raw_tag
    license_id = github_license(repo_data, candidate.ignore_license)
    description = sanitize_description(repo_data.get("description"), candidate.pkgname)
    assets = release_assets(release_data)

    if candidate.requested_kind != "source":
        try:
            asset = select_binary_asset(candidate, assets)
            with tempfile.TemporaryDirectory(prefix="neko-next-binary-") as tmp_name:
                asset_path = Path(tmp_name) / asset.name
                generator.download_asset(asset, asset_path)
                executable = discover_executable(
                    asset_path,
                    asset.name,
                    candidate.requested_target,
                    candidate.pkgname,
                )
            return generator.Recipe(
                candidate.pkgname,
                candidate.repo,
                generated_asset_pattern(asset.name, version),
                "binary",
                executable,
                "-",
                license_id,
                description,
            )
        except generator.GeneratorError:
            if candidate.requested_kind == "binary":
                raise

    return source_recipe(
        candidate, version, raw_tag, license_id, description
    )


def write_recipes(path: Path, recipes: list[generator.Recipe]) -> None:
    original = path.read_text(encoding="utf-8").splitlines()
    comments = [line for line in original if not line.strip() or line.lstrip().startswith("#")]
    active = [line for line in original if line.strip() and not line.lstrip().startswith("#")]
    active.extend(recipe_to_tsv(recipe) for recipe in recipes)
    active.sort(key=lambda line: line.split("\t", 1)[0].lower())
    content = "\n".join(comments + active).rstrip() + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_next(path: Path, comments: list[str], failed: list[str]) -> None:
    content = "\n".join(comments + failed).rstrip() + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-failures", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        comments, candidates = read_candidates(NEXT_LIST)
        existing = generator.read_recipes(RECIPES)
    except (OSError, generator.GeneratorError) as exc:
        log(f"Error: {exc}")
        return 2
    if not candidates:
        log("No hay repositorios pendientes en meta/next.list.")
        return 0

    existing_names = {recipe.pkgname for recipe in existing}
    existing_repos = {recipe.repo.lower() for recipe in existing}
    imported: list[generator.Recipe] = []
    failed: list[str] = []
    token = os.environ.get("GITHUB_TOKEN", "")
    for candidate in candidates:
        if candidate.pkgname in existing_names or candidate.repo.lower() in existing_repos:
            log(f"  SKIP: {candidate.pkgname} ya tiene receta")
            continue
        if (ROOT / candidate.pkgname / "template").exists():
            log(f"  SKIP: {candidate.pkgname} ya tiene un template manual")
            continue
        try:
            recipe = import_candidate(candidate, token)
            imported.append(recipe)
            existing_names.add(recipe.pkgname)
            existing_repos.add(recipe.repo.lower())
            log(f"  OK: receta {recipe.kind} para {recipe.pkgname}")
        except (OSError, generator.GeneratorError) as exc:
            failed.append(candidate.raw_line)
            log(f"  ERROR: {candidate.pkgname}: {exc}")

    if imported:
        write_recipes(RECIPES, imported)
    write_next(NEXT_LIST, comments, failed)
    log(f"Importadas: {len(imported)}; pendientes: {len(failed)}")
    if failed and not args.allow_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
