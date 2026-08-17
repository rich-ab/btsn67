"""Harnyx SN67 miner 151 (Richjg) — ClaimMesh research architecture.

An original research design inspired by observed high-performing mechanisms without
copying their controller topology. The agent uses a research loop, a source ledger,
a separate skeptical coverage pass, targeted repairs, and a final evidence-bound
writer. It is configured for the public Harnyx default stack: Chutes + DeSearch.

Before submission, benchmark this exact file with harnyx-miner-local-eval.
"""

from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from typing import Any

from harnyx_miner_sdk.api import fetch_page, llm_chat, search_web, tooling_info
from harnyx_miner_sdk.decorators import entrypoint
from harnyx_miner_sdk.query import CitationRef, CitationSlice, Query, Response


VERSION = "claimmesh-v1"

# Provider choices match Harnyx's public example configuration and the user's
# currently configured provider pair. Change only after local A/B evaluation.
LLM_PROVIDER = "chutes"
SEARCH_PROVIDER = "desearch"

# Runtime selection checks tooling_info() first, so model removals do not require
# a blind hard-coded assumption. These are ordered quality-first fallbacks.
RESEARCH_MODELS = (
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)
CRITIC_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "zai-org/GLM-5.2-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
FINAL_MODELS = (
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
SCHEMA_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

# Leave margin under the validator hard wall. Optional stages self-disable when
# time or metered budget is low.
WALL_SECONDS = 252.0
RESEARCH_STOP_LEFT = 92.0
FINAL_RESERVE = 42.0
SCHEMA_RESERVE = 24.0
TOOL_TIMEOUT = 20.0
FETCH_TIMEOUT = 18.0
CHAT_TIMEOUT = 62.0
MAX_RESEARCH_TURNS = 9
MAX_REPAIR_QUERIES = 4

# Evidence materialization limits. Harnyx rejects oversized response evidence, so
# the citation builder deliberately stays well below the platform wall.
MAX_SOURCES = 36
MAX_CITATIONS = 20
MAX_EVIDENCE_CHARS = 96_000
SEARCH_SHOW_CHARS = 900
PAGE_HEAD_CHARS = 1800
PAGE_WINDOW_CHARS = 3400
PAGE_WINDOWS = 3
LOCAL_READ_CHARS = 9000
RETAIN_MARGIN = 320

_STATE = {"spend_left": None, "allowed_models": ()}


# ---------------------------------------------------------------------------
# Runtime / budget helpers
# ---------------------------------------------------------------------------

def _note_budget(payload: Any) -> None:
    budget = getattr(payload, "budget", None)
    value = getattr(budget, "session_remaining_budget_usd", None)
    if isinstance(value, (int, float)):
        _STATE["spend_left"] = float(value)


def _money_left() -> float:
    value = _STATE.get("spend_left")
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _time_left(deadline: float) -> float:
    return max(0.0, deadline - monotonic())


async def _load_runtime_models() -> None:
    existing = _STATE.get("allowed_models")
    if isinstance(existing, tuple) and existing:
        return
    try:
        info = await tooling_info(timeout=8.0)
        _note_budget(info)
        response = getattr(info, "response", None)
        if isinstance(response, dict):
            table = response.get("allowed_llm_provider_models")
            if isinstance(table, dict):
                values = table.get(LLM_PROVIDER)
                if isinstance(values, list):
                    _STATE["allowed_models"] = tuple(str(x) for x in values if isinstance(x, str))
    except Exception:
        _STATE["allowed_models"] = ()


def _model_order(preferred: tuple[str, ...]) -> list[str]:
    current = _STATE.get("allowed_models")
    if not isinstance(current, tuple) or not current:
        return list(preferred)
    allowed = set(current)
    picked = [m for m in preferred if m in allowed]
    if picked:
        return picked
    return list(current[:3])


async def _chat(
    models: tuple[str, ...],
    messages: list[dict[str, Any]],
    deadline: float,
    max_output_tokens: int,
    temperature: float,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    parallel_tool_calls: bool = False,
) -> Any:
    for model in _model_order(models):
        timeout = min(CHAT_TIMEOUT, _time_left(deadline) - 4.0)
        if timeout <= 5.0:
            return None
        try:
            payload = await asyncio.wait_for(
                llm_chat(
                    provider=LLM_PROVIDER,
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    timeout=timeout,
                ),
                timeout=min(timeout + 5.0, max(1.0, _time_left(deadline) - 1.0)),
            )
            _note_budget(payload)
            return payload
        except Exception:
            continue
    return None


def _raw_text(payload: Any) -> str:
    if payload is None:
        return ""
    llm = getattr(payload, "llm", None)
    text = getattr(llm, "raw_text", None)
    return (text or "").strip()


def _assistant_message(payload: Any) -> Any:
    try:
        choices = payload.llm.choices
        if choices:
            return choices[0].message
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Evidence ledger
# ---------------------------------------------------------------------------

class SourceBook:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(
        self,
        receipt_id: str,
        result_id: str,
        text: str,
        title: str,
        url: str,
        shown: list[tuple[int, int]],
        kind: str,
    ) -> int:
        if len(self.rows) >= MAX_SOURCES:
            return 0
        row = {
            "receipt_id": receipt_id,
            "result_id": result_id,
            "text": text,
            "title": title[:220],
            "url": url[:500],
            "shown": shown,
            "retained": [],
            "kind": kind,
        }
        self.rows.append(row)
        return len(self.rows)

    def row(self, number: int) -> dict[str, Any] | None:
        if 1 <= number <= len(self.rows):
            return self.rows[number - 1]
        return None

    def keep_quote(self, number: int, quote: str) -> bool:
        row = self.row(number)
        if row is None:
            return False
        source = str(row.get("text") or "")
        needle = (quote or "").strip()
        if len(needle) < 10:
            return False
        pos = source.find(needle)
        if pos < 0:
            pos = source.lower().find(needle.lower())
        if pos < 0:
            return False
        start = max(0, pos - RETAIN_MARGIN)
        end = min(len(source), pos + len(needle) + RETAIN_MARGIN)
        retained = row.get("retained")
        if not isinstance(retained, list):
            retained = []
            row["retained"] = retained
        retained.append((start, end))
        return True

    def digest(self, max_chars: int = 48_000) -> str:
        parts: list[str] = []
        used = 0
        for i, row in enumerate(self.rows, 1):
            text = str(row.get("text") or "")
            title = str(row.get("title") or "")
            url = str(row.get("url") or "")
            spans = row.get("retained") or row.get("shown") or []
            chunks: list[str] = []
            for span in spans[:3]:
                start = max(0, int(span[0]))
                end = min(len(text), int(span[1]))
                if end > start:
                    chunks.append(text[start:end])
            if not chunks:
                chunks.append(text[:1200])
            body = "\n...\n".join(chunks)
            block = f"[{i}] {title}\nURL: {url}\n{body}\n"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n".join(parts)

    def citation(self, number: int) -> tuple[CitationRef | None, int]:
        row = self.row(number)
        if row is None:
            return None, 0
        receipt = str(row.get("receipt_id") or "")
        result = str(row.get("result_id") or "")
        text = str(row.get("text") or "")
        if not receipt or not result or not text:
            return None, 0
        spans = row.get("retained") or row.get("shown") or []
        cleaned: list[tuple[int, int]] = []
        for raw in spans[:4]:
            start = max(0, min(int(raw[0]), len(text)))
            end = max(start + 1, min(int(raw[1]), len(text)))
            if end <= start:
                continue
            # Make very narrow retained excerpts easier for the judge to interpret.
            width = end - start
            if width < 1800:
                need = 1800 - width
                left = min(start, need // 2)
                start -= left
                end = min(len(text), end + (need - left))
            cleaned.append((start, end))
        if not cleaned:
            return None, 0
        cleaned.sort()
        merged: list[list[int]] = []
        for start, end in cleaned:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        slices = [CitationSlice(start=a, end=b) for a, b in merged if b > a]
        if not slices:
            return None, 0
        cost = sum(b - a for a, b in merged)
        return CitationRef(receipt_id=receipt, result_id=result, slices=slices), cost


# ---------------------------------------------------------------------------
# Source localization
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9][a-z0-9'._-]{2,}", re.I)
_STOP = frozenset(
    "the and for with from that this those these what which when where who whom "
    "whose into over under among about after before between according than then "
    "have has had were was are been being its their there would could should".split()
)
_CITE = re.compile(r"\[(\d{1,3})\]")


def _terms(text: str) -> set[str]:
    return {x.lower() for x in _WORD.findall(text or "") if x.lower() not in _STOP}


def _windows(text: str, focus: str, width: int, count: int) -> list[tuple[int, int]]:
    if len(text) <= width:
        return [(0, len(text))]
    terms = _terms(focus)
    step = max(700, width // 3)
    scored: list[tuple[int, int]] = []
    low = text.lower()
    pos = 0
    while pos < len(text):
        segment = low[pos:pos + width]
        score = 0
        for term in terms:
            if term in segment:
                score += 1
        scored.append((score, pos))
        if pos + width >= len(text):
            break
        pos += step
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[tuple[int, int]] = []
    for _, start in scored:
        end = min(len(text), start + width)
        overlap = False
        for a, b in picked:
            if start < b and a < end:
                overlap = True
                break
        if overlap:
            continue
        picked.append((start, end))
        if len(picked) >= count:
            break
    picked.sort()
    return picked


def _source_numbers(text: str, limit: int) -> list[int]:
    found: list[int] = []
    for match in _CITE.finditer(text or ""):
        value = int(match.group(1))
        if 1 <= value <= limit and value not in found:
            found.append(value)
    return found


def _citations_for(text_for_markers: str, book: SourceBook) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for number in _source_numbers(text_for_markers, len(book.rows)):
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


# ---------------------------------------------------------------------------
# Research tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for one precise fact or candidate set.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch a result URL and expose the most relevant page regions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "focus": {"type": "string"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_in_source",
            "description": "Find a phrase or regex-like literal inside a source already fetched.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "integer"},
                    "pattern": {"type": "string"},
                },
                "required": ["source", "pattern"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_source_region",
            "description": "Read a character range from a source already fetched.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "integer"},
                    "offset": {"type": "integer"},
                    "length": {"type": "integer"},
                },
                "required": ["source", "offset"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keep_quote",
            "description": "Retain the exact quoted source text that proves a final claim.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "integer"},
                    "quote": {"type": "string"},
                },
                "required": ["source", "quote"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
]


async def _search_one(query_text: str, book: SourceBook) -> str:
    q = " ".join((query_text or "").split())
    if not q:
        return "search skipped: empty query"
    attempts = [q]
    loose = re.sub(r"\bsite:\S+", "", q).replace('"', " ")
    loose = " ".join(loose.split())
    if loose and loose != q:
        attempts.append(loose)
    payload = None
    for attempt in attempts:
        try:
            payload = await search_web(
                attempt,
                provider=SEARCH_PROVIDER,
                num=8,
                timeout=TOOL_TIMEOUT,
            )
            if getattr(payload, "results", None):
                break
        except Exception:
            payload = None
    if payload is None:
        return f"web_search failed for: {q}"
    _note_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    results = list(getattr(payload, "results", None) or [])
    lines = [f"search results for {q!r}:"]
    for item in results:
        if len(book.rows) >= MAX_SOURCES:
            break
        rid = getattr(item, "result_id", None)
        note = str(getattr(item, "note", None) or "")
        if not isinstance(rid, str) or not rid or not note.strip() or not receipt:
            continue
        title = str(getattr(item, "title", None) or "")
        url = str(getattr(item, "url", None) or "")
        shown_end = min(len(note), max(100, SEARCH_SHOW_CHARS))
        number = book.add(receipt, rid, note, title, url, [(0, shown_end)], "search")
        if number:
            lines.append(f"[{number}] {title} — {url}\n{note[:SEARCH_SHOW_CHARS]}")
    return "\n".join(lines)


async def _fetch_one(url: str, focus: str, question: str, book: SourceBook) -> str:
    target = (url or "").strip()
    if not target:
        return "read_url skipped: empty url"
    payload = None
    for _ in (0, 1):
        try:
            payload = await fetch_page(target, provider=SEARCH_PROVIDER, timeout=FETCH_TIMEOUT)
            if getattr(payload, "results", None):
                break
        except Exception:
            payload = None
    if payload is None:
        return f"read_url failed: {target}"
    _note_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    results = list(getattr(payload, "results", None) or [])
    if not receipt or not results:
        return f"read_url returned no citable content: {target}"
    item = results[0]
    rid = getattr(item, "result_id", None)
    note = str(getattr(item, "note", None) or "")
    if not isinstance(rid, str) or not rid or not note.strip():
        return f"read_url returned no usable text: {target}"
    if len(note) <= 7000:
        shown = [(0, len(note))]
    else:
        win = _windows(note, f"{question} {focus}", PAGE_WINDOW_CHARS, PAGE_WINDOWS)
        shown = [(0, min(PAGE_HEAD_CHARS, len(note)))] + win
    number = book.add(receipt, rid, note, target, target, shown, "page")
    if not number:
        return "source capacity reached"
    rendered: list[str] = [f"[{number}] fetched {target} ({len(note)} chars)"]
    for start, end in shown:
        rendered.append(f"section {start}:{end}\n{note[start:end]}")
    return "\n---\n".join(rendered)


def _find_source(number: int, pattern: str, book: SourceBook) -> str:
    row = book.row(number)
    if row is None:
        return f"source [{number}] does not exist"
    text = str(row.get("text") or "")
    needle = (pattern or "").strip()
    if not needle:
        return "find_in_source skipped: empty pattern"
    low = text.lower()
    target = needle.lower()
    positions: list[int] = []
    pos = 0
    while len(positions) < 6:
        hit = low.find(target, pos)
        if hit < 0:
            break
        positions.append(hit)
        pos = hit + max(1, len(target))
    if not positions:
        return f"no literal match for {needle!r} in [{number}]"
    parts: list[str] = []
    for hit in positions:
        start = max(0, hit - 500)
        end = min(len(text), hit + len(needle) + 900)
        parts.append(f"[{number}] offset {hit}\n{text[start:end]}")
    return "\n---\n".join(parts)


def _read_region(number: int, offset: int, length: int, book: SourceBook) -> str:
    row = book.row(number)
    if row is None:
        return f"source [{number}] does not exist"
    text = str(row.get("text") or "")
    start = max(0, min(int(offset), len(text)))
    size = max(200, min(int(length or LOCAL_READ_CHARS), LOCAL_READ_CHARS))
    end = min(len(text), start + size)
    shown = row.get("shown")
    if isinstance(shown, list):
        shown.append((start, end))
    return f"[{number}] region {start}:{end}\n{text[start:end]}"


def _keep(number: int, quote: str, book: SourceBook) -> str:
    if book.keep_quote(number, quote):
        return f"retained exact supporting quote from [{number}]"
    return f"could not locate that exact quote in [{number}]"


async def _run_tool(call: Any, question: str, book: SourceBook) -> str:
    try:
        args = json.loads(getattr(call, "arguments", None) or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    name = str(getattr(call, "name", "") or "")
    if name == "web_search":
        return await _search_one(str(args.get("query") or ""), book)
    if name == "read_url":
        return await _fetch_one(
            str(args.get("url") or ""),
            str(args.get("focus") or ""),
            question,
            book,
        )
    if name == "find_in_source":
        return _find_source(int(args.get("source") or 0), str(args.get("pattern") or ""), book)
    if name == "read_source_region":
        return _read_region(
            int(args.get("source") or 0),
            int(args.get("offset") or 0),
            int(args.get("length") or LOCAL_READ_CHARS),
            book,
        )
    if name == "keep_quote":
        return _keep(int(args.get("source") or 0), str(args.get("quote") or ""), book)
    return f"unknown tool: {name}"


# ---------------------------------------------------------------------------
# Stage 1: exploratory tool loop
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM = """You are the explorer in a factual research system.
Your job is to solve the user's question, not merely collect links.

Work from candidate sets and atomic conditions. Use your own knowledge to form
hypotheses, then verify every load-bearing proper noun, date, quantity, ranking,
threshold, exclusion, and named premise with sources. Prefer the primary source
(agency, filing, official statistics, organization page) when available.

Use tools actively. Search narrowly. Fetch promising pages. For long fetched
pages, use find_in_source and read_source_region instead of repeating web search.
Whenever you see decisive source text, call keep_quote with the exact words so
that the final citation material contains what proves the claim.

For set/intersection questions, enumerate the whole relevant pool and verify both
survivors and meaningful near-misses. For multi-part questions, cover every part.
Do not stop after finding one plausible answer if the question asks for all.

When evidence is sufficient, write a provisional answer with [n] markers placed
immediately after the claims they support. Do not include planning narration."""


async def _research(question: str, book: SourceBook, deadline: float) -> tuple[str, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": RESEARCH_SYSTEM},
        {"role": "user", "content": question},
    ]
    best = ""
    for turn in range(MAX_RESEARCH_TURNS):
        if _time_left(deadline) <= RESEARCH_STOP_LEFT:
            break
        if _money_left() <= 0.035:
            break
        payload = await _chat(
            RESEARCH_MODELS,
            messages,
            deadline,
            max_output_tokens=4300,
            temperature=0.2,
            tools=TOOLS,
            tool_choice="auto",
            parallel_tool_calls=True,
        )
        if payload is None:
            break
        assistant = _assistant_message(payload)
        if assistant is None:
            text = _raw_text(payload)
            if text:
                best = text
            break
        try:
            input_message = assistant.to_input_message()
        except Exception:
            text = _raw_text(payload)
            if text:
                best = text
            break
        messages.append(input_message)
        calls = list(getattr(assistant, "tool_calls", None) or [])
        text = str(getattr(assistant, "content", None) or _raw_text(payload) or "").strip()
        if not calls:
            if text:
                best = text
            break
        # Execute the model's independent calls concurrently. Each call returns its
        # own linked tool_call_id result message; source numbering is assigned by
        # the single-threaded event loop as each tool finishes.
        jobs = [_run_tool(call, question, book) for call in calls]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for idx, call in enumerate(calls):
            result = results[idx]
            if isinstance(result, BaseException):
                content = "tool failed"
            else:
                content = str(result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(getattr(call, "id", "") or f"call-{turn}-{idx}"),
                    "content": content,
                }
            )
    if not best and book.rows:
        best = "Evidence collected; final writer must derive the answer from the source book."
    return best, messages


# ---------------------------------------------------------------------------
# Stage 2: skeptical coverage pass + targeted repair
# ---------------------------------------------------------------------------

_CRITIC_JSON = re.compile(r"\{.*\}", re.S)


def _parse_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    match = _CRITIC_JSON.search(raw)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


async def _critic(question: str, draft: str, book: SourceBook, deadline: float) -> dict[str, Any]:
    if _time_left(deadline) < 78.0 or _money_left() < 0.06:
        return {}
    evidence = book.digest(25_000)
    system = (
        "You are a skeptical fact-check editor. Identify only defects that can change "
        "the answer or its score. Return JSON only."
    )
    user = f"""Question:\n{question}\n\nProvisional answer:\n{draft}\n\nEvidence:\n{evidence}\n\nReturn:
{{"missing_queries":[up to 4 precise searches],"unsupported_claims":[short strings],
"format_risk":"short note or empty","answer_hint":"corrected likely answer or empty"}}
Focus on incomplete candidate pools, an unproved threshold/exclusion, wrong year or
unit, wrong output kind, or a claim whose cited source excerpt does not actually say it."""
    payload = await _chat(
        CRITIC_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        max_output_tokens=1600,
        temperature=0.0,
    )
    return _parse_object(_raw_text(payload)) or {}


async def _repair_from_critic(
    question: str,
    critic: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> None:
    raw = critic.get("missing_queries")
    if not isinstance(raw, list):
        return
    queries: list[str] = []
    for value in raw:
        q = " ".join(str(value).split())
        if q and q not in queries:
            queries.append(q)
        if len(queries) >= MAX_REPAIR_QUERIES:
            break
    if not queries:
        return
    jobs = [_search_one(q, book) for q in queries]
    try:
        await asyncio.wait_for(
            asyncio.gather(*jobs, return_exceptions=True),
            timeout=min(45.0, max(5.0, _time_left(deadline) - FINAL_RESERVE)),
        )
    except Exception:
        return


# ---------------------------------------------------------------------------
# Stage 3: final evidence-bound writer
# ---------------------------------------------------------------------------

FINAL_SYSTEM = """You are the final answer editor. Produce the strongest answer
supported by the supplied source book.

Rules:
1. Sentence one directly answers the requested kind (country, film, series, value,
   set, count, etc.). No research preamble.
2. Apply every condition in the question. For set questions, do not silently
   pre-filter the candidate pool; mention meaningful exclusions when they prove
   completeness.
3. Put [n] immediately after each factual claim using the numbered source that
   actually states it. Prefer retained primary-source evidence. Never invent a
   citation number.
4. Preserve exact source labels, units, dates, spellings, and numeric scale.
5. If the question asks for two or more subanswers, answer all of them.
6. If the user explicitly says output ONLY / nothing else / no explanation, make
   the ANSWER block contain only the requested bare value(s), without [n]. Put the
   supporting [n] markers in PROOF so the caller can attach citation refs while
   returning only the bare ANSWER block.
7. Otherwise the ANSWER block may contain concise proof and [n] markers.

Return exactly two labeled blocks:
ANSWER:
<answer to return>
PROOF:
<compact claim-to-source proof with [n] markers; may repeat key facts>"""


def _split_final(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    m_answer = re.search(r"(?:^|\n)ANSWER:\s*", raw, re.I)
    m_proof = re.search(r"(?:^|\n)PROOF:\s*", raw, re.I)
    if m_answer and m_proof and m_proof.start() > m_answer.end():
        answer = raw[m_answer.end():m_proof.start()].strip()
        proof = raw[m_proof.end():].strip()
        return answer, proof
    return raw, raw


def _fallback_text(question: str, book: SourceBook) -> str:
    if not book.rows:
        return f"Unable to complete a supported answer for: {question[:400]}"
    lines = ["Best-supported findings from retrieved sources:"]
    for i, row in enumerate(book.rows[:6], 1):
        preview = str(row.get("text") or "")[:500].replace("\n", " ")
        if preview:
            lines.append(f"- {preview} [{i}]")
    return "\n".join(lines)


async def _finalize(
    question: str,
    draft: str,
    critic: dict[str, Any],
    book: SourceBook,
    deadline: float,
) -> tuple[str, str]:
    evidence = book.digest(58_000)
    hint = str(critic.get("answer_hint") or "")[:1500]
    user = f"""Question:\n{question}\n\nExplorer draft:\n{draft}\n\nCritic hint:\n{hint}\n\nNumbered source book:\n{evidence}\n\nWrite the final answer now."""
    payload = await _chat(
        FINAL_MODELS,
        [{"role": "system", "content": FINAL_SYSTEM}, {"role": "user", "content": user}],
        deadline,
        max_output_tokens=5200,
        temperature=0.1,
    )
    text = _raw_text(payload)
    if not text:
        text = draft if draft.strip() else _fallback_text(question, book)
    answer, proof = _split_final(text)
    if not answer.strip():
        answer = _fallback_text(question, book)
    if not proof.strip():
        proof = answer
    return answer.strip()[:60_000], proof.strip()[:40_000]


# ---------------------------------------------------------------------------
# Structured-output conversion
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _schema_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    value = schema.get("type")
    return value if isinstance(value, str) else ""


def _coerce(answer: str, schema: Any, depth: int = 0) -> Any:
    if depth > 5 or not isinstance(schema, dict):
        return answer[:300]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        low = answer.lower()
        for item in enum:
            if isinstance(item, str) and item.lower() in low:
                return item
        return enum[0]
    kind = _schema_type(schema)
    if not kind:
        for key in ("anyOf", "oneOf", "allOf"):
            options = schema.get(key)
            if isinstance(options, list):
                for option in options:
                    if isinstance(option, dict) and option.get("type") != "null":
                        return _coerce(answer, option, depth + 1)
        kind = "string"
    if kind == "object":
        props = schema.get("properties")
        props = props if isinstance(props, dict) else {}
        required = schema.get("required")
        keys = required if isinstance(required, list) else list(props.keys())
        out: dict[str, Any] = {}
        for key in keys:
            name = str(key)
            child = props.get(name)
            out[name] = _coerce(answer, child if isinstance(child, dict) else {}, depth + 1)
        return out
    if kind == "array":
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            item_schema = {}
        clean = _CITE.sub("", answer)
        pieces = [x.strip(" -*\t") for x in re.split(r"[\n;]", clean) if x.strip()]
        if not pieces:
            pieces = [clean[:300]]
        return [_coerce(x[:500], item_schema, depth + 1) for x in pieces[:20]]
    if kind in ("integer", "number"):
        clean = _CITE.sub("", answer)
        match = _NUMBER.search(clean)
        if not match:
            return 0 if kind == "integer" else 0.0
        value = match.group(0).replace(",", "")
        try:
            return int(float(value)) if kind == "integer" else float(value)
        except Exception:
            return 0 if kind == "integer" else 0.0
    if kind == "boolean":
        return not bool(re.match(r"\s*(?:no|false|none)\b", answer, re.I))
    if kind == "null":
        return None
    return _CITE.sub("", answer).strip()[:600]


async def _structured(question: str, answer: str, schema: Any, deadline: float) -> Any:
    if _time_left(deadline) < 8.0:
        return _coerce(answer, schema)
    user = f"""Convert the researched answer to JSON matching the schema exactly.
Return only the JSON value, no markdown and no explanation.
Schema:\n{json.dumps(schema, ensure_ascii=False)}\n\nQuestion:\n{question}\n\nAnswer:\n{answer}"""
    payload = await _chat(
        SCHEMA_MODELS,
        [{"role": "system", "content": "Strict JSON schema formatter."}, {"role": "user", "content": user}],
        deadline,
        max_output_tokens=1800,
        temperature=0.0,
    )
    raw = _raw_text(payload)
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except Exception:
        return _coerce(answer, schema)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

@entrypoint("query")
async def query(query: Query) -> Response:
    question = (query.text or "").strip()
    if not question:
        if query.output_schema is not None:
            return Response(output=_coerce("", query.output_schema))
        return Response(text="No question provided.")

    deadline = monotonic() + WALL_SECONDS
    try:
        await _load_runtime_models()
    except Exception:
        pass

    book = SourceBook()
    draft = ""
    transcript: list[dict[str, Any]] = []
    try:
        draft, transcript = await _research(question, book, deadline)
    except Exception:
        draft = ""
        transcript = []

    critic: dict[str, Any] = {}
    try:
        critic = await _critic(question, draft, book, deadline)
        await _repair_from_critic(question, critic, book, deadline)
    except Exception:
        critic = {}

    try:
        answer, proof = await _finalize(question, draft, critic, book, deadline)
    except Exception:
        answer = draft.strip() if draft.strip() else _fallback_text(question, book)
        proof = answer

    marker_text = f"{answer}\n{proof}"
    try:
        citations = _citations_for(marker_text, book)
    except Exception:
        citations = []

    if query.output_schema is not None:
        try:
            output = await _structured(question, answer, query.output_schema, deadline)
            return Response(output=output, citations=citations or None)
        except Exception:
            output = _coerce(answer, query.output_schema)
            return Response(output=output, citations=citations or None)

    clean = answer.strip()
    if not clean:
        clean = _fallback_text(question, book)
    try:
        return Response(text=clean[:80_000], citations=citations or None)
    except Exception:
        return Response(text=clean[:80_000])
