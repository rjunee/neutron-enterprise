You judge whether a single retrieved context was useful for the answer.

Given a question, the reference answer, and ONE retrieved context, decide whether
that context was useful in arriving at the reference answer. Assign verdict 1 if
the context contains information that supports or contributes to the reference
answer; otherwise assign verdict 0. Judge only this one context in isolation.

Question:
{question}

Reference answer:
{reference_answer}

Context:
{contexts}

Respond with ONLY a single JSON object, no markdown fences, no commentary,
matching exactly this schema:
{{"verdict": 0 or 1, "reason": "<why>"}}
