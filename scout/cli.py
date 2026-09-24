"""CLI entry point.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  export GOOGLE_CSE_API_KEY=...
  export GOOGLE_CSE_CX=...

  python -m scout.cli discover "automotive spare parts shop Nairobi" --max 8
  python -m scout.cli list
  python -m scout.cli watch <brief_id>
"""
import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .config import load_config
from .providers.google_cse import GoogleCSEProvider
from .providers.requests_browser import RequestsBrowserProvider
from .providers.anthropic_llm import AnthropicLLMProvider
from . import intelligence, pipeline, store


def _print_report(report):
    print(json.dumps(report, default=lambda value: asdict(value) if is_dataclass(value) else str(value), indent=2))


def _build_providers(cfg):
    if not (cfg.google_cse_api_key and cfg.google_cse_cx):
        raise RuntimeError("GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX not set.")
    search = GoogleCSEProvider(cfg.google_cse_api_key, cfg.google_cse_cx)
    browser = RequestsBrowserProvider()
    if not cfg.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    llm = AnthropicLLMProvider(cfg.anthropic_api_key, cfg.model)
    return search, browser, llm


def cmd_discover(args):
    cfg = load_config()
    search, browser, llm = _build_providers(cfg)
    results = pipeline.run(args.query, search, browser, llm,
                            cfg.db_path, cfg.log_path, max_candidates=args.max,
                            entity_type=args.entity_type, industry=args.industry)
    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True, parents=True)
    for r in results.briefs:
        brief = r["brief"]
        fname = out_dir / f"{brief.id}_{brief.business_name[:40].replace('/', '_')}.md"
        fname.write_text(r["markdown"], encoding="utf-8")
        print(f"Wrote {fname}")
    print(f"\n{results.discovered} candidates discovered")
    print(f"{results.completed} completed")
    print(f"{len(results.failures)} failed")
    for failure in results.failures:
        print(f"  FAILED {failure['candidate_name']}: {failure['error']}")
    print(f"{results.completed} brief(s) generated. Stored in {cfg.db_path}, logged to {cfg.log_path}.")


def cmd_list(args):
    cfg = load_config(require_llm=False)
    for b in store.list_briefs(cfg.db_path):
        print(f"[{b['status']:>10}] {b['id']}  {b['business_name']}  ({len(b['opportunities'])} findings)")


def cmd_watch(args):
    cfg = load_config(require_llm=False)
    store.set_watch(cfg.db_path, args.brief_id, watch=not args.unwatch)
    print(f"{'Unwatched' if args.unwatch else 'Now watching'}: {args.brief_id}")


def cmd_entities(args):
    cfg = load_config(require_llm=False)
    for entity in store.list_entities(cfg.db_path, entity_type=args.entity_type):
        print(f"[{entity.entity_type:>18}] {entity.id}  {entity.canonical_name}  {entity.industry or 'unknown'}  {entity.primary_domain or '-'}")


def cmd_industry_report(args):
    cfg = load_config(require_llm=False)
    _print_report(intelligence.industry_report(cfg.db_path, args.industry))


def cmd_opportunities(args):
    cfg = load_config(require_llm=False)
    _print_report(intelligence.rank_opportunities(cfg.db_path))


def cmd_competitors(args):
    cfg = load_config(require_llm=False)
    _print_report(intelligence.competitor_report(cfg.db_path))


def cmd_market_summary(args):
    cfg = load_config(require_llm=False)
    _print_report(intelligence.market_summary(cfg.db_path))


def main():
    parser = argparse.ArgumentParser(prog="scout")
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Run the full pipeline for a query")
    p_discover.add_argument("query")
    p_discover.add_argument("--max", type=int, default=8)
    p_discover.add_argument("--out", default="./briefs")
    p_discover.add_argument("--entity-type", choices=("prospect", "competitor", "industry_reference"), default="prospect")
    p_discover.add_argument("--industry", default="")
    p_discover.set_defaults(func=cmd_discover)

    p_list = sub.add_parser("list", help="List saved briefs")
    p_list.set_defaults(func=cmd_list)

    p_watch = sub.add_parser("watch", help="Mark a brief for periodic re-checking")
    p_watch.add_argument("brief_id")
    p_watch.add_argument("--unwatch", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_entities = sub.add_parser("entities", help="List canonical tracked entities")
    p_entities.add_argument("--type", dest="entity_type", choices=("prospect", "competitor", "industry_reference"))
    p_entities.set_defaults(func=cmd_entities)

    p_industry = sub.add_parser("industry-report", help="Summarize Scout's stored industry sample")
    p_industry.add_argument("industry")
    p_industry.set_defaults(func=cmd_industry_report)

    p_opportunities = sub.add_parser("opportunities", help="Rank stored opportunities deterministically")
    p_opportunities.set_defaults(func=cmd_opportunities)

    p_competitors = sub.add_parser("competitors", help="Show stored competitor entities")
    p_competitors.set_defaults(func=cmd_competitors)

    p_market = sub.add_parser("market-summary", help="Summarize Scout's stored sample")
    p_market.set_defaults(func=cmd_market_summary)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
