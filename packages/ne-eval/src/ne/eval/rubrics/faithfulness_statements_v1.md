You decompose an answer into atomic factual statements.

Given a question and an answer, break the answer into a list of standalone,
atomic factual claims. Each statement must be self-contained (resolve pronouns
and references) and assert exactly one fact. Do not add facts that are not in
the answer. If the answer contains no verifiable factual claims, return an empty
list.

Question:
{question}

Answer:
{answer}

Respond with ONLY a single JSON object, no markdown fences, no commentary,
matching exactly this schema:
{{"statements": ["<atomic statement>", "..."]}}
