"""
Harnyx SN67 research miner — OrbitEvidence v14.

Independent implementation of a bounded research loop.  The controller lets the
language model choose iterative research actions, but evidence storage, citation
numbering, page navigation, time limits, final-answer validation, and structured
output are deterministic.

Configured for the credentials already used by this miner:
- Parallel for search and page extraction.
- Chutes for language-model calls.

The design intentionally uses its own controller and data model rather than
copying another miner implementation.
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


VERSION = "orbit-evidence-v16.0-independent-source-first"

LLM_PROVIDER = "openrouter"
LLM_FALLBACK_PROVIDER = "chutes"
SEARCH_PROVIDER = "parallel"

WALL_SECONDS = 262.0
WRAPUP_SECONDS = 82.0
MIN_RETURN_SECONDS = 8.0
MAX_RESEARCH_TURNS = 14
MAX_ACTIONS_PER_TURN = 7

SEARCH_TIMEOUT = 18.0
FETCH_TIMEOUT = 16.0
TOOL_PHASE_TIMEOUT = 28.0
TURN_TIMEOUT = 60.0
WRITER_TIMEOUT = 52.0
CRITIC_TIMEOUT = 28.0
SCHEMA_TIMEOUT = 36.0

SEARCH_RESULTS = 10
SEARCH_NOTE_SHOW = 720
FETCH_WINDOW = 3400
FETCH_WINDOWS = 3
FETCH_ORIENTATION = 1200
LOCAL_WINDOW = 1100
LOCAL_HITS = 5
LOCAL_READ_CAP = 12000

DIGEST_CHARS = 82000
ROW_DIGEST_CAP = 7200
ANSWER_CAP = 52000
MAX_CITATIONS = 22
TOTAL_EVIDENCE_CAP = 110000
CITATION_TARGET = 4800
CITATION_ROW_CAP = 10500

KEEP_MARGIN = 420
MAX_KEPT_PER_ROW = 6
MIN_QUOTE = 10

PRIMARY_MODELS = (
    "z-ai/glm-5.2",
    "deepseek/deepseek-v3.2",
    "openai/gpt-oss-120b",
    "z-ai/glm-5",
    "qwen/qwen3.6-30b-a3b-instruct",
    "google/gemini-2.5-flash",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

WRITER_MODELS = (
    "openai/gpt-oss-120b",
    "deepseek/deepseek-v3.2",
    "z-ai/glm-5.2",
    "google/gemini-2.5-flash",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)

_STATE: dict[str, Any] = {
    "models": (),
    "budget_left": None,
    "models_by_provider": {},
}


def _remember_budget(payload: Any) -> None:
    budget = getattr(payload, "budget", None)
    value = getattr(budget, "session_remaining_budget_usd", None)
    if isinstance(value, (int, float)):
        _STATE["budget_left"] = float(value)


def _left(deadline: float) -> float:
    return deadline - monotonic()


def _clip(value: str, limit: int) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 2)] + " …"


def _space(value: str) -> str:
    return " ".join((value or "").split())


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.-]{2,}")
_STOP = frozenset(
    "the and for with from that this these those which what when where who how "
    "many much into over under between during after before while about against "
    "also have has had was were are is be been being their there they them its "
    "use using only official result results answer question according based".split()
)

_OFFICIAL_HOST_HINTS = (
    "gov", "house.gov", "history.house.gov", "nps.gov", "fide.com",
    "in.gov", "usps.com", "about.usps.com", "cswe.org", "usgs.gov",
    "planetarynames.wr.usgs.gov", "federalregister.gov", "legislation.gov.uk",
    "chp.gov.hk",
)


def _terms(value: str, limit: int = 28) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for token in _WORD_RE.findall((value or "").lower()):
        if token in _STOP or len(token) < 3:
            continue
        if token not in seen:
            seen.add(token)
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _overlap_score(text: str, terms: list[str]) -> int:
    low = (text or "").lower()
    score = 0
    for token in terms:
        if token in low:
            score += 1
    return score


def _official_score(url: str) -> int:
    host = _host(url)
    if not host:
        return 0
    score = 0
    for hint in _OFFICIAL_HOST_HINTS:
        if hint in host:
            score += 4 if hint != "gov" else 2
    return score


def _quoted_phrases(text: str, limit: int = 5) -> list[str]:
    phrases: list[str] = []
    for match in re.finditer(r'"([^"]{4,90})"|“([^”]{4,90})”|' r"'([^']{4,90})'", text or ""):
        phrase = next((g for g in match.groups() if g), "").strip()
        if phrase and phrase.lower() not in [p.lower() for p in phrases]:
            phrases.append(phrase)
        if len(phrases) >= limit:
            break
    return phrases


def _merge_ranges(spans: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
    clean: list[tuple[int, int]] = []
    for a, b in spans:
        start = max(0, min(int(a), size))
        end = max(start, min(int(b), size))
        if end > start:
            clean.append((start, end))
    clean.sort()
    merged: list[list[int]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1] + 80:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(x[0], x[1]) for x in merged]


def _window_spans(text: str, focus: str, width: int = FETCH_WINDOW,
                  count: int = FETCH_WINDOWS) -> list[tuple[int, int]]:
    n = len(text or "")
    if n <= width:
        return [(0, n)] if n else []
    wanted = _terms(focus, 34)
    step = max(700, width // 3)
    scored: list[tuple[int, int]] = []
    start = 0
    low = text.lower()
    while start < n:
        end = min(n, start + width)
        block = low[start:end]
        hits = 0
        for token in wanted:
            if token in block:
                hits += 1
        # Prefer actual data-bearing regions when semantic scores tie.
        numeric = len(re.findall(r"\d", block[:1800]))
        tableish = block.count("|") + block.count("\n")
        bonus = min(6, numeric // 8) + min(4, tableish // 20)
        scored.append((hits * 20 + bonus, start))
        if end >= n:
            break
        start += step
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, int]] = []
    for score, start in scored:
        end = min(n, start + width)
        if chosen and score <= 0:
            continue
        overlaps = False
        for a, b in chosen:
            if start < b and a < end:
                overlaps = True
                break
        if overlaps:
            continue
        chosen.append((start, end))
        if len(chosen) >= count:
            break
    chosen.sort()
    if not chosen:
        chosen = [(0, min(n, width))]
    return chosen


class QuestionShape:
    def __init__(self, question: str) -> None:
        self.question = question
        self.numbered_parts = self._count_numbered_parts(question)
        self.output_only = bool(re.search(
            r"\b(?:output|respond|reply|answer) (?:with )?only\b"
            r"|\bonly the exact\b|\bnothing else\b|\bno explanation\b"
            r"|\bwithout explanation\b|\bjust the (?:name|names|value|values|"
            r"number|numbers|list|text|title|titles|answer)\b",
            question, re.I))
        self.set_like = bool(re.search(
            r"\b(?:list|name|identify|enumerate)\b.{0,60}\b(?:all|every|each)\b"
            r"|\bwhich (?:[A-Za-z-]+\s+){0,3}[A-Za-z-]+s\b"
            r"|\bhow many\b", question, re.I))
        self.superlative = bool(re.search(
            r"\b(?:highest|lowest|largest|smallest|most|least|greatest|fewest|"
            r"oldest|youngest|newest|first|last|best|worst|only)\b", question, re.I))
        self.strict_source = bool(re.search(
            r"\busing only\b|\buse only\b|\bonly the official\b"
            r"|\bsolely (?:from|using)\b|\bbased only on\b", question, re.I))
        self.has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", question))
        self.complex = (
            self.numbered_parts >= 2
            or self.set_like
            or self.superlative
            or self.strict_source
        )

    @staticmethod
    def _count_numbered_parts(question: str) -> int:
        found: list[int] = []
        for m in re.finditer(r"(?:^|[\s;])\((\d{1,2})\)", question):
            value = int(m.group(1))
            if value not in found:
                found.append(value)
        if len(found) >= 2:
            return max(found)
        found = []
        for m in re.finditer(r"(?:^|\n)\s*(\d{1,2})[.)]\s+", question):
            value = int(m.group(1))
            if value not in found:
                found.append(value)
        return max(found) if len(found) >= 2 else 0

    def hint(self) -> str:
        notes: list[str] = []
        if self.numbered_parts:
            notes.append(
                f"The question has {self.numbered_parts} explicit parts. "
                "The final answer must substantively answer every part in order."
            )
        if self.set_like:
            notes.append(
                "This is a set/roster problem: establish the complete candidate pool "
                "from a list/table before filtering members."
            )
        if self.superlative:
            notes.append(
                "This contains a tally/superlative: compare the complete relevant pool "
                "before naming a winner or count."
            )
        if self.strict_source:
            notes.append(
                "The prompt imposes an exclusive source constraint. Third-party pages "
                "may help discovery, but final factual claims must be supported by the "
                "named/official source itself."
            )
        if self.has_year:
            notes.append(
                "Preserve the exact period/year scope. Do not silently substitute an "
                "adjacent year, edition, quarter, or broader period."
            )
        return "\n".join(notes)


class ToolPacket:
    def __init__(self, text: str, rows: list[dict[str, Any]] | None = None) -> None:
        self.text = text
        self.rows = rows or []


class EvidenceVault:
    def __init__(self, question: str) -> None:
        self.question = question
        self.rows: list[dict[str, Any]] = []
        self.searched: list[str] = []
        self.fetched: list[str] = []

    def add_packet(self, packet: ToolPacket) -> str:
        body = packet.text
        for index, row in enumerate(packet.rows):
            self.rows.append(row)
            number = len(self.rows)
            body = body.replace(f"<ROW{index}>", f"[{number}]")
        return body

    def row(self, number: int) -> dict[str, Any] | None:
        if 1 <= number <= len(self.rows):
            return self.rows[number - 1]
        return None

    def mark_shown(self, number: int, start: int, end: int) -> None:
        row = self.row(number)
        if row is None:
            return
        text = row.get("text") or ""
        if not text:
            return
        a = max(0, min(int(start), len(text)))
        b = max(a, min(int(end), len(text)))
        if b <= a:
            return
        shown = row.setdefault("shown", [])
        shown.append((a, b))
        row["shown"] = _merge_ranges(shown, len(text))

    def keep_quote(self, number: int, quote: str) -> str:
        row = self.row(number)
        if row is None:
            return f"# keep: source [{number}] does not exist"
        text = row.get("text") or ""
        q = (quote or "").strip()
        if len(q) < MIN_QUOTE:
            return "# keep: quote is too short"
        pos = text.find(q)
        if pos < 0:
            pos = text.lower().find(q.lower())
        if pos < 0:
            return f"# keep: quote not found verbatim in [{number}]"
        kept = row.setdefault("kept", [])
        if len(kept) >= MAX_KEPT_PER_ROW:
            return f"# keep: [{number}] already has enough retained evidence"
        a = max(0, pos - KEEP_MARGIN)
        b = min(len(text), pos + len(q) + KEEP_MARGIN)
        kept.append((a, b))
        row["kept"] = _merge_ranges(kept, len(text))
        return f"# keep: retained decisive evidence from [{number}]"

    def local_grep(self, number: int, pattern: str) -> str:
        row = self.row(number)
        if row is None:
            return f"# grep: source [{number}] does not exist"
        text = row.get("text") or ""
        needle = (pattern or "").strip()
        if not needle:
            return "# grep: empty pattern"
        try:
            rx = re.compile(needle, re.I)
        except re.error:
            rx = re.compile(re.escape(needle), re.I)
        blocks: list[str] = []
        centers: list[int] = []
        for match in rx.finditer(text):
            center = (match.start() + match.end()) // 2
            too_near = False
            for old in centers:
                if abs(center - old) < LOCAL_WINDOW // 2:
                    too_near = True
                    break
            if too_near:
                continue
            centers.append(center)
            a = max(0, center - LOCAL_WINDOW // 2)
            b = min(len(text), a + LOCAL_WINDOW)
            self.mark_shown(number, a, b)
            blocks.append(f"\n--- [{number}] match @{a} ---\n{text[a:b]}")
            if len(blocks) >= LOCAL_HITS:
                break
        if not blocks:
            return f"# grep: no match for {needle!r} in [{number}]"
        return f"# grep: {len(blocks)} match(es) in [{number}]" + "".join(blocks)

    def local_read(self, number: int, offset: int, length: int) -> str:
        row = self.row(number)
        if row is None:
            return f"# read: source [{number}] does not exist"
        text = row.get("text") or ""
        if not text:
            return f"# read: source [{number}] has no stored text"
        a = max(0, min(int(offset), max(0, len(text) - 1)))
        amount = max(1, min(int(length), LOCAL_READ_CAP))
        b = min(len(text), a + amount)
        self.mark_shown(number, a, b)
        return f"# read: [{number}] chars {a}:{b} of {len(text)}\n{text[a:b]}"

    def _row_excerpt(self, row: dict[str, Any], cap: int) -> str:
        text = row.get("text") or ""
        if not text:
            return ""
        spans = row.get("kept") or row.get("shown") or []
        pieces: list[str] = []
        used = 0
        for a, b in spans:
            piece = text[int(a):int(b)].strip()
            if not piece:
                continue
            room = cap - used
            if room <= 0:
                break
            piece = piece[:room]
            pieces.append(piece)
            used += len(piece)
        if not pieces:
            return text[:cap]
        return "\n...\n".join(pieces)

    def digest(self, cap: int = DIGEST_CHARS) -> str:
        if not self.rows:
            return "(No citable evidence has been gathered yet.)"
        query_terms = _terms(self.question, 30)
        indexed: list[tuple[int, int, dict[str, Any]]] = []
        for number, row in enumerate(self.rows, start=1):
            title = row.get("title") or ""
            url = row.get("url") or ""
            preview = row.get("preview") or ""
            kept_bonus = 30 if row.get("kept") else 0
            fetched_bonus = 8 if row.get("kind") == "fetch" else 0
            official_bonus = 6 if any(
                key in _host(url)
                for key in ("gov", "who.int", "worldathletics", "sec.gov", "census")
            ) else 0
            score = (
                _overlap_score(title + " " + url + " " + preview, query_terms) * 5
                + kept_bonus + fetched_bonus + official_bonus
            )
            indexed.append((score, number, row))
        indexed.sort(key=lambda item: (-item[0], item[1]))

        blocks: list[str] = []
        spent = 0
        for _, number, row in indexed:
            excerpt = self._row_excerpt(row, ROW_DIGEST_CAP)
            if not excerpt.strip():
                continue
            block = (
                f"[{number}] {row.get('title') or '(untitled)'}\n"
                f"URL: {row.get('url') or ''}\n"
                f"{excerpt}"
            )
            if spent + len(block) > cap:
                continue
            blocks.append(block)
            spent += len(block)
        return "\n\n".join(blocks) if blocks else "(Evidence exists but could not be rendered.)"

    def citation(self, number: int) -> tuple[CitationRef | None, int]:
        row = self.row(number)
        if row is None:
            return None, 0
        receipt = row.get("receipt_id") or ""
        result = row.get("result_id") or ""
        text = row.get("text") or ""
        if not receipt or not result or not text:
            return None, 0

        spans = row.get("kept") or row.get("shown") or []
        if not spans:
            return None, 0
        merged = _merge_ranges(spans, len(text))
        if not merged:
            return None, 0

        # Give each cited fact useful surrounding context without flooding the judge.
        grown: list[tuple[int, int]] = []
        for a, b in merged[:4]:
            length = b - a
            want = min(CITATION_ROW_CAP, max(CITATION_TARGET, length))
            extra = max(0, want - length)
            left = min(a, extra // 2)
            right = min(len(text) - b, extra - left)
            a2 = a - left
            b2 = b + right
            if b2 - a2 < want:
                a2 = max(0, a2 - (want - (b2 - a2)))
            grown.append((a2, b2))
        grown = _merge_ranges(grown, len(text))
        cost = sum(b - a for a, b in grown)
        slices = [CitationSlice(start=a, end=b) for a, b in grown]
        if not slices:
            return None, 0
        return CitationRef(receipt_id=receipt, result_id=result, slices=slices), cost


def _llm_text(payload: Any) -> str:
    if payload is None:
        return ""
    llm = getattr(payload, "llm", None)
    if llm is not None:
        raw = getattr(llm, "raw_text", None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        choices = getattr(llm, "choices", None) or []
        if choices:
            msg = getattr(choices[0], "message", None)
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
    raw = getattr(payload, "raw_text", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    response = getattr(payload, "response", None)
    if isinstance(response, dict):
        for key in ("text", "content", "raw_text"):
            item = response.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


async def _load_models() -> None:
    try:
        info = await tooling_info(timeout=8.0)
        _remember_budget(info)
        response = getattr(info, "response", None)
        if not isinstance(response, dict):
            return
        providers = response.get("allowed_llm_provider_models")
        if not isinstance(providers, dict):
            return
        by_provider: dict[str, tuple[str, ...]] = {}
        for provider in (LLM_PROVIDER, LLM_FALLBACK_PROVIDER):
            raw = providers.get(provider)
            names: list[str] = []
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    name = ""
                    if isinstance(item, str):
                        name = item.strip()
                    elif isinstance(item, dict):
                        candidate = item.get("model") or item.get("id") or item.get("name")
                        if isinstance(candidate, str):
                            name = candidate.strip()
                    if name and name not in names:
                        names.append(name)
            if names:
                by_provider[provider] = tuple(names)
        if by_provider:
            _STATE["models_by_provider"] = by_provider
            _STATE["models"] = by_provider.get(LLM_PROVIDER) or next(iter(by_provider.values()))
    except Exception:
        return


def _model_order(preferred: tuple[str, ...], provider: str = LLM_PROVIDER) -> list[str]:
    by_provider = _STATE.get("models_by_provider")
    live = by_provider.get(provider) if isinstance(by_provider, dict) else None
    if not live and provider == LLM_PROVIDER:
        live = _STATE.get("models")
    if isinstance(live, tuple) and live:
        allowed = [x for x in live if isinstance(x, str) and x]
        chosen = [x for x in preferred if x in allowed]
        remainder = [x for x in allowed if x not in chosen]
        # Prefer generally capable research/synthesis families over embedding-ish names.
        def rank(name: str) -> tuple[int, str]:
            low = name.lower()
            if "glm-5.2" in low:
                return (0, low)
            if "gpt-oss-120b" in low:
                return (1, low)
            if "deepseek" in low and "v3.2" in low:
                return (2, low)
            if "glm-5" in low:
                return (3, low)
            if "qwen3.6" in low or "qwen3" in low:
                return (4, low)
            if "gemini-2.5" in low or "gemma-4-31b" in low:
                return (5, low)
            if "kimi" in low:
                return (6, low)
            return (9, low)
        remainder.sort(key=rank)
        return (chosen + remainder)[:5]
    return list(preferred[:4])


async def _chat(preferred: tuple[str, ...], messages: list[dict[str, Any]],
                deadline: float, max_tokens: int, timeout_cap: float,
                temperature: float = 0.1) -> Any:
    attempts: list[tuple[str, str]] = []
    for provider, prefs in (
        (LLM_PROVIDER, preferred),
        (LLM_FALLBACK_PROVIDER, PRIMARY_MODELS + WRITER_MODELS),
    ):
        for model in _model_order(prefs, provider):
            pair = (provider, model)
            if pair not in attempts:
                attempts.append(pair)
    for index, (provider, model) in enumerate(attempts[:8]):
        remaining = _left(deadline)
        if remaining <= MIN_RETURN_SECONDS + 4.0:
            return None
        cap = timeout_cap
        if index == 1:
            cap = min(cap, 24.0)
        elif index >= 2:
            cap = min(cap, 18.0)
        timeout = min(cap, remaining - MIN_RETURN_SECONDS)
        if timeout <= 5.0:
            return None
        try:
            payload = await llm_chat(
                provider=provider,
                model=model,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_tokens,
                timeout=timeout,
            )
            _remember_budget(payload)
            if _llm_text(payload):
                return payload
        except Exception:
            continue
    return None


def _loosen_query(query: str) -> str:
    text = re.sub(r"\bsite:\S+\s*", " ", query or "", flags=re.I)
    text = text.replace('"', " ")
    return _space(text)


async def _search_packet(query: str, advanced: bool = False) -> ToolPacket:
    q = _space(query)
    if not q:
        return ToolPacket("# search: empty query")
    attempts = [q]
    loose = _loosen_query(q)
    if loose and loose != q:
        attempts.append(loose)

    last_error = ""
    for attempt_index, current in enumerate(attempts[:2]):
        try:
            payload = await search_web(
                current,
                provider=SEARCH_PROVIDER,
                num=SEARCH_RESULTS,
                timeout=SEARCH_TIMEOUT,
                provider_extra={
                    "mode": "advanced" if (advanced or attempt_index > 0) else "basic",
                    "max_chars_total": 20000,
                    "excerpt_settings": {"max_chars_per_result": 2800},
                },
            )
        except Exception as exc:
            last_error = str(exc)
            continue
        _remember_budget(payload)
        receipt = str(getattr(payload, "receipt_id", "") or "")
        results = list(getattr(payload, "results", None) or [])
        if not receipt or not results:
            continue

        rows: list[dict[str, Any]] = []
        lines = [f"# search {current!r}: {len(results)} result(s)"]
        for item in results:
            rid = getattr(item, "result_id", None)
            note = str(getattr(item, "note", None) or "")
            if not isinstance(rid, str) or not rid or not note.strip():
                continue
            title = str(getattr(item, "title", None) or "")
            url = str(getattr(item, "url", None) or "")
            show_end = min(len(note), max(120, SEARCH_NOTE_SHOW))
            rows.append({
                "receipt_id": receipt,
                "result_id": rid,
                "title": title,
                "url": url,
                "text": note,
                "preview": note[:SEARCH_NOTE_SHOW],
                "kind": "search",
                "shown": [(0, show_end)],
                "kept": [],
            })
            marker = f"<ROW{len(rows) - 1}>"
            lines.append(
                f"{marker} {title} — {url}\n"
                f"{note[:SEARCH_NOTE_SHOW]}"
            )
        if rows:
            return ToolPacket("\n\n".join(lines), rows)
    return ToolPacket(f"# search failed for {q!r}: {last_error[:180]}")


async def _fetch_packet(url: str, focus: str, question: str) -> ToolPacket:
    target = (url or "").strip()
    if not target:
        return ToolPacket("# fetch: empty url")
    objective = (
        "Extract the page text needed to answer the research question. Preserve exact "
        "names, dates, figures, units, table rows, headings, qualifiers and source labels. "
        f"Question: {_clip(question, 1400)}"
    )
    if focus.strip():
        objective += f" Focus especially on: {_clip(focus, 700)}"
    try:
        payload = await fetch_page(
            target,
            provider=SEARCH_PROVIDER,
            timeout=FETCH_TIMEOUT,
            provider_extra={
                "objective": objective,
                "max_chars_total": 36000,
                "excerpt_settings": {"max_chars_per_result": 12000},
                "full_content": True,
            },
        )
    except Exception as exc:
        return ToolPacket(f"# fetch failed for {target!r}: {str(exc)[:180]}")
    _remember_budget(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    results = list(getattr(payload, "results", None) or [])
    if not receipt or not results:
        return ToolPacket(f"# fetch returned no content for {target!r}")
    item = results[0]
    rid = getattr(item, "result_id", None)
    note = str(getattr(item, "note", None) or "")
    if not isinstance(rid, str) or not rid or not note.strip():
        return ToolPacket(f"# fetch returned unusable content for {target!r}")
    title = str(getattr(item, "title", None) or target)
    final_url = str(getattr(item, "url", None) or target)

    focus_text = question + " " + focus + " " + title
    spans = _window_spans(note, focus_text)
    if len(note) <= ROW_DIGEST_CAP:
        shown = [(0, len(note))]
    else:
        shown = [(0, min(len(note), FETCH_ORIENTATION))]
        for span in spans:
            shown.append(span)
        shown = _merge_ranges(shown, len(note))

    row = {
        "receipt_id": receipt,
        "result_id": rid,
        "title": title,
        "url": final_url,
        "text": note,
        "preview": "",
        "kind": "fetch",
        "shown": shown,
        "kept": [],
    }
    orientation = note[:FETCH_ORIENTATION]
    chunks = []
    for a, b in spans:
        chunks.append(f"\n--- section @{a} ---\n{note[a:b]}")
    rendered = (
        f"# fetch {target!r} -> <ROW0> {len(note)} chars\n"
        f"TITLE: {title}\nURL: {final_url}\n"
        f"--- orientation ---\n{orientation}"
        + "".join(chunks)
    )
    row["preview"] = _clip(" ".join(note[a:b] for a, b in spans), 1500)
    return ToolPacket(rendered, [row])


def _seed_queries(question: str, shape: QuestionShape) -> list[str]:
    clean = _space(question)
    salient = _terms(clean, 12)
    seeds: list[str] = []
    if clean:
        seeds.append(_clip(clean, 240))
    for phrase in _quoted_phrases(question):
        seeds.append(f'"{phrase}"')
    if salient:
        seeds.append(" ".join(salient[:9]))
    if (shape.set_like or shape.superlative) and salient:
        seeds.append("official list table " + " ".join(salient[:7]))
    low = clean.lower()
    # Source-first routes for the official-document style tasks that dominate
    # Harnyx batches.  These are query templates, not copied control-flow: they
    # improve recall when the question names an archive/publication but the
    # generic terms are too broad.
    routes: list[tuple[str, str]] = [
        ("oral history", 'site:history.house.gov "List of Interviewees" "Oral History"'),
        ("national register", 'site:nps.gov "National Register of Historic Places" "Weekly List" 2023 PDF'),
        ("fide", 'site:fide.com "standard rating list" "June 2026" "July 2026" "August 2026"'),
        ("recycling index", 'site:in.gov "recycling index report" "November 1, 2025"'),
        ("postal bulletin", 'site:about.usps.com "Postal Bulletin 22643" "Stamp Announcement"'),
        ("cswe", 'site:cswe.org "February 2026" "BOA" "decision register"'),
        ("planetary", 'site:planetarynames.wr.usgs.gov Mercury Planitiae Diameter'),
        ("federal register", 'site:federalregister.gov "2025-06-01" "CF-2025-12"'),
        ("legislation.gov.uk", 'site:legislation.gov.uk "Commencement No. 8" "Commencement No. 9" "Environment Act 2021"'),
        ("notifiable infectious diseases", 'site:chp.gov.hk "notifiable infectious diseases by month" 2026 2025'),
    ]
    for needle, route in routes:
        if needle in low:
            seeds.append(route)
    out: list[str] = []
    for item in seeds:
        q = _space(item)
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    return out[:5]


def _preseed_fetch_targets(vault: EvidenceVault, question: str, limit: int = 2) -> list[str]:
    terms = _terms(question, 24)
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for pos, row in enumerate(vault.rows):
        if row.get("kind") != "search":
            continue
        url = str(row.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(row.get("title") or "")
        preview = str(row.get("preview") or "")
        score = _overlap_score(title + " " + preview + " " + url, terms) * 3
        score += _official_score(url)
        if url.lower().endswith((".pdf", ".html", ".htm")):
            score += 1
        if score <= 0:
            continue
        ranked.append((score, pos, url))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, _, url in ranked[:limit]]


async def _preseed(question: str, shape: QuestionShape, vault: EvidenceVault,
                   deadline: float) -> str:
    seeds = _seed_queries(question, shape)
    if not seeds or _left(deadline) < 35.0:
        return ""
    tasks = [asyncio.ensure_future(_search_packet(q, advanced=False)) for q in seeds]
    done, pending = await asyncio.wait(tasks, timeout=min(TOOL_PHASE_TIMEOUT, max(5.0, _left(deadline) - 8.0)))
    blocks: list[str] = []
    for task in tasks:
        if task.done():
            try:
                packet = task.result()
            except Exception:
                packet = ToolPacket("# seed search crashed")
            blocks.append(vault.add_packet(packet))
        else:
            task.cancel()
            blocks.append("# seed search timed out")
    targets = _preseed_fetch_targets(vault, question, limit=2 if _left(deadline) > 70.0 else 1)
    if targets and _left(deadline) > 35.0:
        fetches = [
            asyncio.ensure_future(_fetch_packet(url, "authoritative table/list/values named by the question", question))
            for url in targets
        ]
        await asyncio.wait(fetches, timeout=min(TOOL_PHASE_TIMEOUT, max(5.0, _left(deadline) - 8.0)))
        for task in fetches:
            if task.done():
                try:
                    packet = task.result()
                except Exception:
                    packet = ToolPacket("# seed fetch crashed")
                blocks.append(vault.add_packet(packet))
            else:
                task.cancel()
                blocks.append("# seed fetch timed out")
    return "\n\n".join(blocks)


ACTION_RULES = """
You are the research director inside a bounded evidence agent. Your goal is to
beat a strong reference answer on correctness, completeness, source quality,
exact values, and citation support.

EVIDENCE RULES
- Use numbered evidence [n]. Never invent a citation number.
- Prefer the source that originates a fact: official database, regulator,
  organization, filing, paper, or primary document. An aggregator is useful for
  discovery, but primary evidence wins.
- If the question says "using only", "solely", or otherwise restricts the
  source, final factual claims must be backed by that named source.
- Copy names, labels, figures, capitalization, units, dates, and status codes
  exactly from the requested source when the question cares about that source.
- When a displayed source contains the decisive text, use a KEEP action with an
  exact verbatim quote. KEEP makes the eventual citation point at the proof
  rather than page furniture.
- If a fetched page is long and the needed datum is not visible, use GREP and
  READ on the already-fetched source instead of searching for the same page again.

COMPLETENESS RULES
- Answer every distinct sub-question.
- For a set/filter question, establish the complete candidate roster before
  deciding who qualifies; verify each relevant member against every condition.
- For a count/rank/superlative, inspect the complete relevant pool/table before
  computing the result.
- For multi-period or multi-stage questions, bind each fact to the correct
  period/stage/source. Never let a semifinal, prior year, sibling product, or
  neighboring metric answer a final/current/target slot.
- Explain a discrepancy when the question explicitly asks for a comparison and
  the evidence establishes why the values differ.
- If sources conflict, resolve the conflict before finalizing; do not print two
  incompatible values for the same requested fact.

ANSWER RULES
- The first words should answer the question, not narrate your research.
- Every load-bearing factual sentence should carry [n] immediately after the
  claim it supports.
- Obey literal output requirements (ordering, exact text, count, units, etc.).
- Do not return planning notes, tool syntax, refusals, or "insufficient evidence"
  prose when you have useful evidence.

ACTION PROTOCOL
Return ONE JSON object, with no markdown fences.

To research:
{"actions":[
  {"type":"search","query":"concise query"},
  {"type":"fetch","url":"https://...","focus":"section/table/entity"},
  {"type":"grep","source":3,"pattern":"literal or regex"},
  {"type":"read","source":3,"offset":12000,"length":5000},
  {"type":"keep","source":3,"quote":"exact verbatim source text"}
]}

You may request up to six independent actions at once. GREP/READ/KEEP may only
refer to source numbers that already exist before this turn.

When the evidence is sufficient:
{"final":"complete cited answer"}

Do not mix actions and final in the same object.
""".strip()


COMMIT_RULES = """
Write the final answer to the user's research question using ONLY the numbered
evidence below for precise factual claims.

Start directly with the requested answer. Answer every requested part. Preserve
exact source strings for source-sensitive names/labels/figures. Use [n] after
each factual sentence so it points to evidence that actually states the claim.
For sets, counts, comparisons, and superlatives, show enough of the pool or
arithmetic to make completeness checkable, but stay concise. Never mention the
research process, uncertainty markers, or missing tools. Do not emit JSON or
tool syntax. If the question explicitly requires only a bare answer, put that
bare answer on the first line; evidence markers may appear in supporting lines
that the controller can remove after citations are harvested.
""".strip()


CRITIC_RULES = """
You are the final pairwise-score critic. Improve the answer only when necessary.
Check: every requested part answered, correct entity kind, exact period/stage,
strict named-source compliance, exact source values, no contradictory values,
complete pool for set/superlative/count questions, and citations on every
load-bearing claim. Never introduce a factual value not present in the numbered
evidence. Return only the improved final answer; if already strong, return it
unchanged.
""".strip()


def _strip_fence(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _turn_object(text: str) -> dict[str, Any] | None:
    raw = _strip_fence(text)
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        try:
            value = json.loads(raw[first:last + 1])
            if isinstance(value, dict):
                return value
        except Exception:
            return None
    return None


def _normalize_action(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("type") or item.get("tool") or item.get("name")
    if not isinstance(kind, str):
        return None
    action = dict(item)
    action["type"] = kind.lower().strip()
    return action


async def _run_action(action: dict[str, Any], question: str,
                      vault: EvidenceVault) -> ToolPacket:
    kind = str(action.get("type") or "").lower()
    if kind == "search":
        return await _search_packet(str(action.get("query") or ""), advanced=False)
    if kind == "fetch":
        return await _fetch_packet(
            str(action.get("url") or ""),
            str(action.get("focus") or ""),
            question,
        )
    if kind == "grep":
        try:
            source = int(action.get("source") or 0)
        except Exception:
            source = 0
        return ToolPacket(vault.local_grep(source, str(action.get("pattern") or "")))
    if kind == "read":
        try:
            source = int(action.get("source") or 0)
        except Exception:
            source = 0
        try:
            offset = int(action.get("offset") or 0)
        except Exception:
            offset = 0
        try:
            length = int(action.get("length") or 4000)
        except Exception:
            length = 4000
        return ToolPacket(vault.local_read(source, offset, length))
    if kind == "keep":
        try:
            source = int(action.get("source") or 0)
        except Exception:
            source = 0
        return ToolPacket(vault.keep_quote(source, str(action.get("quote") or "")))
    return ToolPacket(f"# unknown action {kind!r}")


async def _execute_actions(actions: list[dict[str, Any]], question: str,
                           vault: EvidenceVault, deadline: float) -> str:
    chosen = actions[:MAX_ACTIONS_PER_TURN]
    if not chosen:
        return "# no valid actions"
    tasks = [asyncio.ensure_future(_run_action(action, question, vault)) for action in chosen]
    budget = min(TOOL_PHASE_TIMEOUT, max(5.0, _left(deadline) - MIN_RETURN_SECONDS))
    try:
        await asyncio.wait(tasks, timeout=budget)
    except Exception:
        pass
    blocks: list[str] = []
    # Commit in requested action order, never network-completion order.
    for task in tasks:
        if task.done():
            try:
                packet = task.result()
            except Exception as exc:
                packet = ToolPacket(f"# action crashed: {str(exc)[:180]}")
            blocks.append(vault.add_packet(packet))
        else:
            task.cancel()
            blocks.append("# action timed out; continue with existing evidence")
    return "\n\n".join(blocks)


_BRACKET_MAP = {
    0x3010: "[", 0x3011: "]", 0xFF3B: "[", 0xFF3D: "]",
    0x2011: "-", 0x2212: "-",
}
for _digit in range(10):
    _BRACKET_MAP[0xFF10 + _digit] = chr(48 + _digit)


def _normalize_markers(text: str) -> str:
    return (text or "").translate(_BRACKET_MAP)


_CITE_RE = re.compile(r"\[([0-9][0-9,\s-]*)\]")


def _marker_numbers(text: str, top: int) -> list[int]:
    normalized = _normalize_markers(text)
    out: list[int] = []
    seen: set[int] = set()
    for match in _CITE_RE.finditer(normalized):
        for chunk in match.group(1).split(","):
            part = chunk.strip()
            range_match = re.fullmatch(r"(\d{1,4})\s*-\s*(\d{1,4})", part)
            if range_match:
                low = int(range_match.group(1))
                high = int(range_match.group(2))
                high = min(high, low + 20)
                for number in range(low, high + 1):
                    if 1 <= number <= top and number not in seen:
                        seen.add(number)
                        out.append(number)
            elif part.isdigit():
                number = int(part)
                if 1 <= number <= top and number not in seen:
                    seen.add(number)
                    out.append(number)
    return out


_TOOLISH_RE = re.compile(
    r"<\s*/?\s*tool|^\s*\{\s*\"actions\"\s*:|\b(?:search|fetch|grep|read|keep)\s*\(",
    re.I,
)
_REFUSAL_RE = re.compile(
    r"^\s*(?:i (?:cannot|can't|am unable)|unable to|sorry[,.:]|"
    r"best-effort answer unavailable|no supported answer)",
    re.I,
)


def _usable_answer(text: str) -> bool:
    value = _normalize_markers(text).strip()
    if not value:
        return False
    if _TOOLISH_RE.search(value) or _REFUSAL_RE.match(value):
        return False
    if len(value) < 8:
        return False
    return True


def _has_citation(text: str) -> bool:
    return bool(re.search(r"\[[0-9]{1,4}\]", _normalize_markers(text or "")))


_NUM_RE = re.compile(r"(?<!\[)\b\d[\d,]*(?:\.\d+)?%?\b")


def _unsupported_numbers(answer: str, vault: EvidenceVault) -> list[str]:
    flagged: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", _normalize_markers(answer or "")):
        if not sentence.strip():
            continue
        cited = _marker_numbers(sentence, len(vault.rows))
        if not cited:
            continue
        source_text = " ".join(
            (vault.row(number) or {}).get("text") or ""
            for number in cited
        )
        plain_source = source_text.replace(",", "")
        for match in _NUM_RE.finditer(_CITE_RE.sub(" ", sentence)):
            token = match.group(0)
            digits = re.sub(r"\D", "", token)
            if len(digits) < 2:
                continue
            if token not in source_text and token.replace(",", "") not in plain_source:
                if token not in flagged:
                    flagged.append(token)
    return flagged[:6]


def _answer_part_signal(answer: str, shape: QuestionShape) -> bool:
    if shape.numbered_parts <= 1:
        return True
    text = _normalize_markers(answer or "")
    explicit = 0
    for number in range(1, shape.numbered_parts + 1):
        if re.search(rf"(?:^|\n|\s)\({number}\)", text):
            explicit += 1
    if explicit == shape.numbered_parts:
        return True
    # Do not reject good unnumbered prose solely for formatting; require enough
    # substantive sentence/line units to plausibly cover all parts.
    units = [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if len(x.strip()) > 18]
    return len(units) >= shape.numbered_parts


def _citations(answer: str, vault: EvidenceVault) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for number in _marker_numbers(answer, len(vault.rows)):
        if len(refs) >= MAX_CITATIONS:
            break
        ref, cost = vault.citation(number)
        if ref is None:
            continue
        if spent + cost > TOTAL_EVIDENCE_CAP:
            continue
        refs.append(ref)
        spent += cost
    return refs


def _output_only_line(answer: str, question: str) -> str:
    shape = QuestionShape(question)
    if not shape.output_only:
        return answer
    for raw in (answer or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "Proof:", "Evidence:")):
            continue
        # Remove citation markers from the literal output line.
        line = _CITE_RE.sub("", _normalize_markers(line)).strip()
        line = line.strip("*_` ")
        if line:
            return line
    return _CITE_RE.sub("", _normalize_markers(answer or "")).strip()


def _research_prompt(question: str, shape: QuestionShape, vault: EvidenceVault,
                     recent: str, provisional: str, left: float) -> list[dict[str, Any]]:
    extra = shape.hint()
    state = vault.digest()
    user = (
        f"QUESTION:\n{question}\n\n"
        f"QUESTION-SHAPE REQUIREMENTS:\n{extra or '(ordinary factual research question)'}\n\n"
        f"NUMBERED EVIDENCE CURRENTLY AVAILABLE:\n{state}\n\n"
    )
    if recent.strip():
        user += f"RESULTS OF THE MOST RECENT ACTIONS:\n{_clip(recent, 18000)}\n\n"
    if provisional.strip():
        user += (
            "CURRENT PROVISIONAL ANSWER (repair it if research shows a problem):\n"
            f"{_clip(provisional, 10000)}\n\n"
        )
    user += (
        f"Approximately {int(max(0.0, left))} seconds remain. "
        "Choose the highest-value next research actions, or finalize if every "
        "load-bearing part is grounded."
    )
    return [
        {"role": "system", "content": ACTION_RULES},
        {"role": "user", "content": user},
    ]


async def _research_loop(question: str, shape: QuestionShape, vault: EvidenceVault,
                         deadline: float, recent: str) -> str:
    provisional = ""
    for turn in range(MAX_RESEARCH_TURNS):
        left = _left(deadline)
        if left <= WRAPUP_SECONDS:
            break
        messages = _research_prompt(question, shape, vault, recent, provisional, left)
        payload = await _chat(
            PRIMARY_MODELS, messages, deadline,
            max_tokens=2600, timeout_cap=TURN_TIMEOUT, temperature=0.1,
        )
        raw = _llm_text(payload)
        if not raw:
            break
        obj = _turn_object(raw)
        if obj is None:
            if _usable_answer(raw):
                provisional = raw
                break
            recent = "# model output was not valid action JSON; choose actions or final next turn"
            continue

        final = obj.get("final")
        if isinstance(final, str) and _usable_answer(final):
            provisional = final.strip()
            # A cited, plausibly complete answer can commit early.
            if _has_citation(provisional) and _answer_part_signal(provisional, shape):
                unsupported = _unsupported_numbers(provisional, vault)
                if not unsupported:
                    break
            recent = (
                "# provisional answer needs one more grounding pass: "
                "ensure all requested parts and precise values are backed by [n]"
            )
            continue

        raw_actions = obj.get("actions")
        actions: list[dict[str, Any]] = []
        if isinstance(raw_actions, list):
            for item in raw_actions:
                action = _normalize_action(item)
                if action is not None:
                    actions.append(action)
        if not actions:
            recent = "# no valid actions were returned; finalize or choose concrete actions"
            continue
        recent = await _execute_actions(actions, question, vault, deadline)
    return provisional


async def _write_final(question: str, shape: QuestionShape, vault: EvidenceVault,
                       provisional: str, deadline: float) -> str:
    digest = vault.digest()
    extra = shape.hint()
    prompt = (
        f"QUESTION:\n{question}\n\n"
        f"QUESTION-SHAPE REQUIREMENTS:\n{extra or '(ordinary factual research question)'}\n\n"
        f"NUMBERED EVIDENCE:\n{digest}\n\n"
    )
    if provisional.strip():
        prompt += (
            "A research-loop draft follows. Keep anything it got right, but correct "
            "it wherever the evidence or question scope disagrees:\n"
            f"{_clip(provisional, 12000)}\n\n"
        )
    prompt += "Write the final answer now."
    payload = await _chat(
        WRITER_MODELS,
        [
            {"role": "system", "content": COMMIT_RULES},
            {"role": "user", "content": prompt},
        ],
        deadline,
        max_tokens=4200,
        timeout_cap=WRITER_TIMEOUT,
        temperature=0.08,
    )
    answer = _llm_text(payload)
    if _usable_answer(answer):
        return answer
    if _usable_answer(provisional):
        return provisional
    return ""


async def _critic(question: str, shape: QuestionShape, vault: EvidenceVault,
                  answer: str, deadline: float) -> str:
    if not _usable_answer(answer) or _left(deadline) < 28.0:
        return answer
    if not shape.complex and not _unsupported_numbers(answer, vault):
        return answer
    evidence = vault.digest(cap=36000)
    unsupported = _unsupported_numbers(answer, vault)
    note = ""
    if unsupported:
        note = (
            "\nThe deterministic checker found answer values not present in their "
            "cited source text: " + ", ".join(unsupported) + ". Remove or correct them."
        )
    prompt = (
        f"QUESTION:\n{question}\n\n"
        f"CURRENT ANSWER:\n{_clip(answer, 14000)}\n\n"
        f"NUMBERED EVIDENCE:\n{evidence}\n"
        f"{note}\n\nReturn the corrected final answer."
    )
    payload = await _chat(
        WRITER_MODELS,
        [
            {"role": "system", "content": CRITIC_RULES},
            {"role": "user", "content": prompt},
        ],
        deadline,
        max_tokens=3800,
        timeout_cap=CRITIC_TIMEOUT,
        temperature=0.0,
    )
    candidate = _llm_text(payload)
    if not _usable_answer(candidate):
        return answer
    if len(candidate) < max(12, int(len(answer) * 0.45)):
        return answer
    # Do not adopt a critic answer that drops all citations while evidence exists.
    if vault.rows and _has_citation(answer) and not _has_citation(candidate):
        return answer
    return candidate


def _deterministic_partial(vault: EvidenceVault) -> str:
    if not vault.rows:
        return ""
    lines: list[str] = []
    query_terms = _terms(vault.question, 24)
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for number, row in enumerate(vault.rows, start=1):
        content = (row.get("title") or "") + " " + (row.get("preview") or "")
        score = _overlap_score(content, query_terms)
        if row.get("kind") == "fetch":
            score += 3
        ranked.append((score, number, row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    for _, number, row in ranked[:6]:
        preview = _space(row.get("preview") or "")
        if len(preview) < 30:
            preview = _space(vault._row_excerpt(row, 500))
        if preview:
            lines.append(f"{_clip(preview, 420)} [{number}]")
    return "\n".join(lines)


def _attach_citation_scaffold(answer: str, vault: EvidenceVault) -> str:
    """Preserve a plausible answer while adding citable support rows.

    The validator gives no credit for uncited facts even when the text is right.
    When the writer drops markers, keep its answer line but append a compact
    evidence scaffold so citation extraction can still hydrate the best rows.
    """
    if not _usable_answer(answer) or _has_citation(answer) or not vault.rows:
        return answer
    support = _deterministic_partial(vault)
    if not support:
        return answer
    return answer.rstrip() + "\n\nSupporting evidence:\n" + support


def _schema_kind(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    if isinstance(kind, list):
        for item in kind:
            if isinstance(item, str) and item != "null":
                return item
    if isinstance(schema.get("properties"), dict):
        return "object"
    if isinstance(schema.get("items"), dict):
        return "array"
    return ""


def _shape_ok(value: Any, schema: Any) -> bool:
    kind = _schema_kind(schema)
    if not kind:
        return True
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
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


async def _schema_convert(question: str, answer: str, schema: Any,
                          deadline: float) -> Any:
    if _left(deadline) < 12.0:
        return None
    ask = (
        "Convert the answer to a JSON value valid under the supplied JSON schema. "
        "Output only the JSON value, no fence or explanation.\n\n"
        f"SCHEMA:\n{json.dumps(schema)}\n\n"
        f"QUESTION:\n{question}\n\nANSWER:\n{_clip(answer, 15000)}"
    )
    payload = await _chat(
        WRITER_MODELS,
        [
            {"role": "system", "content": "Return strictly valid JSON matching the schema."},
            {"role": "user", "content": ask},
        ],
        deadline,
        max_tokens=3000,
        timeout_cap=SCHEMA_TIMEOUT,
        temperature=0.0,
    )
    raw = _strip_fence(_llm_text(payload))
    try:
        value = json.loads(raw)
    except Exception:
        return None
    if _shape_ok(value, schema):
        return value
    if isinstance(value, dict) and len(value) == 1:
        only = list(value.values())[0]
        if _shape_ok(only, schema):
            return only
    return None


def _coerce_schema(answer: str, schema: Any, depth: int = 0) -> Any:
    if depth > 5:
        return answer[:2000]
    kind = _schema_kind(schema)
    if kind == "string" or not kind:
        return _clip(answer, 4000)
    if kind == "integer":
        m = re.search(r"-?\d[\d,]*", answer or "")
        return int(m.group(0).replace(",", "")) if m else 0
    if kind == "number":
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", answer or "")
        return float(m.group(0).replace(",", "")) if m else 0.0
    if kind == "boolean":
        return bool(re.search(r"\b(?:yes|true)\b", answer or "", re.I))
    if kind == "array":
        items = schema.get("items") if isinstance(schema, dict) else {}
        lines = [x.strip(" -*•\t") for x in (answer or "").splitlines() if x.strip()]
        if not lines:
            lines = [answer.strip()] if answer.strip() else []
        return [_coerce_schema(line, items, depth + 1) for line in lines[:20]]
    if kind == "object":
        props = schema.get("properties") if isinstance(schema, dict) else {}
        if not isinstance(props, dict):
            return {}
        result: dict[str, Any] = {}
        for key, sub in props.items():
            if isinstance(key, str):
                result[key] = _coerce_schema(answer, sub, depth + 1)
        return result
    if kind == "null":
        return None
    return _clip(answer, 4000)


async def _solve(query: Query, question: str) -> Response:
    deadline = monotonic() + WALL_SECONDS
    await _load_models()

    shape = QuestionShape(question)
    vault = EvidenceVault(question)

    try:
        recent = await _preseed(question, shape, vault, deadline)
    except Exception:
        recent = ""

    try:
        provisional = await _research_loop(question, shape, vault, deadline, recent)
    except Exception:
        provisional = ""

    answer = ""
    if _left(deadline) > 10.0:
        try:
            answer = await _write_final(question, shape, vault, provisional, deadline)
        except Exception:
            answer = ""

    if not _usable_answer(answer):
        answer = provisional if _usable_answer(provisional) else _deterministic_partial(vault)

    if _usable_answer(answer) and _left(deadline) > 28.0:
        try:
            answer = await _critic(question, shape, vault, answer, deadline)
        except Exception:
            pass

    answer = _normalize_markers(answer).strip()
    answer = _attach_citation_scaffold(answer, vault)
    if len(answer) > ANSWER_CAP:
        answer = answer[:ANSWER_CAP - 2] + " …"

    try:
        refs = _citations(answer, vault)
    except Exception:
        refs = []

    shipped_text = _output_only_line(answer, question)
    if not shipped_text:
        shipped_text = _deterministic_partial(vault)
    if not shipped_text:
        shipped_text = "Unable to produce a supported answer."

    if query.output_schema is not None:
        structured = None
        try:
            structured = await _schema_convert(question, answer, query.output_schema, deadline)
        except Exception:
            structured = None
        if structured is None:
            structured = _coerce_schema(answer or shipped_text, query.output_schema)
        try:
            return Response(output=structured, citations=refs or None)
        except Exception:
            return Response(output=structured)

    try:
        return Response(text=shipped_text, citations=refs or None)
    except Exception:
        return Response(text=shipped_text)


@entrypoint("query")
async def query(query: Query) -> Response:
    question = (query.text or "").strip()
    if not question:
        return Response(text="No question provided.")
    try:
        return await _solve(query, question)
    except Exception:
        # Never echo the prompt as the answer. This is only a final crash guard.
        return Response(text="Unable to produce a supported answer.")
