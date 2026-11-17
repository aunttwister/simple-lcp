"""The board label space — what an Arena-routed message can be classified as.

Operator decision, 2026-11-17: "Board names for Arena routing; task names for Intent
routing" (design doc §15 Q1). The judge is the same component as ever; only the label
space differs. This module is that label space.

**Why a curated subset and not all 36 boards.** LMArena publishes 36 boards, and three
kinds of them cannot be a label space:

* **meta boards** — `overall`, `exclude_ties`, `longer_query`, `non_english`. These are
  artefacts of how the poll computes standings, not things a request can be *about*.
  "Route to whatever judges `exclude_ties`" is meaningless.
* **language boards** — `english`, `chinese`, … `spanish`. As a label these mean "route
  by the language the message is written in", which is a product decision nobody made.
  A German question about Python should go to the best coding model, not to the best
  German model.
* **industry boards** are kept — `industry_medicine_and_healthcare` and friends describe
  a genuine domain a request can belong to.

The remaining boards are the label space, and each one needs exemplars: the classifier
builds a centroid per label from example texts, so a board with no exemplars can never
be assigned. `board_exemplars.BOARD_EXEMPLARS` is the authored set, and
`tests/test_arena_routing.py` pins that the two agree exactly.

Nothing here removes a board from the leaderboard — this is the *routing* vocabulary
only. The leaderboard still shows every board the poll found.
"""
from __future__ import annotations

#: Boards that describe the poll's own methodology, not a kind of request.
META_BOARDS = frozenset({
    "overall",
    "exclude_ties",
    "longer_query",
    "non_english",
})

#: Boards whose label would be "the language of this message" — not an intent.
LANGUAGE_BOARDS = frozenset({
    "english", "chinese", "french", "german", "japanese",
    "korean", "polish", "russian", "spanish",
})

#: Anything describing a methodology or a language is excluded from routing labels.
NON_LABEL_BOARDS = META_BOARDS | LANGUAGE_BOARDS
