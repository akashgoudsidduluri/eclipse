# ECLIPSE

**The browser agent that sees for you — and eclipses what no one else should see.**

Privacy-first browser agent for SIH 2026 PS 26171, "On-device Visual
Perception for Light-weight Browser Agents" (ISRO). The same build is the
Theme-Based Project (TBP): SIH is the core prototype, TBP adds connectors,
live monitoring, more flows, and the academic deliverables.

Fresh build, fresh repo. Not PrivAgent.

## What it does

Eclipse finishes real browser tasks on real sites, with privacy first:

1. **Job hunt** — share a resume once, Eclipse searches Internshala,
   LinkedIn and Unstop, shortlists with match reasons, applies after your
   approval, and reports a receipt per role.
2. **Cold mail** — drafts one short personal mail per recruiter, sends
   only after your approval.
3. **Secure checkout and login** — card and login filled locally from the
   vault. Full card, CVV and OTP never leave the device, never reach the
   planner, never enter logs.
4. **Scanned documents** — reads PNG scans with on-device vision only.
   DOM scores zero by design.
5. **Live monitoring** — watches result portals and the inbox, alerts on
   change, morning briefing.

Privacy pipeline, every step, no exceptions: observe locally → detect PII
→ redact (destructive blackout, never blur) → fail-closed GATE → second
PII scan → plan (cloud, redacted only) → guarded execute → verify by
re-observing → checkpoint + log.

## Setup (10 steps or fewer)

1. `git clone https://github.com/akashgoudsidduluri/eclipse.git && cd eclipse`
   (Setup works after the first push lands — until then, work from the
   local checkout.)
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000`
5. Read the token: `cat data/.eclipse-token` (file is 0600, never logged)
6. Check health: `curl -H "X-Eclipse-Token: <token>" http://127.0.0.1:8000/health`
7. Run the benchmark harness: `python3 evals/sih-benchmark/run.py --runs 1`
8. Score it: `python3 evals/sih-benchmark/score.py --latest`
9. Run the tests: `.venv/bin/python -m unittest discover -s tests -v`
10. Load the extension unpacked in Chrome (`extension/`, `npm run build` first).
11. Open the workspace: `http://127.0.0.1:8000/workspace` (paste the token from step 5) or the proof page: `http://127.0.0.1:8000/proof`.

## Model providers — the brain (.env)

The chat brain and the planner call models through a local-first
chain (PR #7): **Ollama → Antseed → Groq**. The first provider
that answers wins; a provider that fails (Ollama not running, a
dead gateway) is skipped at once. Every chat reply is labelled
with the provider that answered (`provider` in the
POST /chat/message response, `null` when the rules answered) —
and GET /brain shows which providers are configured, in chain
order, and whether each one answers a cheap probe. A missing
model is visible there, not silent.

Keys live in a `.env` file at the repo root (copy
`.env.example`, fill in your own keys) or in the real
environment — never in the repo (`.env` is gitignored). The
server loads `.env` at startup, before any server module reads
the environment. Real environment variables win over `.env`
values.

| Variable | Default | What it does |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Local Ollama (no key needed — must be running on this machine) |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Local model |
| `ANTSEED_API_KEY` | — (vault surrogate when unset) | Antseed gateway key |
| `ANTSEED_BASE_URL` | `https://akash-antseed.duckdns.org/v1` | Antseed gateway. **This default is Akash's gateway** — if you run your own gateway, point this at it, e.g. `ANTSEED_BASE_URL=https://prateek-antseed.duckdns.org/v1` |
| `ANTSEED_MODEL` | `google-gemma-4-31b-it` | Antseed model |
| `GROQ_API_KEY` | — (Groq skipped when unset) | Groq key |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq endpoint |
| `GROQ_MODEL` | `gpt-oss-120b` | Groq model |
| `ECLIPSE_PROVIDER_ORDER` | `ollama,antseed,groq` | Chain order override, e.g. `ECLIPSE_PROVIDER_ORDER=groq,ollama,antseed`. Providers not listed keep their default relative order |

Without any key and without Ollama running, the chat still
works — the rules answer greetings, name statements and clear
instructions, and anything else gets an honest "I couldn't
reach a model" reply instead of a repeated question.

## Architecture — 3 tiers, local first

- **Tier 1, Extension** (Chrome MV3, `extension/`): perceive + redact +
  execute. Offscreen document runs on-device inference (DOM walker, YOLO,
  OCR, BlazeFace, Florence-2 lazy, WebGPU only). Redactor, vault and gate
  run here. IndexedDB holds model weights only.
- **Tier 2, Local server** (`server/`, FastAPI, localhost:8000, bind
  127.0.0.1 only, token auth): orchestrate + guard + vault + memory +
  RAG. One SQLite file (`data/eclipse.db`, WAL). The server is the ONLY
  SQLite writer. Vault file separate, Fernet-encrypted, never in SQLite.
- **Tier 3, Cloud planner** (redacted input only, open-weights only):
  local-first chain (PR #7) — Ollama local qwen2.5:7b, then the own
  Antseed gateway (verified free models, gemma-4-31b live planner),
  then Groq gpt-oss-120b, then OrcaRouter free when fully
  configured. Never silently fall back from local mode to cloud.

## Status — honest grades

| Sprint | Scope | Grade |
|---|---|---|
| 0 | Start blocks: model probe, connector check, repo, resume, laptop spec, WebGPU | done, verified live 2026-11-21 |
| 1 | Fixtures + harness + server skeleton | done — harness 60/60, unittests 12/12, metrics null (no loop yet) |
| 2 | Vision + redaction + gate | done — Node 22/22, Python 20/20, harness 60/60; OCR 6/6 lines, BlazeFace 2+1 faces (ground truth); Florence-2 implemented but not live-verified (no WebGPU in sandbox) |
| 3 | Planner + executor + verify | done — Python 66/66, Node 39/39, harness 60/60; review BLOCK fixed (server PII scan, verify baseline diff, handoff resume, default-submit guard); planner is implemented and unit-tested with a mock planner but NOT live-verified against the real gateway (needs ANTSEED_API_KEY in the server env) |
| 4 | Job-hunt flow end to end | done — Python 99/99, Node 48/48, harness 60/60; review pass BLOCK (backend 2 blockers + 11 minors, frontend 1 blocker + 6 minors) all fixed + regression tests; matcher reproduces the fixture ground truth (5/12); approval-before-submit + already-applied gate + receipts recorded by the loop; fingerprint now live in the execute path |
| 5 | Vault + cold mail + checkout | done — Python 137/137, Node 52/52, harness 60/60; Fernet vault file (PBKDF2, 0600, create ≠ unlock, 10-min key idle lock), per-task grants (15-min expiry, 10-min idle lock, origin-scoped, card wiped on expiry), fill map in the execute response (approval BEFORE vault fill, placeholder↔element binding, [CARD_LAST4] fills the full number); cold-mail drafts from the real shortlist + approval (one approval sends one draft) + partial unique-index dedup + send log; checkout flow on the fixture (card fill → approve → verify); extension WebCrypto vault module (AES-256-GCM, unit-tested, not yet wired into the fill path); review pass: backend BLOCK + frontend BLOCK, all findings fixed + regression tests |
| 6 | RAG + benchmark + proof views | done — Python 159/159, Node 52/52, harness 60/60; sqlite-vec RAG (all-MiniLM-L6-v2, 384-dim, cosine ≥0.6, PII text refused, forget works); benchmark run 2026-11-21: grounding 100% (mock planner — loop, not a live model), PII recall 100% / precision 100% (regex types), redaction 0 recoverable tokens, extension models 26.7 MB (budget 300), latency p50 0.001s (mock planner — loop overhead only); proof views live at /proof (metrics, what-was-sent, egress audit, redaction proof, latency waterfall) + web/proof.html; scan.py overlap fix (card vs Aadhaar) + ground-truth fix (hidden-fields current email); latency gate passes — no §6.10 fixes needed (live planner latency still unmeasured, honest expectation 4–8s/step) |
| 7 | Connectors (Gmail → Calendar → Drive) | done — Python 196/196, Node 52/52, harness 60/60; Gmail/Calendar/Drive via the platform CLI (OAuth tokens live in the platform connector store, never in Eclipse — see server/connectors/__init__.py); real cold-mail delivery through Gmail with the resume PDF attached (opt-in per send), rate limits (10/task, 1 per 5s), bounce check via inbox DSN search (a bounce blocks that address); Calendar free/busy + event create behind a single-use approval grant + events recorded in SQLite (idempotent per task/summary/start); Drive find-resume + upload behind a single-use approval grant, path must be inside home; per-connector on/off in SQLite + live status/reconnect URL; demoed live 2026-11-21: Gmail 20 keyword-matching mails found (bodies redacted — the keyword match is broad, includes newsletters), Calendar free 2026-11-22 09:00–10:00 IST, Drive resume Akash_Goud_Sidduluri_Resume_new.pdf found; a real mail SEND has not been performed (needs a user-approved draft + the platform send card); review pass BLOCK (6 blockers incl. wrong-resume attach risk) — all fixed + 14 new tests |
| 8 | Live monitoring | done — Python 235/235, Node 62/62, harness 60/60; watches (URL + selector + condition changed/appeared/disappeared/contains + interval ≥15 min) in SQLite; check evaluation with baseline (no alert on first sighting), 1-hour cooldown that only suppresses the same change (a new value fires), 3 misses → broken with one alert, broken recovers on a good read, mute expires (due again), repair/re-pick endpoint; chrome.alarms in the extension (one alarm per active or muted watch + daily 08:00 briefing alarm, reschedule message + token-appears retry), exact-URL tab matching, per-watch error isolation; morning briefing 5–10 cards (watch firings deduped per watch + one Gmail scan, inbox failure shown on a card, never sends mail); demoed live 2026-11-21: contains watch fired on a new value inside the cooldown, muted check refused, repair endpoint restarted a broken watch; review pass BLOCK (6 blockers + 18 minors + 9 nits) — all fixed + 15 new tests |
| 9 | Demo + TBP deliverables | done — 3-pane workspace (Vite + React 19 + TS, served at /workspace, token in tab only) with Task/Plan, Proof (SIH metrics + what-was-sent + Diff + redaction proof + HUD), Trace/Audit (hash chain + Verify), watches + briefing, and the lazy Privacy Boundary 3D scene (static fallback without WebGL); execute/verify are NOT wired in the workspace (execution runs in the extension; see web/README.md); TBP report (docs/tbp-report.md + .html, all §12 sections + appendices, real numbers only); Review-I/II decks (docs/deck-review-1/2.html); demo video script (docs/demo-video-script.md); runbooks (sqlite-vec, ONNX + Transformers.js), demo rehearsal checklist, test report; landing page (web/landing.html, static). NOT screenshot-reviewed in a real browser — the cloud browser cannot reach this VM's localhost and outbound tunnels are blocked by the sandbox network policy; the Playwright browser install (needs approval) is the path to it. Honest deviation: no Tailwind/shadcn — the design tokens + hand-rolled CSS follow the same §5.1 system; the workspace is the Vite build, not the Antideploy landing (landing is the static file). Review pass BLOCK (6 blockers incl. workspace crash on first load from wrong API types) — all fixed + 2 new regression tests. Screenshot-reviewed in a real browser 2026-11-21 ~17:1x IST (Playwright Chromium headless on this VM): all 4 panes render, metrics real, plan timeline shows real descriptions, 3D scene renders via WebGL; screenshots in docs/screenshots/. 60/30/10 fix 2026-11-21 ~17:1x IST (panes now --surface, measured 71% surface / 15% bg / <1% accent — the literal 60/30 split is not reachable in a full-bleed 3-pane layout; the two colours now read distinctly, before it was 90% one dark family). TBP report PDF generated 2026-11-21 ~17:4x IST (21 pages, local only — snapshot push is text-only). Gate re-run 2026-11-21 ~17:4x IST: Python 237/237, Node 62/62, harness 60/60, builds clean. |

Grades: implemented-and-verified / implemented-but-not-live-verified /
partial / mocked / not implemented. No number on any slide until
`score.py` produces it from a real benchmark run.

## SIH metrics — measured 2026-11-21 (benchmark run)

From `evals/sih-benchmark/benchmark.py` on the 12 fixtures
(`results/benchmark-20261121-084924.json`, `score.py --latest`):

- Visual grounding, first try: **100%** (18/18 executable steps; 2 human-only handoffs — OTP + pay — counted separately). Target ≥85%. **Mock planner** — this measures the loop, not a live model.
- PII recall / precision: **100% / 100%** (regex-detectable types only). Target ≥95% / ≥90%. Names, addresses, CVV, OTP and passwords are caught by label/field, not regex — listed separately in the benchmark detail.
- Redaction: **0 recoverable tokens** after redaction. Target 0.
- Resource use: extension models **26.7 MB** (budget 300 MB). Target ≤500 MB RAM / ≤300 MB download.
- Latency p50: **0.001s/step** (mock planner — loop overhead only). Target ≤6s/step. A live gateway run is still needed for real step latency (honest expectation 4–8s/step on free cloud models).

Targets (for reference): visual accuracy ≥85%, PII recall ≥95% / precision ≥90%, redaction 0 tokens, RAM ≤500MB / models ≤300MB, latency ≤6s/step, demo task ≤90s.

## Rules

- No code until Start Blocks are green. Build in sprint order, do not
  reorder. After each task: test gate, then review-and-fix, then next.
- Never say done until verified. Never fabricate numbers.
- CAPTCHA, 3-D Secure, passkey and second-device OTP are human-only.
  Never build a CAPTCHA solver.
- Approvals per action: sends, purchases, pushes and deletes each need
  their own go-ahead. (Standing rule change 2026-11-21 12:39 IST: the
  user superseded this for the build — push and build without asking,
  complete the whole project. The per-action rule still describes the
  product's approval behaviour for sends/purchases, not the build.)
- No secrets in chat, files, logs, screenshots, or the repo.

## Docs

- Full plan: `~/workspace/privagent-rebuild-research/11-ECLIPSE-FINALISED-PLAN-SIH-TBP.md`
  (also the source of truth while the repo docs grow).
- `docs/` holds the sqlite-vec runbook, the ONNX + Transformers.js
  runbook, and the demo rehearsal checklist (as sprints land).
