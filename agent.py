"""
Harnyx SN67 research miner — evidence-contract architecture.

Designed for the current harnyx-miner-sdk contract:
- one async @entrypoint("query")
- search_web + llm_chat only
- receipt-backed citations
- plain-text and structured-output queries
- adaptive research planning
- explicit evidence ledger
- budget-aware stopping
- provider/model fallbacks

IMPORTANT:
This is a strong candidate, not a guaranteed champion. Run Harnyx local-eval
against a pinned batch and iterate based on failures before mainnet submission.
"""

from __future__ import annotations

import json
import re
from typing import Any

from harnyx_miner_sdk.api import llm_chat, search_web
from harnyx_miner_sdk.decorators import entrypoint
from harnyx_miner_sdk.query import CitationRef, Query, Response


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Search providers are attempted in this order. Configure at least one matching
# credential with harnyx-miner-config / local .env.
SEARCH_PROVIDERS = ("parallel", "desearch")

# Current allowed Chutes models in the Harnyx repository include these.
# The list is ordered for quality first, with fallbacks for availability.
PLANNER_MODELS = (
    "Qwen/Qwen3.6-27B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "moonshotai/Kimi-K2.6-TEE",
)

SYNTHESIS_MODELS = (
    "moonshotai/Kimi-K2.6-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

MAX_SEARCH_QUERIES = 5
MAX_EVIDENCE_ITEMS = 12
MAX_NOTE_CHARS = 1800
MIN_EVIDENCE_ITEMS = 4
SEARCH_RESULTS_PER_QUERY = 5

# If the last successful tool call reports less than this remaining budget,
# avoid optional extra retrieval. This is intentionally conservative.
LOW_BUDGET_USD = 0.015


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parser for model outputs that should contain one JSON object."""
    if not text:
        return None

    cleaned = text.strip()

    # Remove a single markdown fence if the model ignored the instruction.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    # Fallback: locate the first balanced JSON object.
    start = cleaned.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = cleaned[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    return None
    return None


def _dedupe_strings(values: list[str], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _authority_score(url: str | None, title: str | None, note: str | None) -> float:
    """
    Lightweight source-ranking prior.

    This does NOT declare a source truthful. It only prefers sources that are
    often more useful for factual research when all else is equal.
    """
    u = (url or "").lower()
    t = (title or "").lower()
    n = (note or "").strip()

    score = min(len(n) / 1200.0, 1.2)

    strong_tokens = (
        ".gov/",
        ".gov.",
        ".edu/",
        "who.int",
        "worldbank.org",
        "oecd.org",
        "sec.gov",
        "europa.eu",
        "un.org",
        "nih.gov",
        "pubmed",
        "arxiv.org",
        "github.com",
        "docs.",
    )
    if any(token in u for token in strong_tokens):
        score += 1.0

    if any(word in t for word in ("official", "documentation", "report", "paper", "study")):
        score += 0.35

    # Mildly demote obvious low-information pages.
    if len(n) < 120:
        score -= 0.5

    return score


def _budget_remaining(tool_result: Any) -> float | None:
    try:
        return float(tool_result.budget.session_remaining_budget_usd)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Hosted-tool wrappers with graceful fallbacks
# ---------------------------------------------------------------------------

async def _llm(
    *,
    models: tuple[str, ...],
    messages: list[dict[str, str]],
    max_output_tokens: int,
    temperature: float = 0.0,
    timeout: float = 55.0,
):
    last_error: Exception | None = None
    for model in models:
        try:
            return await llm_chat(
                provider="chutes",
                model=model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("No LLM model configured.")


async def _search(queries: list[str]):
    last_error: Exception | None = None

    for provider in SEARCH_PROVIDERS:
        try:
            if provider == "parallel":
                # Keep Parallel-specific options explicit. Harnyx's submitted-script
                # validator rejects expanded keyword arguments such as **kwargs.
                return await search_web(
                    queries,
                    provider=provider,
                    num=SEARCH_RESULTS_PER_QUERY,
                    timeout=35.0,
                    provider_extra={
                        "mode": "basic",
                        "max_chars_total": 18000,
                    },
                )

            return await search_web(
                queries,
                provider=provider,
                num=SEARCH_RESULTS_PER_QUERY,
                timeout=35.0,
            )
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise RuntimeError("No search provider configured.")


# ---------------------------------------------------------------------------
# Research planning
# ---------------------------------------------------------------------------

def _fallback_plan(question: str) -> dict[str, Any]:
    q = _compact(question, 500)
    return {
        "question_type": "research",
        "currentness": False,
        "required_facts": [
            "Direct answer to the user's question",
            "Key evidence needed to support the answer",
        ],
        "search_queries": [
            q,
            f"{q} official source",
            f"{q} evidence report",
        ],
        "answer_instructions": "Answer directly, distinguish verified facts from inference, and avoid unsupported claims.",
    }


async def _make_plan(question: str) -> tuple[dict[str, Any], float | None]:
    system = """You are the planning stage of a competitive deep-research agent.

Your job is NOT to answer the question. Create the smallest research plan that
will reliably support a high-quality answer under a tight budget.

Return ONLY a JSON object with exactly these keys:
{
  "question_type": "factual|current|comparison|explanation|multi_hop|synthesis",
  "currentness": true|false,
  "required_facts": ["fact/field needed", ...],
  "search_queries": ["targeted web query", ...],
  "answer_instructions": "brief instruction"
}

Rules:
- Produce 2-5 search queries.
- Decompose multi-part questions into answer-required fields.
- For current/status questions, include the current year or a recency-aware query.
- Prefer queries likely to surface primary/official sources where relevant.
- Include queries that can detect a false premise, conflicting evidence, or scope ambiguity.
- Do not broaden search after the required answer fields are covered.
- Do not include an answer or fabricate facts.
"""

    user = f"Question:\n{question}"

    try:
        result = await _llm(
            models=PLANNER_MODELS,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_output_tokens=700,
            temperature=0.0,
            timeout=45.0,
        )
        raw = result.llm.raw_text or ""
        plan = _extract_json_object(raw) or _fallback_plan(question)

        required_facts = [
            _compact(str(x), 220)
            for x in plan.get("required_facts", [])
            if str(x).strip()
        ]
        queries = [
            _compact(str(x), 300)
            for x in plan.get("search_queries", [])
            if str(x).strip()
        ]

        plan["required_facts"] = _dedupe_strings(required_facts, 8)
        plan["search_queries"] = _dedupe_strings(queries, MAX_SEARCH_QUERIES)

        if not plan["search_queries"]:
            plan["search_queries"] = _fallback_plan(question)["search_queries"]

        return plan, _budget_remaining(result)
    except Exception:
        return _fallback_plan(question), None


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------

def _build_ledger(search_calls: list[Any]) -> tuple[list[dict[str, Any]], dict[str, CitationRef]]:
    candidates: list[dict[str, Any]] = []
    citation_map: dict[str, CitationRef] = {}
    seen_urls: set[str] = set()

    for call in search_calls:
        receipt_id = getattr(call, "receipt_id", None)
        if not receipt_id:
            continue

        for item in getattr(call, "results", ()) or ():
            note = _compact(getattr(item, "note", "") or "", MAX_NOTE_CHARS)
            url = (getattr(item, "url", None) or "").strip()
            title = _compact(getattr(item, "title", "") or "", 240)
            result_id = getattr(item, "result_id", None)

            if not result_id or not note:
                continue

            dedupe_key = url.lower() if url else f"{title.lower()}::{note[:120].lower()}"
            if dedupe_key in seen_urls:
                continue
            seen_urls.add(dedupe_key)

            candidates.append(
                {
                    "receipt_id": receipt_id,
                    "result_id": result_id,
                    "url": url,
                    "title": title,
                    "note": note,
                    "score": _authority_score(url, title, note),
                }
            )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    chosen = candidates[:MAX_EVIDENCE_ITEMS]

    ledger: list[dict[str, Any]] = []
    for idx, item in enumerate(chosen, start=1):
        evidence_id = f"E{idx}"
        ledger.append(
            {
                "id": evidence_id,
                "title": item["title"],
                "url": item["url"],
                "note": item["note"],
            }
        )
        citation_map[evidence_id] = CitationRef(
            receipt_id=item["receipt_id"],
            result_id=item["result_id"],
        )

    return ledger, citation_map


def _ledger_text(ledger: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for item in ledger:
        blocks.append(
            f"[{item['id']}]\n"
            f"Title: {item['title'] or '(untitled)'}\n"
            f"URL: {item['url'] or '(not supplied)'}\n"
            f"Evidence: {item['note']}"
        )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------

def _plain_synthesis_prompt(
    question: str,
    plan: dict[str, Any],
    ledger: list[dict[str, Any]],
) -> list[dict[str, str]]:
    required = "\n".join(f"- {x}" for x in plan.get("required_facts", []))
    evidence = _ledger_text(ledger)

    system = """You are the synthesis stage of a competitive deep-research agent.

Answer the user's exact question using the supplied evidence ledger.

Quality rules:
1. Resolve the question directly; do not dump search snippets.
2. Cover every answer-required field that is actually supportable.
3. Treat evidence as claims to evaluate, not automatically true.
4. Prefer primary/official evidence when it matches the exact entity, date,
   jurisdiction, metric, and scope.
5. If sources conflict, say so and prefer the best-scoped evidence.
6. Correct false premises rather than accepting them.
7. Never invent missing facts. State uncertainty narrowly when necessary.
8. Distinguish verified fact from inference.
9. Use concise, natural prose unless the question requires detail.
10. Use ONLY evidence IDs from the ledger in `used_evidence`.

Return ONLY valid JSON:
{
  "answer": "final answer text",
  "used_evidence": ["E1", "E3"]
}

`used_evidence` must contain only sources that materially support claims in the
answer. Do not cite everything merely because it was retrieved.
"""

    user = f"""QUESTION
{question}

REQUIRED ANSWER FIELDS
{required or "- Directly answer the question"}

ANSWER-SPECIFIC INSTRUCTIONS
{plan.get("answer_instructions", "")}

EVIDENCE LEDGER
{evidence or "(No usable retrieved evidence. If the question cannot be safely answered, say what could not be verified.)"}
"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _structured_synthesis_prompt(
    question: str,
    plan: dict[str, Any],
    ledger: list[dict[str, Any]],
    schema: dict[str, Any],
) -> list[dict[str, str]]:
    required = "\n".join(f"- {x}" for x in plan.get("required_facts", []))
    evidence = _ledger_text(ledger)
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))

    system = """You are the structured synthesis stage of a competitive
deep-research agent.

Return ONLY valid JSON with this wrapper:
{
  "output": <JSON value that satisfies the caller schema>,
  "used_evidence": ["E1", "E2"]
}

Rules:
- `output` must satisfy the supplied JSON Schema as exactly as possible.
- Do not put citations inside `output`; citations are handled separately.
- Use only supplied evidence for non-obvious factual claims.
- Do not invent missing facts.
- Correct false premises when the schema permits it.
- `used_evidence` must list only evidence IDs materially used.
"""

    user = f"""QUESTION
{question}

REQUIRED ANSWER FIELDS
{required or "- Directly answer the question"}

CALLER JSON SCHEMA
{schema_text}

EVIDENCE LEDGER
{evidence or "(No usable retrieved evidence.)"}
"""

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


async def _synthesize(
    question: str,
    plan: dict[str, Any],
    ledger: list[dict[str, Any]],
    output_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str, float | None]:
    if output_schema is None:
        messages = _plain_synthesis_prompt(question, plan, ledger)
    else:
        messages = _structured_synthesis_prompt(question, plan, ledger, output_schema)

    result = await _llm(
        models=SYNTHESIS_MODELS,
        messages=messages,
        max_output_tokens=2200,
        temperature=0.0,
        timeout=60.0,
    )

    raw = (result.llm.raw_text or "").strip()
    parsed = _extract_json_object(raw)
    return parsed, raw, _budget_remaining(result)


def _citations_from_ids(
    used_ids: Any,
    citation_map: dict[str, CitationRef],
) -> list[CitationRef]:
    if not isinstance(used_ids, list):
        used_ids = []

    refs: list[CitationRef] = []
    seen: set[tuple[str, str]] = set()

    for raw_id in used_ids:
        evidence_id = str(raw_id).strip()
        ref = citation_map.get(evidence_id)
        if ref is None:
            continue
        key = (ref.receipt_id, ref.result_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)

    return refs[:12]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@entrypoint("query")
async def query(query: Query) -> Response:
    question = query.text.strip()

    # 1) Build an explicit answer contract before retrieval.
    plan, remaining_after_plan = await _make_plan(question)
    search_queries = plan.get("search_queries", [])[:MAX_SEARCH_QUERIES]

    # 2) Retrieve in one batched search call where possible.
    search_calls: list[Any] = []
    try:
        first_search = await _search(search_queries)
        search_calls.append(first_search)
        remaining_after_search = _budget_remaining(first_search)
    except Exception:
        remaining_after_search = remaining_after_plan

    ledger, citation_map = _build_ledger(search_calls)

    # 3) Targeted fallback retrieval only when evidence is clearly sparse and
    #    budget does not already look constrained.
    if (
        len(ledger) < MIN_EVIDENCE_ITEMS
        and (remaining_after_search is None or remaining_after_search > LOW_BUDGET_USD)
    ):
        fallback_queries = _dedupe_strings(
            [
                question,
                f"{question} official documentation",
                f"{question} primary source",
            ],
            3,
        )
        try:
            second_search = await _search(fallback_queries)
            search_calls.append(second_search)
            ledger, citation_map = _build_ledger(search_calls)
        except Exception:
            pass

    # 4) Evidence-bound synthesis.
    try:
        parsed, raw, _ = await _synthesize(
            question,
            plan,
            ledger,
            query.output_schema,
        )
    except Exception:
        parsed, raw = None, ""

    # 5) Plain-text response.
    if query.output_schema is None:
        if parsed and isinstance(parsed.get("answer"), str):
            answer = parsed["answer"].strip()
            refs = _citations_from_ids(parsed.get("used_evidence"), citation_map)
        else:
            # Fail gracefully rather than returning no answer.
            answer = raw.strip() if raw.strip() else (
                "I could not complete a sufficiently supported answer within the "
                "available research/tool budget."
            )
            refs = []

        # If the model produced a factual answer but failed to return its IDs,
        # attach only a very small set of top evidence rather than citation spam.
        if not refs and ledger and answer:
            refs = [
                citation_map[item["id"]]
                for item in ledger[:2]
                if item["id"] in citation_map
            ]

        return Response(text=answer[:80000], citations=refs or None)

    # 6) Structured response.
    if parsed and "output" in parsed:
        refs = _citations_from_ids(parsed.get("used_evidence"), citation_map)
        if not refs and ledger:
            refs = [
                citation_map[item["id"]]
                for item in ledger[:2]
                if item["id"] in citation_map
            ]
        return Response(output=parsed["output"], citations=refs or None)

    # Structured queries cannot legally fall back to Response.text.
    # Return a conservative JSON value rather than violating the Response mode.
    # The caller's schema remains the final validator of this value.
    schema = query.output_schema or {}
    schema_type = schema.get("type") if isinstance(schema, dict) else None

    if schema_type == "array":
        fallback_output: Any = []
    elif schema_type == "string":
        fallback_output = "Unable to produce a schema-valid researched answer."
    elif schema_type == "number":
        fallback_output = 0.0
    elif schema_type == "integer":
        fallback_output = 0
    elif schema_type == "boolean":
        fallback_output = False
    elif schema_type == "null":
        fallback_output = None
    else:
        fallback_output = {}

    refs = [
        citation_map[item["id"]]
        for item in ledger[:2]
        if item["id"] in citation_map
    ]
    return Response(output=fallback_output, citations=refs or None)
