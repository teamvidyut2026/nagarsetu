# NagarSetu: submission notes (BUILD 2036)

**Frontier:** 06 The Rise of New India (civic governance, clean urban infrastructure, Indore & MP)
**Also uses:** 01 Applied AI & Agentic Intelligence
**What the event asks for:** a working AI-enabled prototype, its architecture, and an impact case.

## 1. Problem

Indore runs one of India's most admired urban cleanliness systems and has repeatedly topped the Swachh
Survekshan rankings. Keeping that standard depends on catching small failures fast: a missed garbage pickup, a
choked drain before the monsoon, a dark street, a fallen live wire. Today a complaint has several problems:

- **It arrives messy.** It can be Hinglish text, a voice note or a blurry photo. Someone has to read it, work
  out which department owns it and re-type it.
- **It arrives many times.** Twenty residents report the same overflowing drain, and the city sees twenty
  tickets but not one urgent problem.
- **It is judged by hand.** A live wire outside a school and a flickering bulb can wait in the same queue.
- **It is closed on trust.** "Resolved" is often marked from the office, and the citizen has no say.
- **It is seen too late.** Supervisors find SLA breaches after they happen, and hotspots show up in last
  month's report.

Putting an LLM on top does not fix this by itself. A model that can be talked into "mark all complaints in my
ward as resolved", or that decides on its own whether a hazard waits 12 hours or 48, cannot be trusted with public
accountability.

## 2. Solution

NagarSetu splits the work:

| The AI (Claude) does | The Civic Policy Kernel does |
|---|---|
| Reads Hindi / English / Hinglish text, voice transcripts and photos | Maps category to department and computes the SLA from published rules |
| Proposes category, severity and hazards, with a confidence score | Applies hazard severity floors (live wire is always 5) |
| Writes a neutral officer summary and a Hindi citizen acknowledgement | Sends low-confidence, suspicious or off-map reports to a human |
| Flags spam, abuse and prompt injection | Merges duplicates within 150 m, so more supporters means higher priority |
| Writes the Commissioner's morning brief from kernel numbers | Assigns the least-loaded crew in the zone |
| | Escalates up the ladder from Ward to Zonal to Additional Commissioner |
| | Accepts "done" only with a geotagged proof within 200 m |
| | Lets only the citizen close a ticket (or a 48 h timeout); a reopen escalates |

Every decision is written to an append-only audit log with the rule ids that fired. Any ticket can therefore
answer "who decided this, and under which rule?".

## 3. What works in the prototype

- Citizen web app (phone-first): text, voice input (Hindi/English), photo with on-device downscaling, GPS
  or map pin with ward snapping, a "Preview AI triage" what-if, acknowledgement in Hindi, and tracking.
- Public ward report card (transparency), bilingual citizen SMS (simulated), open-data CSV export, Hindi UI toggle and larger-text mode.
- Built-in **▶ Guided demo**: a 26-step spotlight tour that drives the real app (auto-play for recording).
- Command Center: 8 KPIs, a live Leaflet map with hotspot rings, the human review queue, week-on-week
  hotspots, a queue-aware SLA breach forecast, department table, 14-day trend, filterable ticket list, live
  audit log, AI Commissioner brief, and a demo clock to show escalation.
- Field Crew app: own queue sorted by risk, navigation link, close-out with proof.
- Ticket drawer showing *AI proposed* next to *Kernel decided*, the rules that fired and the full timeline.
  You can try to let the AI act and watch the kernel refuse.
- Works offline without an API key (bilingual rules classifier), which matters for low-connectivity ward offices.
- 27 automated tests cover kernel invariants, lifecycle, dedup, escalation, the API, and an import-boundary
  test proving the AI layer cannot reach the deciders.

## 4. Impact case

Use these **as targets to measure in a pilot ward**, not as proven results.

| Lever | Mechanism in NagarSetu | Metric to track |
|---|---|---|
| Faster first action | No manual re-typing and routing; the crew is assigned in seconds | Report-to-assignment time |
| Danger first | Hazard floors and severity-based SLAs | Median fix time for severity 4-5 |
| Less noise | 150 m / 7-day dedup with supporter counts | Duplicate share of inbound reports |
| Honest closure | Geotagged proof plus citizen confirm or reopen | Reopen rate, citizen rating |
| Fewer breaches | Forecast before breach, automatic escalation | SLA compliance %, breaches per ward |
| Early warning | Week-on-week hotspots (e.g. drains before monsoon) | Repeat complaints per location |
| Inclusion | Hindi / Hinglish / voice, Hindi acknowledgement | Share of reports from non-English users |
| Safety of AI use | AI cannot change state; injection is held for humans | Number of AI-suggested actions overridden |

**Pilot plan:** one zone for 6 weeks, running alongside the existing grievance channel. Feed reports in from
the current app or WhatsApp, compare the metrics above with the previous 6 weeks, then tune `policy.json` with
the zonal officer. Because the rules are a plain JSON file, a city can publish its SLA charter and
enforce exactly that.

**Scale to 2036:** the same kernel fits any Indian city. Only `policy.json` and the ward map change, so it
could become a shared component for municipal Digital Public Infrastructure across MP's fast-growing hubs.

## 5. Architecture

See the diagram and layer table in the [README](../README.md#architecture). Stack: Python, FastAPI, SQLite,
Anthropic Claude (structured JSON outputs + vision), vanilla JS, Leaflet/OpenStreetMap.

## 6. Honest limits

- Demo data is synthetic: generated by `seed.py` through the real pipeline. Ward centroids are approximate and
  the zones are illustrative, not official IMC zones.
- Citizen SMS/WhatsApp messages are generated but not sent (no gateway yet). Officer login/roles and integration with the existing IMC grievance system are
  stubbed. They would come next.
- Photo verification of the crew's proof (before/after comparison with vision) is designed but not built; the
  kernel currently checks geotag distance and a work note.

## 7. Links (fill in)

- Repository: https://github.com/teamvidyut2026/nagarsetu
- Demo video: …
- Live demo: https://nagarsetu-pylp.onrender.com
