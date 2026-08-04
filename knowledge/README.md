# PawPal+ care knowledge base

These notes are the retrieval corpus for the AI planner's `lookup_care_guidance`
tool (see `pawpal_knowledge.py`). When the owner asks for a routine without
saying exactly what they want, the agent searches these notes for the pet's
species, breed, and activity level, and bases its task list on what it finds
instead of on whatever it happens to remember.

## Not veterinary advice

These are short, general-interest care notes written for a course project. They
are illustrative retrieval material, not veterinary guidance — real advice for a
real animal comes from a vet who has examined it. Several notes say so where it
matters most (medication, puppies and kittens, heat).

## File format

Each note starts with a metadata block between `---` fences, then markdown
sections. `pawpal_knowledge.py` splits each note into one chunk per `##`
heading, so retrieval can return just the relevant section of a long note.

```markdown
---
title: High-energy dogs
species: dog
activity_level: high
tags: exercise, walks, enrichment
---

## How much exercise

...body text...
```

| Field | Purpose |
|---|---|
| `title` | Human-readable name, shown in citations |
| `species` | `dog`, `cat`, or blank for a note that applies to any pet |
| `activity_level` | `high`, `low`, or blank for any |
| `tags` | Comma-separated keywords, weighted heavily when scoring |

`species` and `activity_level` are scoring *boosts*, not filters — a general
note still surfaces for a specific pet if the text matches well.

## Adding a note

Drop a new `.md` file in this directory with the metadata block above. The
loader picks it up automatically; nothing needs registering. `tests/test_knowledge.py`
checks every note in here parses and carries the required fields, so a malformed
one fails the suite rather than silently going missing from retrieval.
