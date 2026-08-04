"""Tests for the PawPal+ AI planner's tool layer (pawpal_agent.py).

These cover the part of the agent that is deterministic: the tools the model
calls, and the declarations describing them. No API key, no network, and no
google-genai SDK needed — pawpal_agent imports the SDK lazily inside plan_day(),
so everything below runs in the normal suite.

Organized by what's under test:

- Adding       -- add_care_task / add_commitment reach the domain model correctly.
- Repairs      -- retime_task / resize_task, the tools the fix step uses.
- Review       -- the 'check' half of the loop reports the scheduler's verdict.
- Bad input    -- every tool returns an error string instead of raising, so a
                  wrong argument gives the model something to correct, and
                  dispatch survives unknown tool names.
- Declarations -- the hand-written JSON schemas stay in step with the Python
                  signatures they describe.

Times are minutes since midnight (09:00 = 540).
"""

import inspect
from datetime import date

import pytest

from pawpal_agent import (
    TOOL_DECLARATIONS,
    TOOL_HANDLERS,
    _dispatch,
    add_care_task,
    add_commitment,
    build_plan,
    resize_task,
    retime_task,
    review_plan,
    show_current_state,
)
from pawpal_system import DAY_END_MIN, DAY_START_MIN, Owner, Pet

TODAY = date.today()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def owner() -> Owner:
    """An owner planning for TODAY, with one high-energy dog and no tasks."""
    owner = Owner("Jordan", TODAY)
    owner.add_pet(Pet("Mochi", "dog", "Shiba Inu", "high"))
    return owner


@pytest.fixture
def mochi(owner: Owner) -> Pet:
    """The owner's dog."""
    return owner.pets[0]


# ===========================================================================
# Adding tasks and commitments
# ===========================================================================
class TestAdding:
    def test_add_care_task_reaches_the_pet(self, owner, mochi):
        """The tool creates a real Task, linked to the right pet."""
        result = add_care_task(
            owner, "Mochi", "Morning walk", 45, "high",
            scheduled_time="08:00", recurrence="daily",
        )

        assert len(mochi.tasks) == 1
        task = mochi.tasks[0]
        assert task.title == "Morning walk"
        assert task.duration_minutes == 45
        assert task.priority == "high"
        assert task.scheduled_time == "08:00"
        assert task.recurrence == "daily"
        assert task.pet is mochi
        assert "Morning walk" in result

    def test_pet_name_matching_is_case_insensitive(self, owner, mochi):
        """The model doesn't have to reproduce the pet's exact casing."""
        add_care_task(owner, "mochi", "Feed", 15, "high")

        assert len(mochi.tasks) == 1

    def test_add_commitment_blocks_out_time(self, owner):
        """"HH:MM" strings become a commitment block in minutes."""
        add_commitment(owner, "Work", "09:00", "17:00")

        commitments = [b for b in owner.schedule.blocks if b.task is None]
        assert len(commitments) == 1
        assert (commitments[0].start_min, commitments[0].end_min) == (540, 1020)

    def test_overlapping_commitment_warning_is_passed_through(self, owner):
        """The scheduler's overlap warning reaches the model, not just the UI."""
        add_commitment(owner, "Work", "09:00", "17:00")
        result = add_commitment(owner, "Lunch w/ Sam", "12:00", "13:00")

        assert "overlaps" in result.lower()

    def test_show_current_state_lists_pets_tasks_and_commitments(self, owner):
        """The agent's first look at the day includes everything it needs."""
        add_care_task(owner, "Mochi", "Morning walk", 45, "high", scheduled_time="08:00")
        add_commitment(owner, "Work", "09:00", "17:00")

        state = show_current_state(owner)

        assert "Mochi" in state
        assert "high energy" in state       # activity level drives soft targets
        assert "Morning walk" in state
        assert "08:00" in state
        assert "Work" in state
        assert "09:00-17:00" in state


# ===========================================================================
# Repair tools (what the agent reaches for after a bad review)
# ===========================================================================
class TestRepairs:
    def test_retime_task_changes_the_preferred_time(self, owner, mochi):
        """Retiming updates the task the scheduler will read on the next build."""
        add_care_task(owner, "Mochi", "Walk", 30, "high", scheduled_time="06:30")

        retime_task(owner, "Mochi", "Walk", "18:00")

        assert mochi.tasks[0].scheduled_time == "18:00"

    def test_retime_task_can_clear_the_preference(self, owner, mochi):
        """An empty time hands placement back to the scheduler's soft targets."""
        add_care_task(owner, "Mochi", "Walk", 30, "high", scheduled_time="06:30")

        retime_task(owner, "Mochi", "Walk", "")
        owner.build_day()

        assert mochi.tasks[0].scheduled_time == ""
        # High-energy pet with no preferred time -> nudged to the morning target.
        assert owner.schedule.blocks[0].start_min == 8 * 60

    def test_resize_task_changes_duration(self, owner, mochi):
        """Shortening a task is the other way to make it fit."""
        add_care_task(owner, "Mochi", "Walk", 60, "high")

        resize_task(owner, "Mochi", "Walk", 20)

        assert mochi.tasks[0].duration_minutes == 20

    def test_a_retime_then_rebuild_actually_fixes_a_conflict(self, owner, mochi):
        """The full repair path: conflict -> retime -> rebuild -> clean review.

        This is the loop the agent runs, exercised without the model, so the
        tools are proven to be capable of fixing what review_plan reports.
        """
        add_care_task(owner, "Mochi", "Groomer", 30, "medium", scheduled_time="06:30")
        add_care_task(owner, "Mochi", "Playdate", 30, "low", scheduled_time="06:30")
        build_plan(owner)
        assert "conflict" in review_plan(owner)

        retime_task(owner, "Mochi", "Playdate", "10:00")
        build_plan(owner)

        assert review_plan(owner).startswith("OK")


# ===========================================================================
# Review — the 'check' half of the loop
# ===========================================================================
class TestReview:
    def test_review_asks_for_a_build_first(self, owner):
        """With nothing built, review tells the model what to do instead."""
        add_care_task(owner, "Mochi", "Walk", 30, "high")

        assert "build_plan" in review_plan(owner)

    def test_clean_plan_reports_ok(self, owner):
        """A plan with room to spare comes back clean."""
        add_care_task(owner, "Mochi", "Walk", 30, "high")
        build_plan(owner)

        assert review_plan(owner).startswith("OK")

    def test_review_reports_same_time_conflicts(self, owner):
        """Two tasks wanting one slot are surfaced as a conflict."""
        add_care_task(owner, "Mochi", "Groomer", 30, "medium", scheduled_time="06:30")
        add_care_task(owner, "Mochi", "Playdate", 30, "low", scheduled_time="06:30")
        build_plan(owner)

        assert "conflict" in review_plan(owner)

    def test_review_reports_unplaced_tasks(self, owner):
        """A task with nowhere to go is named, with what it needs."""
        add_commitment(owner, "All-day shift", "06:00", "22:00")
        add_care_task(owner, "Mochi", "Long hike", 120, "high")
        build_plan(owner)

        report = review_plan(owner)
        assert "unplaced" in report
        assert "Long hike" in report
        assert "120 min" in report

    def test_review_offers_open_gaps_to_move_into(self, owner):
        """A broken plan comes with somewhere to move the task to.

        The gaps account for placed tasks, not just commitments — otherwise the
        agent would be handed slots that are already taken.
        """
        add_commitment(owner, "Work", "09:00", "17:00")
        add_care_task(owner, "Mochi", "Walk A", 30, "high", scheduled_time="07:00")
        add_care_task(owner, "Mochi", "Walk B", 30, "low", scheduled_time="07:00")
        build_plan(owner)

        report = review_plan(owner)
        assert "Open gaps" in report
        # Check the gaps list itself — the conflict line above it also mentions
        # 07:00-07:30, which is the slot Walk A actually occupies.
        gaps = report.split("Open gaps you can move a task into:")[1]
        assert "07:00-07:30" not in gaps
        assert "17:00-22:00" in gaps

    def test_review_ignores_completed_tasks(self, owner, mochi):
        """A checked-off task isn't a problem to solve, even if it couldn't fit."""
        add_commitment(owner, "Work", "09:00", "17:00")
        add_care_task(owner, "Mochi", "Ten-hour hike", 600, "high")
        mochi.tasks[0].mark_complete()
        build_plan(owner)

        # Uncompleted, a 600-minute task has no window big enough and would be
        # reported as unplaced. Completed, it's out of scope for the plan.
        assert review_plan(owner).startswith("OK")


# ===========================================================================
# Bad input: tools explain, never raise
# ===========================================================================
class TestBadInput:
    def test_unknown_pet_is_reported_and_changes_nothing(self, owner, mochi):
        """A hallucinated pet name gets an error listing the real pets."""
        result = add_care_task(owner, "Luna", "Feed", 15, "high")

        assert result.startswith("Error")
        assert "Mochi" in result          # tells the model what does exist
        assert mochi.tasks == []          # and nothing was added

    def test_invalid_priority_is_rejected(self, owner, mochi):
        """Priorities outside low/medium/high would silently rank as lowest."""
        result = add_care_task(owner, "Mochi", "Walk", 30, "urgent")

        assert result.startswith("Error")
        assert mochi.tasks == []

    def test_invalid_recurrence_is_rejected(self, owner, mochi):
        """Only "", daily, and weekly can spawn a next occurrence."""
        result = add_care_task(owner, "Mochi", "Walk", 30, "high", recurrence="monthly")

        assert result.startswith("Error")
        assert mochi.tasks == []

    @pytest.mark.parametrize("bad_time", ["25:00", "8am", "08-00", "07:99", "8:00"])
    def test_invalid_time_is_rejected(self, owner, mochi, bad_time):
        """Malformed times never reach a Task.

        "07:99" and "8:00" are the interesting ones: both survive to_minutes()
        (as 519 and 480), so validation has to check the fields, not just the
        total. Un-padded times would also break sort_by_time's string sort.
        """
        result = add_care_task(owner, "Mochi", "Walk", 30, "high", scheduled_time=bad_time)

        assert result.startswith("Error")
        assert mochi.tasks == []

    def test_zero_duration_is_rejected(self, owner, mochi):
        """A zero-minute task would be meaningless to place."""
        assert add_care_task(owner, "Mochi", "Walk", 0, "high").startswith("Error")
        assert mochi.tasks == []

    def test_backwards_commitment_is_rejected(self, owner):
        """An end before its start would make a negative-length block."""
        result = add_commitment(owner, "Work", "17:00", "09:00")

        assert result.startswith("Error")
        assert owner.schedule.blocks == []

    def test_retiming_an_unknown_task_is_reported(self, owner):
        """The model gets told the task doesn't exist rather than an exception."""
        assert retime_task(owner, "Mochi", "Nap", "09:00").startswith("Error")

    def test_resizing_an_unknown_task_is_reported(self, owner):
        """Same for the other repair tool."""
        assert resize_task(owner, "Mochi", "Nap", 20).startswith("Error")

    def test_dispatch_reports_an_unknown_tool(self, owner):
        """A hallucinated tool name comes back as a correctable message."""
        result = _dispatch(owner, "delete_everything", {})

        assert result.startswith("Error")
        assert "add_care_task" in result  # tells the model what does exist

    def test_dispatch_reports_wrong_argument_names(self, owner, mochi):
        """A malformed call names the bad argument instead of raising."""
        result = _dispatch(owner, "add_care_task", {"pet": "Mochi"})

        assert result.startswith("Error")
        assert "pet" in result
        assert mochi.tasks == []

    def test_dispatch_runs_a_valid_call(self, owner, mochi):
        """The happy path still reaches the implementation."""
        _dispatch(
            owner,
            "add_care_task",
            {"pet_name": "Mochi", "title": "Walk", "duration_minutes": 30, "priority": "high"},
        )

        assert len(mochi.tasks) == 1

    def test_planning_window_constants_are_the_ones_the_agent_is_told(self):
        """The system prompt quotes the scheduler's window, so pin it here.

        If DAY_START_MIN/DAY_END_MIN ever change, the prompt follows them
        automatically — this test just fails loudly if the window itself moves,
        since every repair suggestion the agent makes depends on it.
        """
        assert (DAY_START_MIN, DAY_END_MIN) == (6 * 60, 22 * 60)


# ===========================================================================
# Tool declarations — what the model is actually shown
# ===========================================================================
class TestToolDeclarations:
    """The declarations are hand-written JSON Schema, so they can drift from the
    Python implementations. These tests are the thing that stops that."""

    def test_every_declaration_has_a_handler_and_vice_versa(self):
        """A declared tool with no handler is a guaranteed runtime error."""
        declared = {d["name"] for d in TOOL_DECLARATIONS}

        assert declared == set(TOOL_HANDLERS)

    def test_declarations_are_well_formed(self):
        """Missing a description or schema silently degrades tool selection."""
        for declaration in TOOL_DECLARATIONS:
            assert declaration["type"] == "function"
            assert declaration["name"]
            assert len(declaration["description"]) > 40, declaration["name"]
            schema = declaration["parameters"]
            assert schema["type"] == "object"
            assert isinstance(schema["properties"], dict)
            assert isinstance(schema["required"], list)

    def test_every_parameter_is_described(self):
        """An undescribed parameter is one the model has to guess at."""
        for declaration in TOOL_DECLARATIONS:
            for name, spec in declaration["parameters"]["properties"].items():
                assert spec.get("description"), f"{declaration['name']}.{name}"
                assert spec.get("type"), f"{declaration['name']}.{name}"

    def test_declarations_match_their_implementations(self):
        """The drift guard: declared parameters must match the real signature.

        Hand-written schemas are the weak point of this design — rename an
        implementation argument and the model keeps calling the old name, which
        only shows up as a confusing runtime error mid-run. This compares every
        declaration against `inspect.signature` instead.
        """
        for declaration in TOOL_DECLARATIONS:
            handler = TOOL_HANDLERS[declaration["name"]]
            # Drop `owner`: dispatch supplies it, the model never sees it.
            actual = set(inspect.signature(handler).parameters) - {"owner"}
            declared = set(declaration["parameters"]["properties"])

            assert declared == actual, (
                f"{declaration['name']}: declared {sorted(declared)} "
                f"but the implementation takes {sorted(actual)}"
            )

    def test_required_parameters_have_no_default(self):
        """Anything the implementation defaults must not be marked required.

        And anything without a default must be, or the model can omit it and
        dispatch raises TypeError on a call that looked valid.
        """
        for declaration in TOOL_DECLARATIONS:
            handler = TOOL_HANDLERS[declaration["name"]]
            parameters = inspect.signature(handler).parameters
            no_default = {
                name
                for name, p in parameters.items()
                if name != "owner" and p.default is inspect.Parameter.empty
            }

            assert set(declaration["parameters"]["required"]) == no_default, (
                declaration["name"]
            )
