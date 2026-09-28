"""CLI entry point.

Usage:
  export ANTHROPIC_API_KEY=sk-ant-...
  export GOOGLE_CSE_API_KEY=...
  export GOOGLE_CSE_CX=...

  python -m scout.cli discover "automotive spare parts shop Nairobi" --max 8
  python -m scout.cli list
  python -m scout.cli watch <brief_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .config import load_config
from .providers.google_cse import GoogleCSEProvider
from .providers.requests_browser import RequestsBrowserProvider
from . import intelligence, pipeline, store
from .mission import ScoutMissionEngine


def _print_report(report):
    print(json.dumps(report, default=lambda value: asdict(value) if is_dataclass(value) else str(value), indent=2))


def _bounded_int(label: str, minimum: int, maximum: int):
    """Argparse converter that rejects unsafe mission values before creation."""
    def convert(value: str) -> int:
        try:
            number = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"{label} must be an integer") from error
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"{label} must be between {minimum} and {maximum}")
        return number
    return convert


def _build_providers(cfg):
    if not (cfg.google_cse_api_key and cfg.google_cse_cx):
        raise RuntimeError("GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX not set.")
    search = GoogleCSEProvider(cfg.google_cse_api_key, cfg.google_cse_cx)
    browser = RequestsBrowserProvider()
    if cfg.llm_provider == "ollama":
        from .providers.ollama_llm import OllamaLLMProvider
        llm = OllamaLLMProvider(cfg.ollama_model, cfg.ollama_base_url)
    elif cfg.llm_provider == "anthropic":
        if not cfg.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        try:
            from .providers.anthropic_llm import AnthropicLLMProvider
        except ModuleNotFoundError as error:
            if error.name == "anthropic":
                raise RuntimeError(
                    "Anthropic SDK is not installed. Install the declared dependency before selecting "
                    "SCOUT_LLM_PROVIDER=anthropic."
                ) from error
            raise
        llm = AnthropicLLMProvider(cfg.anthropic_api_key, cfg.anthropic_model)
    else:  # Config validates this; keep the boundary explicit.
        raise RuntimeError(f"Unsupported LLM provider: {cfg.llm_provider}")
    return search, browser, llm


def cmd_discover(args):
    cfg = load_config(require_llm=True)
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


def doctor_report(cfg) -> list[str]:
    """Diagnostic only; never prints credentials or performs a search."""
    lines = ["Python: OK", f"Google CSE API key: {'configured' if cfg.google_cse_api_key else 'missing'}",
             f"Google CSE CX: {'configured' if cfg.google_cse_cx else 'missing'}",
             f"Selected LLM provider: {cfg.llm_provider}"]
    if cfg.llm_provider == "ollama":
        lines.append(f"Ollama endpoint: {cfg.ollama_base_url}")
        try:
            with urlopen(f"{cfg.ollama_base_url.rstrip('/')}/api/tags", timeout=3) as response:
                models = {model.get("name") for model in json.loads(response.read().decode("utf-8")).get("models", [])}
            lines.append("Ollama endpoint status: reachable")
            lines.append(f"Ollama model {cfg.ollama_model}: {'available' if cfg.ollama_model in models else 'missing'}")
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            lines.append("Ollama endpoint status: unreachable")
            lines.append(f"Ollama model {cfg.ollama_model}: unknown")
        lines.append("Anthropic: not required")
    else:
        import importlib.util
        lines.append(f"Anthropic API key: {'configured' if cfg.anthropic_api_key else 'missing'}")
        lines.append(f"Anthropic SDK: {'available' if importlib.util.find_spec('anthropic') else 'missing'}")
    return lines


def cmd_doctor(args):
    for line in doctor_report(load_config(require_llm=False)):
        print(line)


def _mission_engine(args, live: bool = False):
    cfg = load_config(require_llm=False)
    if not live:
        return ScoutMissionEngine(cfg.db_path)
    if not (cfg.google_cse_api_key and cfg.google_cse_cx):
        raise RuntimeError("GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX not set.")
    # The deterministic Mission Engine does not call an LLM; do not require or
    # initialize one just to perform bounded public-web research.
    return ScoutMissionEngine(cfg.db_path, GoogleCSEProvider(cfg.google_cse_api_key, cfg.google_cse_cx), RequestsBrowserProvider())


def cmd_mission_start(args):
    engine = _mission_engine(args, live=False)
    mission = engine.create(args.objective, args.location, args.industry,
                            max_entities=args.max_entities, max_searches=args.max_searches,
                            max_pages_per_entity=args.max_pages_per_entity, max_total_pages=args.max_total_pages,
                            worker_count=args.worker_count, max_retries=args.max_retries,
                            freshness_seconds=args.freshness_seconds, time_budget_seconds=args.time_budget_seconds)
    print(mission.id)
    if args.run:
        _mission_engine(args, live=True).run(mission.id)


def cmd_mission_resume(args):
    _mission_engine(args, live=True).run(args.mission_id)


def cmd_mission_pause(args):
    mission = _mission_engine(args).pause(args.mission_id)
    print(f"{mission.id}: {mission.status}")


def cmd_mission_status(args):
    mission = store.get_mission(load_config(require_llm=False).db_path, args.mission_id)
    if not mission:
        raise SystemExit(f"Unknown mission: {args.mission_id}")
    _print_report(mission)


def cmd_mission_report(args):
    cfg = load_config(require_llm=False)
    report = store.get_mission_report(cfg.db_path, args.mission_id) or ScoutMissionEngine(cfg.db_path).build_report(args.mission_id)
    print(report)


def cmd_missions(args):
    for mission in store.list_missions(load_config(require_llm=False).db_path):
        print(f"[{mission.status:>9}] {mission.id}  {mission.objective}")


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

    p_doctor = sub.add_parser("doctor", help="Report local provider readiness without exposing secrets")
    p_doctor.set_defaults(func=cmd_doctor)

    p_missions = sub.add_parser("missions", help="List persisted autonomous research missions")
    p_missions.set_defaults(func=cmd_missions)
    p_mission = sub.add_parser("mission", help="Create, run, inspect, or report a research mission")
    mission_sub = p_mission.add_subparsers(dest="mission_command", required=True)
    p_start = mission_sub.add_parser("start", help="Create a bounded public-web mission; --run requires Google CSE and respects robots.txt")
    p_start.add_argument("--objective", required=True); p_start.add_argument("--location", default="")
    p_start.add_argument("--industry", action="append", choices=("automotive", "hospitality", "health_fitness", "education_professional", "retail_local", "construction_services"))
    p_start.add_argument("--max-entities", type=_bounded_int("max entities", 1, 100), default=50)
    p_start.add_argument("--max-searches", type=_bounded_int("max searches", 1, 50), default=20)
    p_start.add_argument("--max-pages-per-entity", type=_bounded_int("max pages per entity", 1, 10), default=3)
    p_start.add_argument("--max-total-pages", type=_bounded_int("max total pages", 1, 500), default=100)
    p_start.add_argument("--worker-count", type=_bounded_int("worker count", 1, 8), default=4, help="Concurrent tasks, bounded to 1–8 (default: 4)")
    p_start.add_argument("--max-retries", type=_bounded_int("max retries", 0, 5), default=2)
    p_start.add_argument("--freshness-seconds", type=_bounded_int("freshness seconds", 0, 31536000), default=60 * 60 * 24 * 30)
    p_start.add_argument("--time-budget-seconds", type=_bounded_int("time budget seconds", 1, 86400), help="Stop after this many seconds")
    p_start.add_argument("--run", action="store_true"); p_start.set_defaults(func=cmd_mission_start)
    p_resume = mission_sub.add_parser("resume", help="Resume a configured public-research mission")
    p_resume.add_argument("mission_id"); p_resume.set_defaults(func=cmd_mission_resume)
    p_pause = mission_sub.add_parser("pause", help="Pause a mission")
    p_pause.add_argument("mission_id"); p_pause.set_defaults(func=cmd_mission_pause)
    p_status = mission_sub.add_parser("status", help="Show persisted mission state")
    p_status.add_argument("mission_id"); p_status.set_defaults(func=cmd_mission_status)
    p_report = mission_sub.add_parser("report", help="Print persisted mission report")
    p_report.add_argument("mission_id"); p_report.set_defaults(func=cmd_mission_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
