"""PawPal+ — the AI planning agent (plan -> act -> check -> fix).

This is the AI layer that sits on top of the rule-based scheduler in
``pawpal_system.py``. It exposes the scheduler to Gemini as a small set of tools
and lets the model run an agentic loop:

    plan     -- read the owner's request and the current state of the day
    retrieve -- look up care guidance for this pet before inventing a routine
    act      -- add tasks/commitments, then build the plan with the real scheduler
    check    -- call review_plan() to find conflicts and tasks that didn't fit
    fix      -- retime or shorten the offending tasks, rebuild, and check again

The retrieve step is the RAG half of the system: ``lookup_care_guidance`` searches
the notes in ``knowledge/`` (see ``pawpal_knowledge.py``) so that "Mochi needs a
routine" produces walk lengths taken from the corpus and cited by filename,
rather than plausible-sounding numbers from the model's memory.

The scheduler stays the single source of truth. The model never writes a
timetable itself: it decides *what* to schedule and *how to repair* a plan that
the scheduler reports as broken, and every placement decision is still made by
``Schedule.build()``.

Dependency direction matters here: ``pawpal_system.py`` does NOT import this
module, so the domain model and its whole test suite still run with no AI
dependency. The ``google-genai`` SDK is imported lazily inside ``plan_day()`` so
this file (and its unit tests) can be imported without the SDK installed.

Everything above the "Gemini wiring" section is provider-agnostic — plain
functions over an ``Owner``. Only the bottom third knows which model is driving,
which is what made swapping providers a contained change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Load GEMINI_API_KEY from a local .env if there is one, so the key lives in a
# gitignored file rather than in shell history. A real environment variable always
# wins (override=False), and a missing .env or a missing package is fine — the
# export-it-yourself path still works.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ModuleNotFoundError:  # pragma: no cover - optional convenience dependency
    pass

from pawpal_knowledge import lookup_care_guidance
from pawpal_system import (
    BUFFER_MIN,
    DAY_END_MIN,
    DAY_START_MIN,
    PRIORITY_RANK,
    Owner,
    Pet,
    Task,
    fmt,
    to_minutes,
)

# The model the agent runs on. gemini-3.6-flash balances speed against the
# reasoning the repair step needs (which of two clashing tasks should move, and
# where to) and is available on the free tier. PAWPAL_MODEL overrides it from
# the environment, so comparing models needs no code change:
#
#   PAWPAL_MODEL=gemini-3.5-flash streamlit run app.py
#
# plan_day(model=...) takes a per-call override too.
DEFAULT_MODEL = os.getenv("PAWPAL_MODEL", "gemini-3.5-flash")

# Ceiling on build -> review -> fix cycles, enforced through the system prompt.
# Without a cap, a request that can never be satisfied — 20 hours of tasks in a
# 16-hour day — would loop the model forever chasing a clean plan.
MAX_REPAIR_ROUNDS = 3

# Hard ceiling on model turns, enforced in code. MAX_REPAIR_ROUNDS is an
# instruction the model can ignore; this one it cannot. It's a backstop against
# a runaway loop (and, on the free tier, against burning the daily request
# quota in a single request), not the normal way the loop ends.
MAX_MODEL_TURNS = 15

VALID_PRIORITIES = tuple(PRIORITY_RANK)  # ("high", "medium", "low")
VALID_RECURRENCES = ("", "daily", "weekly")

SYSTEM_PROMPT = """You are the planning assistant inside PawPal+, a pet care app.
You help one owner get their pets' care tasks onto today's schedule ({day}).

You do not decide *when* tasks happen — the PawPal+ scheduler does that. Your job
is to turn what the owner says into tasks and commitments, run the scheduler, and
then repair anything it reports as broken.

How the scheduler behaves, so your repairs are realistic:
- It only places tasks between {day_start} and {day_end}.
- It places tasks highest-priority first, shorter tasks breaking ties.
- A task with a preferred time lands as close to it as a free window allows; a
  task with no preferred time is nudged by its pet's energy (high -> morning,
  low -> afternoon).
- It leaves a {buffer}-minute gap between placed tasks, so back-to-back times
  will not both fit.
- It never double-books. A task with nowhere to go comes back as "unplaced"
  rather than overlapping something else.

Your loop, every time:
1. Call show_current_state first, so you are working from the real day and not
   from assumptions.
2. If the owner has not spelled out what a pet needs — no duration given, no
   time given, or a broad ask like "set up a routine for Mochi" — call
   lookup_care_guidance BEFORE you decide anything, passing the pet's species
   and activity level along with what you want to know. Build your task list
   from what comes back.
3. Add what is needed with add_care_task / add_commitment.
4. Call build_plan.
5. Call review_plan. This is not optional — it is how you find out whether your
   plan actually worked.
6. If review_plan reports problems, fix them and go back to step 4:
   - conflict between two tasks: retime_task on whichever one is less
     time-sensitive (lower priority, or the one whose time the owner did not
     explicitly ask for), moving it into one of the open gaps review_plan lists.
   - unplaced task: retime_task it into an open gap big enough for it, or
     resize_task it shorter if the owner's request leaves you that freedom.
   Stop repairing after {max_rounds} rounds even if problems remain.

Rules:
- Ground your care advice. Any specific claim about how long, how often, or what
  a breed needs must come from lookup_care_guidance, and you must cite the source
  filenames it gave you. If the knowledge base returns nothing relevant, say that
  plainly and keep to what the owner actually asked for — do not fill the gap
  with numbers from memory.
- Never delete or silently drop a task the owner asked for. If something truly
  cannot fit, leave it and say so plainly in your answer.
- Only use pets that show_current_state lists. If the owner mentions a pet you
  do not have, say so and ask them to add it — do not invent one.
- Do not change a time the owner explicitly asked for unless it conflicts with
  something and you say that you moved it.

Then answer the owner directly: what you scheduled and when, anything you moved
and why, anything you could not fit, and the care notes you based it on. Be
brief — a short paragraph or a few lines, not a report. They can see the schedule
table themselves."""


@dataclass
class AgentResult:
    """What one agent run produced.

    ``reply`` is the model's message to the owner. ``actions`` is the ordered
    log of tool results, which is what makes the plan/act/check loop visible in
    the UI instead of a black box.
    """

    reply: str
    actions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool implementations
#
# These are plain functions taking the Owner explicitly, so they can be unit
# tested with no API key and no SDK installed. They are also provider-agnostic:
# nothing below knows a model exists. TOOL_DECLARATIONS further down describes
# them to Gemini, and _dispatch() routes a call back to the right one.
#
# Each returns a human-readable string. Errors are returned as normal strings
# (not raised): a tool that explains what went wrong lets the model correct
# itself on the next turn, whereas an exception would end the loop.
# ---------------------------------------------------------------------------
def _find_pet(owner: Owner, pet_name: str) -> Pet | None:
    """Look up one of the owner's pets by name, case-insensitively."""
    return next((p for p in owner.pets if p.name.lower() == pet_name.lower()), None)


def _find_task(owner: Owner, pet_name: str, title: str) -> Task | None:
    """Find a pet's task by title, preferring one that isn't done yet."""
    pet = _find_pet(owner, pet_name)
    if pet is None:
        return None
    matches = [t for t in pet.tasks if t.title.lower() == title.lower()]
    return next((t for t in matches if not t.completed), matches[0] if matches else None)


def _valid_hhmm(value: str) -> bool:
    """True if ``value`` is a zero-padded 24-hour "HH:MM" time.

    Checked field by field rather than through ``to_minutes()``: that helper
    happily turns "07:99" into 519 minutes, which would sail past a
    total-minutes bounds check. The two-digit requirement isn't fussiness —
    ``Schedule.sort_by_time()`` sorts these as plain strings, so "8:00" would
    sort after "10:00".
    """
    hours, separator, mins = value.partition(":")
    if separator != ":" or len(hours) != 2 or len(mins) != 2:
        return False
    if not (hours.isdigit() and mins.isdigit()):
        return False
    return int(hours) < 24 and int(mins) < 60


def _open_gaps(owner: Owner) -> list[tuple[int, int]]:
    """Stretches of the planning day holding nothing at all.

    ``Schedule.free_windows()`` deliberately ignores placed tasks (it reports
    the gaps *between commitments* so build() can fill them). For repairs the
    agent needs the opposite: what is still empty once tasks are placed, so it
    can retime a task into somewhere it will actually fit.
    """
    gaps: list[tuple[int, int]] = []
    cursor = DAY_START_MIN
    for block in sorted(owner.schedule.blocks, key=lambda b: b.start_min):
        if block.start_min > cursor:
            gaps.append((cursor, block.start_min))
        cursor = max(cursor, block.end_min)
    if cursor < DAY_END_MIN:
        gaps.append((cursor, DAY_END_MIN))
    return gaps


def show_current_state(owner: Owner) -> str:
    """Describe the pets, their tasks, and the day's commitments."""
    lines = [f"Day: {owner.schedule.day.isoformat()}  Owner: {owner.name}"]

    if not owner.pets:
        lines.append("Pets: none yet.")
    for pet in owner.pets:
        lines.append(
            f"Pet: {pet.name} ({pet.species}, {pet.breed}, "
            f"{pet.activity_level} energy)"
        )
        if not pet.tasks:
            lines.append("  (no tasks)")
        for task in pet.tasks:
            when = task.scheduled_time or "no preferred time"
            status = "done" if task.completed else "pending"
            repeats = f", repeats {task.recurrence}" if task.recurrence else ""
            lines.append(
                f"  - {task.title}: {when}, {task.duration_minutes} min, "
                f"{task.priority} priority{repeats} ({status})"
            )

    commitments = [b for b in owner.schedule.blocks if b.task is None]
    if commitments:
        lines.append("Commitments (owner is busy):")
        for b in sorted(commitments, key=lambda b: b.start_min):
            lines.append(f"  - {b.label}: {fmt(b.start_min)}-{fmt(b.end_min)}")
    else:
        lines.append("Commitments: none.")

    return "\n".join(lines)


def add_care_task(
    owner: Owner,
    pet_name: str,
    title: str,
    duration_minutes: int,
    priority: str,
    scheduled_time: str = "",
    recurrence: str = "",
) -> str:
    """Attach a new care task to one of the owner's pets."""
    pet = _find_pet(owner, pet_name)
    if pet is None:
        known = ", ".join(p.name for p in owner.pets) or "none"
        return f"Error: no pet named '{pet_name}'. The owner's pets are: {known}."
    if priority.lower() not in VALID_PRIORITIES:
        return (
            f"Error: priority '{priority}' is not valid. "
            f"Use one of: {', '.join(VALID_PRIORITIES)}."
        )
    if recurrence.lower() not in VALID_RECURRENCES:
        return (
            f"Error: recurrence '{recurrence}' is not valid. "
            "Use '' for one-off, 'daily', or 'weekly'."
        )
    if duration_minutes < 1:
        return "Error: duration_minutes must be at least 1."
    if scheduled_time and not _valid_hhmm(scheduled_time):
        return (
            f"Error: scheduled_time '{scheduled_time}' is not a 24-hour "
            "'HH:MM' time. Use '' for no preference."
        )

    pet.add_task(
        Task(
            title,
            int(duration_minutes),
            priority,
            scheduled_time=scheduled_time,
            recurrence=recurrence,
        )
    )
    when = f" at {scheduled_time}" if scheduled_time else " (no preferred time)"
    return (
        f"Added '{title}' for {pet.name}{when}: {duration_minutes} min, "
        f"{priority.lower()} priority"
        f"{', repeats ' + recurrence.lower() if recurrence else ''}. "
        "Call build_plan to place it."
    )


def add_commitment(owner: Owner, label: str, start_time: str, end_time: str) -> str:
    """Block out time the owner is busy, so the scheduler plans around it."""
    if not _valid_hhmm(start_time) or not _valid_hhmm(end_time):
        return (
            f"Error: '{start_time}' to '{end_time}' is not a valid 24-hour "
            "'HH:MM' range."
        )
    start, end = to_minutes(start_time), to_minutes(end_time)
    if end <= start:
        return f"Error: end_time ({end_time}) must be after start_time ({start_time})."

    warning = owner.add_commitment(start, end, label)
    message = f"Added commitment '{label}' {start_time}-{end_time}."
    return f"{message} {warning}" if warning else message


def retime_task(owner: Owner, pet_name: str, title: str, scheduled_time: str) -> str:
    """Change a task's preferred time (or clear it with an empty string)."""
    task = _find_task(owner, pet_name, title)
    if task is None:
        return f"Error: {pet_name} has no task called '{title}'."
    if scheduled_time and not _valid_hhmm(scheduled_time):
        return (
            f"Error: scheduled_time '{scheduled_time}' is not a 24-hour "
            "'HH:MM' time. Use '' to clear the preference."
        )

    previous = task.scheduled_time or "no preferred time"
    task.scheduled_time = scheduled_time
    now = scheduled_time or "no preferred time"
    return (
        f"'{task.title}' for {task.pet_name}: preferred time {previous} -> {now}. "
        "Call build_plan to rebuild with the new time."
    )


def resize_task(owner: Owner, pet_name: str, title: str, duration_minutes: int) -> str:
    """Change how long a task takes."""
    task = _find_task(owner, pet_name, title)
    if task is None:
        return f"Error: {pet_name} has no task called '{title}'."
    if duration_minutes < 1:
        return "Error: duration_minutes must be at least 1."

    previous = task.duration_minutes
    task.duration_minutes = int(duration_minutes)
    return (
        f"'{task.title}' for {task.pet_name}: {previous} min -> "
        f"{duration_minutes} min. Call build_plan to rebuild."
    )


def build_plan(owner: Owner) -> str:
    """Run the scheduler and return the plan it produced."""
    owner.build_day()
    return owner.schedule.explain()


def review_plan(owner: Owner) -> str:
    """Check the built plan: conflicts, unplaced tasks, and remaining open gaps.

    This is the 'check' half of the agent loop. It reports the scheduler's own
    verdict — ``detect_conflicts()`` and ``schedule.unplaced`` — so success is
    measured by the domain model, not by the model's opinion of its own work.
    """
    conflicts = owner.schedule.detect_conflicts()
    unplaced = owner.schedule.unplaced

    if not owner.schedule.blocks and not unplaced:
        # Ambiguous on purpose: an empty day looks the same whether build_plan
        # hasn't run or there was simply nothing to place. Saying only "call
        # build_plan" would send the agent round that loop forever.
        return (
            "Nothing is scheduled. Either the plan has not been built yet "
            "(call build_plan), or there are no pending tasks and no "
            "commitments to place."
        )

    if not conflicts and not unplaced:
        return "OK: no conflicts, and every pending task was placed."

    lines = ["Problems with the current plan:"]
    for warning in conflicts:
        lines.append(f"  - conflict: {warning}")
    for task in unplaced:
        lines.append(
            f"  - unplaced: '{task.title}' for {task.pet_name} needs "
            f"{task.duration_minutes} min ({task.priority} priority)"
        )

    gaps = _open_gaps(owner)
    if gaps:
        lines.append("Open gaps you can move a task into:")
        for start, end in gaps:
            lines.append(f"  - {fmt(start)}-{fmt(end)} ({end - start} min free)")
    else:
        lines.append(f"No open gaps left between {fmt(DAY_START_MIN)} and {fmt(DAY_END_MIN)}.")

    return "\n".join(lines)




# ---------------------------------------------------------------------------
# Gemini wiring
#
# The Interactions API takes tool declarations as plain JSON Schema, so the
# schemas below are hand-written rather than derived from the Python signatures.
# That could drift from the implementations above — so it doesn't, there's a
# test (test_declarations_match_their_implementations) that cross-checks every
# declaration's properties against the real signature with inspect.
#
# The loop is hand-rolled rather than delegated to an SDK helper: send the
# history, execute whatever functions come back, append the results, send again.
# That's the agentic loop in plain sight, and it's where the turn cap lives.
# ---------------------------------------------------------------------------
def _lookup_tool(
    owner: Owner,
    query: str,
    species: str = "",
    activity_level: str = "",
    max_results: int = 3,
) -> str:
    """Adapt retrieval to the (owner, **kwargs) shape every other tool has.

    ``owner`` is deliberately unused — the knowledge base knows nothing about
    pets or schedules — but taking it keeps dispatch uniform.
    """
    # Clamped rather than trusted: the model asking for 25 would flood the
    # context with the whole corpus.
    capped = min(max(int(max_results), 1), 5)
    return lookup_care_guidance(query, species, activity_level, capped)


# Tool name -> implementation. Every handler takes the Owner first, then the
# arguments the model supplies.
TOOL_HANDLERS = {
    "show_current_state": show_current_state,
    "lookup_care_guidance": _lookup_tool,
    "add_care_task": add_care_task,
    "add_commitment": add_commitment,
    "retime_task": retime_task,
    "resize_task": resize_task,
    "build_plan": build_plan,
    "review_plan": review_plan,
}

# What the model sees. Descriptions say *when* to reach for a tool, not just
# what it does — that's what actually drives the loop in the right order.
TOOL_DECLARATIONS = [
    {
        "type": "function",
        "name": "show_current_state",
        "description": (
            "Show the owner's pets, their care tasks, and the day's commitments. "
            "Call this first, before adding anything, so you know which pets "
            "exist and what is already scheduled."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "lookup_care_guidance",
        "description": (
            "Search the pet care knowledge base for guidance on what a pet "
            "needs. Call this before recommending a routine the owner did not "
            "spell out — how long a walk should be, how often to feed, what a "
            "breed requires. Ground your suggestions in what this returns "
            "rather than in memory, and cite the source filenames it gives you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What you want to know, in a few words, e.g. 'how much "
                        "exercise for a high energy dog'."
                    ),
                },
                "species": {
                    "type": "string",
                    "description": (
                        "The pet's species ('dog' or 'cat') to prefer notes "
                        "about it. Leave empty for general topics like feeding."
                    ),
                },
                "activity_level": {
                    "type": "string",
                    "description": (
                        "The pet's activity level ('high' or 'low') when "
                        "relevant. Leave empty otherwise."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "How many note sections to return, 1 to 5.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "add_care_task",
        "description": "Add a care task to one of the owner's pets.",
        "parameters": {
            "type": "object",
            "properties": {
                "pet_name": {
                    "type": "string",
                    "description": "Name of an existing pet, as show_current_state lists it.",
                },
                "title": {
                    "type": "string",
                    "description": "Short name for the task, e.g. 'Morning walk'.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "How many minutes the task takes.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How important the task is.",
                },
                "scheduled_time": {
                    "type": "string",
                    "description": (
                        "Preferred 24-hour zero-padded 'HH:MM' time, e.g. "
                        "'08:00'. Empty means no preference, and the scheduler "
                        "picks a slot from the pet's energy level."
                    ),
                },
                "recurrence": {
                    "type": "string",
                    "enum": ["", "daily", "weekly"],
                    "description": "Empty for a one-off task, or how often it repeats.",
                },
            },
            "required": ["pet_name", "title", "duration_minutes", "priority"],
        },
    },
    {
        "type": "function",
        "name": "add_commitment",
        "description": (
            "Block out time the owner is busy, so tasks are planned around it. "
            "Use this for work, appointments, or anything they cannot move."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "What the commitment is, e.g. 'Work'.",
                },
                "start_time": {
                    "type": "string",
                    "description": "24-hour zero-padded 'HH:MM' start, e.g. '09:00'.",
                },
                "end_time": {
                    "type": "string",
                    "description": "24-hour zero-padded 'HH:MM' end, later than start_time.",
                },
            },
            "required": ["label", "start_time", "end_time"],
        },
    },
    {
        "type": "function",
        "name": "retime_task",
        "description": (
            "Change a task's preferred time. Your main tool for fixing a plan "
            "that review_plan reported as broken."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pet_name": {
                    "type": "string",
                    "description": "Name of the pet the task belongs to.",
                },
                "title": {
                    "type": "string",
                    "description": "The task's title, as shown by show_current_state.",
                },
                "scheduled_time": {
                    "type": "string",
                    "description": (
                        "New 24-hour zero-padded 'HH:MM' time, or empty to drop "
                        "the preference and let the scheduler choose."
                    ),
                },
            },
            "required": ["pet_name", "title", "scheduled_time"],
        },
    },
    {
        "type": "function",
        "name": "resize_task",
        "description": (
            "Change how long a task takes, to squeeze it into a smaller gap. "
            "Only shorten a task if the owner's request leaves you that freedom."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pet_name": {
                    "type": "string",
                    "description": "Name of the pet the task belongs to.",
                },
                "title": {
                    "type": "string",
                    "description": "The task's title, as shown by show_current_state.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "New duration in minutes.",
                },
            },
            "required": ["pet_name", "title", "duration_minutes"],
        },
    },
    {
        "type": "function",
        "name": "build_plan",
        "description": (
            "Build the day's schedule and return the plan. Call this after "
            "adding or changing tasks, and always follow it with review_plan."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "review_plan",
        "description": (
            "Check the plan you just built for conflicts and tasks that did not "
            "fit. Reports the scheduler's own verdict plus the open gaps left in "
            "the day. Call this after every build_plan and act on what it says."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def _dispatch(owner: Owner, name: str, arguments: dict | None) -> str:
    """Run one tool call against the owner, turning any failure into a message.

    Nothing in here raises. An unknown tool name or a bad argument set comes
    back as an error string the model can read and correct on its next turn,
    which is the whole reason the loop can recover from a mis-call.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return (
            f"Error: no tool named '{name}'. "
            f"Available tools: {', '.join(sorted(TOOL_HANDLERS))}."
        )
    try:
        return handler(owner, **(arguments or {}))
    except TypeError as exc:
        # Wrong or missing argument names — tell the model exactly what broke.
        return f"Error calling {name}: {exc}. Check the argument names and retry."
    except Exception as exc:  # pragma: no cover - belt and braces
        return f"Error calling {name}: {exc!r}."


def _require_client(model: str):
    """Build the Gemini client, with actionable errors for the two usual gaps."""
    try:
        from google import genai
    except ModuleNotFoundError as exc:  # pragma: no cover - environment issue
        raise RuntimeError(
            "The google-genai SDK is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc

    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        raise RuntimeError(
            "No API key found. Get a free one at https://aistudio.google.com/apikey "
            "then run: export GEMINI_API_KEY=your-key"
        )
    return genai.Client()


def plan_day(
    owner: Owner,
    request: str,
    *,
    model: str = DEFAULT_MODEL,
    max_repair_rounds: int = MAX_REPAIR_ROUNDS,
    max_turns: int = MAX_MODEL_TURNS,
    thinking_level: str | None = None,
) -> AgentResult:
    """Run the plan -> retrieve -> act -> check -> fix loop for a plain request.

    Mutates ``owner`` in place — that is the point, since the agent's tools are
    the real scheduler methods — so the app's existing tables show the result.

    Args:
        model: Gemini model id. See DEFAULT_MODEL / the PAWPAL_MODEL env var.
        max_repair_rounds: How many build/review/fix cycles the prompt allows.
        max_turns: Hard ceiling on model turns. A backstop for a model that
            never stops calling tools, not the normal way the loop ends.
        thinking_level: Passed through as generation_config.thinking_level
            (e.g. "low"). Left unset by default so the model's own default
            applies rather than a value guessed here.

    Raises:
        RuntimeError: if the SDK isn't installed or no API key is set.
    """
    client = _require_client(model)
    actions: list[str] = []

    # store=False keeps the conversation on our side: nothing is retained
    # server-side between turns, and the history we resend is exactly what we
    # can see. Costs a little more input per turn, buys full transparency.
    history: list[dict] = [
        {"type": "user_input", "content": [{"type": "text", "text": request}]}
    ]
    system_instruction = SYSTEM_PROMPT.format(
        day=owner.schedule.day.isoformat(),
        day_start=fmt(DAY_START_MIN),
        day_end=fmt(DAY_END_MIN),
        buffer=BUFFER_MIN,
        max_rounds=max_repair_rounds,
    )
    generation_config = {"thinking_level": thinking_level} if thinking_level else None

    reply = ""
    for _ in range(max_turns):
        kwargs = {
            "model": model,
            "store": False,
            "system_instruction": system_instruction,
            "input": history,
            "tools": TOOL_DECLARATIONS,
        }
        if generation_config:
            kwargs["generation_config"] = generation_config
        interaction = client.interactions.create(**kwargs)

        steps = list(interaction.steps or [])
        for step in steps:
            history.append(step.model_dump() if hasattr(step, "model_dump") else step)

        calls = [s for s in steps if getattr(s, "type", None) == "function_call"]
        if not calls:
            # No tools requested: the model is answering, so the loop is done.
            reply = (interaction.output_text or "").strip()
            break

        # Several calls can come back at once (parallel function calling), so
        # every one gets executed and answered before the next turn.
        for call in calls:
            result = _dispatch(owner, call.name, call.arguments)
            actions.append(f"{call.name} -> {result}")
            history.append(
                {
                    "type": "function_result",
                    "name": call.name,
                    "call_id": call.id,
                    "result": [{"type": "text", "text": result}],
                }
            )
    else:
        # Fell out of the for-loop without breaking: the turn cap was hit.
        reply = (
            f"I stopped after {max_turns} steps without finishing. "
            "Here's the plan as it stands — check the schedule below."
        )

    return AgentResult(reply=reply, actions=actions)
