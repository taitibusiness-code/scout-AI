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

## Alcatrax Knowledge Brain

The curated catalogue in `scout/alcatrax_knowledge.py` separates four things:

- **Capability** — a service Alcatrax can offer, with a proof state:
  `proven` (recorded client evidence), `demonstrated` (credible internal or
  public demonstration), or `developing` (catalogued direction without a
  recorded client deployment).
- **Solution pattern** — a reusable solution shape, such as an automotive
  digital catalogue. It names the business-problem tags, capabilities, and
  projects that actually support that pattern.
- **Past project** — a factual reference record. It is proof only for its
  explicitly recorded capabilities and solution patterns, not a generic
  marketing recommendation.
- **Opportunity** — a source-attributed observation and consequence that may
  match a pattern, capability, and relevant project.

The matching chain is:

```text
Evidence → problem tags → solution pattern → capability → relevant project
```

Application code validates every relationship. A known capability plus a
known project is still rejected as a reference when that project does not
demonstrate the capability or support the selected pattern. Scout may retain
a valid pattern/capability without a project reference when no honest proof
record exists.

## Canonical entities and stored-sample intelligence

A **Candidate** is one discovery occurrence. An **Entity** is the canonical
business or organization that candidates can share. Scout resolves an entity
conservatively: normalized domain first (`www.example.com/about` and
`example.com` match), then exact normalized name plus exact non-empty
location. It never uses LLM or fuzzy matching for automatic merges; uncertain
matches remain separate.

Entities have one of three tracking types: `prospect`, `competitor`, or
`industry_reference`. This is classification only—the same evidence-first
research pipeline is used for every type.

The deterministic query layer exposes stored-sample reports:

```bash
python -m scout.cli entities [--type prospect]
python -m scout.cli industry-report automotive_spare_parts
python -m scout.cli opportunities
python -m scout.cli competitors
python -m scout.cli market-summary
```

Reports state their scope explicitly: they describe only entities Scout has
researched and persisted, never an entire industry or market. Opportunity
ranking is deterministic and explainable: confidence, severity, direct source
excerpt, capability maturity, solution-pattern match, and relevant project
proof are fixed components in `intelligence.PRIORITY_WEIGHTS`.

## Pipeline

```
discover -> browse -> understand -> verify -> analyze -> remember -> recommend
```

Every step is read-only / automatic tier: Scout searches, fetches public
pages, extracts structured facts, cross-checks them, and writes a brief.
It never contacts a business, never writes to any external system, and
never acts on anything without you. This is intentional for v1.

## Provider configuration

Scout uses a pluggable web-search provider. **Exa is the default**; Google CSE
remains supported but is no longer required. The search adapter returns the
same provider-neutral result shape regardless of source.

- **Exa** — default general web search provider.
- **Tavily** — alternate web-search provider for an explicitly configured
  fallback chain.
- **Serper** — Google-style result provider; treat its one-time free
  allocation as a reserve fallback, not a normal daily provider.
- **Google CSE** — retained for existing setups that select `google_cse`.
- **Google Places API (New)** — optional, disabled by default, for official
  Google Maps business-listing discovery. Enable billing in Google Cloud and
  enable **Places API (New)** before setting `PLACES_PROVIDER_ENABLED=true` and
  `GOOGLE_MAPS_API_KEY`. Scout never scrapes or automates Google Maps.

Places discovery uses only Text Search (New) with Nairobi bias and a strict
field mask: place ID, display name, address/location, permitted phone, website,
business status, and Maps URI. It never requests photos, ratings, reviews, AI
summaries, or other nonessential fields; ratings/reviews are never sales-fit
signals. Places data is discovery context only: Maps URLs are never browsed and
a listing without a first-party website is not persisted as a candidate. Scout
persists only the permitted place ID and source provenance for a Places-derived
candidate; phone/address/listing fields remain transient until independently
observed on a permitted first-party public page. Google Places content is not
used to make a Strong lead decision.

This follows Google's [Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies): place IDs may be retained (Google recommends refreshing them after 12 months), while Scout chooses not to retain Places display-name, address, phone, website, Maps URI, business-status, or latitude/longitude data. Latitude/longitude is therefore not cached even though the service terms allow a limited cache period. If a future UI displays Google-derived content, it must add the required Google attribution; Scout's CLI does not display that content.

Free allowances, quotas, and pricing change. Check each provider dashboard
before relying on any plan or using it in a recurring workflow.

Local LLM mode remains:

```text
Google CSE → RequestsBrowserProvider → Ollama qwen3:8b
```

The application reads shell environment variables. `.env.example` is a
placeholder-only reference; Scout does not load `.env` files itself.

Local mode:

```bash
export SCOUT_LLM_PROVIDER=ollama
export SCOUT_OLLAMA_MODEL=qwen3:8b
export SCOUT_OLLAMA_BASE_URL=http://127.0.0.1:11434
export EXA_API_KEY=replace_me
export SEARCH_PROVIDER=exa
```

One provider can be selected explicitly:

```bash
export SEARCH_PROVIDER=tavily
export TAVILY_API_KEY=replace_me
```

Or configure an ordered fallback chain. Scout advances only after timeout,
connection failure, HTTP 429, or HTTP 5xx; credentials, malformed requests,
and invalid responses stop clearly rather than silently consuming another
provider's quota.

```bash
export SEARCH_PROVIDERS=exa,tavily,serper
export EXA_API_KEY=replace_me
export TAVILY_API_KEY=replace_me
export SERPER_API_KEY=replace_me
```

Existing Google CSE setup remains available:

```bash
export SEARCH_PROVIDER=google_cse
export GOOGLE_CSE_API_KEY=replace_me
export GOOGLE_CSE_CX=replace_me
```

Optional cloud mode:

```bash
export SCOUT_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=replace_me
export EXA_API_KEY=replace_me
export SEARCH_PROVIDER=exa
```

`discover` requires the selected search provider's credentials and the
selected LLM. Mission `--run` requires only the selected search provider.
Stored-data commands and `python -m scout.cli --help` require neither. Run
`python -m scout.cli doctor` for a secret-safe local readiness report.

## Run it

```bash
python -m scout.cli discover "automotive spare parts shop Nairobi" --max 8
python -m scout.cli list
python -m scout.cli watch <brief_id>     # mark for periodic re-checking later
```

For missions, web remains the default. To opt in to official Places discovery:

```bash
export PLACES_PROVIDER_ENABLED=true
export GOOGLE_MAPS_API_KEY=replace_me
python -m scout.cli mission start --objective "Find local hardware shops" --location Nairobi \
  --industry retail_local --business-type "hardware shops" --discovery-sources web,places --run
```

## Mission Engine (Phase 2.4)

The Mission Engine is the bounded option for limited public-web reconnaissance.
It plans search and page tasks locally, persists each task and evidence version
to SQLite, and produces an evidence-backed report. It does not contact a
business or perform any external write.

```bash
python -m scout.cli mission start --objective "Find local hardware shops" --location Nairobi \
  --industry retail_local --max-searches 2 --max-entities 5 --max-total-pages 10 \
  --worker-count 2 --time-budget-seconds 120 --run
```

`--run` requires Google CSE credentials, but the deterministic Mission Engine
does not require an LLM. The older LLM-backed `discover` command requires the
selected Ollama or Anthropic provider; mission planning and reporting do not.
Worker count is strictly bounded to 1–8;
searches, entities, pages, retries, freshness, and elapsed time are all
bounded and shown in the mission report.

Before each live page request Scout permits only public HTTP(S) destinations,
checks redirects under the same policy, identifies itself with a clear user
agent, and reads/caches `robots.txt`. If robots policy is unavailable or
disallows a path, Scout skips it and records the policy skip. It does not
bypass robots rules. RequestsBrowserProvider is not a JavaScript browser and
its DNS pre-check cannot fully eliminate DNS-rebinding risk.

Evidence visits are versioned: a revisit retains the previous observation;
current assessments use the newest visit per URL. The report's **Public
Business Readiness** / **Digital Presence Signal** is only a public-web
sales-prioritisation signal. It is not proof of revenue, creditworthiness, or
financial health.

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
| `analyze.py` | Analyze | Validates evidence excerpts and the problem → pattern → capability → project graph; deterministic automotive catalogue matching is intentionally narrow. |
| `brief.py` | Recommend | Assembles everything into a `ProspectBrief`, renders Markdown. |
| `store.py` | Remember | SQLite evidence plus canonical entities and candidate-to-entity links. |
| `intelligence.py` | Aggregate | Deterministic stored-sample industry, competitor, market, and opportunity views. |
| `logging_utils.py` | (all steps) | One JSON line per action, in `scout_log.jsonl`. |
| `pipeline.py` | orchestration | Sequential, not parallel. One bad site/candidate never kills the run. |

## Known gaps to close next (in rough priority order)

1. **Source coverage is still limited** — the default CLI fetches one search
   result URL per business. A second source needs a real per-business source
   selection design before it can improve verification.
2. **JS-heavy sites** — swap `browse.fetch` for a Playwright-based version once
   you notice snapshots coming back near-empty for known-real businesses.
3. **Robots coverage is intentionally conservative** — a robots endpoint that
   cannot be retrieved causes a skip. This protects sites but can reduce
   coverage, especially for small or unreliable sites.
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
- Knowledge-graph guards for invented IDs and invalid pattern/capability/project relationships

Run them with:

```bash
python3 -m unittest discover -s tests -v
```

**Stub / not yet wired in:**
- `providers/ollama_llm.py` is the default local LLM path. It relies on a
  JSON-only prompt and has no validate-and-retry loop, so local structured
  extraction quality should be assessed before relying on it at scale.
- `action_registry.py` — nothing calls `.propose()` yet since Scout has no
  write/external actions in this phase; it's here so Developer/Sales don't
  each invent their own permission check later
- Karani's EC & BV Motors and Weru Interiors are recorded by name, but their
  industry, delivery, capabilities, and pattern proof remain deliberately
  unknown until factual project information is supplied.

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
