# PawPal+ (Module 2 Project)

You are building **PawPal+**, a Streamlit app that helps a pet owner plan care tasks for their pet.

## Scenario

A busy pet owner needs help staying consistent with pet care. They want an assistant that can:

- Track pet care tasks (walks, feeding, meds, enrichment, grooming, etc.)
- Consider constraints (time available, priority, owner preferences)
- Produce a daily plan and explain why it chose that plan

Your job is to design the system first (UML), then implement the logic in Python, then connect it to the Streamlit UI.

## What you will build

Your final app should:

- Let a user enter basic owner + pet info
- Let a user add/edit tasks (duration + priority at minimum)
- Generate a daily schedule/plan based on constraints and priorities
- Display the plan clearly (and ideally explain the reasoning)
- Include tests for the most important scheduling behaviors

## Getting started

### Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

The AI planner (see **AI Features** below) runs on the Gemini API. The
`google-genai` line in `requirements.txt` installs the SDK; the key itself is a
secret read from the environment and is never committed:

```bash
export GEMINI_API_KEY=your-key   # Windows: setx GEMINI_API_KEY your-key
```

Get a key free at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
— the free tier covers a project of this size, with per-minute and per-day
request caps you can check at
[aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit).

The default model is `gemini-3.6-flash`. `PAWPAL_MODEL` overrides it without a
code change, which is how you'd compare models:

```bash
PAWPAL_MODEL=gemini-3.5-flash streamlit run app.py
```

Everything else — the scheduler, the terminal demo, the test suite, and the whole
Streamlit UI apart from the "Ask PawPal" box — runs without a key.

### Suggested workflow

1. Read the scenario carefully and identify requirements and edge cases.
2. Draft a UML diagram (classes, attributes, methods, relationships).
3. Convert UML into Python class stubs (no logic yet).
4. Implement scheduling logic in small increments.
5. Add tests to verify key behaviors.
6. Connect your logic to the Streamlit UI in `app.py`.
7. Refine UML so it matches what you actually built.

## 🖥️ Sample Output

Running the terminal demo (`python main.py`) creates an owner with two pets, adds
several tasks and the owner's commitments, then builds and prints the day's plan:

```
Heads up: 'Lunch w/ Sam' overlaps 'Work'.

===== Today's Schedule =====
Plan for 2026-07-06:
  06:00-06:15  Feeding for Luna (high priority)
  06:15-07:00  Morning walk for Mochi (high priority)
  07:00-07:30  Training session for Mochi (medium priority)
  07:30-07:50  Playtime for Luna (low priority)
  09:00-17:00  Work (commitment)
  12:00-13:00  Lunch w/ Sam (commitment)
```

The scheduler places pet tasks highest-priority-first into the free time around
the owner's fixed commitments, labels each task with the pet it belongs to, and
warns when a new commitment overlaps an existing one.

## ✨ Features

The scheduling logic lives in `pawpal_system.py`. Each feature below maps to a
concrete algorithm (see **Smarter Scheduling** further down for method-level
detail).

**Planning & placement**
- **Priority-first placement** — tasks are placed high-priority-first, with
  shorter tasks breaking ties, into the free time around fixed commitments.
- **Preferred-time placement** — a task with a preferred `"HH:MM"` lands as
  close to it as a free window allows; if that time is blocked, it's bumped to
  the nearest opening (and the plan flags it with `[wanted HH:MM]`).
- **Activity-based soft targets** — a task with no preferred time is nudged by
  its pet's energy: high-energy → morning, low-energy → afternoon.
- **Free-window detection** — computes the open gaps between commitments within
  the 06:00–22:00 day, merging overlapping/nested commitments.
- **Buffers between tasks** — keeps a 10-minute gap between placed tasks for
  travel/transition time.
- **Unplaced tracking** — anything that can't fit is collected and reported
  rather than silently dropped.

**Organizing tasks**
- **Sorting by time** — returns tasks in chronological order (untimed tasks
  first).
- **Filtering** — by pet name (case-insensitive) and/or completion status.
- **Conflict warnings** — flags pairs of tasks requested for overlapping times,
  labeled *same pet* vs *different pets*.

**Recurrence & day management**
- **Daily / weekly recurrence** — completing a recurring task automatically
  spawns its next occurrence (+1 day / +7 days) on the same pet.
- **Day rollover** — advances to the next day, keeps commitments, and rolls any
  missed recurring task forward so it never piles up into a backlog.
- **Commitment management** — add / remove / move fixed commitments, with an
  overlap warning when a new one clashes.
- **Plan explanation** — a human-readable summary of why each task was placed
  when, plus what didn't fit.

## AI Features

PawPal+ implements two of the four AI features, as one system:

| Feature | Where | What it does |
|---|---|---|
| **Agentic workflow** | `pawpal_agent.py` | Plans, acts, and checks its own work — builds a schedule, finds its own conflicts, repairs them |
| **Retrieval-Augmented Generation** | `pawpal_knowledge.py` + `knowledge/` | Looks up care guidance *before* suggesting a routine, and cites what it used |

They're combined rather than bolted on side by side: retrieval is a tool inside
the agent's loop, so the same run that grounds its advice in the knowledge base
also verifies the schedule it built from that advice.

## AI Feature 1 — Agentic Workflow

`pawpal_agent.py` adds an AI planner that **plans, acts, and checks its own
work**. You describe the day in plain English; the model turns that into tasks and
commitments, runs the real scheduler, inspects the result for problems, and
repairs the plan before answering.

The key design decision: **the scheduler stays the source of truth.** The model
never writes a timetable. It decides *what* to schedule and *how to repair* a
plan the scheduler reports as broken — every actual placement is still made by
`Schedule.build()`. That keeps the deterministic logic (and its 38 tests)
authoritative and confines the AI to the judgment calls.

### The loop

| Phase | What happens | Tools used |
|---|---|---|
| **Plan** | Read the request and the day as it actually is | `show_current_state` |
| **Retrieve** | Look up what this pet actually needs (see AI Feature 2) | `lookup_care_guidance` |
| **Act** | Add the tasks, then schedule them | `add_care_task`, `add_commitment`, `build_plan` |
| **Check** | Ask the scheduler whether the plan works | `review_plan` |
| **Fix** | Move or shorten what didn't work, then rebuild | `retime_task`, `resize_task`, `build_plan` |

Check and Fix repeat until the plan is clean or three repair rounds are spent.

### Why this project suits an agentic workflow

The check step is the hard part of any self-checking agent, and PawPal+ already
had it. `Schedule.detect_conflicts()` returns real conflict warnings and
`schedule.unplaced` lists what didn't fit — so success is measured by the domain
model, not by the model's opinion of its own work. `review_plan` reports both,
plus the open gaps still left in the day, giving the agent somewhere concrete to
move a task to.

### The tools

Each tool is a thin wrapper over a method the scheduler already had:

| Tool | Wraps | Notes |
|---|---|---|
| `show_current_state` | `Owner.pets`, `Schedule.blocks` | Pets, tasks, commitments — called first, so the agent can't invent a pet |
| `lookup_care_guidance` | `pawpal_knowledge.search()` | The retrieval step — the only place its care advice may come from |
| `add_care_task` | `Pet.add_task(Task(...))` | Validates pet, priority, recurrence, duration, and `"HH:MM"` format |
| `add_commitment` | `Owner.add_commitment` | Passes the scheduler's overlap warning back to the model |
| `retime_task` | `Task.scheduled_time` | The main repair tool; `""` hands the choice back to the scheduler |
| `resize_task` | `Task.duration_minutes` | The other repair: shorten a task to fit a smaller gap |
| `build_plan` | `Owner.build_day()` | Returns `Schedule.explain()` |
| `review_plan` | `detect_conflicts()` + `unplaced` | The check step, plus the day's remaining open gaps |

Tools return error *strings* rather than raising: a tool that explains what went
wrong (`"Error: no pet named 'Luna'. The owner's pets are: Mochi."`) lets the
model correct itself on the next turn, where an exception would just end the run.

### Using it

In the app, the **🤖 Ask PawPal** section takes a request like:

> *Mochi needs a 45-minute walk every morning and Luna needs feeding at 07:30.
> I'm at work 09:00–17:00.*

The agent's tools mutate the same `Owner` the rest of the page is built from, so
the pets, tasks, and schedule tables all update with whatever it did. The
expander shows the numbered step log — build, review, repair, rebuild — so the
loop is visible rather than a black box.

### Guardrails

- **Repair cap** — three build/review/fix rounds, so a request that can never be
  satisfied (20 hours of tasks in a 16-hour day) reports the problem instead of
  looping forever.
- **Turn cap** — `MAX_MODEL_TURNS = 15`, enforced in the loop rather than the
  prompt. The repair cap is an instruction the model *can* ignore; this one it
  can't, which matters on a free tier with a daily request quota.
- **No tool can crash the run** — `_dispatch` turns an unknown tool name or a
  wrong argument set into an error string the model reads and corrects, rather
  than an exception that ends the loop.
- **No silent deletion** — the agent may move or shorten a task, never drop one.
  Anything that truly can't fit is reported to the owner.
- **No invented pets** — it can only schedule for pets `show_current_state`
  lists.
- **Isolated dependency** — `pawpal_system.py` doesn't import `pawpal_agent.py`,
  and the SDK is imported lazily inside `plan_day()`, so the domain model and
  every existing test run with no AI dependency at all.

### How it talks to Gemini

`plan_day()` hand-rolls the loop against the Gemini Interactions API rather than
using an SDK auto-execute helper — send the history, run whatever functions come
back, append the results, send again:

```python
interaction = client.interactions.create(
    model=model, store=False, system_instruction=..., input=history,
    tools=TOOL_DECLARATIONS,
)
calls = [s for s in interaction.steps if s.type == "function_call"]
if not calls:
    break                       # the model is answering, so we're done
for call in calls:
    result = _dispatch(owner, call.name, call.arguments)
    history.append({"type": "function_result", "name": call.name,
                    "call_id": call.id, "result": [{"type": "text", "text": result}]})
```

`store=False` keeps the conversation on our side — nothing is retained
server-side between turns, so the history we resend is exactly what we can
inspect. Parallel function calls are handled: every call in a turn is executed
and answered before the next request.

Tool schemas are hand-written JSON Schema in `TOOL_DECLARATIONS`, since the API
takes plain declarations. That risks drifting from the Python functions, so
`test_declarations_match_their_implementations` compares every schema against
`inspect.signature` — rename an argument and the suite fails instead of the
model silently calling a parameter that no longer exists.

**On swapping providers:** this project started on Claude and moved to Gemini.
Only the bottom third of `pawpal_agent.py` changed — the tool implementations,
the scheduler, the knowledge base, and 81 of the 117 tests never knew a provider
existed. Keeping the tools as plain functions over an `Owner`, rather than
building them around one SDK's decorators, is what made that a contained edit.

## AI Feature 2 — Retrieval-Augmented Generation

Ask *"set up a routine for Mochi"* and a language model will happily produce
confident numbers — 45 minutes here, twice a day there — with no source behind
them. `pawpal_knowledge.py` makes the agent **retrieve before it answers**: it
searches the notes in `knowledge/` for the pet's species, breed, and activity
level, and builds the task list from what it finds, citing the filenames.

### The pipeline

1. **Load** — every `.md` note in `knowledge/`, parsing the `---` metadata block
   (`title`, `species`, `activity_level`, `tags`) at the top.
2. **Chunk** — each note splits into one chunk per `##` heading, so a query about
   grooming returns the grooming section rather than a whole file. Nine notes
   currently produce **36 chunks**.
3. **Score** — weighted keyword matching. Heading, title, and tag hits count 4×;
   body hits 1×. A chunk whose species matches the pet gets +6, matching activity
   level +3, and a general note (feeding, medication — no species) +1.
4. **Retrieve** — top 3 by default, above a relevance floor, each labelled with
   its source file.

Then generation: the model reads those chunks and turns them into tasks.

### Why keyword scoring, not embeddings

No embedding model, no vector store, no network call, no extra dependency. For
nine short files that's the honest engineering choice — and because scoring is
deterministic, `tests/test_knowledge.py` can assert on **exact rankings** rather
than "something came back".

The tradeoff is real: this matches *words*, not meaning. That bit immediately —
"when should I **feed** my pets" found nothing, because the note says
"**feeding**", and the top hit was a stray sentence about cats "picking fights
with other pets in the house". Two fixes came out of it:

- **A stemmer** (`_stem`) so query and note vocabulary meet: `feeding → feed`,
  `walking/walks → walk`, `grooming → groom`, `scheduled/schedule → schedul`.
  Precision suffers slightly (`morning → morn`), but both sides go through the
  same function, so the match still lands.
- **A relevance floor** (`MIN_SCORE = 4`) — one tag or heading hit clears it, a
  single passing mention in prose does not.

A query with entirely different vocabulary would still score lower than it
deserves; the hand-written `tags` list in each note exists to widen that
vocabulary. Embeddings would fix it properly, at the cost of a dependency and
non-deterministic tests.

### Grounding guardrails

- **Cite or don't claim** — the system prompt requires any specific claim about
  how long, how often, or what a breed needs to come from
  `lookup_care_guidance`, with the source filenames in the answer.
- **A miss says so** — when nothing matches, the tool returns *"No care notes
  matched that query… say so rather than filling the gap with a guess"*, so an
  empty retrieval produces an admission instead of invention.
- **Wrong-species notes are excluded** — a cat note answering a dog question is
  worse than no answer, so a species mismatch drops below the floor.
- **Metadata alone is never a hit** — a species match with no word overlap won't
  drag a note in, or every dog question would return every dog note.
- **Capped context** — 5 chunks maximum per lookup, 900 characters each.
- **Inspectable corpus** — the app lists all nine notes in an expander, so you can
  read the source the advice came from.

### Adding to the knowledge base

Drop a `.md` file in `knowledge/` with the metadata block; the loader finds it
automatically. `tests/test_knowledge.py` verifies every note parses and carries
`title`, `heading`, `tags`, and a recognized `species`/`activity_level`, so a
malformed note fails the suite instead of quietly dropping out of retrieval.

These notes are general-interest care writing for a course project — **not
veterinary advice**, as `knowledge/README.md` says.

## 🧪 Testing PawPal+

Run the full test suite from the project root:

```bash
python -m pytest
```

Add `-v` for the per-test breakdown shown below.

### What the tests cover

**Scheduler** — `tests/test_pawpal.py` (38 tests) exercises the scheduling logic
in `pawpal_system.py` across seven areas:

- **Happy paths** — tasks get placed, never overlap commitments, high-priority
  tasks win a contested slot, preferred times are honored (and bumped when
  blocked), and a pet's activity level biases placement (high → morning,
  low → afternoon).
- **Recurrence** — completing a daily task spawns the next day's task, weekly
  spawns +7 days, one-off tasks spawn nothing, and the new task is anchored to
  the original's due date.
- **Task retrieval** — `pending_tasks` excludes completed and future-dated
  tasks; `filter_tasks` combines pet-name (case-insensitive) and completion
  filters; `sort_by_time` returns tasks in chronological order.
- **Conflict detection** — same-time tasks are flagged with the correct
  same-pet / different-pet label, non-overlapping and untimed tasks don't clash,
  and both conflicting tasks still get scheduled.
- **Free windows** — gaps are computed correctly around commitments, including
  overlapping/nested ones and a fully booked day.
- **Edge cases** — no pets / no tasks, a task too big to fit (goes to
  `unplaced`), exact-fit windows, the 10-minute buffer between tasks, and input
  normalization.
- **Day rollover** — future-dated recurrences stay hidden until their day
  arrives, stale tasks roll forward without duplicating, and commitments survive
  the rollover.

**AI planner** — `tests/test_agent.py` (36 tests) covers the deterministic half
of the agent: the tools the model calls, and the declarations describing them,
in `pawpal_agent.py`.

- **Adding** — `add_care_task` / `add_commitment` reach the domain model with the
  right values, pet names match case-insensitively, and the scheduler's overlap
  warning is passed back to the model.
- **Repairs** — `retime_task` and `resize_task` change what the next build reads,
  including the full repair path end to end: conflict → retime → rebuild → clean
  review.
- **Review** — the check step reports conflicts, names unplaced tasks with what
  they need, offers open gaps that account for placed tasks (not just
  commitments), and ignores completed tasks.
- **Bad input** — every tool returns an explanatory error string instead of
  raising, and changes nothing: unknown pets, invalid priorities and
  recurrences, malformed times, zero durations, backwards commitments. Dispatch
  also survives a hallucinated tool name and wrong argument names.
- **Declarations** — the hand-written JSON schemas stay in step with the Python
  functions they describe: every declared tool has a handler, every parameter is
  described and typed, and `test_declarations_match_their_implementations`
  cross-checks each schema against `inspect.signature`.

**Retrieval** — `tests/test_knowledge.py` (43 tests) covers the RAG pipeline in
`pawpal_knowledge.py`.

- **Loading** — notes chunk by `##` heading, metadata lands on every chunk, blank
  fields mean "any pet", the directory's README isn't indexed, a missing corpus
  returns empty rather than raising.
- **Real corpus** — every note in `knowledge/` parses, has a title, heading, tags
  and body, and uses a recognized `species`/`activity_level`. Eight
  parametrized end-to-end queries assert the *right note* wins.
- **Stemming** — the pairs that have to collide (`feed`/`feeding`,
  `walk`/`walking`/`walks`, `schedule`/`scheduled`), doubled-consonant handling,
  short words left alone, and stopwords filtered *after* stemming so `needs`
  doesn't sneak through as `need`.
- **Ranking** — tags outrank body text, the species boost picks the right animal,
  wrong-species notes are excluded, general notes still surface for a specific
  pet, `max_results` is respected, ranking is deterministic, and metadata alone
  is never a hit.
- **No match** — nonsense and empty queries return nothing, and the no-match
  message tells the model not to guess.
- **Formatting** — output carries citable filenames and real body text, and long
  sections are truncated.

Both suites run in the normal test run with **no API key and no network** — the
SDK is imported lazily inside `plan_day()`, and retrieval only reads local files.
Three tests caught real bugs while being written: `"07:99"` passed a naive
total-minutes bounds check (it parses to 519), un-padded `"8:00"` would have
broken `sort_by_time`'s plain-string sort, and `"feed my pets"` retrieved a
sentence about cats fighting instead of the feeding note.

What these tests deliberately don't cover is the model's judgment — whether
the model picks the *sensible* task to move. That needs a live API and would be
non-deterministic, so it's verified by hand rather than asserted in CI.

Tests are deterministic (dates pinned relative to `today`) and import the
module's config constants, so they stay valid if those values change.

### Successful test run

```
$ python -m pytest -v
============================= test session starts ==============================
platform darwin -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
cachedir: .pytest_cache
rootdir: .../ai110-module2show-pawpal-starter
plugins: anyio-4.14.0
collected 38 items

tests/test_pawpal.py::TestHappyPaths::test_basic_build_places_everything PASSED [  2%]
tests/test_pawpal.py::TestHappyPaths::test_placed_tasks_never_overlap_a_commitment PASSED [  5%]
tests/test_pawpal.py::TestHappyPaths::test_priority_wins_the_contested_slot PASSED [  7%]
tests/test_pawpal.py::TestHappyPaths::test_scheduled_time_is_honored_when_free PASSED [ 10%]
tests/test_pawpal.py::TestHappyPaths::test_scheduled_time_bumped_when_blocked PASSED [ 13%]
tests/test_pawpal.py::TestHappyPaths::test_high_energy_pet_gets_a_morning_slot PASSED [ 15%]
tests/test_pawpal.py::TestHappyPaths::test_low_energy_pet_gets_an_afternoon_slot PASSED [ 18%]
tests/test_pawpal.py::TestRecurrence::test_daily_task_spawns_next_day PASSED [ 21%]
tests/test_pawpal.py::TestRecurrence::test_weekly_task_spawns_seven_days_later PASSED [ 23%]
tests/test_pawpal.py::TestRecurrence::test_one_off_task_spawns_nothing PASSED [ 26%]
tests/test_pawpal.py::TestRecurrence::test_next_occurrence_anchored_to_due_date_not_today PASSED [ 28%]
tests/test_pawpal.py::TestTaskRetrieval::test_pending_excludes_completed PASSED [ 31%]
tests/test_pawpal.py::TestTaskRetrieval::test_pending_excludes_future_dated PASSED [ 34%]
tests/test_pawpal.py::TestTaskRetrieval::test_pending_includes_no_due_date_and_today PASSED [ 36%]
tests/test_pawpal.py::TestTaskRetrieval::test_completed_task_not_placed_in_plan PASSED [ 39%]
tests/test_pawpal.py::TestTaskRetrieval::test_filter_by_pet_name_is_case_insensitive PASSED [ 42%]
tests/test_pawpal.py::TestTaskRetrieval::test_filter_combines_pet_and_completion PASSED [ 44%]
tests/test_pawpal.py::TestTaskRetrieval::test_sort_by_time_orders_chronologically PASSED [ 47%]
tests/test_pawpal.py::TestConflicts::test_same_time_tasks_flagged PASSED [ 50%]
tests/test_pawpal.py::TestConflicts::test_different_pets_labeled PASSED  [ 52%]
tests/test_pawpal.py::TestConflicts::test_non_overlapping_times_no_conflict PASSED [ 55%]
tests/test_pawpal.py::TestConflicts::test_tasks_without_a_time_never_conflict PASSED [ 57%]
tests/test_pawpal.py::TestConflicts::test_both_conflicting_tasks_still_get_placed PASSED [ 60%]
tests/test_pawpal.py::TestFreeWindows::test_empty_schedule_is_one_full_window PASSED [ 63%]
tests/test_pawpal.py::TestFreeWindows::test_commitment_splits_the_day PASSED [ 65%]
tests/test_pawpal.py::TestFreeWindows::test_overlapping_commitments_merge PASSED [ 68%]
tests/test_pawpal.py::TestFreeWindows::test_fully_booked_day_has_no_windows PASSED [ 71%]
tests/test_pawpal.py::TestEdgeCases::test_owner_with_no_pets_builds_empty PASSED [ 73%]
tests/test_pawpal.py::TestEdgeCases::test_pet_with_no_tasks_builds_empty PASSED [ 76%]
tests/test_pawpal.py::TestEdgeCases::test_task_that_does_not_fit_is_unplaced PASSED [ 78%]
tests/test_pawpal.py::TestEdgeCases::test_task_fitting_a_window_exactly_is_placed PASSED [ 81%]
tests/test_pawpal.py::TestEdgeCases::test_buffer_is_kept_between_placed_tasks PASSED [ 84%]
tests/test_pawpal.py::TestEdgeCases::test_priority_normalized_to_lowercase PASSED [ 86%]
tests/test_pawpal.py::TestEdgeCases::test_recurrence_normalized_to_lowercase PASSED [ 89%]
tests/test_pawpal.py::TestDayRollover::test_start_new_day_advances_the_date PASSED [ 92%]
tests/test_pawpal.py::TestDayRollover::test_spawned_recurrence_held_back_then_appears PASSED [ 94%]
tests/test_pawpal.py::TestDayRollover::test_stale_recurring_task_rolls_forward_without_duplicating PASSED [ 97%]
tests/test_pawpal.py::TestDayRollover::test_commitments_survive_rollover PASSED [100%]

============================== 38 passed in 0.02s ==============================
```

That transcript is the scheduler suite on its own, from before the AI features
were added. With `tests/test_agent.py` (36) and `tests/test_knowledge.py` (43),
the current total is **117 passed**.

### Confidence level

**★★★☆☆ (3 / 5)**

All 117 tests pass and cover the core behaviors plus the edge cases I could think
of. I'm holding the rating at 3 stars on purpose: the tests were written *after*
the code (not test-driven), so they largely confirm the scheduler does what the
implementation already assumes rather than proving the design is complete. One known open question remains unverified: whether a
buffer should also be enforced between a task and a fixed commitment, not just
between two tasks. Fresh eyes or real-world use could still
surface a case neither the code nor the tests anticipated.

## 📐 Smarter Scheduling

The scheduling logic lives in the `Schedule` class in `pawpal_system.py` (the
class docstring calls it the "brain" — it retrieves tasks from the owner's pets
and places them around fixed commitments). The table summarizes each feature and
the method that implements it; details follow below.

| Feature | Method(s) | Notes |
|---------|-----------|-------|
| Sorting | `Schedule.sort_by_time()` | Orders tasks by preferred `"HH:MM"` time |
| Filtering | `Schedule.filter_tasks()` (+ `all_tasks()`, `pending_tasks()`) | By pet name and/or completion status |
| Conflict detection | `Schedule.detect_conflicts()` | Flags tasks requested for overlapping times |
| Recurring tasks | `Task.mark_complete()` (+ `Owner.start_new_day()`, `Task.__post_init__`) | Daily/weekly tasks spawn their next occurrence |
| Placement | `Schedule.build()` (+ `free_windows()`) | Priority-first, preference-aware, best-fit, with buffers |

### Sorting — `Schedule.sort_by_time()`

Returns all tasks ordered by their preferred time of day. Each `Task` carries a
`scheduled_time` as a zero-padded `"HH:MM"` string (e.g. `"09:30"`); because
fixed-width `"HH:MM"` strings sort chronologically as plain strings, the method
is a one-line `sorted(..., key=lambda t: t.scheduled_time)`. Tasks with no
preferred time (`""`) sort to the front.

### Filtering — `Schedule.filter_tasks(pet_name=..., completed=...)`

Filters tasks by **pet name** and/or **completion status**. Both arguments are
optional keyword-only filters that combine with AND; passing `None` (the default)
means "don't filter on that field," and pet-name matching is case-insensitive.
Two helpers back it: `all_tasks()` returns every task across all pets (done or
not), and `pending_tasks()` returns only incomplete tasks that are due on or
before the schedule's day (so future-dated recurrences stay hidden until their
day arrives).

### Conflict detection — `Schedule.detect_conflicts()`

Reports pairs of tasks whose **requested** time slots overlap — a task with a
`scheduled_time` "wants" the slot `[start, start + duration]`, and two such slots
can't both happen as asked (one pet can't be in two places, and neither can the
single owner). It's a lightweight check: sort the timed slots by start, then
compare each against later ones only until they're clearly past it. It **collects
warning strings and returns them** (empty list = no conflicts) rather than
raising, so a clash is surfaced as a message the caller can print — the terminal
demo prints them, and `app.py` shows them as a Streamlit `st.warning` banner.
Note this checks the times you *requested*, not the built plan, since `build()`
itself never double-books.

### Recurring tasks — `Task.mark_complete()`

A `Task` has a `recurrence` field of `""` (one-off), `"daily"`, or `"weekly"`,
plus a `due_date`. When a recurring task is completed, `mark_complete()`
**automatically spawns a new `Task` instance** for the next occurrence, attached
to the same pet — one day later for daily, seven for weekly, computed with
`datetime.timedelta` so month/year boundaries and leap years roll over correctly.
Supporting pieces:

- `Task.__post_init__()` gives a recurring task a concrete `due_date` (today) as
  its anchor for the next occurrence.
- `Schedule.pending_tasks()` hides future-dated instances until their day.
- `Owner.start_new_day()` advances the schedule date and rolls any *missed*
  recurring task's `due_date` forward — so a skipped daily task stays "today's
  task" instead of lingering stale-dated, and never piles up into duplicates
  (spawning only happens on completion).

## 📸 Demo Walkthrough

### The app (`streamlit run app.py`)

The Streamlit UI is organized top-to-bottom as a single planning page:

- **Owner** — set the owner's name.
- **🐾 Pets** — add a pet (name, species, breed, activity level). The activity
  level feeds the scheduler's morning/afternoon bias.
- **✅ Tasks** — add care tasks for a chosen pet (title, duration, priority, and
  a Never/Daily/Weekly repeat). Current tasks are shown in a table.
- **📌 Commitments** — add the fixed blocks you're busy (e.g. Work 09:00–17:00);
  an overlapping commitment triggers an `st.warning`.
- **🗓️ Today's Schedule** — **Generate schedule** builds the plan and shows it
  as a table (time · what · pet/priority); **Start new day** rolls the date
  forward. Same-time task conflicts appear as an `st.warning`, and anything that
  couldn't fit is listed separately.
- **☑️ Check off tasks** — mark a task complete; a recurring task's next
  occurrence is spawned automatically.

### Example workflow

1. Enter the owner's name (e.g. *Zuriel*).
2. **Add a pet** — *Mochi*, a high-energy dog.
3. **Add a task** — *Morning walk*, 45 min, high priority, repeats Daily.
4. **Add a commitment** — *Work*, 09:00–17:00.
5. Click **Generate schedule** — the walk is placed in the morning, before Work.
6. **Check off** the walk — tomorrow's walk is spawned automatically; click
   **Start new day** to see it become today's task.

### Key scheduler behaviors on display

- **Sorting** — the task/plan views show tasks in time order.
- **Conflict warnings** — two tasks set for the same time raise a clear notice
  while still both getting scheduled (one bumped to the next slot).
- **Preferred-time bumping** — a task whose wanted time is blocked is moved and
  flagged with `[wanted HH:MM]`.
- **Priority + activity placement** — high-priority and high-energy tasks land
  earlier in the day.
- **Recurrence & rollover** — completing a daily/weekly task spawns its next
  occurrence, held back until its day arrives.

### Sample CLI output (`python main.py`)

The terminal demo builds a two-pet scenario and exercises every feature end to
end — sorting, filtering, conflict detection, scheduling, and recurrence:

```
Heads up: 'Lunch w/ Sam' overlaps 'Work'.

===== Tasks sorted by time =====
  --:--  Fetch in the yard for Mochi (pending)
  --:--  Grooming for Luna (pending)
  06:30  Groomer appointment for Mochi (pending)
  06:30  Puppy playdate for Mochi (pending)
  07:30  Feeding for Luna (pending)
  08:00  Morning walk for Mochi (pending)
  12:30  Vet phone call for Mochi (pending)
  15:00  Training session for Mochi (done)
  18:30  Playtime for Luna (pending)

===== Luna's tasks =====
  07:30  Feeding
  18:30  Playtime
  --:--  Grooming

===== Still to do (incomplete) =====
  08:00  Morning walk for Mochi
  12:30  Vet phone call for Mochi
  --:--  Fetch in the yard for Mochi
  06:30  Groomer appointment for Mochi
  06:30  Puppy playdate for Mochi
  07:30  Feeding for Luna
  18:30  Playtime for Luna
  --:--  Grooming for Luna

===== Conflict check =====
  ⚠️  Conflict (same pet): 'Groomer appointment' for Mochi (06:30-07:00) overlaps 'Puppy playdate' for Mochi (06:30-07:00).

===== Today's Schedule =====
Plan for 2026-07-07:
  06:20-06:50  Groomer appointment for Mochi (medium priority) [wanted 06:30]
  07:00-07:20  Fetch in the yard for Mochi (medium priority)
  07:30-07:45  Feeding for Luna (high priority)
  08:00-08:45  Morning walk for Mochi (high priority)
  09:00-17:00  Work (commitment)
  12:00-13:00  Lunch w/ Sam (commitment)
  17:00-17:20  Vet phone call for Mochi (medium priority) [wanted 12:30]
  17:30-17:50  Grooming for Luna (low priority)
  18:30-18:50  Playtime for Luna (low priority)
  19:00-19:30  Puppy playdate for Mochi (low priority) [wanted 06:30]

===== Recurrence (spawn on complete) =====
Before: Luna has 3 tasks; 'Feeding' (daily) due 2026-07-07.
After completing it: Luna has 4 tasks; new 'Feeding' due 2026-07-08.

===== Schedule after 'start new day' (2026-07-08) =====
Plan for 2026-07-08:
  06:20-06:50  Groomer appointment for Mochi (medium priority) [wanted 06:30]
  07:00-07:20  Fetch in the yard for Mochi (medium priority)
  07:30-07:45  Feeding for Luna (high priority)
  08:00-08:45  Morning walk for Mochi (high priority)
  09:00-17:00  Work (commitment)
  12:00-13:00  Lunch w/ Sam (commitment)
  17:00-17:20  Vet phone call for Mochi (medium priority) [wanted 12:30]
  17:30-17:50  Grooming for Luna (low priority)
  18:30-18:50  Playtime for Luna (low priority)
  19:00-19:30  Puppy playdate for Mochi (low priority) [wanted 06:30]
```

> Note: the two same-time tasks (*Groomer appointment* and *Puppy playdate*,
> both wanted 06:30) are flagged as a conflict, then the scheduler keeps both —
> giving 06:30 to the higher-priority one and bumping the other, each marked
> `[wanted 06:30]`.
