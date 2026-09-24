# Scout — Alcatrax Research Agent (v2 / Phase 1 of Super Agent)

**v2 additions over v1:** Alcatrax knowledge brain (`alcatrax_knowledge.py`),
source-specific opportunity evidence chains with a guard rail against fabricated
capabilities/references (`analyze.py`), per-fact confidence tiers replacing
one whole-profile flag (`verify.py`, `models.SourceObservation`/`FactCheck`), Search/Browser/LLM
provider interfaces so no module imports `anthropic`/`requests` directly
anymore (`providers/`), and an Action Registry stub enforcing
read-only/external/destructive tiers even though nothing calls it yet
(`action_registry.py`). Evidence and pipeline tests are in `tests/`.


Scout is the first agent in the DennisOS architecture. It is deliberately
**not** wired into a Commander/orchestrator yet — see "Why not multi-agent
yet" below.

## Evidence model

Each fetched URL produces its own `SourceObservation`: URL, fetch metadata,
bounded readable excerpt, and source-specific extracted facts. Each direct
fact keeps its source URL, extraction time, and evidence excerpt. Scout does
not retain full HTML documents.

`VERIFIED` means two **distinct URLs** independently extracted the same
directly supported substantive fact. Multiple URLs put into one aggregate LLM
extraction never qualify. `OBSERVED` means one source directly supports a
fact; `INFERRED` is an analysis without a validated direct excerpt; `UNKNOWN`
means insufficient evidence. Conflicts remain visible and are never marked
verified. Source observations are persisted to SQLite for later dossier audit.

## Pipeline

```
discover -> browse -> understand -> verify -> analyze -> remember -> recommend
```

Every step is read-only / automatic tier: Scout searches, fetches public
pages, extracts structured facts, cross-checks them, and writes a brief.
It never contacts a business, never writes to any external system, and
never acts on anything without you. This is intentional for v1.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
export GOOGLE_CSE_API_KEY=...        # from console.cloud.google.com
export GOOGLE_CSE_CX=...             # from programmablesearchengine.google.com
```

## Run it

```bash
python -m scout.cli discover "automotive spare parts shop Nairobi" --max 8
python -m scout.cli list
python -m scout.cli watch <brief_id>     # mark for periodic re-checking later
```

Output: one Markdown brief per business in `./briefs/`, plus everything
persisted to `scout.db` (SQLite) and every action logged to
`scout_log.jsonl`.

## What each file does

| File | Step | Notes |
|---|---|---|
| `discover.py` | Discover | Google Custom Search JSON API. Swap for another backend by writing a function that returns `list[Candidate]`. |
| `browse.py` | Browse | `requests` + BeautifulSoup. Will miss JS-rendered content — swap for Playwright when you hit that wall; `PageSnapshot` contract doesn't change. |
| `extract.py` | Understand | Extracts each source separately and requires evidence excerpts for direct claims. |
| `verify.py` | Verify | Requires two independently extracted URLs before a fact is verified. |
| `analyze.py` | Analyze | Deterministic checks (viewport tag, meta description, WhatsApp link, cart keywords) run first — zero hallucination risk — then one LLM pass for judgment calls the heuristics can't make. Capped at 8 findings. |
| `brief.py` | Recommend | Assembles everything into a `ProspectBrief`, renders Markdown. |
| `store.py` | Remember | Plain SQLite, including durable bounded source observations/evidence. |
| `logging_utils.py` | (all steps) | One JSON line per action, in `scout_log.jsonl`. |
| `pipeline.py` | orchestration | Sequential, not parallel. One bad site/candidate never kills the run. |

## Known gaps to close next (in rough priority order)

1. **Source coverage is still limited** — the default CLI fetches one search
   result URL per business. A second source needs a real per-business source
   selection design before it can improve verification.
2. **JS-heavy sites** — swap `browse.fetch` for a Playwright-based version once
   you notice snapshots coming back near-empty for known-real businesses.
3. **Rate limiting / politeness** — add delays between fetches before running
   this against a long candidate list; right now it's unthrottled.
4. **Real discover coverage of Kenyan SMEs** — Google CSE indexes what Google
   indexes. Consider supplementing with a Google Maps Places API discover
   backend (structured business listings, not just indexed pages) once this
   proves useful — that's a second `discover_*` function, same `Candidate`
   output contract.

## Why this isn't wired into Commander/orchestrator-worker yet

Anthropic's own guidance on agent architecture: find the simplest solution,
add complexity only when needed. Multi-agent orchestration earns its (real —
roughly 15x the token cost of a single call) overhead specifically when a
task decomposes into independent, parallelizable subtasks. Scout's own
workflow is a sequential pipeline, not a fan-out — so it's built as one
well-instrumented agent, not an orchestrator dispatching to itself.

Commander becomes worth building once there's a second working agent (SEO,
say) and a real task that needs both at once — e.g. "find 5 clients and show
what we'd build" genuinely decomposes into Scout-finds-them +
Developer-assesses-feasibility running in parallel per candidate, which is
exactly the shape orchestrator-worker is good at. Building the router before
there's anything to route to just adds an abstraction layer with nothing
behind it.

## What's tested vs. what's still a stub

**Tested with Python standard-library unittest and fake providers (no real API calls needed):**
- Source-specific verification: observed, verified, aggregate-extraction,
  conflict, and unknown cases
- SQLite source-observation persistence and source-attributed opportunities
- Full fake-provider pipeline producing a persisted brief
- Guard rail: a fabricated capability id / fake reference project from the
  LLM gets stripped to empty string, never trusted into the brief

Run them with:

```bash
python3 -m unittest discover -s tests -v
```

**Stub / not yet wired in:**
- `providers/ollama_llm.py` exists and implements the interface but
  `pipeline.py`/`cli.py` still default to Anthropic only — swapping requires
  passing an `OllamaLLMProvider` instance instead in `_build_providers()`
- `action_registry.py` — nothing calls `.propose()` yet since Scout has no
  write/external actions in this phase; it's here so Developer/Sales don't
  each invent their own permission check later
- `alcatrax_knowledge.py`'s `PAST_PROJECTS` list only has the 5 projects
  publicly listed on the Alcatrax site — add Karani's EC & BV Motors, Weru
  Interiors, and the rest with their real problem/capability mapping as you
  have that internally; the analyze.py guard rail only allows references to
  what's actually in this list, so an empty entry here means Scout correctly
  says nothing rather than inventing a comparison

## Still open from the Phase 1/2/3 plan (not in this drop)

- Google Business Profile as a real second verification source (Phase 2)
- Snapshot history/change detector for Watch mode (Phase 2). The current
  `watch` command only marks a stored brief; it does not re-check it.
- Research modes (Prospect/Company/Competitor/Industry/Opportunity/Watch) as
  distinct CLI entry points over the same tool set — right now there's one
  `discover` command
- Social presence (Facebook/Instagram/TikTok/LinkedIn) — Phase 3, and per
  the earlier discussion, LinkedIn specifically should probably stay
  manual-check-only given their scraping ToS enforcement history

## Permission model implemented here

Matches DennisOS: read-only actions (search, fetch, extract) = automatic,
which is everything Scout does in v1. There is no write/external/destructive
path in this codebase at all yet — that's deliberate, not an oversight. When
Developer/Sales agents exist and Scout needs to hand a brief to them, that
handoff is itself an "external action" and should go through an explicit
approval step, not get bolted on silently here.
