"""Local setup, diagnostics and lifecycle commands."""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hdmi_follow import __version__, autostart, tv
from hdmi_follow.config import (
    Config,
    Device,
    default_config,
    key_path,
    load_key,
    write_private_json,
)
from hdmi_follow.devices import enumerate_devices
from hdmi_follow.instance import AlreadyRunning, instance_lock
from hdmi_follow.runner import run


def device_arg(value: str) -> Device:
    try:
        vendor, product = value.split(":")
        return Device(int(vendor, 16), int(product, 16))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("Use hexadecimal VID:PID, for example 3434:0e21") from exc


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Follow a USB switch with LG HDMI input selection")
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)
    for name in (
        "setup",
        "pair",
        "run",
        "start",
        "stop",
        "status",
        "switch",
        "inputs",
        "install",
        "uninstall",
        "devices",
        "diagnose",
    ):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=default_config())
        if name == "setup":
            command.add_argument("--tv-ip", required=True)
            command.add_argument("--own-input", required=True)
            command.add_argument(
                "--away-input", help="Optional: select peer HDMI when devices leave"
            )
            command.add_argument("--device", action="append", type=device_arg, required=True)
            command.add_argument("--overwrite", action="store_true")
        if name == "pair":
            command.add_argument(
                "--import-key-file",
                type=Path,
                help="Import an existing plaintext key, never print it",
            )
        if name == "switch":
            command.add_argument("input", choices=[f"HDMI_{i}" for i in range(1, 5)])
        if name == "diagnose":
            command.add_argument("--seconds", type=int, default=30)
    return result


def diagnostic(seconds: int) -> None:
    if not 1 <= seconds <= 3600:
        raise ValueError("Diagnostic duration must be 1..3600 seconds")
    last: set[tuple[int, int]] | None = None
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        seen = {(d.vendor_id, d.product_id) for d in enumerate_devices()}
        if last is None:
            print("Baseline:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(seen)))
        elif seen != last:
            print("Added:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(seen - last)))
            print("Removed:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(last - seen)))
        last = seen
        time.sleep(0.5)


def _logging(path: Path) -> None:
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("hdmi_follow").setLevel(logging.INFO)
    logging.getLogger("hdmi_follow").addHandler(handler)


def execute(args: argparse.Namespace) -> None:
    path: Path = args.config.expanduser().resolve()
    if args.command == "devices":
        unique = {(d.vendor_id, d.product_id, d.name) for d in enumerate_devices()}
        for vendor, product, name in sorted(unique):
            print(f"{vendor:04x}:{product:04x}  {name}")
        return
    if args.command == "diagnose":
        diagnostic(args.seconds)
        return
    if args.command == "setup":
        if path.exists() and not args.overwrite:
            raise ValueError("Configuration exists. Use --overwrite to replace it explicitly")
        Config(args.tv_ip, args.own_input, tuple(args.device), args.away_input).save(path)
        print(f"Saved {path}. Pair the TV before starting the helper")
        return
    if args.command == "uninstall":
        path.with_name("stop").parent.mkdir(parents=True, exist_ok=True)
        path.with_name("stop").touch()
        print(autostart.uninstall())
        print("Stop requested. Configuration and credentials retained")
        return
    if args.command == "stop":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_name("stop").touch()
        print("Stop requested")
        return
    if args.command == "status":
        try:
            with instance_lock(path.with_name("run.lock")):
                print("Helper stopped")
        except AlreadyRunning:
            print("Helper running")
        print(f"Configuration: {path}")
        print(autostart.status())
        return
    config = Config.load(path)
    credentials = key_path(path)
    key = load_key(credentials, config.tv_ip)
    if args.command == "pair":
        if args.import_key_file:
            key = args.import_key_file.read_text(encoding="utf-8").strip()
            if not key:
                raise ValueError("Imported pairing key is empty")
            # Validate a key with a read-only request, without forcing pairing.
            asyncio.run(
                tv.request(
                    config.tv_ip, key, "ssap://com.webos.applicationManager/getForegroundAppInfo"
                )
            )
        else:
            print("Accept the HDMI Follow connection prompt on the TV, if shown", flush=True)
            key = asyncio.run(tv.pair(config.tv_ip, key))
        write_private_json(credentials, {"tv_ip": config.tv_ip, "client_key": key})
        print("Pairing saved locally")
        return
    if not key:
        raise tv.PairingRequired("No pairing key for this TV. Run pair first")
    if args.command == "inputs":
        print(
            json.dumps(
                asyncio.run(
                    tv.request(
                        config.tv_ip,
                        key,
                        "ssap://tv/getExternalInputList",
                    )
                ),
                indent=2,
            )
        )
    elif args.command == "switch":
        asyncio.run(tv.switch(config.tv_ip, key, args.input))
        print(f"Selected {args.input}")
    elif args.command == "install":
        print(autostart.install(path))
    elif args.command == "start":
        # The child holds the lock; duplicate starts exit without touching the TV.
        executable = sys.executable
        if sys.platform == "win32" and not getattr(sys, "frozen", False):
            candidate = Path(executable).with_name("pythonw.exe")
            if candidate.exists():
                executable = str(candidate)
        command = [executable]
        if not getattr(sys, "frozen", False):
            command += ["-m", "hdmi_follow"]
        command += ["run", "--config", str(path)]
        with (path.parent / "startup.log").open("ab") as output:
            environment = os.environ.copy()
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                creationflags=0x08000000 if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
                env=environment,
            )
        print("Background start requested; use status to verify")
    elif args.command == "run":
        with instance_lock(path.with_name("run.lock")):
            path.with_name("stop").unlink(missing_ok=True)
            _logging(path.parent / "follow.log")
            asyncio.run(run(config, key, stop_file=path.with_name("stop")))


def main() -> None:
    try:
        execute(parser().parse_args())
    except KeyboardInterrupt:
        pass
    except (
        ValueError,
        OSError,
        RuntimeError,
        tv.TVError,
        AlreadyRunning,
        TimeoutError,
        KeyError,
        TypeError,
    ) as exc:
        print(f"HDMI Follow: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"HDMI Follow: {type(exc).__name__}; operation failed", file=sys.stderr)
        raise SystemExit(1) from None
