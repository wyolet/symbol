"""Platform wheels: SYMBOL_WHEEL_TARGET=<os>-<arch> bundles that target's binaries.

Without it the wheel is pure (py3-none-any) and carries no binaries.
"""

import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Linux binaries are fully static (musl), so they run on glibc and musl
# distros alike. The macOS floor is the Go toolchain's (Go 1.27: macOS 13).
WHEEL_PLATFORMS = {
    "darwin-arm64": "macosx_13_0_arm64",
    "darwin-amd64": "macosx_13_0_x86_64",
    "linux-amd64": "manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64",
    "linux-arm64": "manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64",
    "windows-amd64": "win_amd64",
}


class PlatformWheelHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        target = os.environ.get("SYMBOL_WHEEL_TARGET")
        if self.target_name != "wheel" or version == "editable" or not target:
            return
        if target not in WHEEL_PLATFORMS:
            raise ValueError(f"SYMBOL_WHEEL_TARGET={target!r}; expected one of {sorted(WHEEL_PLATFORMS)}")
        staging_dir = Path(self.root) / self.config["staging-dir"] / target
        suffix = ".exe" if target.startswith("windows-") else ""
        for name in ("symbol", "go-scan"):
            filename = f"{name}-{target}{suffix}"
            binary = staging_dir / filename
            if not binary.is_file():
                raise FileNotFoundError(f"{binary} missing; run scripts/build_dist.py --target {target}")
            build_data["force_include"][str(binary)] = f"wyolet/symbol/bin/{filename}"
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{WHEEL_PLATFORMS[target]}"
