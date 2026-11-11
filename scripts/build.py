"""Build unsigned native CLI archives without inventing a cross-platform binary."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    machine = platform.machine().lower()
    if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
        target, suffix = "windows-x64", ".exe"
    elif sys.platform == "darwin" and machine == "arm64":
        target, suffix = "macos-arm64", ""
    else:
        raise SystemExit(
            "Build on Windows x64 or an Apple Silicon Mac; cross-compilation is not supported."
        )
    staging = root / "build" / "packaging"
    staging.mkdir(parents=True, exist_ok=True)
    entry = staging / "entry.py"
    entry.write_text("from hdmi_follow.cli import main\nmain()\n", encoding="utf-8")
    binaries = ["hdmi-follow"]
    if sys.platform == "win32":
        binaries.append("hdmi-follow-background")
    for name in binaries:
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            name,
            "--paths",
            str(root / "src"),
            "--distpath",
            str(staging),
            "--workpath",
            str(staging / f"work-{name}"),
            "--specpath",
            str(staging),
            "--collect-submodules",
            "websockets",
            "--hidden-import",
            "hid",
        ]
        if name.endswith("-background"):
            command.append("--windowed")
        command.append(str(entry))
        subprocess.run(command, cwd=root, check=True)
    subprocess.run([str(staging / f"hdmi-follow{suffix}"), "--help"], check=True, cwd=root)
    output = root / "dist" / "release"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"HDMI-Follow-{target}-{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for name in binaries:
            binary = staging / f"{name}{suffix}"
            package.write(binary, binary.name)
        for name in ["README.md", "LICENSE", "docs/macos-handoff.md"]:
            path = root / name
            if path.is_file():
                package.write(path, name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / f"{archive.name}.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    print(f"Built unsigned {archive.name}")


if __name__ == "__main__":
    main()
