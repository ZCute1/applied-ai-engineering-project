import streamlit as st
from datetime import date, time
from pawpal_system import Task, Pet, Owner, fmt
from pawpal_agent import plan_day
from pawpal_knowledge import load_chunks

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

st.title("🐾 PawPal+")

st.markdown(
    """
Welcome to the PawPal+ starter app.
"""
)

# ----------------------------------------------------------------------------
# Original starter scaffolding — commented out, kept for reference/direction.
# ----------------------------------------------------------------------------
# with st.expander("Scenario", expanded=True):
#     st.markdown(
#         """
# **PawPal+** is a pet care planning assistant. It helps a pet owner plan care tasks
# for their pet(s) based on constraints like time, priority, and preferences.
#
# You will design and implement the scheduling logic and connect it to this Streamlit UI.
# """
#     )
#
# with st.expander("What you need to build", expanded=True):
#     st.markdown(
#         """
# At minimum, your system should:
# - Represent pet care tasks (what needs to happen, how long it takes, priority)
# - Represent the pet and the owner (basic info and preferences)
# - Build a plan/schedule for a day that chooses and orders tasks based on constraints
# - Explain the plan (why each task was chosen and when it happens)
# """
#     )
#
# st.divider()
#
# st.subheader("Quick Demo Inputs (UI only)")
# owner_name = st.text_input("Owner name", value="Jordan")
# pet_name = st.text_input("Pet name", value="Mochi")
# species = st.selectbox("Species", ["dog", "cat", "other"])
#
# st.markdown("### Tasks")
# st.caption("Add a few tasks. In your final version, these should feed into your scheduler.")
#
# if "tasks" not in st.session_state:
#     st.session_state.tasks = []
#
# col1, col2, col3 = st.columns(3)
# with col1:
#     task_title = st.text_input("Task title", value="Morning walk")
# with col2:
#     duration = st.number_input("Duration (minutes)", min_value=1, max_value=240, value=20)
# with col3:
#     priority = st.selectbox("Priority", ["low", "medium", "high"], index=2)
#
# if st.button("Add task"):
#     st.session_state.tasks.append(
#         {"title": task_title, "duration_minutes": int(duration), "priority": priority}
#     )
#
# if st.session_state.tasks:
#     st.write("Current tasks:")
#     st.table(st.session_state.tasks)
# else:
#     st.info("No tasks yet. Add one above.")
#
# st.divider()
#
# st.subheader("Build Schedule")
# st.caption("This button should call your scheduling logic once you implement it.")
#
# if st.button("Generate schedule"):
#     st.warning(
#         "Not implemented yet. Next step: create your scheduling logic (classes/functions) and call it here."
#     )
#     st.markdown(
#         """
# Suggested approach:
# 1. Design your UML (draft).
# 2. Create class stubs (no logic).
# 3. Implement scheduling behavior.
# 4. Connect your scheduler here and display results.
# """
#     )

# ============================================================================
# Working PawPal+ app
# ============================================================================
st.caption("A pet care planning assistant — plan the day's tasks around your commitments.")


def get_owner() -> Owner:
    """Create the Owner once per session and reuse it on every rerun.

    Streamlit re-runs the whole script on each interaction, but st.session_state
    persists — so we only build the Owner if it isn't already in the 'vault'.
    Everything else (pets, tasks, schedule) hangs off this one object.
    """
    if "owner" not in st.session_state:
        st.session_state.owner = Owner("Jordan", date.today())
    return st.session_state.owner


def to_minutes(t: time) -> int:
    """Convert a datetime.time to minutes since midnight (what the model uses)."""
    return t.hour * 60 + t.minute


owner = get_owner()

# Keep the owner's name in sync with the input without recreating the object.
owner.name = st.text_input("Owner name", value=owner.name)

st.divider()

# ----------------------------------------------------------------------------
# Pets
# ----------------------------------------------------------------------------
st.subheader("🐾 Pets")
with st.form("add_pet", clear_on_submit=True):
    c1, c2 = st.columns(2)
    with c1:
        pet_name = st.text_input("Name", value="Mochi")
        species = st.selectbox("Species", ["dog", "cat", "other"])
    with c2:
        breed = st.text_input("Breed", value="Shiba Inu")
        activity = st.selectbox("Activity level", ["low", "medium", "high"], index=2)
    if st.form_submit_button("Add pet"):
        if pet_name.strip():
            owner.add_pet(Pet(pet_name.strip(), species, breed, activity))
            st.success(f"Added {pet_name}.")
        else:
            st.error("Pet needs a name.")

if owner.pets:
    st.write("Current pets: " + ", ".join(f"{p.name} ({p.species})" for p in owner.pets))
else:
    st.info("No pets yet. Add one above.")

st.divider()

# ----------------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------------
st.subheader("✅ Tasks")
if not owner.pets:
    st.caption("Add a pet first, then you can give it tasks.")
else:
    with st.form("add_task", clear_on_submit=True):
        chosen = st.selectbox("For which pet?", [p.name for p in owner.pets])
        t1, t2, t3 = st.columns(3)
        with t1:
            title = st.text_input("Task", value="Morning walk")
        with t2:
            duration = st.number_input("Minutes", min_value=1, max_value=240, value=30)
        with t3:
            priority = st.selectbox("Priority", ["low", "medium", "high"], index=2)
        repeats = st.selectbox("Repeats", ["Never", "Daily", "Weekly"])
        if st.form_submit_button("Add task"):
            pet = next(p for p in owner.pets if p.name == chosen)
            recurrence = "" if repeats == "Never" else repeats.lower()
            pet.add_task(Task(title, int(duration), priority, recurrence=recurrence))
            st.success(f"Added '{title}' for {chosen}.")

    rows = [
        {
            "Pet": p.name,
            "Task": t.title,
            "Min": t.duration_minutes,
            "Priority": t.priority,
            "Repeats": t.recurrence or "—",
            "Done": t.completed,
        }
        for p in owner.pets
        for t in p.tasks
    ]
    if rows:
        st.table(rows)

st.divider()

# ----------------------------------------------------------------------------
# Commitments (owner's fixed time constraints)
# ----------------------------------------------------------------------------
st.subheader("📌 Commitments (time you're busy)")
with st.form("add_commitment", clear_on_submit=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        label = st.text_input("Label", value="Work")
    with c2:
        start = st.time_input("Start", value=time(9, 0))
    with c3:
        end = st.time_input("End", value=time(17, 0))
    if st.form_submit_button("Add commitment"):
        warning = owner.add_commitment(to_minutes(start), to_minutes(end), label)
        st.success(f"Added '{label}'.")
        if warning:
            st.warning(warning)

st.divider()

# ----------------------------------------------------------------------------
# Schedule
# ----------------------------------------------------------------------------
st.subheader("🗓️ Today's Schedule")
col_a, col_b = st.columns(2)
with col_a:
    if st.button("Generate schedule", type="primary"):
        owner.build_day()
with col_b:
    if st.button("Start new day"):
        owner.start_new_day()

# Surface any same-time task conflicts, with a short explanation. This checks the
# times you *requested* (not the built plan), so it warns even before you
# generate — the scheduler will still place both tasks, just not at once.
conflicts = owner.schedule.detect_conflicts()
if conflicts:
    st.warning(
        "⚠️ **Scheduling conflict**\n\n"
        "Some tasks are set for the same time. A pet can't be in two places at "
        "once — and neither can you — so the scheduler keeps each task's "
        "priority but moves one to the next free slot. Edit their times if "
        "you'd rather decide yourself.\n\n"
        + "\n".join(f"- {c}" for c in conflicts)
    )

if owner.schedule.blocks:
    plan_rows = []
    for b in sorted(owner.schedule.blocks, key=lambda b: b.start_min):
        plan_rows.append(
            {
                "Time": f"{fmt(b.start_min)}–{fmt(b.end_min)}",
                "What": b.label,
                "Type": "commitment"
                if b.task is None
                else f"{b.task.pet.name} · {b.task.priority} priority",
            }
        )
    st.table(plan_rows)

    if owner.schedule.unplaced:
        st.warning(
            "Couldn't fit: "
            + ", ".join(
                f"{t.title} ({t.pet.name}, {t.duration_minutes} min)"
                for t in owner.schedule.unplaced
            )
        )
else:
    st.info("No plan yet. Add tasks/commitments, then click Generate schedule.")

st.divider()

# ----------------------------------------------------------------------------
# AI planner — the agentic workflow (plan -> act -> check -> fix).
#
# The agent's tools ARE the scheduler methods, so a run mutates the same owner
# the rest of this page is built from. That's why the result is kept in
# session_state and drawn on the next rerun: the tables above are rendered
# before the agent runs, so they'd otherwise show the pre-run day.
# ----------------------------------------------------------------------------
st.subheader("🤖 Ask PawPal")
st.caption(
    "Describe what your pets need in plain English. PawPal looks up care guidance "
    "for your pet, adds the tasks, runs the scheduler, checks its own plan for "
    "conflicts and tasks that didn't fit, then repairs it before answering."
)

# The retrieval corpus, on show. Care advice in the agent's answers may only come
# from these notes, so being able to read them is how you check its work.
knowledge = load_chunks()
if knowledge:
    sources = sorted({(c.source, c.title, c.species) for c in knowledge})
    with st.expander(
        f"📚 Care knowledge base — {len(sources)} notes, {len(knowledge)} sections"
    ):
        st.caption(
            "PawPal searches these before suggesting a routine, and cites the "
            "filenames it used. General-interest notes for a course project — "
            "not veterinary advice."
        )
        st.table(
            [
                {
                    "Note": source,
                    "Covers": title,
                    "Species": species or "any",
                    "Sections": sum(1 for c in knowledge if c.source == source),
                }
                for source, title, species in sources
            ]
        )

if "agent_error" in st.session_state:
    st.error(f"PawPal couldn't run: {st.session_state.agent_error}")

if "agent_result" in st.session_state:
    result = st.session_state.agent_result
    if result.reply:
        st.success(result.reply)
    # The step log is what makes the loop visible: you can see it build, review,
    # repair, and rebuild instead of just handing back an answer.
    with st.expander(f"What PawPal did ({len(result.actions)} steps)"):
        for i, action in enumerate(result.actions, start=1):
            st.text(f"{i}. {action}")

if not owner.pets:
    st.caption("Add a pet first, then PawPal can plan for it.")
else:
    with st.form("ask_pawpal"):
        request = st.text_area(
            "What do you need?",
            placeholder=(
                "Mochi needs a 45-minute walk every morning and Luna needs "
                "feeding at 07:30. I'm at work 09:00-17:00."
            ),
            height=90,
        )
        asked = st.form_submit_button("Ask PawPal", type="primary")

    if asked and not request.strip():
        st.error("Tell PawPal what you need first.")
    elif asked:
        with st.spinner("Planning, checking, and repairing…"):
            # Broad catch on purpose: a missing SDK, a missing API key, or a
            # rate limit should read as a message in the app, not a traceback.
            try:
                st.session_state.agent_result = plan_day(owner, request)
                st.session_state.pop("agent_error", None)
            except Exception as exc:
                st.session_state.agent_error = str(exc)
                st.session_state.pop("agent_result", None)
        st.rerun()

st.divider()

# ----------------------------------------------------------------------------
# Check off tasks (below the calendar — mark things done as the day goes)
# ----------------------------------------------------------------------------
st.subheader("☑️ Check off tasks")
pending = [(p, t) for p in owner.pets for t in p.tasks if not t.completed]
if pending:
    labels = [f"{p.name}: {t.title}" for p, t in pending]
    pick = st.selectbox("Mark a task complete", ["—"] + labels)
    if st.button("Check off") and pick != "—":
        owner.check_off(pending[labels.index(pick)][1])
        st.rerun()
else:
    st.caption("No pending tasks to check off.")
