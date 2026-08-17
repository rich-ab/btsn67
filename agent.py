"""Harnyx SN67 miner — GroundedProof v5.

This candidate is built around a bounded evidence-compiler rather than a long
conversational tool loop.  It is designed from a real local-eval failure where
all core facts were found correctly, but the pairwise judge preferred the other
answer because it preserved source labels more faithfully, explained a subtle
count discrepancy, and finished more cleanly.

Controller topology:
    question -> deterministic contract map -> deterministic retrieval plan
             -> parallel search -> selective primary-page fetch
             -> proof-grid extraction -> quote-verification firewall
             -> verified-fact answer -> grounded answer compiler
             -> deterministic numeric/entity hallucination guard
             -> source-verbatim restoration -> schema adapter

Design goals:
- reserve final-answer time instead of researching until the wall clock expires;
- preserve exact names, marks, capitalization, units, dates and labels from source;
- answer every numbered/multipart sub-question in order;
- explain discrepancy/counterfactual questions when the evidence supports why;
- keep exact receipt-backed citation slices around decisive quotes;
- never return the user's question as a fallback answer;
- remain useful when one LLM/search/fetch stage fails.
"""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from harnyx_miner_sdk.api import fetch_page, llm_chat, search_web, tooling_info
from harnyx_miner_sdk.decorators import entrypoint
from harnyx_miner_sdk.query import CitationRef, CitationSlice, Query, Response


VERSION = "groundedproof-v5.0"

# ---------------------------------------------------------------------------
# Runtime policy
# ---------------------------------------------------------------------------

LLM_PROVIDER = "chutes"
SEARCH_PROVIDER = "desearch"

PLAN_MODELS = (
    "Qwen/Qwen3.6-27B-TEE",
    "zai-org/GLM-5.2-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
)
GRID_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)
WRITE_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "zai-org/GLM-5.2-TEE",
)
REVIEW_MODELS = (
    "Qwen/Qwen3.6-27B-TEE",
    "zai-org/GLM-5.2-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
SCHEMA_MODELS = (
    "Qwen/Qwen3.6-27B-TEE",
    "zai-org/GLM-5.2-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)

# A Harnyx evaluation can have a larger outer timeout, but this agent commits
# well before it.  The v3 local run used ~251s and left too little finalization
# margin; v4 targets materially less.
WALL_SECONDS = 168.0
REPAIR_LATEST_ELAPSED = 88.0
REVIEW_LATEST_ELAPSED = 132.0
FORCE_COMMIT_ELAPSED = 140.0

PLAN_TIMEOUT = 10.0
SEARCH_TIMEOUT = 13.0
FETCH_TIMEOUT = 15.0
GRID_TIMEOUT = 36.0
WRITE_TIMEOUT = 30.0
REVIEW_TIMEOUT = 18.0
SCHEMA_TIMEOUT = 18.0
EMERGENCY_TIMEOUT = 14.0

MAX_PLAN_QUERIES = 4
MAX_REPAIR_QUERIES = 1
SEARCH_RESULTS = 7
FETCH_CAP = 4
SEARCH_CONCURRENCY = 5
FETCH_CONCURRENCY = 5
MAX_SOURCES = 42
MAX_CITATIONS = 18
MAX_EVIDENCE_CHARS = 104_000
MAX_SOURCE_TEXT = 300_000
MAX_PACK_CHARS = 60_000
MAX_ANSWER_CHARS = 56_000
SEARCH_PREVIEW = 1800
FETCH_HEAD = 2600
FETCH_WINDOW = 5200
FETCH_WINDOW_COUNT = 3
QUOTE_CONTEXT = 620
MAX_SLICE_PER_SOURCE = 15_000
MIN_SLICE = 900

MIN_PLAN_USD = 0.010
MIN_GRID_USD = 0.020
MIN_WRITE_USD = 0.016
MIN_REVIEW_USD = 0.020
MIN_SCHEMA_USD = 0.010

_STATE: dict[str, Any] = {
    "remaining_usd": None,
    "allowed_models": (),
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _remember_budget(payload: Any) -> None:
    budget = getattr(payload, "budget", None)
    remaining = getattr(budget, "session_remaining_budget_usd", None)
    if isinstance(remaining, (int, float)):
        _STATE["remaining_usd"] = float(remaining)


def _money_left() -> float:
    value = _STATE.get("remaining_usd")
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _left(deadline: float) -> float:
    return max(0.0, deadline - monotonic())


def _elapsed(started: float) -> float:
    return max(0.0, monotonic() - started)


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _cap(text: str, limit: int) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 2)].rstrip() + " …"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _uniq_text(values: Any, limit: int, max_len: int = 500) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    lowered: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = _clean_space(value)
        if not item or len(item) > max_len:
            continue
        key = item.lower()
        if key in lowered:
            continue
        lowered.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for pos in range(start, len(raw)):
        ch = raw[pos]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start : pos + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _token_terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][A-Za-zÀ-ÖØ-öø-ÿ0-9_.:/+%-]*", text or "")
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "at",
        "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
        "that", "this", "these", "those", "which", "what", "who", "when", "where",
        "how", "using", "only", "official", "result", "results", "page", "pages",
    }
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = item.strip("._:/+%-")
        if len(value) < 3:
            continue
        key = value.lower()
        if key in stop or key in seen:
            continue
        seen.add(key)
        out.append(value)
        if len(out) >= 28:
            break
    return out


def _authority(url: str, title: str, named_hints: list[str]) -> int:
    host = _host(url)
    low_title = (title or "").lower()
    score = 0
    if host.endswith(".gov") or ".gov." in host:
        score += 12
    if host.endswith(".edu") or ".edu." in host:
        score += 8
    if any(piece in host for piece in ("sec.gov", "who.int", "un.org", "worldbank.org", "oecd.org")):
        score += 11
    if any(piece in host for piece in ("docs.", "developer.", "support.", "help.", "data.")):
        score += 5
    if any(word in low_title for word in ("official", "results", "filing", "report", "statistics", "database")):
        score += 4
    for hint in named_hints:
        h = re.sub(r"[^a-z0-9]", "", hint.lower())
        if not h:
            continue
        host_flat = re.sub(r"[^a-z0-9]", "", host)
        title_flat = re.sub(r"[^a-z0-9]", "", low_title)
        if h in host_flat or h in title_flat:
            score += 14
    return score


def _best_windows(text: str, focus: str, width: int = FETCH_WINDOW, count: int = FETCH_WINDOW_COUNT) -> list[tuple[int, int]]:
    if not text:
        return []
    if len(text) <= width:
        return [(0, len(text))]
    terms = [x.lower() for x in _token_terms(focus)[:18]]
    if not terms:
        return [(0, min(len(text), width))]
    lower = text.lower()
    candidates: list[tuple[int, int, int]] = []
    step = max(600, width // 3)
    for start in range(0, len(text), step):
        end = min(len(text), start + width)
        block = lower[start:end]
        score = 0
        for term in terms:
            hits = block.count(term)
            if hits:
                score += min(hits, 5) * (2 if len(term) >= 7 else 1)
        candidates.append((-score, start, end))
        if end == len(text):
            break
    candidates.sort()
    selected: list[tuple[int, int]] = []
    for neg, start, end in candidates:
        if neg == 0 and selected:
            break
        overlap = False
        for old_start, old_end in selected:
            if min(end, old_end) - max(start, old_start) > width // 2:
                overlap = True
                break
        if not overlap:
            selected.append((start, end))
        if len(selected) >= count:
            break
    return sorted(selected)


def _merge_spans(spans: list[tuple[int, int]], text_len: int) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for start, end in spans:
        a = max(0, min(text_len, _int(start)))
        b = max(0, min(text_len, _int(end)))
        if b > a:
            cleaned.append((a, b))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1] + 80:
            merged.append((start, end))
        else:
            old_start, old_end = merged[-1]
            merged[-1] = (old_start, max(old_end, end))
    return merged


def _llm_text(payload: Any) -> str:
    if payload is None:
        return ""
    direct = getattr(payload, "llm", None)
    if direct is not None:
        value = getattr(direct, "raw_text", None)
        if isinstance(value, str):
            return value
    value = getattr(payload, "raw_text", None)
    if isinstance(value, str):
        return value
    response = getattr(payload, "response", None)
    if isinstance(response, dict):
        for key in ("text", "content", "raw_text"):
            item = response.get(key)
            if isinstance(item, str):
                return item
    return ""


# ---------------------------------------------------------------------------
# Tooling/model selection
# ---------------------------------------------------------------------------


async def _load_tooling() -> None:
    try:
        info = await tooling_info(timeout=8.0)
        _remember_budget(info)
        found: list[str] = []
        for attr in ("llm_models", "models", "allowed_models"):
            value = getattr(info, attr, None)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str) and item not in found:
                        found.append(item)
                    elif isinstance(item, dict):
                        name = item.get("model") or item.get("id") or item.get("name")
                        if isinstance(name, str) and name not in found:
                            found.append(name)
        if found:
            _STATE["allowed_models"] = tuple(found)
    except Exception:
        return


def _models(preferred: tuple[str, ...]) -> list[str]:
    live = _STATE.get("allowed_models")
    if not isinstance(live, tuple) or not live:
        return list(preferred)
    allowed = set(x for x in live if isinstance(x, str))
    chosen = [x for x in preferred if x in allowed]
    if chosen:
        return chosen
    fallback: list[str] = []
    for item in live:
        if isinstance(item, str):
            fallback.append(item)
        if len(fallback) >= 3:
            break
    return fallback


async def _chat(
    preferred: tuple[str, ...],
    messages: list[dict[str, Any]],
    deadline: float,
    max_tokens: int,
    timeout_cap: float,
    temperature: float = 0.0,
) -> Any:
    for model in _models(preferred):
        timeout = min(timeout_cap, _left(deadline) - 2.0)
        if timeout <= 4.0:
            return None
        try:
            payload = await asyncio.wait_for(
                llm_chat(
                    provider=LLM_PROVIDER,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_output_tokens=max_tokens,
                    timeout=timeout,
                ),
                timeout=timeout + 1.0,
            )
            _remember_budget(payload)
            if _llm_text(payload).strip():
                return payload
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Question contract and research plan
# ---------------------------------------------------------------------------


class QuestionMap:
    def __init__(self, question: str, output_schema: Any) -> None:
        self.question = question
        self.output_schema = output_schema
        self.parts: list[str] = []
        self.named_sources: list[str] = []
        self.entities: list[str] = []
        self.must_preserve_exact = False
        self.explain_difference = False
        self.complete_set = False
        self.superlative = False
        self.computed = False
        self.strict_output = False
        self.ordering = ""
        self.answer_kind = "fact"
        self._derive()

    def _derive(self) -> None:
        q = self.question
        low = q.lower()
        if re.search(r"exactly as (?:given|shown|printed|written)|verbatim|exact labels?|exact names?|exact marks?", low):
            self.must_preserve_exact = True
        if re.search(r"how .*compare|compare(?:s|d)? with|difference|why .*more|why .*less|discrep", low):
            self.explain_difference = True
        if re.search(r"\b(all|every|each|both|complete list|which of these|among the .* which)\b", low):
            self.complete_set = True
        if re.search(r"\b(highest|lowest|largest|smallest|best|worst|most|least|top\s+\d+|first|last)\b", low):
            self.superlative = True
        if re.search(r"\b(total|sum|average|mean|ratio|percent|percentage|how many|number of|difference)\b", low):
            self.computed = True
        if "only the answer" in low or "respond with only" in low or "nothing else" in low:
            self.strict_output = True
        if "alphabetical" in low:
            self.ordering = "alphabetical"
        elif "chronological" in low:
            self.ordering = "chronological"
        elif "highest to lowest" in low or "descending" in low:
            self.ordering = "descending"
        elif "lowest to highest" in low or "ascending" in low:
            self.ordering = "ascending"
        if re.search(r"\bhow many\b|\bnumber of\b", low):
            self.answer_kind = "number/comparison"
        elif self.complete_set:
            self.answer_kind = "list/set"
        elif self.superlative:
            self.answer_kind = "ranking/fact"
        numbered = re.findall(r"(?:^|\s)\((\d+)\)\s*([^()]+?)(?=(?:\s\(\d+\)|$))", q)
        for _, body in numbered:
            item = _clean_space(body)
            if item:
                self.parts.append(item[:700])
        if not self.parts and q.count("?") > 1:
            for item in q.split("?"):
                item = _clean_space(item)
                if item:
                    self.parts.append(item[:700])
        # Capture source brands explicitly named in common constructions.
        patterns = (
            r"official\s+([A-Z][A-Za-z0-9&. -]{2,60}?)(?:\s+(?:results?|page|website|site|data|database|report|competition))",
            r"according to\s+([A-Z][A-Za-z0-9&. -]{2,60}?)(?:[,.;]|\s+(?:data|results?|report))",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, q):
                item = _clean_space(match.group(1))
                if item and item.lower() not in [x.lower() for x in self.named_sources]:
                    self.named_sources.append(item)
        # Proper-noun phrases help retrieval but are not treated as facts themselves.
        for match in re.finditer(r"\b(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]+(?:\s+|$)){2,6}", q):
            item = _clean_space(match.group(0))
            if 3 <= len(item) <= 100 and item.lower() not in [x.lower() for x in self.entities]:
                self.entities.append(item)
            if len(self.entities) >= 16:
                break

    def block(self) -> str:
        return json.dumps(
            {
                "answer_kind": self.answer_kind,
                "parts": self.parts,
                "named_sources": self.named_sources,
                "entities": self.entities,
                "must_preserve_exact": self.must_preserve_exact,
                "explain_difference": self.explain_difference,
                "complete_set": self.complete_set,
                "superlative": self.superlative,
                "computed": self.computed,
                "strict_output": self.strict_output,
                "ordering": self.ordering,
            },
            ensure_ascii=False,
        )


class ResearchPlan:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.focus_terms: list[str] = []
        self.must_answer: list[str] = []
        self.must_explain: list[str] = []
        self.exact_values_needed: list[str] = []
        self.preferred_domains: list[str] = []

    def apply(self, data: dict[str, Any], qmap: QuestionMap) -> None:
        self.queries = _uniq_text(data.get("queries"), MAX_PLAN_QUERIES, 430)
        self.focus_terms = _uniq_text(data.get("focus_terms"), 18, 120)
        self.must_answer = _uniq_text(data.get("must_answer"), 10, 500)
        self.must_explain = _uniq_text(data.get("must_explain"), 8, 500)
        self.exact_values_needed = _uniq_text(data.get("exact_values_needed"), 14, 220)
        self.preferred_domains = _uniq_text(data.get("preferred_domains"), 6, 120)
        if not self.must_answer:
            self.must_answer = list(qmap.parts) if qmap.parts else [qmap.question[:700]]

    def block(self) -> str:
        return json.dumps(
            {
                "queries": self.queries,
                "focus_terms": self.focus_terms,
                "must_answer": self.must_answer,
                "must_explain": self.must_explain,
                "exact_values_needed": self.exact_values_needed,
                "preferred_domains": self.preferred_domains,
            },
            ensure_ascii=False,
        )


def _fallback_plan(qmap: QuestionMap) -> ResearchPlan:
    plan = ResearchPlan()
    q = qmap.question
    plan.must_answer = list(qmap.parts) if qmap.parts else [q[:700]]
    terms = _token_terms(q)
    plan.focus_terms = terms[:16]
    queries: list[str] = [q]
    if qmap.named_sources:
        for source in qmap.named_sources[:2]:
            queries.append(f'{source} {" ".join(terms[:8])}')
    queries.append(f'{" ".join(terms[:12])} official results')
    if qmap.complete_set or qmap.superlative:
        queries.append(f'{" ".join(terms[:10])} full results table')
    plan.queries = _uniq_text(queries, MAX_PLAN_QUERIES, 430)
    if qmap.explain_difference:
        plan.must_explain.append("Explain the observed discrepancy, including any extra categories/rows/statuses if the source shows them.")
    if qmap.must_preserve_exact:
        plan.exact_values_needed.append("All requested names, marks, labels, dates and units exactly as the official source prints them.")
    return plan


async def _make_plan(qmap: QuestionMap, deadline: float) -> ResearchPlan:
    """Deterministic retrieval plan.

    v4 spent an LLM call planning queries even when the question already named
    the event/source precisely. v5 deliberately avoids that latency and removes
    a chance for the planner to substitute a remembered event/year.
    """
    plan = _fallback_plan(qmap)
    q = qmap.question
    terms = _token_terms(q)

    # Prefer queries that preserve named source/event/year tokens verbatim.
    extras: list[str] = []
    if qmap.named_sources:
        source = qmap.named_sources[0]
        extras.append(f'{source} {" ".join(terms[:14])}')
    if qmap.entities:
        extras.append(f'{" ".join(qmap.entities[:3])} {" ".join(terms[:10])} official')
    if qmap.complete_set or qmap.superlative or qmap.computed:
        extras.append(f'{" ".join(terms[:14])} official full results table')

    merged = extras + plan.queries
    plan.queries = _uniq_text(merged, MAX_PLAN_QUERIES, 430)
    return plan


# ---------------------------------------------------------------------------
# Evidence store
# ---------------------------------------------------------------------------


class SourceBook:
    def __init__(self, qmap: QuestionMap, plan: ResearchPlan) -> None:
        self.qmap = qmap
        self.plan = plan
        self.rows: list[dict[str, Any]] = []
        self.searched: list[str] = []
        self.fetched: list[str] = []

    def row(self, number: int) -> dict[str, Any] | None:
        if 1 <= number <= len(self.rows):
            return self.rows[number - 1]
        return None

    def add(
        self,
        receipt_id: str,
        result_id: str,
        text: str,
        title: str,
        url: str,
        shown: list[tuple[int, int]],
        origin: str,
    ) -> int:
        if len(self.rows) >= MAX_SOURCES:
            return 0
        if not receipt_id or not result_id or not text.strip():
            return 0
        for idx, old in enumerate(self.rows, 1):
            if old.get("receipt_id") == receipt_id and old.get("result_id") == result_id:
                spans = old.get("shown")
                if not isinstance(spans, list):
                    spans = []
                spans.extend(shown)
                old["shown"] = _merge_spans(spans, len(str(old.get("text") or "")))
                return idx
        body = text[:MAX_SOURCE_TEXT]
        row = {
            "receipt_id": receipt_id,
            "result_id": result_id,
            "text": body,
            "title": _cap(title, 220),
            "url": _cap(url, 600),
            "host": _host(url),
            "authority": _authority(url, title, self.qmap.named_sources + self.plan.preferred_domains),
            "shown": _merge_spans(shown, len(body)),
            "retained": [],
            "origin": origin,
        }
        self.rows.append(row)
        return len(self.rows)

    def retain(self, number: int, quote: str) -> bool:
        row = self.row(number)
        if row is None:
            return False
        body = str(row.get("text") or "")
        needle = (quote or "").strip()
        if len(needle) < 6:
            return False
        pos = body.find(needle)
        if pos < 0:
            pos = body.lower().find(needle.lower())
        if pos < 0:
            return False
        start = max(0, pos - QUOTE_CONTEXT)
        end = min(len(body), pos + len(needle) + QUOTE_CONTEXT)
        spans = row.get("retained")
        if not isinstance(spans, list):
            spans = []
        spans.append((start, end))
        row["retained"] = _merge_spans(spans, len(body))[:8]
        return True

    def relevance(self, number: int) -> int:
        row = self.row(number)
        if row is None:
            return -999
        hay = f"{row.get('title','')} {row.get('url','')} {str(row.get('text',''))[:5000]}".lower()
        score = _int(row.get("authority"), 0) * 5
        for term in _token_terms(self.qmap.question)[:18]:
            if term.lower() in hay:
                score += 2
        for term in self.plan.focus_terms[:14]:
            if term.lower() in hay:
                score += 3
        if row.get("origin") == "fetch":
            score += 8
        return score

    def ranked(self) -> list[int]:
        numbers = list(range(1, len(self.rows) + 1))
        numbers.sort(key=lambda n: (-self.relevance(n), n))
        return numbers

    def _snippet(self, number: int, focus: str, max_chars: int) -> str:
        row = self.row(number)
        if row is None:
            return ""
        body = str(row.get("text") or "")
        retained = row.get("retained")
        pieces: list[str] = []
        if isinstance(retained, list) and retained:
            for start, end in retained[:3]:
                pieces.append(body[_int(start) : _int(end)])
        if not pieces:
            shown = row.get("shown")
            if isinstance(shown, list):
                for start, end in shown[:3]:
                    pieces.append(body[_int(start) : _int(end)])
        if not pieces:
            for start, end in _best_windows(body, focus, min(max_chars, 4200), 2):
                pieces.append(body[start:end])
        if not pieces:
            pieces = [body[:max_chars]]
        return _cap("\n...\n".join(pieces), max_chars)

    def pack(self, max_chars: int = MAX_PACK_CHARS) -> str:
        focus = self.qmap.question + "\n" + " ".join(self.plan.focus_terms)
        blocks: list[str] = []
        spent = 0
        for number in self.ranked():
            row = self.row(number)
            if row is None:
                continue
            snippet = self._snippet(number, focus, 6500 if row.get("origin") == "fetch" else 2600)
            block = (
                f"[{number}] SOURCE\n"
                f"TITLE: {row.get('title','')}\n"
                f"URL: {row.get('url','')}\n"
                f"AUTHORITY: {row.get('authority',0)} ORIGIN: {row.get('origin','')}\n"
                f"TEXT:\n{snippet}\n"
            )
            if spent + len(block) > max_chars:
                continue
            blocks.append(block)
            spent += len(block)
            if spent >= max_chars - 1200:
                break
        return "\n\n".join(blocks)

    def citation(self, number: int) -> tuple[CitationRef | None, int]:
        row = self.row(number)
        if row is None:
            return None, 0
        receipt = str(row.get("receipt_id") or "")
        result = str(row.get("result_id") or "")
        body = str(row.get("text") or "")
        if not receipt or not result or not body:
            return None, 0
        spans = row.get("retained") or row.get("shown") or []
        if not isinstance(spans, list):
            spans = []
        widened: list[tuple[int, int]] = []
        for raw in spans[:6]:
            try:
                start = max(0, int(raw[0]))
                end = min(len(body), int(raw[1]))
            except Exception:
                continue
            if end <= start:
                continue
            if end - start < MIN_SLICE:
                middle = (start + end) // 2
                start = max(0, middle - MIN_SLICE // 2)
                end = min(len(body), start + MIN_SLICE)
                start = max(0, end - MIN_SLICE)
            widened.append((start, end))
        if not widened:
            widened = [(0, min(len(body), 1800))]
        widened = _merge_spans(widened, len(body))
        total = sum(end - start for start, end in widened)
        if total > MAX_SLICE_PER_SOURCE:
            kept: list[tuple[int, int]] = []
            left = MAX_SLICE_PER_SOURCE
            for start, end in widened:
                if left <= 0:
                    break
                width = min(end - start, left)
                kept.append((start, start + width))
                left -= width
            widened = kept
            total = sum(end - start for start, end in widened)
        slices = [CitationSlice(start=start, end=end) for start, end in widened if end > start]
        if not slices:
            return None, 0
        return CitationRef(receipt_id=receipt, result_id=result, slices=slices), total


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _loosen(query: str) -> str:
    value = re.sub(r"\bsite:\S+", " ", query or "", flags=re.I)
    value = value.replace('"', " ").replace("'", " ")
    return _clean_space(value)


async def _search_one(query: str, book: SourceBook) -> list[int]:
    q = _clean_space(query)
    if not q:
        return []
    if q not in book.searched:
        book.searched.append(q)
    attempts = [q]
    loose = _loosen(q)
    if loose and loose != q:
        attempts.append(loose)
    payload = None
    for attempt in attempts[:2]:
        try:
            result = await search_web(
                attempt,
                provider=SEARCH_PROVIDER,
                num=SEARCH_RESULTS,
                timeout=SEARCH_TIMEOUT,
            )
            if getattr(result, "results", None):
                payload = result
                break
        except Exception:
            continue
    if payload is None:
        return []
    _remember_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    items = list(getattr(payload, "results", None) or [])
    if not receipt:
        return []
    numbers: list[int] = []
    for item in items:
        result_id = getattr(item, "result_id", None)
        note = str(getattr(item, "note", None) or "")
        if not isinstance(result_id, str) or not result_id or not note.strip():
            continue
        title = str(getattr(item, "title", None) or "")
        url = str(getattr(item, "url", None) or "")
        number = book.add(
            receipt,
            result_id,
            note,
            title,
            url,
            [(0, min(len(note), SEARCH_PREVIEW))],
            "search",
        )
        if number and number not in numbers:
            numbers.append(number)
    return numbers


async def _search_many(queries: list[str], book: SourceBook, deadline: float) -> list[int]:
    unique: list[str] = []
    for q in queries:
        q = _clean_space(q)
        if q and q.lower() not in [x.lower() for x in unique]:
            unique.append(q)
        if len(unique) >= MAX_PLAN_QUERIES:
            break
    tasks = [asyncio.create_task(_search_one(q, book)) for q in unique]
    if not tasks:
        return []
    timeout = min(SEARCH_TIMEOUT + 4.0, max(2.0, _left(deadline) - 2.0))
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    numbers: list[int] = []
    for task in done:
        try:
            result = task.result()
        except Exception:
            result = []
        if isinstance(result, list):
            for number in result:
                if isinstance(number, int) and number > 0 and number not in numbers:
                    numbers.append(number)
    return numbers


def _fetch_candidates(book: SourceBook, cap: int) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for number in book.ranked():
        row = book.row(number)
        if row is None:
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        score = book.relevance(number)
        ranked.append((-score, number, url))
    ranked.sort()
    out: list[str] = []
    seen: set[str] = set()
    for _, _, url in ranked:
        key = url.split("#", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= cap:
            break
    return out


async def _fetch_one(url: str, book: SourceBook) -> int:
    target = (url or "").strip()
    if not target:
        return 0
    if target not in book.fetched:
        book.fetched.append(target)
    try:
        payload = await fetch_page(
            target,
            provider=SEARCH_PROVIDER,
            timeout=FETCH_TIMEOUT,
        )
    except Exception:
        return 0
    if not getattr(payload, "results", None):
        return 0
    _remember_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    items = list(getattr(payload, "results", None) or [])
    if not receipt or not items:
        return 0
    item = items[0]
    result_id = getattr(item, "result_id", None)
    note = str(getattr(item, "note", None) or "")
    if not isinstance(result_id, str) or not result_id or not note.strip():
        return 0
    title = str(getattr(item, "title", None) or target)
    final_url = str(getattr(item, "url", None) or target)
    shown: list[tuple[int, int]] = []
    if len(note) <= 8500:
        shown = [(0, len(note))]
    else:
        shown.append((0, min(len(note), FETCH_HEAD)))
        focus = book.qmap.question + "\n" + " ".join(book.plan.focus_terms)
        shown.extend(_best_windows(note, focus))
        shown = _merge_spans(shown, len(note))
    return book.add(receipt, result_id, note, title, final_url, shown, "fetch")


async def _fetch_many(urls: list[str], book: SourceBook, deadline: float) -> None:
    unique: list[str] = []
    for url in urls:
        if url and url not in unique:
            unique.append(url)
        if len(unique) >= FETCH_CAP:
            break
    tasks = [asyncio.create_task(_fetch_one(url, book)) for url in unique]
    if not tasks:
        return
    timeout = min(FETCH_TIMEOUT + 5.0, max(2.0, _left(deadline) - 2.0))
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Proof grid
# ---------------------------------------------------------------------------


def _fallback_grid(qmap: QuestionMap, plan: ResearchPlan, book: SourceBook) -> dict[str, Any]:
    return {
        "draft_answer": "",
        "facts": [],
        "verbatim_values": [],
        "coverage": [{"requirement": x, "status": "unknown"} for x in plan.must_answer[:8]],
        "gaps": list(plan.must_answer[:4]),
        "repair_queries": [],
        "comparison_explanation": "",
    }


def _retain_grid_quotes(grid: dict[str, Any], book: SourceBook) -> None:
    facts = grid.get("facts")
    if not isinstance(facts, list):
        return
    for item in facts:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        quote = item.get("quote")
        if source > 0 and isinstance(quote, str):
            book.retain(source, quote)


async def _build_grid(qmap: QuestionMap, plan: ResearchPlan, book: SourceBook, deadline: float) -> dict[str, Any]:
    fallback = _fallback_grid(qmap, plan, book)
    evidence = book.pack(MAX_PACK_CHARS)
    if not evidence or _money_left() < MIN_GRID_USD or _left(deadline) < 52.0:
        return fallback
    system = (
        "You are a CLOSED-BOOK evidence adjudicator. The supplied evidence is the entire world you may use. "
        "Never answer from memory, even when you recognize the event/person/topic. Every load-bearing fact must "
        "identify a source number AND an exact verbatim quote copied from that source. If a value/name/time is "
        "not present in the supplied evidence, mark it missing instead of guessing. Preserve names, capitalization, "
        "marks, units, dates, labels and status codes exactly as printed. Check every sub-question. For computed "
        "facts such as a row count, cite the exact table/source region used for the computation and state the "
        "derivation in the claim. If the question compares a count/rule/result and the evidence reveals WHY they "
        "differ, record that explanation. For set/ranking questions, verify the pool and exclusions. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nQUESTION MAP:\n{qmap.block()}\n\nRESEARCH PLAN:\n{plan.block()}\n\nEVIDENCE:\n{evidence}\n\nReturn exactly:\n{{"draft_answer":"a complete evidence-supported answer with [source-number] markers","facts":[{{"claim":"atomic factual claim using source-exact values","source":1,"quote":"exact verbatim quote","requirement":"which requested part it answers"}}],"verbatim_values":["exact source spelling/capitalization/mark/value that must survive final writing"],"coverage":[{{"requirement":"requested part","status":"proved|partial|missing","sources":[1]}}],"gaps":["only genuinely unresolved facts"],"repair_queries":["high precision query for a gap"],"comparison_explanation":"source-supported reason for a discrepancy, or empty"}}\n\nDo not create a gap merely because you could add background detail. If all requested outputs are proved, gaps must be [].'''
    payload = await _chat(
        GRID_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4200,
        GRID_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        return fallback
    _retain_grid_quotes(data, book)
    return data


def _repair_queries(grid: dict[str, Any]) -> list[str]:
    raw = grid.get("repair_queries")
    return _uniq_text(raw, MAX_REPAIR_QUERIES, 430)


def _grid_draft(grid: dict[str, Any]) -> str:
    value = grid.get("draft_answer")
    return value.strip() if isinstance(value, str) else ""


def _grid_verbatim(grid: dict[str, Any]) -> list[str]:
    return _uniq_text(grid.get("verbatim_values"), 40, 180)


def _grid_has_real_gaps(grid: dict[str, Any]) -> bool:
    gaps = grid.get("gaps")
    if not isinstance(gaps, list):
        return False
    return any(isinstance(x, str) and _clean_space(x) for x in gaps)


# ---------------------------------------------------------------------------
# Grounding firewall
# ---------------------------------------------------------------------------

_NUM_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d+(?::\d{1,2}(?:\.\d+)?|\.\d+|,\d{3})*(?:%|st|nd|rd|th)?", re.I)
_NAME_TOKEN_RE = re.compile(r"\b(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]{2,}|[A-Z]{2,})(?:\s+(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'-]{2,}|[A-Z]{2,})){1,3}\b")
_COMMON_NAME_PHRASES = {
    "World Athletics", "Official Result", "Official Results", "Race Analysis",
    "Photo Finish", "Final Result", "Semifinal Result", "Reference Answer",
}


def _verified_facts(grid: dict[str, Any], book: SourceBook) -> list[dict[str, Any]]:
    """Keep only facts whose quoted evidence literally exists in the cited source."""
    raw = grid.get("facts")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:30]:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        claim = _clean_space(str(item.get("claim") or ""))
        quote = str(item.get("quote") or "").strip()
        row = book.row(source)
        if row is None or not claim or len(quote) < 6:
            continue
        body = str(row.get("text") or "")
        if quote not in body and quote.lower() not in body.lower():
            continue
        clean = dict(item)
        clean["source"] = source
        clean["claim"] = claim
        clean["quote"] = quote
        out.append(clean)
        book.retain(source, quote)
    return out


def _fact_basis(question: str, facts: list[dict[str, Any]], book: SourceBook) -> str:
    pieces = [question]
    for item in facts:
        pieces.append(str(item.get("claim") or ""))
        pieces.append(str(item.get("quote") or ""))
        row = book.row(_int(item.get("source"), 0))
        if row is not None:
            pieces.append(str(row.get("title") or ""))
    return "\n".join(pieces)


def _critical_numbers(text: str) -> list[str]:
    clean = _CITE_RE.sub("", text or "")
    out: list[str] = []
    for match in _NUM_TOKEN_RE.finditer(clean):
        token = match.group(0)
        if token not in out:
            out.append(token)
    return out


def _critical_names(text: str) -> list[str]:
    out: list[str] = []
    for match in _NAME_TOKEN_RE.finditer(text or ""):
        token = _clean_space(match.group(0))
        if token in _COMMON_NAME_PHRASES:
            continue
        if token not in out:
            out.append(token)
    return out


def _token_present(token: str, basis: str) -> bool:
    if not token:
        return True
    return token.lower() in (basis or "").lower()


def _coverage_signature(facts: list[dict[str, Any]]) -> list[str]:
    """Critical values that a final answer should preserve from verified facts."""
    out: list[str] = []
    for item in facts:
        claim = str(item.get("claim") or "")
        for token in _critical_numbers(claim):
            if token not in out:
                out.append(token)
        # Exact multi-word names are especially important in pairwise judging.
        for token in _critical_names(claim):
            if token not in out:
                out.append(token)
    return out[:36]


def _grounded_answer(answer: str, question: str, facts: list[dict[str, Any]], book: SourceBook) -> bool:
    """Reject memory substitutions and unsupported precise values/names."""
    if not _usable_answer(answer, question):
        return False
    basis = _fact_basis(question, facts, book)

    # Any precise numeric token in the answer must exist in the question or
    # verified evidence/facts. This catches remembered times, prices, years, etc.
    for token in _critical_numbers(answer):
        if not _token_present(token, basis):
            return False

    # Any multi-word proper name introduced by the writer must be visible in
    # the question or verified evidence. This catches entity substitution such
    # as answering about a famous athlete from a different championship.
    for token in _critical_names(answer):
        if not _token_present(token, basis):
            return False

    # If verified facts contain distinctive values, the answer must preserve a
    # meaningful share of them. A fluent answer that ignores the proof grid is
    # worse than a mechanical evidence-only fallback.
    required = _coverage_signature(facts)
    if len(required) >= 4:
        hits = sum(1 for token in required if _token_present(token, answer))
        if hits < max(2, int(len(required) * 0.45)):
            return False
    return True


def _verified_fact_answer(question: str, facts: list[dict[str, Any]], book: SourceBook) -> str:
    """Zero-memory fallback built only from quote-verified atomic claims."""
    lines: list[str] = []
    seen: set[str] = set()
    for item in facts[:14]:
        claim = _clean_space(str(item.get("claim") or ""))
        source = _int(item.get("source"), 0)
        if not claim:
            continue
        key = claim.lower()
        if key in seen:
            continue
        seen.add(key)
        marker = f" [{source}]" if 1 <= source <= len(book.rows) else ""
        lines.append(claim + marker)
    text = "\n".join(lines)
    return text if _usable_answer(text, question) else ""


def _grid_for_writer(grid: dict[str, Any], facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Strip unverified fact claims before final synthesis."""
    clean = dict(grid)
    clean["facts"] = facts
    # Draft answer is untrusted free-form text; the writer receives verified
    # atomic facts instead of being anchored to a possibly hallucinated draft.
    clean["draft_answer"] = ""
    return clean


# ---------------------------------------------------------------------------
# Answer quality, citations, source-verbatim restoration
# ---------------------------------------------------------------------------


_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _usable_answer(text: str, question: str = "") -> bool:
    value = (text or "").strip()
    if len(value) < 3:
        return False
    low = value.lower()
    bad = (
        "unable to answer",
        "i could not complete",
        "best-effort answer unavailable",
        "insufficient information",
        "i cannot determine",
    )
    if any(item in low for item in bad):
        return False
    if question:
        q = _clean_space(question)
        v = _clean_space(value)
        # Reject accidental prompt echo as an answer.
        if len(q) > 80 and (v == q or v.startswith(q[: min(300, len(q))])):
            return False
    return True


def _marker_numbers(text: str, maximum: int) -> list[int]:
    out: list[int] = []
    for match in _CITE_RE.finditer(text or ""):
        number = _int(match.group(1), 0)
        if 1 <= number <= maximum and number not in out:
            out.append(number)
    return out


def _normalize_markers(text: str, maximum: int) -> str:
    def repl(match: re.Match[str]) -> str:
        number = _int(match.group(1), 0)
        return f"[{number}]" if 1 <= number <= maximum else ""
    return re.sub(r"\[(\d{1,4})\]", repl, text or "")


def _citations(text: str, book: SourceBook) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for number in _marker_numbers(text, len(book.rows)):
        if len(refs) >= MAX_CITATIONS:
            break
        ref, cost = book.citation(number)
        if ref is None:
            continue
        if spent + cost > MAX_EVIDENCE_CHARS:
            continue
        refs.append(ref)
        spent += cost
    return refs


def _retain_output_quotes(data: dict[str, Any], book: SourceBook) -> None:
    proof = data.get("proof_quotes")
    if not isinstance(proof, list):
        return
    for item in proof:
        if not isinstance(item, dict):
            continue
        source = _int(item.get("source"), 0)
        quote = item.get("quote")
        if source > 0 and isinstance(quote, str):
            book.retain(source, quote)


def _restore_verbatim(answer: str, values: list[str]) -> str:
    result = answer or ""
    # Longest first prevents replacing a short substring inside a longer exact label.
    ordered = sorted([x for x in values if 2 <= len(x) <= 180], key=len, reverse=True)
    for exact in ordered:
        try:
            pattern = re.compile(re.escape(exact), flags=re.I)
            if pattern.search(result):
                result = pattern.sub(lambda _m: exact, result)
        except Exception:
            continue
    return result


def _clean_answer(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:markdown|text)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(r"^(?:Answer|Final answer)\s*:\s*", "", value, flags=re.I)
    return value.strip()


def _fact_rescue(qmap: QuestionMap, grid: dict[str, Any], book: SourceBook) -> str:
    draft = _grid_draft(grid)
    if _usable_answer(draft, qmap.question):
        return _normalize_markers(draft, len(book.rows))
    facts = grid.get("facts")
    lines: list[str] = []
    if isinstance(facts, list):
        for item in facts[:12]:
            if not isinstance(item, dict):
                continue
            claim = _clean_space(str(item.get("claim") or ""))
            source = _int(item.get("source"), 0)
            if claim:
                marker = f" [{source}]" if 1 <= source <= len(book.rows) else ""
                lines.append(claim + marker)
    if lines:
        return "\n".join(lines)
    # Last evidence-based rung: return concise source excerpts, never the question.
    for number in book.ranked()[:3]:
        row = book.row(number)
        if row is None:
            continue
        text = str(row.get("text") or "")
        if text.strip():
            return _cap(text.strip(), 1800) + f" [{number}]"
    return ""


async def _write_answer(
    qmap: QuestionMap,
    plan: ResearchPlan,
    grid: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> str:
    rescue = _fact_rescue(qmap, grid, book)
    if _left(deadline) < 34.0 or _money_left() < MIN_WRITE_USD:
        return rescue
    system = (
        "You are a CLOSED-BOOK precision answer compiler. The VERIFIED FACTS in the proof grid are the only "
        "load-bearing factual claims you may state. Do not substitute model-memory facts, famous people, prior "
        "years, remembered results, or plausible values. If a requested fact is not verified, say only what the "
        "verified evidence supports. Answer EVERY requested sub-question, preferably in the same order. "
        "SOURCE-VERBATIM RULE: if the source prints a person's name, label, status code, mark, time, number, "
        "date, unit or category in a particular form, copy that form exactly; do not title-case, normalize, "
        "round, convert or paraphrase it. COMPARISON RULE: if the question asks how two counts/rules/results "
        "compare and the evidence identifies why they differ, explicitly state the difference and the "
        "source-supported reason. COMPLETENESS RULE: for 'among/all/best/which' questions, make clear that "
        "the selected result was compared against the relevant pool. Put [source-number] immediately after "
        "each factual sentence or bullet it proves. Be concise but do not omit a requested component. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nQUESTION MAP:\n{qmap.block()}\n\nPLAN CHECKLIST:\n{plan.block()}\n\nPROOF GRID:\n{json.dumps(grid, ensure_ascii=False)[:26000]}\n\nEVIDENCE:\n{book.pack(50000)}\n\nReturn exactly:\n{{"answer":"final answer with [n] markers","proof_quotes":[{{"source":1,"quote":"exact decisive source quote used by the answer"}}]}}\n\nBefore returning, silently verify: all requested parts answered; exact source spellings/marks preserved; discrepancy explained when evidence supports it; no unsupported background added.'''
    payload = await _chat(
        WRITE_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4600,
        WRITE_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        return rescue
    _retain_output_quotes(data, book)
    answer = data.get("answer")
    if not isinstance(answer, str):
        return rescue
    answer = _normalize_markers(_clean_answer(answer), len(book.rows))
    answer = _restore_verbatim(answer, _grid_verbatim(grid))
    if not _usable_answer(answer, qmap.question):
        return rescue
    return answer


async def _review_answer(
    answer: str,
    qmap: QuestionMap,
    plan: ResearchPlan,
    grid: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> str:
    if not _usable_answer(answer, qmap.question):
        return answer
    if _left(deadline) < 27.0 or _money_left() < MIN_REVIEW_USD:
        return answer
    system = (
        "You are a strict CLOSED-BOOK final-answer reviewer. You may revise only with supplied verified evidence. "
        "Never replace a source-backed value with remembered knowledge from another year/event/entity. "
        "Check: (1) every explicit/numbered sub-question is answered; (2) names, labels, marks, times, dates, "
        "units and status codes match source spelling/capitalization exactly; (3) a requested comparison says "
        "both the numerical/logical difference and, when evidenced, why it exists; (4) ranking/set answers "
        "demonstrate the relevant comparison pool; (5) every factual sentence has a valid [n] marker. "
        "Do not add uncited model-memory facts. Return JSON only."
    )
    user = f'''QUESTION:\n{qmap.question}\n\nCHECKLIST:\n{plan.block()}\n\nCURRENT ANSWER:\n{answer}\n\nPROOF GRID:\n{json.dumps(grid, ensure_ascii=False)[:22000]}\n\nDECISIVE EVIDENCE:\n{book.pack(42000)}\n\nReturn exactly:\n{{"answer":"corrected answer, or the original unchanged if already optimal","proof_quotes":[{{"source":1,"quote":"exact source quote"}}]}}'''
    payload = await _chat(
        REVIEW_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        3600,
        REVIEW_TIMEOUT,
        0.0,
    )
    data = _json_object(_llm_text(payload))
    if data is None:
        return answer
    _retain_output_quotes(data, book)
    revised = data.get("answer")
    if not isinstance(revised, str):
        return answer
    revised = _normalize_markers(_clean_answer(revised), len(book.rows))
    revised = _restore_verbatim(revised, _grid_verbatim(grid))
    if _usable_answer(revised, qmap.question):
        return revised
    return answer


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def _schema_kind(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _schema_kind(item)
                    if found and found != "null":
                        return found
    return ""


def _schema_branch(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("type"), str):
        return schema
    for key in ("anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and _schema_kind(item) != "null":
                    return item
    return schema


def _shape_ok(value: Any, schema: Any, depth: int = 0) -> bool:
    if depth > 6 or not isinstance(schema, dict):
        return True
    branch = _schema_branch(schema)
    enum = branch.get("enum")
    if isinstance(enum, list) and enum and value not in enum:
        return False
    kind = _schema_kind(branch)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        props = branch.get("properties") or {}
        required = branch.get("required") or []
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    return False
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in value and isinstance(sub, dict) and not _shape_ok(value[key], sub, depth + 1):
                    return False
        return True
    if kind == "array":
        if not isinstance(value, list):
            return False
        sub = branch.get("items") or {}
        return all(_shape_ok(item, sub, depth + 1) for item in value[:40])
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return True


def _strip_citations(text: str) -> str:
    return _clean_space(_CITE_RE.sub("", text or ""))


def _value_lines(text: str) -> list[str]:
    clean = _strip_citations(text)
    parts = re.split(r"[\n;]+", clean)
    out: list[str] = []
    for raw in parts:
        item = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw).strip()
        if item:
            out.append(item[:500])
        if len(out) >= 24:
            break
    return out


def _coerce(text: str, schema: Any, depth: int = 0) -> Any:
    branch = _schema_branch(schema)
    if depth > 6:
        return _strip_citations(text)[:500]
    enum = branch.get("enum")
    if isinstance(enum, list) and enum:
        low = text.lower()
        for option in enum:
            if isinstance(option, str) and option.lower() in low:
                return option
        return enum[0]
    kind = _schema_kind(branch)
    if kind == "object":
        props = branch.get("properties") or {}
        required = branch.get("required") or list(props.keys()) if isinstance(props, dict) else []
        out: dict[str, Any] = {}
        if isinstance(required, list):
            for key in required:
                sub = props.get(key) if isinstance(props, dict) else {}
                out[str(key)] = _coerce(text, sub if isinstance(sub, dict) else {}, depth + 1)
        return out
    if kind == "array":
        sub = branch.get("items") or {}
        return [_coerce(item, sub, depth + 1) for item in _value_lines(text)[:12]]
    clean = _strip_citations(text)
    if kind in ("integer", "number"):
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", clean)
        if match:
            raw = match.group(0).replace(",", "")
            try:
                return int(float(raw)) if kind == "integer" else float(raw)
            except Exception:
                return 0 if kind == "integer" else 0.0
        return 0 if kind == "integer" else 0.0
    if kind == "boolean":
        return not bool(re.match(r"\s*(?:no|false|none|not)\b", clean, flags=re.I))
    if kind == "null":
        return None
    return clean[:1200]


async def _structured(answer: str, question: str, schema: Any, deadline: float) -> Any:
    fallback = _coerce(answer, schema)
    if _left(deadline) < 21.0 or _money_left() < MIN_SCHEMA_USD:
        return fallback
    system = (
        "Convert the supplied researched answer into the requested JSON schema. Preserve source values "
        "verbatim. Output ONLY the JSON value, with no markdown and no explanation. Do not invent facts."
    )
    user = f"QUESTION:\n{question}\n\nANSWER:\n{_strip_citations(answer)}\n\nSCHEMA:\n{json.dumps(schema, ensure_ascii=False)[:16000]}"
    payload = await _chat(
        SCHEMA_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        2600,
        SCHEMA_TIMEOUT,
        0.0,
    )
    raw = _llm_text(payload).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
        if _shape_ok(value, schema):
            return value
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Emergency answer — never echo the prompt
# ---------------------------------------------------------------------------


async def _emergency_answer(question: str, deadline: float) -> str:
    if _left(deadline) < 5.0:
        return "No supported answer was produced."
    system = (
        "Answer the user's question directly and concisely from your best knowledge. This is an emergency "
        "fallback because research tooling failed. Never repeat the question as the answer and never claim "
        "to have citations you do not have."
    )
    payload = await _chat(
        REVIEW_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": question}],
        deadline,
        1800,
        min(EMERGENCY_TIMEOUT, max(5.0, _left(deadline) - 1.0)),
        0.0,
    )
    text = _clean_answer(_llm_text(payload))
    if _usable_answer(text, question):
        return text
    return "No supported answer was produced."


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------


async def _solve(query: Query, question: str) -> Response:
    started = monotonic()
    deadline = started + WALL_SECONDS
    try:
        await _load_tooling()
    except Exception:
        pass

    qmap = QuestionMap(question, query.output_schema)
    try:
        plan = await _make_plan(qmap, deadline)
    except Exception:
        plan = _fallback_plan(qmap)
    book = SourceBook(qmap, plan)

    # One high-value retrieval wave. Keep the exact event/year/source terms from
    # the question; do not let an LLM planner drift to a remembered benchmark.
    try:
        await _search_many(plan.queries, book, deadline)
    except Exception:
        pass
    try:
        targets = _fetch_candidates(book, FETCH_CAP)
        await _fetch_many(targets, book, deadline)
    except Exception:
        pass

    # Build one central grid and immediately discard any fact whose cited quote
    # cannot be found literally in the source. This is the v5 grounding wall.
    try:
        grid = await _build_grid(qmap, plan, book, deadline)
    except Exception:
        grid = _fallback_grid(qmap, plan, book)
    facts = _verified_facts(grid, book)
    clean_grid = _grid_for_writer(grid, facts)
    best_answer = _verified_fact_answer(question, facts, book)

    # A single targeted repair is allowed only when too little quote-verified
    # evidence survived. Do not spend another full wave merely to polish style.
    if len(facts) < 2 and _elapsed(started) < REPAIR_LATEST_ELAPSED:
        repair = _repair_queries(grid)[:1]
        if repair:
            try:
                await _search_many(repair, book, deadline)
                await _fetch_many(_fetch_candidates(book, min(2, FETCH_CAP)), book, deadline)
                newer = await _build_grid(qmap, plan, book, deadline)
                newer_facts = _verified_facts(newer, book)
                if len(newer_facts) > len(facts):
                    grid = newer
                    facts = newer_facts
                    clean_grid = _grid_for_writer(grid, facts)
                    best_answer = _verified_fact_answer(question, facts, book)
            except Exception:
                pass

    # Final synthesis is permitted only from quote-verified facts. Its output
    # must pass a deterministic grounding firewall or it is discarded.
    if facts and _elapsed(started) < FORCE_COMMIT_ELAPSED:
        try:
            written = await _write_answer(qmap, plan, clean_grid, book, deadline)
            if _grounded_answer(written, question, facts, book):
                best_answer = written
        except Exception:
            pass

    # One short review only if there is ample time. The revised answer must pass
    # the same deterministic grounding firewall before it can replace the prior.
    if facts and _elapsed(started) < REVIEW_LATEST_ELAPSED and _grounded_answer(best_answer, question, facts, book):
        try:
            reviewed = await _review_answer(best_answer, qmap, plan, clean_grid, book, deadline)
            if _grounded_answer(reviewed, question, facts, book):
                best_answer = reviewed
        except Exception:
            pass

    # If free-form synthesis drifted even slightly outside the verified basis,
    # fall back to the mechanical fact answer rather than shipping hallucination.
    if facts and not _grounded_answer(best_answer, question, facts, book):
        best_answer = _verified_fact_answer(question, facts, book)

    best_answer = _restore_verbatim(best_answer, _grid_verbatim(grid))
    best_answer = _normalize_markers(_clean_answer(best_answer), len(book.rows))

    if not _usable_answer(best_answer, question):
        # Evidence excerpts are safer than model-memory invention.
        best_answer = _fact_rescue(qmap, clean_grid, book)
    if not _usable_answer(best_answer, question):
        try:
            emergency = await _emergency_answer(question, deadline)
            emergency_ok = _grounded_answer(emergency, question, facts, book) if facts else _usable_answer(emergency, question)
            if emergency_ok:
                best_answer = emergency
        except Exception:
            pass
    if not _usable_answer(best_answer, question):
        best_answer = "No supported answer was produced."

    if len(best_answer) > MAX_ANSWER_CHARS:
        best_answer = best_answer[: MAX_ANSWER_CHARS - 4].rstrip() + " …"

    try:
        refs = _citations(best_answer, book)
    except Exception:
        refs = []

    if query.output_schema is not None:
        try:
            output = await _structured(best_answer, question, query.output_schema, deadline)
        except Exception:
            output = _coerce(best_answer, query.output_schema)
        return Response(output=output, citations=refs or None)

    if qmap.strict_output:
        first = best_answer.splitlines()[0].strip() if best_answer else ""
        first = _CITE_RE.sub("", first)
        best_answer = _clean_space(first)
    return Response(text=best_answer, citations=refs or None)


@entrypoint("query")
async def query(query: Query) -> Response:
    question = (query.text or "").strip()
    if not question:
        if query.output_schema is not None:
            return Response(output=_coerce("", query.output_schema))
        return Response(text="No question provided.")
    try:
        return await _solve(query, question)
    except Exception:
        # A final containment boundary.  Crucially, it never echoes the prompt.
        deadline = monotonic() + 20.0
        try:
            answer = await _emergency_answer(question, deadline)
        except Exception:
            answer = "No supported answer was produced."
        if query.output_schema is not None:
            return Response(output=_coerce(answer, query.output_schema))
        return Response(text=answer)
