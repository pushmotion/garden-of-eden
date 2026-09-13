#!/usr/bin/env python3
"""Generate one Sections dashboard view per tower, without touching HA."""

import argparse
import re
from pathlib import Path


def generate(identifiers):
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ValueError("Supply distinct tower identifiers")
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", item) for item in identifiers):
        raise ValueError("Identifiers must contain lowercase letters, digits and underscores")
    template = (
        Path(__file__).resolve().parents[1] / "docs/homeassistant/pm-example.yaml"
    ).read_text()
    view = template.split("\nviews:\n", 1)[1]
    return "title: Garden of Eden\nviews:\n" + "".join(
        view.replace("gardyn_01", identifier)
        .replace("  - title: Tower", "  - title: " + identifier)
        .replace("    path: tower", "    path: " + identifier.replace("_", "-"))
        for identifier in identifiers
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("identifiers", nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = generate(args.identifiers)
    args.output.write_text(output, encoding="utf-8")
    print(f"Wrote {len(args.identifiers)} views to {args.output}. Import into a new HA dashboard.")


if __name__ == "__main__":
    main()
