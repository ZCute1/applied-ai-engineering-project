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

### Limitations and biases

**Biases baked into the system:**

- **Corpus bias.** The knowledge base is nine notes I wrote myself. It covers dogs
  and cats, skews toward a Western pet-owning norm (leashed walks, indoor cats,
  scheduled feeding), and has one breed note — Shiba Inu — so a Shiba gets
  breed-specific advice while a Beagle gets generic dog advice and never learns why.
- **Schedule bias.** `DAY_START_MIN`/`DAY_END_MIN` hard-code a 06:00–22:00 day, so
  the system silently assumes a daytime worker. A night-shift owner's day doesn't
  exist in this model.
- **Retrieval bias toward my vocabulary.** Scoring is keyword-based over notes and
  `tags` I wrote, so a query phrased the way I'd phrase it retrieves well and one
  phrased differently doesn't. The people this fails hardest are the ones whose
  words for pet care aren't mine.
- **Placement bias from a two-value field.** `activity_level` is high/low, and
  "high energy → morning" is my assumption, not a retrieved fact.

**Technical limitations:**

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

### Could this be misused, and how I'd prevent it

**The real risk is that it gets read as veterinary advice.** It produces confident,
specific numbers about a living animal's exercise, feeding, and medication. Someone
could reasonably follow "45-minute walk, twice daily" for a dog with a heart
condition, or use the medication note to time a dose. That's the misuse that could
actually hurt something.

*What's already in place:* every retrieved claim is cited to a filename the owner
can open and read; `knowledge/README.md` and the app's corpus expander both say
these are general-interest notes, not veterinary guidance; the notes on medication,
puppies/kittens, and heat carry that warning inline where it matters most.

*What I'd add:* a persistent disclaimer in the answer area rather than only in an
expander someone has to open, and a refusal path for dosage and symptom questions
that routes to "ask a vet" instead of retrieving. Right now the prompt doesn't
distinguish "how long should I walk my dog" from "how much ibuprofen for my dog" —
the second should never be answered, and currently nothing stops it. That's the
first thing I'd fix.

**Other misuse paths and mitigations:**

| Misuse | Current mitigation | Gap |
|---|---|---|
| Prompt injection via a `knowledge/` note | Corpus is local, in-repo, reviewed in PRs | Retrieved text isn't sandboxed from instructions |
| Burning someone's API quota | `MAX_MODEL_TURNS = 15`, enforced in code | No per-session request cap |
| Passing AI output off as expert guidance | Filenames cited, corpus readable | No disclaimer in the answer itself |
| Poisoned corpus additions | `tests/test_knowledge.py` rejects malformed notes | Validates *format*, never *accuracy* |

### What surprised me while testing reliability

**The retrieval failure was invisible, not loud.** I expected bad retrieval to
return nothing. Instead `"feed my pets"` returned a confident answer built on a
stray sentence about cats picking fights with other pets in the house — because the
note says "feeding", not "feed", and the fighting sentence happened to contain
"pets". Nothing errored. The model wrote a fluent routine on top of it. If I hadn't
asserted on the *exact top-ranked chunk*, that would have shipped, and a test that
only checked "something came back" would have passed. That single case produced both
the stemmer and the `MIN_SCORE = 4` floor.

**The second surprise: input that looks valid.** `"07:99"` passed my bounds check
because it parses to 519 minutes, comfortably inside the day. And `"8:00"` — a
perfectly reasonable thing for a model to emit — would have silently corrupted
`sort_by_time()`, which sorts `"HH:MM"` as plain strings, so `"8:00"` sorts *after*
`"10:00"`. Neither raises. Both produce a wrong schedule that looks right. The
lesson I keep coming back to: with an LLM writing your inputs, the dangerous case
isn't malformed data, it's plausible data.

**The third: the model agrees with itself.** Early on I let it judge its own plan
instead of calling `review_plan()`. It called a double-booked afternoon fine. That
result is why the check step reads `detect_conflicts()` and `schedule.unplaced`
rather than asking the model how it did.

## Human oversight

Every run's tool calls and results are shown in the app's step log, the retrieval
corpus is readable in an expander, and the schedule table plus unplaced/conflict
warnings come from the scheduler rather than the model. The owner can override any
time by hand. The model never writes a timetable directly — `Schedule.build()`
makes every placement decision.
