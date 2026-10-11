"""Build one target's go-scan + symbol binaries, its platform wheel, and its release archive.

Usage:
    uv run --no-project scripts/build_dist.py --target darwin-arm64 [--smoke]

Outputs dist/wyolet_symbol-<version>-py3-none-<platform>.whl and
build/archives/symbol-<version>-<target>.{tar.gz,zip}. --smoke runs both on
a small Python + Go project (host target only).
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GO_SCAN_DIR = ROOT / "src/wyolet/symbol/adapters/go_ast/daemon"
STAGING_DIR = ROOT / "build/wheel-bin"
ARCHIVE_DIR = ROOT / "build/archives"
MACOS_MIN = "13.0"

RUST_TRIPLES = {
    "darwin-arm64": "aarch64-apple-darwin",
    "darwin-amd64": "x86_64-apple-darwin",
    "linux-amd64": "x86_64-unknown-linux-musl",
    "linux-arm64": "aarch64-unknown-linux-musl",
    "windows-amd64": "x86_64-pc-windows-msvc",
}

SMOKE_PROJECT = {
    "app.py": "class UserService:\n    def save(self):\n        return 1\n",
    "go.mod": "module example.com/smoke\n\ngo 1.22\n",
    "store.go": "package smoke\n\ntype Store struct{}\n\nfunc (s *Store) Save() error { return nil }\n",
}


def host_target() -> str:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    return f"{platform.system().lower()}-{arch}"


def run(argv: list, cwd: Path = ROOT, env: dict | None = None) -> None:
    print("+", " ".join(str(a) for a in argv), flush=True)
    subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})}, check=True)


def capture(argv: list, cwd: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
    print("+", " ".join(str(a) for a in argv), flush=True)
    return subprocess.run(argv, cwd=cwd, env={**os.environ, **(env or {})}, text=True, capture_output=True)


def build_go_scan(target: str, out: Path) -> None:
    goos, goarch = target.split("-")
    env = {"GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"}
    run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(out), "."], cwd=GO_SCAN_DIR, env=env)


def build_symbol(target: str, suffix: str, out: Path) -> None:
    triple = RUST_TRIPLES[target]
    env = {}
    if target.startswith("darwin-"):
        env["MACOSX_DEPLOYMENT_TARGET"] = MACOS_MIN
    elif target.startswith("linux-"):
        # zstd-sys compiles C; musl-gcc keeps the binary fully static.
        env["TARGET_CC"] = os.environ.get("TARGET_CC", "musl-gcc")
    elif target.startswith("windows-"):
        # No dependency on the VC++ redistributable.
        env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_RUSTFLAGS"] = "-C target-feature=+crt-static"
    manifest = ROOT / "rust/Cargo.toml"
    run(["cargo", "build", "--release", "--locked", "--manifest-path", manifest, "-p", "symbol-core", "--target", triple], env=env)
    shutil.copy2(ROOT / "rust/target" / triple / "release" / f"symbol{suffix}", out)


def build_wheel(target: str, wheel_dir: Path) -> Path:
    out_dir = ROOT / "build/wheel-out" / target
    shutil.rmtree(out_dir, ignore_errors=True)
    run(["uv", "build", "--wheel", "--out-dir", out_dir], env={"SYMBOL_WHEEL_TARGET": target})
    (wheel,) = out_dir.glob("*.whl")
    wheel_dir.mkdir(parents=True, exist_ok=True)
    return Path(shutil.move(wheel, wheel_dir / wheel.name))


def build_archive(target: str, version: str, binaries: dict[str, Path]) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if target.startswith("windows-"):
        archive = ARCHIVE_DIR / f"symbol-{version}-{target}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in binaries.items():
                zf.write(path, name)
        return archive
    archive = ARCHIVE_DIR / f"symbol-{version}-{target}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, path in binaries.items():
            info = tf.gettarinfo(path, name)
            info.mode, info.uid, info.gid, info.uname, info.gname = 0o755, 0, 0, "", ""
            with path.open("rb") as fh:
                tf.addfile(info, fh)
    return archive


def check_macos_floor(wheel: Path, binaries: list[Path]) -> None:
    """The wheel's macosx_X_Y tag must not promise less than the binaries need."""
    floor = tuple(int(p) for p in re.search(r"macosx_(\d+)_(\d+)", wheel.name).groups())
    for binary in binaries:
        load_commands = capture(["otool", "-l", binary]).stdout
        for minos in re.findall(r"minos (\d+)\.(\d+)", load_commands):
            if tuple(int(p) for p in minos) > floor:
                sys.exit(f"{binary.name} needs macOS {'.'.join(minos)} but {wheel.name} claims {floor}")


def expect(result: subprocess.CompletedProcess, needle: str, ok: bool = True) -> None:
    passed = (result.returncode == 0) == ok and needle in result.stdout + result.stderr
    if not passed:
        sys.exit(f"smoke failed (exit {result.returncode}, wanted {'success' if ok else 'failure'}, '{needle}'):\n"
                 f"{result.stdout}\n{result.stderr}")


def smoke_project(parent: Path) -> Path:
    project = parent / "project"
    project.mkdir(parents=True)
    for name, text in SMOKE_PROJECT.items():
        (project / name).write_text(text)
    return project


def smoke(archive: Path, wheel: Path, version: str, suffix: str) -> None:
    # No source-checkout fallback, so go-scan must come from beside the binary.
    env = {"SYMBOL_REPO": "/nonexistent", "SYMBOL_PYTHON": sys.executable}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        standalone = tmp_path / "standalone"
        shutil.unpack_archive(archive, standalone)
        binary = standalone / f"symbol{suffix}"
        expect(capture([binary, "--version"]), version)
        project = smoke_project(tmp_path / "a")
        expect(capture([binary, "index"], cwd=project, env=env), "")
        expect(capture([binary, "search", "UserService"], cwd=project, env=env), "UserService")
        expect(capture([binary, "search", "Store"], cwd=project, env=env), "Store")

        venv = tmp_path / "venv"
        uv = os.environ.get("UV", "uv")
        run([uv, "venv", "--quiet", "--python", "3.13", venv])
        scripts = venv / ("Scripts" if suffix else "bin")
        run([uv, "pip", "install", "--quiet", "--python", scripts / f"python{suffix}", wheel])
        symbol = scripts / f"symbol{suffix}"
        project = smoke_project(tmp_path / "b")
        env = {"SYMBOL_REPO": "/nonexistent"}
        expect(capture([symbol, "index"], cwd=project, env=env), "")
        expect(capture([symbol, "search", "Store"], cwd=project, env=env), "Store")
        # Only the Rust binary reads SYMBOL_GOSCAN: failing here proves delegation.
        missing_goscan = {**env, "SYMBOL_GOSCAN": str(tmp_path / "missing-go-scan")}
        expect(capture([symbol, "index"], cwd=project, env=missing_goscan), "", ok=False)
        python_impl = {**missing_goscan, "SYMBOL_IMPL": "python"}
        expect(capture([symbol, "search", "UserService"], cwd=project, env=python_impl), "UserService")
    print(f"smoke ok: {archive.name}, {wheel.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", default=host_target(), choices=sorted(RUST_TRIPLES))
    parser.add_argument("--wheel-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--smoke", action="store_true", help="run the outputs (host target only)")
    args = parser.parse_args()

    target = args.target
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    suffix = ".exe" if target.startswith("windows-") else ""
    staging = STAGING_DIR / target
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    go_scan = staging / f"go-scan-{target}{suffix}"
    symbol = staging / f"symbol-{target}{suffix}"
    build_go_scan(target, go_scan)
    build_symbol(target, suffix, symbol)

    wheel = build_wheel(target, args.wheel_dir)
    archive = build_archive(target, version, {f"symbol{suffix}": symbol, f"go-scan{suffix}": go_scan})
    if target.startswith("darwin-"):
        check_macos_floor(wheel, [symbol, go_scan])
    print(f"built {wheel}\nbuilt {archive}")
    if args.smoke:
        if target != host_target():
            sys.exit(f"--smoke needs the host target ({host_target()}), not {target}")
        smoke(archive, wheel, version, suffix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
