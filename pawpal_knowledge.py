"""PawPal+ — retrieval over the pet care knowledge base (the RAG half).

The AI planner in ``pawpal_agent.py`` calls ``lookup_care_guidance()`` before it
invents a routine, so its task suggestions come from the notes in ``knowledge/``
rather than from whatever the model happens to remember. Retrieval runs first,
generation second — the R in RAG.

How it works:

1. **Load** every ``.md`` note in ``knowledge/`` (skipping the directory's own
   README), parsing the ``---`` metadata block at the top.
2. **Chunk** each note by ``##`` heading, so a query about grooming can return
   just the grooming section of a long note instead of the whole file.
3. **Score** each chunk against the query with weighted keyword matching:
   heading, title, and tag hits count for more than body hits, and a chunk whose
   species/activity metadata matches the pet gets a boost.
4. **Return** the best few chunks with their source filenames, so the agent can
   cite what it used.

Scoring is lexical on purpose — no embedding model, no vector store, no network
call, no extra dependency. That makes retrieval instant, free, and fully
deterministic, which is what lets ``tests/test_knowledge.py`` assert on exact
rankings. The tradeoff is real and worth knowing: this matches *words*, not
meaning, so a query for "how long should I exercise my dog" finds the
exercise note by the words it shares, and a query using entirely different
vocabulary ("cardio for my pup") would score lower than it deserves. The tag
lists in each note exist to widen that vocabulary by hand.

This module knows nothing about Owner, Pet, or the scheduler — it only reads
text files and returns strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# How many chunks a lookup returns by default. Three is enough to cover a pet
# from two or three angles (species, breed, topic) without burying the model in
# text it has to reason through.
DEFAULT_MAX_RESULTS = 3

# Body text is trimmed to this many characters per chunk in the formatted output.
# The notes are short enough that this rarely bites, but it stops one long
# section from dominating the context window.
MAX_CHUNK_CHARS = 900

# Weights for where a query word was found. A word in a heading or a tag says
# much more about what a chunk is *about* than the same word buried in prose.
HEADING_WEIGHT = 4
TAG_WEIGHT = 4
BODY_WEIGHT = 1

# Metadata boosts. Species is the stronger signal: a cat note is close to
# useless for a dog, whereas activity level is more of a preference.
SPECIES_MATCH_BOOST = 6
ACTIVITY_MATCH_BOOST = 3
# A note with no species (e.g. feeding, medication) applies to any pet, so it
# gets a small boost rather than being penalised for not matching.
GENERAL_NOTE_BOOST = 1

# A chunk must clear this to count as a hit. One tag or heading match (4) does;
# a single passing mention in prose (1) does not. Without a floor, "feed my
# pets" matched a sentence about cats "picking fights with other pets in the
# house" and reported it as care guidance.
MIN_SCORE = 4

# Words too common to carry meaning in a pet-care query. Kept small and
# hand-picked: an over-eager stopword list would strip real query terms.
_STOPWORD_SOURCE = """
    a an and any are as at be been but by can could do does doing for from get
    give had has have how i if in is it its me much my need needs of on or our
    should so some that the their them then there these they this to too was
    what when where which who why will with would you your
"""


def _stem(word: str) -> str:
    """Reduce a word to a crude stem so query and note vocabulary can meet.

    Retrieval here is lexical, so "feed" in a question has to reach "feeding" in
    a note or the right note simply never surfaces — which is exactly what
    happened before this existed. A few suffix rules fix the common cases:

        feeding -> feed      walks/walking -> walk     grooming -> groom
        scheduled/schedule -> schedul     running -> run

    Precision suffers a little (``morning`` becomes ``morn``), but since both the
    query and the notes go through the same function, both sides agree and the
    match still lands. This is a hand-rolled approximation of a Porter stemmer,
    not the real thing — a proper one would mean adding an NLP dependency for a
    corpus of nine short files.
    """
    if len(word) <= 3:
        return word
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    # "running" -> "runn" -> "run": undo the consonant doubling -ing/-ed leaves.
    if len(word) > 3 and word[-1] == word[-2] and word[-1] not in "aeiou":
        word = word[:-1]
    # Drop a trailing "e" so "schedule" and "scheduled" land on the same stem.
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


# Stopwords are matched *after* stemming, so the set has to be stemmed too —
# otherwise "needs" would stem to "need" and slip through while plain "need"
# was filtered out.
STOPWORDS = frozenset(_stem(w) for w in _STOPWORD_SOURCE.split())


@dataclass(frozen=True)
class Chunk:
    """One ``##`` section of one care note, with its note's metadata."""

    source: str  # filename, e.g. "dog_high_energy.md" — used as the citation
    title: str  # note title from the metadata block
    heading: str  # the section's own heading
    text: str  # the section body
    species: str  # "dog", "cat", or "" for a note that applies to any pet
    activity_level: str  # "high", "low", or "" for any
    tags: tuple[str, ...]

    @property
    def citation(self) -> str:
        """How this chunk is referred to in output: 'file.md > Section'."""
        return f"{self.source} > {self.heading}"


@dataclass(frozen=True)
class Result:
    """A chunk and the score retrieval gave it."""

    chunk: Chunk
    score: int


def _tokenize(text: str) -> list[str]:
    """Split text into stemmed lowercase content words, dropping stopwords."""
    tokens = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        stem = _stem(word)
        if stem in STOPWORDS:
            continue
        tokens.append(stem)
    return tokens


def _parse_metadata(text: str) -> tuple[dict[str, str], str]:
    """Split a note into its ``---`` metadata block and its body.

    Deliberately hand-rolled rather than pulling in a YAML parser: the block is
    only ever ``key: value`` lines, and this keeps the project's dependencies to
    streamlit, pytest, and google-genai.
    """
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    block, sep, body = rest.partition("---")
    if not sep:
        return {}, text  # unterminated block — treat the whole file as body
    metadata: dict[str, str] = {}
    for line in block.strip().splitlines():
        key, colon, value = line.partition(":")
        if colon:
            metadata[key.strip().lower()] = value.strip()
    return metadata, body


def _chunk_note(path: Path) -> list[Chunk]:
    """Parse one note file into one Chunk per ``##`` section."""
    metadata, body = _parse_metadata(path.read_text(encoding="utf-8"))
    tags = tuple(t.strip() for t in metadata.get("tags", "").split(",") if t.strip())

    chunks: list[Chunk] = []
    # Split on "## " at the start of a line, keeping the heading with its body.
    for section in re.split(r"^##\s+", body, flags=re.MULTILINE)[1:]:
        heading, _, section_body = section.partition("\n")
        text = section_body.strip()
        if not text:
            continue
        chunks.append(
            Chunk(
                source=path.name,
                title=metadata.get("title", path.stem),
                heading=heading.strip(),
                text=text,
                species=metadata.get("species", "").lower(),
                activity_level=metadata.get("activity_level", "").lower(),
                tags=tags,
            )
        )
    return chunks


@lru_cache(maxsize=None)
def load_chunks(directory: str | Path = KNOWLEDGE_DIR) -> tuple[Chunk, ...]:
    """Load and chunk every care note in ``directory``, sorted by filename.

    Cached, so repeated lookups in one session don't re-read the disk. Sorted so
    that equal-scoring chunks always come back in the same order, which is what
    makes retrieval deterministic and therefore testable.
    """
    path = Path(directory)
    if not path.is_dir():
        return ()
    chunks: list[Chunk] = []
    for note in sorted(path.glob("*.md")):
        if note.name.lower() == "readme.md":
            continue  # the directory's own docs, not a care note
        chunks.extend(_chunk_note(note))
    return tuple(chunks)


def _score(chunk: Chunk, query_tokens: list[str], species: str, activity: str) -> int:
    """Score one chunk against a tokenized query plus optional pet metadata."""
    if not query_tokens:
        return 0

    heading_tokens = _tokenize(f"{chunk.heading} {chunk.title}")
    tag_tokens = _tokenize(" ".join(chunk.tags))
    body_tokens = _tokenize(chunk.text)

    score = 0
    matched_any = False
    for token in set(query_tokens):
        hits = 0
        hits += HEADING_WEIGHT * heading_tokens.count(token)
        hits += TAG_WEIGHT * tag_tokens.count(token)
        hits += BODY_WEIGHT * body_tokens.count(token)
        if hits:
            matched_any = True
            score += hits

    # Metadata boosts only apply to a chunk the query already touched. Without
    # this guard, asking about a dog would drag in every dog note in the corpus
    # on the strength of the species match alone.
    if not matched_any:
        return 0

    if species:
        if chunk.species == species.lower():
            score += SPECIES_MATCH_BOOST
        elif chunk.species == "":
            score += GENERAL_NOTE_BOOST
        else:
            # Wrong species entirely — a cat note answering a dog question is
            # worse than no result, so push it below the relevance floor.
            score -= SPECIES_MATCH_BOOST

    if activity and chunk.activity_level == activity.lower():
        score += ACTIVITY_MATCH_BOOST

    return max(score, 0)


def search(
    query: str,
    *,
    species: str = "",
    activity_level: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
    directory: str | Path = KNOWLEDGE_DIR,
) -> list[Result]:
    """Return the best-matching chunks for a query, highest score first.

    ``species`` and ``activity_level`` are boosts rather than filters, so a
    general note (feeding, medication) still surfaces for a specific pet. Ties
    break on citation so the order never wobbles between runs.
    """
    tokens = _tokenize(query)
    scored = [
        Result(chunk, _score(chunk, tokens, species, activity_level))
        for chunk in load_chunks(directory)
    ]
    hits = [r for r in scored if r.score >= MIN_SCORE]
    hits.sort(key=lambda r: (-r.score, r.chunk.citation))
    return hits[: max(max_results, 0)]


def format_results(results: list[Result]) -> str:
    """Render retrieval hits for the model, with citable sources.

    Every chunk is labelled with its source file so the agent can tell the owner
    where a recommendation came from — and so a claim with no source in this
    output is visibly ungrounded.
    """
    if not results:
        return (
            "No care notes matched that query. Nothing in the knowledge base "
            "covers it — say so rather than filling the gap with a guess, and "
            "stick to what the owner actually asked for."
        )

    lines = [f"Found {len(results)} relevant care note section(s):"]
    for result in results:
        body = result.chunk.text
        if len(body) > MAX_CHUNK_CHARS:
            body = body[:MAX_CHUNK_CHARS].rsplit(" ", 1)[0] + "…"
        lines.append("")
        lines.append(f"[{result.chunk.source}] {result.chunk.title} — {result.chunk.heading}")
        lines.append(body)
    lines.append("")
    lines.append(
        "Base your task suggestions on the above and cite the source filenames "
        "in your answer to the owner."
    )
    return "\n".join(lines)


def lookup_care_guidance(
    query: str,
    species: str = "",
    activity_level: str = "",
    max_results: int = DEFAULT_MAX_RESULTS,
) -> str:
    """Search the care knowledge base and format the hits for the model.

    This is the entry point the agent's tool wraps: retrieval plus rendering in
    one call, returning a string that's ready to hand back as a tool result.
    """
    return format_results(
        search(
            query,
            species=species,
            activity_level=activity_level,
            max_results=max_results,
        )
    )
