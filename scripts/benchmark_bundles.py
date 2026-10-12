"""Compare frozen startup without timing gates or touching real user state."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--onefile", type=Path, required=True)
    parser.add_argument("--onedir", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("at least two samples are required")
    measurements: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="luminesk-startup-") as temporary:
        directory = Path(temporary)
        env = dict(os.environ)
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "WIN_PD_OVERRIDE_APPDATA",
            "WIN_PD_OVERRIDE_LOCAL_APPDATA",
            "APPDATA",
            "LOCALAPPDATA",
        ):
            env[key] = str(directory / key.lower())
        # Measure doctor deterministically on its missing-Docker path; never
        # connect to the developer's daemon. Record that nonzero exit explicitly.
        env["PATH"] = str(directory)
        targets = {
            "onefile": args.onefile,
            "onedir": args.onedir,
            "launcher": args.launcher,
        }
        for command in ("--version", "--help", "list", "doctor"):
            samples: dict[str, list[float]] = {name: [] for name in targets}
            statuses: dict[str, set[int]] = {name: set() for name in targets}
            for repetition in range(args.samples + 1):
                # Interleave variants to reduce drift caused by system load.
                for name, executable in targets.items():
                    executable = executable.absolute()
                    argv = [str(executable), command]
                    if executable.suffix.lower() == ".cmd":
                        argv = [os.environ["COMSPEC"], "/d", "/c", *argv]
                    started = time.perf_counter()
                    result = subprocess.run(
                        argv,
                        cwd=directory,
                        env=env,
                        capture_output=True,
                        check=False,
                        shell=False,
                    )
                    elapsed = (time.perf_counter() - started) * 1000
                    if (command != "doctor" and result.returncode != 0) or (
                        command == "doctor"
                        and b"Docker CLI is not available"
                        not in result.stdout + result.stderr
                    ):
                        raise RuntimeError(
                            f"benchmark command failed: {argv}: {result.stdout!r} {result.stderr!r}"
                        )
                    samples[name].append(elapsed)
                    statuses[name].add(result.returncode)
            for name, values in samples.items():
                warm = values[1:]
                measurements.append(
                    {
                        "variant": name,
                        "command": command,
                        "first_observed_ms": values[0],
                        "warm_samples": len(warm),
                        "median_ms": statistics.median(warm),
                        "p95_ms": sorted(warm)[math.ceil(len(warm) * 0.95) - 1],
                        "exit_codes": sorted(statuses[name]),
                        "samples_ms": warm,
                    }
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "platform": platform.platform(),
                "processor": platform.processor(),
                "method": "fresh process per sample, warm OS cache; first observed is not a cold-cache measurement",
                "doctor": "missing-Docker path, isolated PATH; no daemon contacted",
                "cold_cache": "not measured: OS file cache was not purged and host was not rebooted",
                "measurements": measurements,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for row in measurements:
        print(
            f"{row['variant']:8} {row['command']:10} median={row['median_ms']:.2f} ms p95={row['p95_ms']:.2f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
