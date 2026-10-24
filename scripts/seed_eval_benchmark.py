#!/usr/bin/env python3
"""Populate the offline evaluation benchmark (default: 50 labelled scenarios).

Writes ``benchmarks/scenarios.json``. Commit it to pin the benchmark, edit it to
add your own historical incidents, then score any LLM against it::

    python scripts/seed_eval_benchmark.py
    resilio eval run --scenarios benchmarks/scenarios.json --llm ollama
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from resilio.utils.evaluator_benchmark import generate_scenarios, save_scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", "-o", type=Path, default=Path("benchmarks/scenarios.json"))
    parser.add_argument("--count", "-n", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    scenarios = generate_scenarios(args.count, args.seed)
    save_scenarios(scenarios, args.output)

    labels = Counter(s.label for s in scenarios)
    types = Counter(s.incident_type for s in scenarios)
    print(f"wrote {len(scenarios)} scenarios to {args.output}")
    print("  labels: " + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))
    print("  types:  " + ", ".join(f"{k}={v}" for k, v in types.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
