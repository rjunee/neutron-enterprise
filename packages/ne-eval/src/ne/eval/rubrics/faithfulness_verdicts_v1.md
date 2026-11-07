You judge whether each statement is supported by the retrieved contexts.

You are given the retrieved contexts and a JSON array of atomic statements. For
each statement, decide whether it is DIRECTLY supported by the contexts. Assign
verdict 1 if and only if the statement can be inferred from the contexts alone;
otherwise assign verdict 0. Do not use outside knowledge. Return exactly one
verdict per input statement, in the same order.

Contexts:
{contexts}

Statements (JSON array):
{statements}

Respond with ONLY a single JSON object, no markdown fences, no commentary,
matching exactly this schema (one entry per input statement):
{{"verdicts": [{{"statement": "<the statement>", "verdict": 0 or 1, "reason": "<why>"}}]}}
