"""Generate a cross-platform wheel hash lock from pip's successful resolution."""

import argparse
import json
from pathlib import Path
from urllib.request import urlopen


def main():
    arguments = argparse.ArgumentParser()
    arguments.add_argument("report", type=Path)
    arguments.add_argument("output", type=Path)
    args = arguments.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    lines = ["# Generated from a clean CPython 3.14 resolution. Do not edit manually."]
    for item in sorted(report["install"], key=lambda item: item["metadata"]["name"].lower()):
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        with urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
            release = json.load(response)
        hashes = sorted({file["digests"]["sha256"] for file in release["urls"]
                         if file["packagetype"] == "bdist_wheel" and not file.get("yanked")})
        if not hashes:
            raise ValueError(f"No approved wheels for {name}")
        lines.append(f"{name}=={version} " + " ".join(f"--hash=sha256:{digest}" for digest in hashes))
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Locked {len(report['install'])} packages with wheel hashes.")


if __name__ == "__main__":
    main()
