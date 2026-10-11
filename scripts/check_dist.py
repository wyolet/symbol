"""Verify built distributions carry exactly the right binaries.

Usage:
    uv run --no-project scripts/check_dist.py dist build/archives [--all-targets]

Platform wheels hold exactly symbol-<target> and go-scan-<target>; the pure
wheel and sdist hold none; archives hold exactly symbol and go-scan. Linux
binaries must be static (no ELF interpreter) to honor the musllinux tag.
"""

import argparse
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

TARGETS = ("darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64", "windows-amd64")
BINARY_NAMES = ("symbol", "go-scan")
PT_INTERP = 3


def wheel_target(wheel: Path) -> str | None:
    platform_tag = wheel.stem.split("-")[-1]
    if platform_tag == "any":
        return None
    system = "darwin" if platform_tag.startswith("macosx") else "windows" if platform_tag.startswith("win") else "linux"
    arch = "arm64" if "arm64" in platform_tag or "aarch64" in platform_tag else "amd64"
    return f"{system}-{arch}"


def has_elf_interpreter(data: bytes) -> bool:
    if data[:4] != b"\x7fELF" or data[4] != 2:
        raise ValueError("not a 64-bit ELF")
    endian = "<" if data[5] == 1 else ">"
    phoff = struct.unpack_from(f"{endian}Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from(f"{endian}HH", data, 0x36)
    return any(struct.unpack_from(f"{endian}I", data, phoff + i * phentsize)[0] == PT_INTERP for i in range(phnum))


def check_binary(label: str, name: str, mode: int, data: bytes, target: str) -> list[str]:
    errors = []
    if not target.startswith("windows-") and not mode & 0o111:
        errors.append(f"{label}: {name} is not executable (mode {oct(mode)})")
    if target.startswith("linux-") and has_elf_interpreter(data):
        errors.append(f"{label}: {name} is dynamically linked")
    return errors


def check_wheel(wheel: Path) -> list[str]:
    target = wheel_target(wheel)
    with zipfile.ZipFile(wheel) as zf:
        binaries = {info.filename.rsplit("/", 1)[1]: info for info in zf.infolist() if "/bin/" in info.filename}
        if target is None:
            return [f"{wheel.name}: pure wheel carries {sorted(binaries)}"] if binaries else []
        suffix = ".exe" if target.startswith("windows-") else ""
        expected = {f"{name}-{target}{suffix}" for name in BINARY_NAMES}
        if set(binaries) != expected:
            return [f"{wheel.name}: binaries {sorted(binaries)}, expected {sorted(expected)}"]
        errors = []
        for name, info in binaries.items():
            errors += check_binary(wheel.name, name, info.external_attr >> 16, zf.read(info), target)
        return errors


def check_archive(archive: Path) -> list[str]:
    target = archive.name.removesuffix(".tar.gz").removesuffix(".zip").split("-", 2)[2]
    suffix = ".exe" if target.startswith("windows-") else ""
    expected = {f"{name}{suffix}" for name in BINARY_NAMES}
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            members = {info.filename: (info.external_attr >> 16, zf.read(info)) for info in zf.infolist()}
    else:
        with tarfile.open(archive) as tf:
            members = {m.name: (m.mode, tf.extractfile(m).read()) for m in tf.getmembers() if m.isfile()}
    if set(members) != expected:
        return [f"{archive.name}: members {sorted(members)}, expected {sorted(expected)}"]
    errors = []
    for name, (mode, data) in members.items():
        errors += check_binary(archive.name, name, mode, data, target)
    return errors


def check_sdist(sdist: Path) -> list[str]:
    with tarfile.open(sdist) as tf:
        names = tf.getnames()
    stray = [n for n in names if "/bin/" in n or "/target/" in n]
    return [f"{sdist.name}: carries build outputs {stray[:5]}"] if stray else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dirs", nargs="+", type=Path, help="directories holding wheels, sdists, archives")
    parser.add_argument("--all-targets", action="store_true", help="require every target plus the pure wheel and sdist")
    args = parser.parse_args()

    files = [f for d in args.dirs for f in sorted(d.iterdir()) if f.is_file()]
    wheels = [f for f in files if f.suffix == ".whl"]
    sdists = [f for f in files if f.name.startswith("wyolet_symbol-") and f.name.endswith(".tar.gz")]
    archives = [f for f in files if f.name.startswith("symbol-") and f.name.endswith((".tar.gz", ".zip"))]
    errors = [e for w in wheels for e in check_wheel(w)]
    errors += [e for s in sdists for e in check_sdist(s)]
    errors += [e for a in archives for e in check_archive(a)]

    if args.all_targets:
        wheel_targets = sorted(str(wheel_target(w)) for w in wheels)
        archive_targets = sorted(a.name.removesuffix(".tar.gz").removesuffix(".zip").split("-", 2)[2] for a in archives)
        if wheel_targets != sorted([*TARGETS, "None"]):
            errors.append(f"wheel targets {wheel_targets}; want one per {TARGETS} plus one pure wheel")
        if archive_targets != sorted(TARGETS):
            errors.append(f"archive targets {archive_targets}; want one per {TARGETS}")
        if len(sdists) != 1:
            errors.append(f"{len(sdists)} sdists; want 1")

    for f in wheels + sdists + archives:
        print(f"checked {f.name}")
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
