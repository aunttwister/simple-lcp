"""Exemplars for the Arena routing label space — one authored set per board.

The classifier builds a centroid per label from example texts, so a board with no
exemplars can never be assigned. This module is what makes the 23 curated boards
(``board_labels``) a label space the judge can actually decide in.

How these were authored: four short, realistic user requests per board, written to be
unmistakably of that kind and unlike the neighbouring boards. `coding` gets code
requests; `industry_software_and_it_services` gets a business question *about* software
work; `agent_bash_reliability` gets a request that needs shell commands run reliably.
The distinctions that matter most are the ones a naive keyword rule would get wrong —
that is why `industry_*` boards describe a domain ("is this legal?") rather than an
activity ("write me a function").

Adding a board: add exemplars here AND make sure the board is in the curated set
(``board_labels``). ``tests/test_arena_routing.py`` fails if the two disagree, in either
direction, so a label can never exist without text to classify against.
"""
from __future__ import annotations

#: Stamped on every routing decision made in this label space, so a decision logged
#: under Arena is never confused with one logged under the task taxonomy.
BOARD_LABEL_SPACE_ID = "arena-boards-v0"

#: board -> example user requests. Every board here must be in `board_labels`'s curated
#: set (all published boards minus meta and language boards).
BOARD_EXEMPLARS: dict[str, list[str]] = {
    "coding": [
        "write a Python function that merges two sorted lists",
        "implement a retry decorator with exponential backoff",
        "refactor this class to use dependency injection",
        "fix the off-by-one error in this loop",
    ],
    "math": [
        "solve this integral step by step",
        "what is the probability of drawing two aces",
        "prove that the square root of two is irrational",
        "compute the derivative of this function",
    ],
    "creative_writing": [
        "write a short story about a lighthouse keeper",
        "give me a poem about the sea at night",
        "continue this scene in the style of a noir novel",
        "rewrite this paragraph to be more evocative",
    ],
    "expert": [
        "explain the tradeoffs between B-trees and LSM trees for writes",
        "walk me through the physics of orbital transfer windows",
        "what are the failure modes of distributed consensus under partition",
        "summarise the current understanding of protein folding",
    ],
    "hard_prompts": [
        "answer this multi-part question where each part depends on the last",
        "solve this logic puzzle with the constraints given",
        "explain this dense technical passage and what it implies",
        "reason carefully about which of these claims is inconsistent",
    ],
    "hard_prompts_english": [
        "answer these three questions in one response in English",
        "read this English passage and answer the questions about it",
        "explain this difficult English text in plain English",
        "reason about this English riddle and give the answer",
    ],
    "instruction_following": [
        "reply in exactly three bullet points, no preamble",
        "give the answer as a JSON object with these exact keys",
        "answer in one sentence and do not use the word cloud",
        "format the output as a markdown table with two columns",
    ],
    "multi_turn": [
        "as I mentioned earlier, continue from where we left off",
        "now do the same for the second case we discussed",
        "building on your previous answer, expand the third point",
        "remember what I told you before and keep that style",
    ],
    "agent": [
        "book the earliest train and email me the confirmation",
        "find the file, update the config, and restart the service",
        "look up the price and then place the order for me",
        "check the logs, diagnose the error, and apply the fix",
    ],
    "agent_steerability": [
        "stop what you are doing and switch to the other approach",
        "ignore the previous plan and follow these new instructions instead",
        "change the target directory and redo the last step",
        "halt the task and wait for my next instruction",
    ],
    "agent_bash_reliability": [
        "run these shell commands and make sure each succeeds",
        "execute the build script and report any failing command",
        "install the package and verify it is importable",
        "run the test suite and tell me which tests failed",
    ],
    "agent_bash_recovery_steps": [
        "the previous command failed, work out why and recover",
        "retry the failed step after fixing the cause",
        "that command errored, diagnose it and continue from there",
        "recover from the broken state and finish the remaining steps",
    ],
    "agent_tool_hallucination": [
        "use the tools that actually exist, do not invent any",
        "only call functions you have been given access to",
        "do not make up command line flags that are not real",
        "verify the tool exists before you claim to have used it",
    ],
    "agent_task_outcome_explicit": [
        "carry out the task and tell me whether it succeeded or failed",
        "state clearly and explicitly what the final outcome was",
        "confirm whether each step completed successfully",
        "give me a definite success or failure, not a summary",
    ],
    "agent_praise_complaint": [
        "that was wrong, here is what you did badly",
        "great job on the last step, please continue",
        "you made a mistake, do it again properly",
        "thanks, that worked, now do the next one",
    ],
    "industry_business_and_management_and_financial_operations": [
        "what is a reasonable gross margin for a small SaaS business",
        "explain how revenue recognition works for annual subscriptions",
        "draft a summary of this quarterly operations report",
        "what does a strong current ratio tell an investor",
    ],
    "industry_entertainment_and_sports_and_media": [
        "write a press release announcing the season schedule",
        "summarise this match report for a fan newsletter",
        "explain how film distribution windows work",
        "draft social copy for a new album release",
    ],
    "industry_legal_and_government": [
        "explain what this clause in the contract means",
        "summarise the notice requirements in this regulation",
        "what does this statute say about notice periods",
        "draft a response to this formal complaint letter",
    ],
    "industry_life_and_physical_and_social_science": [
        "explain how this chemical reaction proceeds",
        "summarise the findings of this biology paper",
        "what does the research say about sleep and memory",
        "explain the geology of this region",
    ],
    "industry_mathematical": [
        "work out the optimisation for this scheduling problem",
        "prove this inequality for all positive integers",
        "model this queuing system and give the waiting time",
        "solve this linear system and show the working",
    ],
    "industry_medicine_and_healthcare": [
        "explain what this blood test result indicates",
        "summarise the recommended treatment pathway for this condition",
        "what are the contraindications listed for this medication",
        "explain this clinical trial design",
    ],
    "industry_software_and_it_services": [
        "what should the SLA be for a small internal service",
        "summarise this incident postmortem for management",
        "explain the difference between an incident and a problem",
        "draft a migration plan for moving this workload to the cloud",
    ],
    "industry_writing_and_literature_and_language": [
        "explain the arguments in this philosophical essay",
        "summarise the plot and themes of this novel",
        "what does this passage mean in its historical context",
        "edit this text for clarity and tone",
    ],
}
