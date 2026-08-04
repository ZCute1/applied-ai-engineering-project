"""Tests for PawPal+ retrieval over the care knowledge base (pawpal_knowledge.py).

Retrieval is lexical and reads plain files, so all of this runs offline with no
API key — and because scoring is deterministic, the tests can assert on exact
rankings rather than just "something came back".

Organized by what's under test:

- Loading      -- notes parse into chunks, metadata and all.
- Corpus       -- every real note in knowledge/ is well-formed (data integrity).
- Stemming     -- the tokenizer bridges query and note vocabulary.
- Ranking      -- the right note wins, for the right reasons.
- No match     -- an unanswerable query returns nothing, and says so.
- Formatting   -- output carries citable sources.

Retrieval tests use a temporary corpus where the assertion is about *scoring
mechanics*, so they don't break every time a real note is reworded, and the real
knowledge/ directory where the assertion is about the corpus itself.
"""

import pytest

import pawpal_knowledge as kb
from pawpal_knowledge import (
    KNOWLEDGE_DIR,
    MIN_SCORE,
    format_results,
    load_chunks,
    lookup_care_guidance,
    search,
)

NOTE_DOG = """---
title: Dog exercise
species: dog
activity_level: high
tags: exercise, walks
---

## How much exercise

A high-energy dog needs a long walk every day.

## Split it up

Two shorter walks beat one long walk.
"""

NOTE_CAT = """---
title: Cat grooming
species: cat
activity_level:
tags: grooming, brushing
---

## How often

Brush a shorthaired cat weekly.
"""

NOTE_GENERAL = """---
title: Feeding
species:
activity_level:
tags: feeding, meals
---

## Same times daily

Feed at the same times each day.
"""


@pytest.fixture
def corpus(tmp_path):
    """A small three-note corpus on disk, with the loader cache cleared.

    load_chunks is lru_cached for speed in the app, so the cache has to be
    dropped between tests or a later test would read an earlier one's files.
    """
    (tmp_path / "dog.md").write_text(NOTE_DOG)
    (tmp_path / "cat.md").write_text(NOTE_CAT)
    (tmp_path / "general.md").write_text(NOTE_GENERAL)
    (tmp_path / "README.md").write_text("# not a care note\n\n## Ignore me\n\nwalk walk\n")
    load_chunks.cache_clear()
    yield tmp_path
    load_chunks.cache_clear()


# ===========================================================================
# Loading and chunking
# ===========================================================================
class TestLoading:
    def test_notes_are_split_one_chunk_per_section(self, corpus):
        """Chunking is by '##' heading, so a long note isn't one blob."""
        chunks = load_chunks(corpus)

        dog_chunks = [c for c in chunks if c.source == "dog.md"]
        assert len(dog_chunks) == 2
        assert {c.heading for c in dog_chunks} == {"How much exercise", "Split it up"}

    def test_metadata_is_parsed_onto_every_chunk(self, corpus):
        """Each chunk carries its note's metadata, for scoring and citation."""
        chunk = next(c for c in load_chunks(corpus) if c.source == "dog.md")

        assert chunk.title == "Dog exercise"
        assert chunk.species == "dog"
        assert chunk.activity_level == "high"
        assert chunk.tags == ("exercise", "walks")

    def test_blank_metadata_field_means_applies_to_any_pet(self, corpus):
        """An empty species is 'general', not the literal string 'none'."""
        chunk = next(c for c in load_chunks(corpus) if c.source == "general.md")

        assert chunk.species == ""
        assert chunk.activity_level == ""

    def test_readme_is_not_indexed(self, corpus):
        """The directory's own docs aren't care guidance."""
        assert all(c.source != "README.md" for c in load_chunks(corpus))

    def test_missing_directory_returns_nothing(self, tmp_path):
        """A missing corpus is empty, not an exception."""
        load_chunks.cache_clear()
        assert load_chunks(tmp_path / "nope") == ()

    def test_citation_names_the_file_and_section(self, corpus):
        """Citations have to be specific enough to look up by hand."""
        chunk = next(c for c in load_chunks(corpus) if c.heading == "Split it up")

        assert chunk.citation == "dog.md > Split it up"


# ===========================================================================
# The real corpus (data integrity)
# ===========================================================================
class TestRealCorpus:
    def test_every_note_parses_into_chunks(self):
        """A malformed note fails here rather than silently vanishing."""
        load_chunks.cache_clear()
        chunks = load_chunks(KNOWLEDGE_DIR)

        notes = {c.source for c in chunks}
        markdown = {
            p.name for p in KNOWLEDGE_DIR.glob("*.md") if p.name.lower() != "readme.md"
        }
        assert notes == markdown, "a note produced no chunks — check its ## headings"
        assert len(chunks) > len(notes), "expected notes to split into sections"

    def test_every_chunk_has_a_title_heading_and_tags(self):
        """Retrieval leans on all three, so a note missing them scores badly."""
        load_chunks.cache_clear()

        for chunk in load_chunks(KNOWLEDGE_DIR):
            assert chunk.title, f"{chunk.source} has no title"
            assert chunk.heading, f"{chunk.source} has a section with no heading"
            assert chunk.tags, f"{chunk.source} has no tags"
            assert chunk.text.strip(), f"{chunk.citation} has no body"

    def test_species_values_are_recognized(self):
        """A typo like 'dogs' would silently stop the species boost working."""
        load_chunks.cache_clear()

        for chunk in load_chunks(KNOWLEDGE_DIR):
            assert chunk.species in ("dog", "cat", ""), chunk.source
            assert chunk.activity_level in ("high", "low", ""), chunk.source

    @pytest.mark.parametrize(
        "query, species, expected_source",
        [
            ("how much exercise does my dog need", "dog", "dog_high_energy.md"),
            ("how often should I brush my cat", "cat", "cat_grooming.md"),
            ("when should I feed my pets", "", "feeding_schedules.md"),
            ("shiba inu off leash recall", "dog", "breed_shiba_inu.md"),
            ("walking in hot summer weather", "dog", "hot_weather_walks.md"),
            ("playtime for an indoor cat", "cat", "cat_indoor_enrichment.md"),
            ("giving daily medication", "", "medication_routines.md"),
            ("how often to feed a puppy", "dog", "puppies_and_kittens.md"),
        ],
    )
    def test_real_queries_find_the_right_note(self, query, species, expected_source):
        """End-to-end relevance over the actual corpus.

        The top hit has to come from the expected note. "when should I feed my
        pets" is the case that drove the stemmer: the query says "feed", the note
        says "feeding", and before stemming this query matched a stray sentence
        about cats "picking fights with other pets" instead.
        """
        load_chunks.cache_clear()
        hits = search(query, species=species, directory=KNOWLEDGE_DIR)

        assert hits, f"no hits for {query!r}"
        assert hits[0].chunk.source == expected_source


# ===========================================================================
# Stemming — bridging query and note vocabulary
# ===========================================================================
class TestStemming:
    @pytest.mark.parametrize(
        "word, other",
        [
            ("feed", "feeding"),
            ("walk", "walking"),
            ("walk", "walks"),
            ("groom", "grooming"),
            ("schedule", "scheduled"),
            ("session", "sessions"),
            ("exercise", "exercises"),
        ],
    )
    def test_related_words_share_a_stem(self, word, other):
        """These pairs have to collide, or the right note never surfaces."""
        assert kb._stem(word) == kb._stem(other)

    def test_doubled_consonant_is_undone(self):
        """'running' -> 'runn' -> 'run', not a stem nothing else reaches."""
        assert kb._stem("running") == kb._stem("run")

    def test_short_words_are_left_alone(self):
        """Aggressive stemming on short words destroys them."""
        assert kb._stem("cat") == "cat"
        assert kb._stem("dog") == "dog"

    def test_stopwords_are_dropped_after_stemming(self):
        """'needs' stems to 'need', which is a stopword — it must not survive.

        Stopwords are matched post-stemming for exactly this reason; checking
        them first would let inflected stopwords through.
        """
        assert kb._tokenize("what does my dog need") == ["dog"]
        assert kb._tokenize("what my dog needs") == ["dog"]


# ===========================================================================
# Ranking
# ===========================================================================
class TestRanking:
    def test_tag_and_heading_hits_outrank_body_hits(self, corpus):
        """A note *about* walks beats one that merely mentions the word."""
        hits = search("walks", directory=corpus)

        assert hits[0].chunk.source == "dog.md"  # 'walks' is one of its tags

    def test_species_boost_prefers_the_right_animal(self, corpus):
        """Same query, different pet, different winner."""
        load_chunks.cache_clear()
        for_cat = search("how often", species="cat", directory=corpus)

        assert for_cat[0].chunk.source == "cat.md"

    def test_wrong_species_notes_are_excluded(self, corpus):
        """A cat note answering a dog question is worse than no answer."""
        hits = search("brushing", species="dog", directory=corpus)

        assert all(h.chunk.source != "cat.md" for h in hits)

    def test_general_notes_still_surface_for_a_specific_pet(self, corpus):
        """Feeding applies to any animal, so a species filter mustn't hide it."""
        hits = search("feeding times", species="dog", directory=corpus)

        assert any(h.chunk.source == "general.md" for h in hits)

    def test_activity_level_boost_applies(self, corpus):
        """Matching activity level lifts a chunk above the same chunk without it."""
        load_chunks.cache_clear()
        with_match = search("exercise", species="dog", activity_level="high", directory=corpus)
        without = search("exercise", species="dog", directory=corpus)

        assert with_match[0].score > without[0].score

    def test_max_results_is_respected(self, corpus):
        """The agent controls how much context it pulls in."""
        assert len(search("walk", max_results=1, directory=corpus)) == 1

    def test_ranking_is_deterministic(self, corpus):
        """Same query, same order, every time — what makes these tests possible."""
        first = [r.chunk.citation for r in search("walk exercise", directory=corpus)]
        second = [r.chunk.citation for r in search("walk exercise", directory=corpus)]

        assert first == second

    def test_metadata_alone_is_not_a_hit(self, corpus):
        """A species match with no word overlap must not drag a note in.

        Otherwise any question about a dog would return every dog note in the
        corpus regardless of what was asked.
        """
        hits = search("thunderstorm anxiety", species="dog", directory=corpus)

        assert hits == []


# ===========================================================================
# No match
# ===========================================================================
class TestNoMatch:
    def test_unrelated_query_returns_nothing(self, corpus):
        """The corpus is small; admitting a miss beats inventing a hit."""
        assert search("quantum chromodynamics", directory=corpus) == []

    def test_empty_query_returns_nothing(self, corpus):
        """No query, no evidence."""
        assert search("", directory=corpus) == []

    def test_single_passing_mention_is_below_the_floor(self, corpus):
        """One incidental body word isn't guidance — that's what MIN_SCORE is for."""
        assert MIN_SCORE > kb.BODY_WEIGHT

    def test_no_match_output_tells_the_model_not_to_guess(self):
        """The empty-result string is a guardrail, so pin its intent."""
        message = format_results([])

        assert "no care notes matched" in message.lower()
        assert "guess" in message.lower()


# ===========================================================================
# Formatting for the model
# ===========================================================================
class TestFormatting:
    def test_output_cites_source_filenames(self, corpus):
        """The agent can only cite sources if the tool result carries them."""
        output = format_results(search("walks", directory=corpus))

        assert "dog.md" in output
        assert "Dog exercise" in output

    def test_output_includes_the_body_text(self, corpus):
        """Retrieval has to return the actual content, not just references."""
        output = format_results(search("brushing", species="cat", directory=corpus))

        assert "shorthaired cat weekly" in output

    def test_long_chunks_are_truncated(self, tmp_path):
        """One long section can't be allowed to fill the context window."""
        body = "walk " * 500
        (tmp_path / "long.md").write_text(
            f"---\ntitle: Long\nspecies: dog\ntags: walks\n---\n\n## Walking\n\n{body}\n"
        )
        load_chunks.cache_clear()

        output = format_results(search("walking", directory=tmp_path))

        assert "…" in output
        assert len(output) < len(body)
        load_chunks.cache_clear()

    def test_lookup_care_guidance_returns_a_formatted_string(self):
        """The agent's tool entry point: search plus render in one call."""
        load_chunks.cache_clear()
        output = lookup_care_guidance("how much exercise", species="dog", activity_level="high")

        assert "dog_high_energy.md" in output
        assert "cite the source" in output.lower()
