You judge whether each reference-answer statement is attributable to the contexts.

You are given the retrieved contexts and a JSON array of atomic statements taken
from the reference answer. For each statement, decide whether it can be
attributed to the contexts — i.e. whether the contexts contain the information
needed to support that statement. Assign attributed 1 if and only if the
statement can be attributed to the contexts; otherwise assign attributed 0.
Return exactly one classification per input statement, in the same order.

Contexts:
{contexts}

Reference-answer statements (JSON array):
{statements}

Respond with ONLY a single JSON object, no markdown fences, no commentary,
matching exactly this schema (one entry per input statement):
{{"classifications": [{{"statement": "<the statement>", "attributed": 0 or 1, "reason": "<why>"}}]}}
