# Model Card — PawPal+ AI Planner

## What the system is

PawPal+ is a pet care planning assistant. The AI layer (`pawpal_agent.py`) turns a
plain-English request into care tasks, runs the deterministic scheduler in
`pawpal_system.py`, checks its own plan for conflicts and tasks that didn't fit,
and repairs it before answering. Care advice is retrieved from the notes in
`knowledge/` rather than recalled from the model's memory.

- **Model:** `gemini-3.6-flash` (override with `PAWPAL_MODEL`)
- **Interface:** Gemini Interactions API, hand-rolled tool loop, `store=False`
- **Tools:** 8 (see `TOOL_DECLARATIONS`) — all thin wrappers over scheduler methods
- **Retrieval:** deterministic weighted keyword scoring over 9 notes / 36 chunks
- **Caps:** 3 repair rounds (prompt), 15 model turns (enforced in code)

## Intended use

A single owner planning one day of pet care. It is a course project.

**Not for:** veterinary decisions, medication dosing, multi-day or multi-user
planning, or anything where a wrong schedule causes harm. The knowledge base is
general-interest care writing, not clinical guidance.

## Responsible-AI reflection

### How I collaborated with AI

I used AI as a reviewer rather than an author for design work. I brought my own
UML and scheduling rules, then asked it to hunt for grey areas and cases I hadn't
accounted for — which is where things like the buffer between tasks and the
"unplaced" concept came from. I kept separate chat sessions per phase (design,
implementation, tests, AI features) so each session's working memory stayed on
one problem; that was more effective than one long thread.

For the AI features I wrote the tool functions myself as plain functions over an
`Owner` and used AI mainly to pressure-test the loop's failure modes: what happens
on a hallucinated tool name, a bad argument set, a request that can never be
satisfied. Those questions produced the guardrails, not the happy path.

### One helpful AI suggestion

Reviewing the retrieval code, it pointed out that scoring on raw words meant the
query "when should I feed my pets" would never match a note that says "feeding".
That was real — the top hit was an unrelated sentence about cats picking fights.
Two fixes came out of it: a `_stem()` helper both sides go through, and a relevance
floor (`MIN_SCORE = 4`) so a single passing mention in prose isn't a hit. It's now
a test (`tests/test_knowledge.py`), and it's the bug I'd have shipped.

### One flawed AI suggestion

For daily recurrence it suggested rolling the calendar to a new day every time a
recurring task was completed. I pushed back: that breaks an owner planning ahead,
because completing tomorrow's task would silently advance today. It agreed and we
landed on separating the two concerns — `Task.mark_complete()` spawns the next
occurrence, and `Owner.start_new_day()` is the only thing that moves the date, with
`clear_plan()` wiping generated blocks while keeping the owner's commitments. The
suggestion was confidently wrong and would have produced a subtly broken app, and
the only reason it got caught is that I knew the intended behavior and it didn't.

The broader lesson: it's fluent enough that agreement feels like verification. My
habit now is to ask for pros and cons of each option and for the logical loopholes
in its own suggestion, then decide myself.

### Limitations

- **Retrieval matches words, not meaning.** A query using entirely different
  vocabulary from the notes will score lower than it deserves. The hand-written
  `tags` field is the workaround; embeddings would be the real fix, at the cost of
  a dependency and non-deterministic tests.
- **The corpus is small and mine.** Nine notes, written for this project. Anything
  outside them either returns nothing or gets answered generically.
- **Model judgment is untested.** The tests cover the tools, the retrieval ranking,
  and the scheduler — 117 of them, all offline. Whether the model picks the
  *sensible* task to move is verified by hand, because it needs a live API and is
  non-deterministic.
- **Grounding is instructed, not enforced.** The prompt requires care claims to
  come from `lookup_care_guidance` with filenames cited, but nothing in code
  blocks an ungrounded number. The step log exists so a human can catch it.
- **Single day, single owner, in memory.** Nothing persists past the Streamlit
  session, and there's no week view.
- **Free-tier limits.** Rate limits or a missing key surface as an in-app error
  message, not a traceback — but the AI section simply won't work without a key.

## Human oversight

Every run's tool calls and results are shown in the app's step log, the retrieval
corpus is readable in an expander, and the schedule table plus unplaced/conflict
warnings come from the scheduler rather than the model. The owner can override any
time by hand. The model never writes a timetable directly — `Schedule.build()`
makes every placement decision.
