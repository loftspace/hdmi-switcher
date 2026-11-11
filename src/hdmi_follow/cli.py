"""Local setup, diagnostics and lifecycle commands."""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hdmi_follow import __version__, autostart, diagnostics, exit_evidence, launch, lifecycle, tv
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
from hdmi_follow.tv_session import Session


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
        "usb-instances",
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
            command.add_argument("--device", action="append", type=device_arg)
            command.add_argument("--switch-hub", help="Windows USB hub VID/PID or full instance ID")
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
            command.add_argument(
                "--switch-hub", help="Monitor a Windows USB hub by VID/PID or instance ID"
            )
    return result


def diagnostic(seconds: int, switch_hub: str | None = None) -> None:
    if not 1 <= seconds <= 3600:
        raise ValueError("Diagnostic duration must be 1..3600 seconds")
    last: set[tuple[int, int]] | None = None
    end = time.monotonic() + seconds
    last_hub: bool | None = None
    while time.monotonic() < end:
        if switch_hub:
            from hdmi_follow.windows_usb import is_present

            hub = is_present(switch_hub)
            if hub != last_hub:
                print(f"{time.time():.3f} Switch hub present: {hub}", flush=True)
                last_hub = hub
            time.sleep(0.05)
            continue
        seen = {(d.vendor_id, d.product_id) for d in enumerate_devices()}
        if last is None:
            print("Baseline:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(seen)))
        elif seen != last:
            print("Added:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(seen - last)))
            print("Removed:", " ".join(f"{v:04x}:{p:04x}" for v, p in sorted(last - seen)))
        last = seen
        time.sleep(0.5)


def _logging(path: Path) -> None:
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s pid=%(process)d %(name)s %(message)s")
    )
    logging.getLogger("hdmi_follow").setLevel(logging.INFO)
    logging.getLogger("hdmi_follow").addHandler(handler)


def _request_stop(path: Path, reason: str = "cli_stop") -> None:
    stop_file = path.with_name("stop")
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(
        json.dumps(
            {
                "reason": reason,
                "requester_pid": os.getpid(),
                "requested_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        from hdmi_follow.windows_stop import signal_stop

        signal_stop(stop_file)


def _wait_for_exit(path: Path, timeout: float = 3.0) -> bool:
    """Wait, bounded, until the helper releases its run lock."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with instance_lock(path.with_name("run.lock")):
                return True
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def execute(args: argparse.Namespace) -> None:
    path: Path = args.config.expanduser().resolve()
    if args.command == "usb-instances":
        from hdmi_follow.windows_usb import present_instances

        print("\n".join(sorted(present_instances())))
        return
    if args.command == "devices":
        unique = {(d.vendor_id, d.product_id, d.name) for d in enumerate_devices()}
        for vendor, product, name in sorted(unique):
            print(f"{vendor:04x}:{product:04x}  {name}")
        return
    if args.command == "diagnose":
        diagnostic(args.seconds, args.switch_hub)
        return
    if args.command == "setup":
        if args.switch_hub and sys.platform != "win32":
            raise RuntimeError(
                "Switch-hub detection is Windows-only; use --device on this platform"
            )
        if path.exists() and not args.overwrite:
            raise ValueError("Configuration exists. Use --overwrite to replace it explicitly")
        Config(
            args.tv_ip,
            args.own_input,
            tuple(args.device or []),
            args.away_input,
            switch_hub=args.switch_hub,
        ).save(path)
        print(f"Saved {path}. Pair the TV before starting the helper")
        return
    if args.command == "uninstall":
        _request_stop(path, "uninstall")
        # Let the helper record a clean exit before autostart removal ends the task.
        if not _wait_for_exit(path):
            print("Helper still running after 3 seconds")
        print(autostart.uninstall())
        print("Stop requested. Configuration and credentials retained")
        return
    if args.command == "stop":
        _request_stop(path)
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
    if (
        config.switch_hub
        and sys.platform != "win32"
        and args.command in {"run", "start", "install"}
    ):
        raise RuntimeError(
            "Switch-hub detection is Windows-only; configure --device on this platform"
        )
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
            output.write(
                (
                    f"{datetime.now(UTC).isoformat()} background launch requested "
                    f"launcher_pid={os.getpid()}\n"
                ).encode()
            )
            output.flush()
            child = launch.spawn_background(command, output, os.environ.copy())
            output.write((f"{datetime.now(UTC).isoformat()} launched_pid={child.pid}\n").encode())
            output.flush()
        print("Background start requested; use status to verify")
    elif args.command == "run":
        with instance_lock(path.with_name("run.lock")):
            path.with_name("stop").unlink(missing_ok=True)
            _logging(path.parent / "follow.log")
            log = logging.getLogger(__name__)
            record = lifecycle.RunRecord(path.with_name("run-state.json"), __version__)
            record.start()
            lifecycle.set_current(record)
            crash_log = diagnostics.enable_fatal_traces(path.with_name("crash.log"))
            log.info(
                "Helper starting version=%s platform=%s mode=%s debounce_ms=%.0f own=%s away=%s %s",
                __version__,
                sys.platform,
                "hub" if config.switch_hub else "keyboard",
                config.debounce * 1000,
                config.own_input,
                config.away_input,
                diagnostics.health_summary(),
            )

            async def monitored_run() -> None:
                evidence = None
                if record.previous is not None and record.previous.get("status") == "running":
                    evidence = asyncio.create_task(
                        exit_evidence.report_unclean_exit(record.previous)
                    )
                try:
                    await run(
                        config,
                        key,
                        stop_file=path.with_name("stop"),
                        session=Session(config.tv_ip, key),
                    )
                finally:
                    if evidence is not None:
                        evidence.cancel()
                        await asyncio.gather(evidence, return_exceptions=True)

            exit_reason = "normal"
            try:
                asyncio.run(monitored_run())
                if path.with_name("stop").exists():
                    exit_reason = "cli_stop"
                    log.info("Exit requested %s", diagnostics.stop_summary(path.with_name("stop")))
            except KeyboardInterrupt:
                exit_reason = "interrupt"
                log.info("Helper interrupted by console signal")
                raise
            except BaseException as exc:
                exit_reason = "error"
                log.error("Helper exiting error_type=%s", type(exc).__name__)
                raise
            finally:
                record.finish(exit_reason)
                lifecycle.set_current(None)
                diagnostics.close_fatal_traces(crash_log)
                log.info("Helper stopped reason=%s", exit_reason)


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
