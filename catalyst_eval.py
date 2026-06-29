#!/usr/bin/env python3
"""A/B a local Ollama model on catalyst-labeling accuracy against a fixed set.

Usage:
    PYTHONPATH=. python catalyst_eval.py                       # default model
    PYTHONPATH=. python catalyst_eval.py --model qwen3:14b     # A/B a bigger model
    PYTHONPATH=. python catalyst_eval.py --no-schema           # plain JSON format

Runs the hand-labeled fixture in ``research/ingestion/catalyst_eval.py`` through
the live model and prints type accuracy, is_dilutive precision/recall (the veto's
basis), and the type mistakes. Pick the model that wins here for OLLAMA_MODEL.
"""

import argparse

from research.ingestion.catalyst_eval import format_report, run_eval


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", default="qwen2.5:7b-instruct")
    p.add_argument("--host", default="http://localhost:11434")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--no-schema", action="store_true",
                   help="send plain 'format: json' instead of the JSON schema")
    args = p.parse_args(argv)

    metrics, _ = run_eval(
        host=args.host, model=args.model, timeout=args.timeout,
        use_schema=not args.no_schema,
    )
    print(format_report(metrics, model=args.model))
    if metrics["scored"] == 0:
        print(f"\nNo predictions scored — is Ollama running at {args.host} "
              f"with model '{args.model}' pulled?")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
