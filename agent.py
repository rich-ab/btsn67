"""Harnyx SN67 miner 151 (richjg) — MissionGraph v3.

A deliberately different controller from conversational tool-loop miners.
The language model never drives a long tool-call conversation. Instead it
compiles the question into a constraint graph, creates independent research
missions, runs those missions as bounded parallel jobs, merges their evidence,
launches counterexample missions, and only then compiles the final answer.

Topology:
    question -> contract compiler -> mission scheduler -> parallel research wave
             -> evidence/claim graph -> adjudicator -> gap wave -> skeptic wave
             -> proof selector -> answer compiler -> schema adapter

The source is intentionally self-contained and uses only the Harnyx miner SDK.
It should be benchmarked with local-eval before production submission.
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


VERSION = "missiongraph-v3.0"

# ---------------------------------------------------------------------------
# Runtime policy
# ---------------------------------------------------------------------------

LLM_PROVIDER = "chutes"
SEARCH_PROVIDER_A = "desearch"
SEARCH_PROVIDER_B = "parallel"

CONTRACT_MODELS = (
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)
MISSION_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "zai-org/GLM-5.2-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
ADJUDICATOR_MODELS = (
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
SKEPTIC_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
    "zai-org/GLM-5.2-TEE",
)
WRITER_MODELS = (
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "deepseek-ai/DeepSeek-V3.2-TEE",
)
SCHEMA_MODELS = (
    "Qwen/Qwen3.5-397B-A17B-TEE",
    "zai-org/GLM-5.2-TEE",
    "Qwen/Qwen3.6-27B-TEE",
)

WALL_SECONDS = 258.0
CONTRACT_TIMEOUT = 34.0
MISSION_PLAN_TIMEOUT = 38.0
MISSION_SEARCH_TIMEOUT = 18.0
MISSION_FETCH_TIMEOUT = 17.0
ADJUDICATE_TIMEOUT = 43.0
SKEPTIC_TIMEOUT = 34.0
WRITER_TIMEOUT = 49.0
SCHEMA_TIMEOUT = 24.0

FIRST_WAVE_MISSIONS = 8
SECOND_WAVE_MISSIONS = 5
SKEPTIC_MISSIONS = 4
MISSION_QUERY_CAP = 3
SEARCH_RESULTS = 7
FETCH_PER_MISSION = 2
MISSION_CONCURRENCY = 4

STOP_SECOND_WAVE_LEFT = 93.0
STOP_SKEPTIC_LEFT = 63.0
STOP_WRITE_LEFT = 35.0
MIN_SCHEMA_LEFT = 12.0

MAX_SOURCES = 64
MAX_CITATIONS = 24
MAX_MATERIALIZED_EVIDENCE = 104_000
MAX_SOURCE_SLICE_CHARS = 14_000
MAX_SOURCE_TEXT = 360_000
MAX_DIGEST = 70_000
MAX_ANSWER = 62_000

SEARCH_NOTE_SHOW = 1100
FETCH_HEAD = 2200
FETCH_WINDOW = 4200
FETCH_WINDOWS = 3
QUOTE_PAD = 500
MIN_SLICE = 2400

CONTRACT_MIN_USD = 0.025
ADJUDICATE_MIN_USD = 0.035
SKEPTIC_MIN_USD = 0.035
WRITE_MIN_USD = 0.020
SCHEMA_MIN_USD = 0.012

_STATE: dict[str, Any] = {
    "remaining_usd": None,
    "allowed_models": (),
    "search_order": (SEARCH_PROVIDER_A, SEARCH_PROVIDER_B),
    "tooling_loaded": False,
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _budget_note(payload: Any) -> None:
    budget = getattr(payload, "budget", None)
    remaining = getattr(budget, "session_remaining_budget_usd", None)
    if isinstance(remaining, (int, float)):
        _STATE["remaining_usd"] = float(remaining)


def _money() -> float:
    value = _STATE.get("remaining_usd")
    if isinstance(value, (int, float)):
        return float(value)
    return 1.0


def _seconds(deadline: float) -> float:
    return max(0.0, deadline - monotonic())


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _cut(text: str, limit: int) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 2)].rstrip() + " …"


def _to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return fallback


def _to_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return fallback


def _unique_strings(values: Any, limit: int, max_len: int = 400) -> list[str]:
    out: list[str] = []
    if not isinstance(values, list):
        return out
    for item in values:
        if not isinstance(item, str):
            continue
        value = _space(item)
        if not value or len(value) > max_len:
            continue
        if value.lower() in [x.lower() for x in out]:
            continue
        out.append(value)
        if len(out) >= limit:
            break
    return out


def _json_obj(text: str) -> dict[str, Any] | None:
    value = (text or "").strip()
    if not value:
        return None
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    start = value.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(value)):
        ch = value[idx]
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
                try:
                    parsed = json.loads(value[start : idx + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return None
    return None


def _json_arr(text: str) -> list[Any] | None:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        return None
    return None


def _host(url: str) -> str:
    try:
        return (urlparse(url or "").hostname or "").lower()
    except Exception:
        return ""


def _authority(url: str, title: str = "") -> int:
    host = _host(url)
    score = 0
    if not host:
        return score
    if host.endswith(".gov") or ".gov." in host:
        score += 10
    if host.endswith(".edu") or ".edu." in host:
        score += 8
    if host.endswith(".int"):
        score += 9
    if host.endswith("sec.gov"):
        score += 4
    if host.endswith("who.int") or host.endswith("worldbank.org"):
        score += 3
    if host.endswith("reuters.com") or host.endswith("apnews.com"):
        score += 5
    if host.endswith("wikipedia.org"):
        score += 1
    if "official" in (title or "").lower():
        score += 2
    return score


def _terms(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9._%$+-]{1,}", (text or "").lower())
    stop = {
        "the", "and", "for", "from", "that", "this", "with", "which", "what",
        "when", "where", "were", "was", "are", "has", "have", "had", "into",
        "than", "then", "only", "according", "about", "would", "could", "should",
        "does", "did", "its", "their", "them", "they", "who", "how", "why",
    }
    out: list[str] = []
    for token in raw:
        if token in stop or len(token) < 3:
            continue
        if token not in out:
            out.append(token)
        if len(out) >= 34:
            break
    return out


def _best_windows(text: str, focus: str, width: int = FETCH_WINDOW, count: int = FETCH_WINDOWS) -> list[tuple[int, int]]:
    if not text:
        return []
    if len(text) <= width:
        return [(0, len(text))]
    terms = _terms(focus)
    low = text.lower()
    stride = max(700, width // 3)
    scored: list[tuple[float, int]] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + width)
        chunk = low[start:end]
        score = 0.0
        for term in terms:
            hits = chunk.count(term)
            if hits:
                score += 1.0 + min(3.0, 0.35 * hits)
                if any(ch.isdigit() for ch in term):
                    score += 0.7
        if "%" in chunk or "$" in chunk or "table" in chunk:
            score += 0.25
        scored.append((score, start))
        if end >= len(text):
            break
        start += stride
    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: list[tuple[int, int]] = []
    for _, start in scored:
        end = min(len(text), start + width)
        collision = False
        for old_start, old_end in picked:
            overlap = max(0, min(end, old_end) - max(start, old_start))
            if overlap > width // 3:
                collision = True
                break
        if collision:
            continue
        picked.append((start, end))
        if len(picked) >= count:
            break
    picked.sort()
    return picked


def _merge_spans(spans: list[tuple[int, int]], text_len: int) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for start, end in spans:
        a = max(0, min(text_len, _to_int(start, 0)))
        b = max(0, min(text_len, _to_int(end, 0)))
        if b <= a:
            continue
        cleaned.append((a, b))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if not merged or start > merged[-1][1] + 160:
            merged.append((start, end))
        else:
            old_start, old_end = merged[-1]
            merged[-1] = (old_start, max(old_end, end))
    return merged


# ---------------------------------------------------------------------------
# Tool/model discovery
# ---------------------------------------------------------------------------


async def _load_tooling() -> None:
    if bool(_STATE.get("tooling_loaded")):
        return
    _STATE["tooling_loaded"] = True
    try:
        info = await tooling_info(timeout=8.0)
        _budget_note(info)
        response = getattr(info, "response", None)
        if not isinstance(response, dict):
            return
        model_map = response.get("allowed_llm_provider_models")
        if isinstance(model_map, dict):
            raw_models = model_map.get(LLM_PROVIDER)
            if isinstance(raw_models, list):
                models: list[str] = []
                for model in raw_models:
                    if isinstance(model, str) and model and model not in models:
                        models.append(model)
                _STATE["allowed_models"] = tuple(models)
        search_names: list[str] = []
        for key in ("allowed_search_providers", "search_providers", "allowed_web_search_providers"):
            raw = response.get(key)
            if isinstance(raw, list):
                for provider in raw:
                    if isinstance(provider, str) and provider not in search_names:
                        search_names.append(provider)
        order: list[str] = []
        for provider in (SEARCH_PROVIDER_A, SEARCH_PROVIDER_B):
            if not search_names or provider in search_names:
                order.append(provider)
        if order:
            _STATE["search_order"] = tuple(order)
    except Exception:
        return


def _model_order(preferred: tuple[str, ...]) -> list[str]:
    live = _STATE.get("allowed_models")
    if not isinstance(live, tuple) or not live:
        return list(preferred)
    allowed = set(live)
    picked: list[str] = []
    for model in preferred:
        if model in allowed:
            picked.append(model)
    if picked:
        return picked
    out: list[str] = []
    for model in live:
        if isinstance(model, str):
            out.append(model)
        if len(out) >= 3:
            break
    return out


def _search_order() -> list[str]:
    raw = _STATE.get("search_order")
    if isinstance(raw, tuple):
        return [x for x in raw if isinstance(x, str)]
    return [SEARCH_PROVIDER_A, SEARCH_PROVIDER_B]


async def _chat(
    preferred: tuple[str, ...],
    messages: list[dict[str, Any]],
    deadline: float,
    max_tokens: int,
    temperature: float,
    timeout_cap: float,
) -> Any:
    for model in _model_order(preferred):
        left = _seconds(deadline) - 2.5
        timeout = min(timeout_cap, left)
        if timeout <= 5.0:
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
            _budget_note(payload)
            raw = _llm_text(payload)
            if raw.strip():
                return payload
        except Exception:
            continue
    return None


def _llm_text(payload: Any) -> str:
    if payload is None:
        return ""
    direct = getattr(payload, "llm", None)
    if direct is not None:
        raw = getattr(direct, "raw_text", None)
        if isinstance(raw, str):
            return raw
    raw_text = getattr(payload, "raw_text", None)
    if isinstance(raw_text, str):
        return raw_text
    response = getattr(payload, "response", None)
    if isinstance(response, dict):
        for key in ("text", "content", "raw_text"):
            value = response.get(key)
            if isinstance(value, str):
                return value
    return ""


# ---------------------------------------------------------------------------
# Contract and mission data models
# ---------------------------------------------------------------------------


class Contract:
    def __init__(self, question: str, output_schema: Any) -> None:
        self.question = question
        self.output_schema = output_schema
        self.answer_kind = "fact"
        self.constraints: list[str] = []
        self.named_sources: list[str] = []
        self.entities: list[str] = []
        self.measures: list[str] = []
        self.ordering = ""
        self.strict_output = False
        self.complete_set = False
        self.superlative = False
        self.multi_part = False
        self.temporal = False
        self.computed = False
        self.requested_count = False
        self.question_parts: list[str] = []
        self.risk_flags: list[str] = []

    def apply(self, data: dict[str, Any]) -> None:
        kind = data.get("answer_kind")
        if isinstance(kind, str) and kind.strip():
            self.answer_kind = _cut(_space(kind), 80)
        self.constraints = _unique_strings(data.get("constraints"), 14, 500)
        self.named_sources = _unique_strings(data.get("named_sources"), 8, 180)
        self.entities = _unique_strings(data.get("entities"), 24, 180)
        self.measures = _unique_strings(data.get("measures"), 10, 180)
        self.question_parts = _unique_strings(data.get("question_parts"), 8, 600)
        self.risk_flags = _unique_strings(data.get("risk_flags"), 12, 220)
        value = data.get("strict_output")
        if isinstance(value, bool):
            self.strict_output = value
        value = data.get("complete_set")
        if isinstance(value, bool):
            self.complete_set = value
        value = data.get("superlative")
        if isinstance(value, bool):
            self.superlative = value
        value = data.get("multi_part")
        if isinstance(value, bool):
            self.multi_part = value
        value = data.get("temporal")
        if isinstance(value, bool):
            self.temporal = value
        value = data.get("computed")
        if isinstance(value, bool):
            self.computed = value
        value = data.get("requested_count")
        if isinstance(value, bool):
            self.requested_count = value
        ordering = data.get("ordering")
        if isinstance(ordering, str):
            self.ordering = _cut(_space(ordering), 160)

    def block(self) -> str:
        return json.dumps(
            {
                "answer_kind": self.answer_kind,
                "constraints": self.constraints,
                "named_sources": self.named_sources,
                "entities": self.entities,
                "measures": self.measures,
                "ordering": self.ordering,
                "strict_output": self.strict_output,
                "complete_set": self.complete_set,
                "superlative": self.superlative,
                "multi_part": self.multi_part,
                "temporal": self.temporal,
                "computed": self.computed,
                "requested_count": self.requested_count,
                "question_parts": self.question_parts,
                "risk_flags": self.risk_flags,
            },
            ensure_ascii=False,
        )


class Mission:
    def __init__(self, number: int, objective: str) -> None:
        self.number = number
        self.objective = _cut(_space(objective), 700)
        self.role = "verifier"
        self.queries: list[str] = []
        self.focus_terms: list[str] = []
        self.candidate = ""
        self.condition = ""
        self.prefer_primary = True
        self.must_resolve = True
        self.status = "pending"
        self.source_numbers: list[int] = []
        self.findings: list[str] = []

    def apply(self, data: dict[str, Any]) -> None:
        role = data.get("role")
        if isinstance(role, str) and role.strip():
            self.role = _cut(_space(role), 60)
        self.queries = _unique_strings(data.get("queries"), MISSION_QUERY_CAP, 450)
        self.focus_terms = _unique_strings(data.get("focus_terms"), 8, 120)
        candidate = data.get("candidate")
        if isinstance(candidate, str):
            self.candidate = _cut(_space(candidate), 180)
        condition = data.get("condition")
        if isinstance(condition, str):
            self.condition = _cut(_space(condition), 420)
        if isinstance(data.get("prefer_primary"), bool):
            self.prefer_primary = bool(data.get("prefer_primary"))
        if isinstance(data.get("must_resolve"), bool):
            self.must_resolve = bool(data.get("must_resolve"))

    def block(self) -> str:
        return json.dumps(
            {
                "mission": self.number,
                "role": self.role,
                "objective": self.objective,
                "queries": self.queries,
                "focus_terms": self.focus_terms,
                "candidate": self.candidate,
                "condition": self.condition,
                "prefer_primary": self.prefer_primary,
                "status": self.status,
                "sources": self.source_numbers,
                "findings": self.findings,
            },
            ensure_ascii=False,
        )


class Vault:
    def __init__(self) -> None:
        self.sources: list[dict[str, Any]] = []
        self.missions: list[Mission] = []
        self.claims: list[dict[str, Any]] = []
        self.candidates: list[str] = []
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def source(self, number: int) -> dict[str, Any] | None:
        if 1 <= number <= len(self.sources):
            return self.sources[number - 1]
        return None

    def add_source(
        self,
        receipt_id: str,
        result_id: str,
        text: str,
        title: str,
        url: str,
        shown: list[tuple[int, int]],
        origin: str,
        mission_number: int,
    ) -> int:
        if len(self.sources) >= MAX_SOURCES:
            return 0
        if not receipt_id or not result_id or not text.strip():
            return 0
        # Same receipt/result is the same citable artifact. Merge display windows
        # rather than creating artificial duplicate evidence rows.
        for idx, row in enumerate(self.sources, 1):
            if row.get("receipt_id") == receipt_id and row.get("result_id") == result_id:
                old_shown = row.get("shown")
                if not isinstance(old_shown, list):
                    old_shown = []
                old_shown.extend(shown)
                row["shown"] = _merge_spans(old_shown, len(str(row.get("text") or "")))
                missions = row.get("missions")
                if not isinstance(missions, list):
                    missions = []
                if mission_number and mission_number not in missions:
                    missions.append(mission_number)
                row["missions"] = missions
                return idx
        content = text[:MAX_SOURCE_TEXT]
        row = {
            "receipt_id": receipt_id,
            "result_id": result_id,
            "text": content,
            "title": _cut(title, 220),
            "url": _cut(url, 500),
            "host": _host(url),
            "authority": _authority(url, title),
            "shown": _merge_spans(shown, len(content)),
            "retained": [],
            "origin": origin,
            "missions": [mission_number] if mission_number else [],
        }
        self.sources.append(row)
        return len(self.sources)

    def retain(self, number: int, quote: str) -> bool:
        row = self.source(number)
        if row is None:
            return False
        text = str(row.get("text") or "")
        needle = (quote or "").strip()
        if len(needle) < 8:
            return False
        pos = text.find(needle)
        if pos < 0:
            pos = text.lower().find(needle.lower())
        if pos < 0:
            return False
        start = max(0, pos - QUOTE_PAD)
        end = min(len(text), pos + len(needle) + QUOTE_PAD)
        kept = row.get("retained")
        if not isinstance(kept, list):
            kept = []
        kept.append((start, end))
        row["retained"] = _merge_spans(kept, len(text))[:8]
        return True

    def add_candidate(self, value: str) -> None:
        candidate = _space(value)
        if not candidate or len(candidate) > 180:
            return
        if candidate.lower() not in [x.lower() for x in self.candidates]:
            self.candidates.append(candidate)

    def add_claim(self, claim: str, status: str, sources: list[int], candidate: str = "", condition: str = "") -> None:
        text = _cut(_space(claim), 720)
        if not text:
            return
        refs: list[int] = []
        for number in sources:
            if 1 <= number <= len(self.sources) and number not in refs:
                refs.append(number)
        self.claims.append(
            {
                "claim": text,
                "status": _cut(status, 50),
                "sources": refs,
                "candidate": _cut(_space(candidate), 180),
                "condition": _cut(_space(condition), 420),
            }
        )
        if len(self.claims) > 120:
            self.claims = self.claims[-120:]

    def evidence_digest(self, max_chars: int = MAX_DIGEST) -> str:
        ranked: list[tuple[int, int, int]] = []
        for idx, row in enumerate(self.sources, 1):
            retained = row.get("retained")
            retain_count = len(retained) if isinstance(retained, list) else 0
            authority = _to_int(row.get("authority"), 0)
            ranked.append((retain_count, authority, idx))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        blocks: list[str] = []
        spent = 0
        for _, _, number in ranked:
            row = self.source(number)
            if row is None:
                continue
            text = str(row.get("text") or "")
            spans = row.get("retained") or row.get("shown") or []
            snippets: list[str] = []
            if isinstance(spans, list):
                for span in spans[:4]:
                    try:
                        start = max(0, int(span[0]))
                        end = min(len(text), int(span[1]))
                    except Exception:
                        continue
                    if end > start:
                        snippets.append(text[start:end])
            if not snippets:
                snippets = [text[:1300]]
            body = "\n...\n".join(snippets)
            block = (
                f"[{number}] authority={row.get('authority',0)} host={row.get('host','')}\n"
                f"TITLE: {row.get('title','')}\nURL: {row.get('url','')}\n{body}\n"
            )
            if spent + len(block) > max_chars:
                continue
            blocks.append(block)
            spent += len(block)
        return "\n".join(blocks)

    def claim_digest(self) -> str:
        if not self.claims:
            return "(no adjudicated claims yet)"
        lines: list[str] = []
        for item in self.claims[-80:]:
            refs = ",".join(str(x) for x in item.get("sources", [])) or "none"
            lines.append(
                f"- status={item.get('status','')} candidate={item.get('candidate','')} "
                f"condition={item.get('condition','')} sources={refs} :: {item.get('claim','')}"
            )
        return "\n".join(lines)

    def mission_digest(self) -> str:
        if not self.missions:
            return "(no missions)"
        blocks: list[str] = []
        for mission in self.missions[-24:]:
            blocks.append(mission.block())
        return "\n".join(blocks)

    def citation(self, number: int) -> tuple[CitationRef | None, int]:
        row = self.source(number)
        if row is None:
            return None, 0
        receipt = str(row.get("receipt_id") or "")
        result = str(row.get("result_id") or "")
        text = str(row.get("text") or "")
        if not receipt or not result or not text:
            return None, 0
        spans = row.get("retained") or row.get("shown") or []
        if not isinstance(spans, list) or not spans:
            return None, 0
        merged = _merge_spans(spans, len(text))
        widened: list[tuple[int, int]] = []
        for start, end in merged[:5]:
            width = end - start
            if width < MIN_SLICE:
                need = MIN_SLICE - width
                left = min(start, need // 2)
                start -= left
                remaining = need - left
                right = min(len(text) - end, remaining)
                end += right
                if right < remaining:
                    start = max(0, start - (remaining - right))
            widened.append((start, end))
        widened = _merge_spans(widened, len(text))
        total = sum(end - start for start, end in widened)
        if total > MAX_SOURCE_SLICE_CHARS and widened:
            each = max(700, MAX_SOURCE_SLICE_CHARS // len(widened))
            trimmed: list[tuple[int, int]] = []
            for start, end in widened:
                if end - start <= each:
                    trimmed.append((start, end))
                    continue
                mid = (start + end) // 2
                a = max(0, mid - each // 2)
                b = min(len(text), a + each)
                a = max(0, b - each)
                trimmed.append((a, b))
            widened = _merge_spans(trimmed, len(text))
            total = sum(end - start for start, end in widened)
        slices = [CitationSlice(start=start, end=end) for start, end in widened if end > start]
        if not slices:
            return None, 0
        return CitationRef(receipt_id=receipt, result_id=result, slices=slices), total


# ---------------------------------------------------------------------------
# Contract compiler
# ---------------------------------------------------------------------------


def _deterministic_contract(contract: Contract) -> None:
    q = contract.question
    low = q.lower()
    if re.search(r"\b(which|what)\s+(companies|people|films|books|countries|cities|states|items|members)\b", low):
        contract.complete_set = True
    if re.search(r"\b(all|every|each|both|entire|complete list|which of these)\b", low):
        contract.complete_set = True
    if re.search(r"\b(highest|lowest|largest|smallest|most|least|top\s+\d+|first|last|best|worst)\b", low):
        contract.superlative = True
    if re.search(r"\b(total|sum|average|mean|difference|percentage|percent|ratio|count|how many)\b", low):
        contract.computed = True
    if re.search(r"\bhow many\b|\bnumber of\b", low):
        contract.requested_count = True
    if re.search(r"\b(in|during|between|from)\s+(19|20)\d{2}\b|\bas of\b", low):
        contract.temporal = True
    if "only the answer" in low or "respond with only" in low or "nothing else" in low:
        contract.strict_output = True
    if "alphabetical" in low:
        contract.ordering = "alphabetical"
    elif "chronological" in low:
        contract.ordering = "chronological"
    elif re.search(r"\bdescending\b|\bhighest to lowest\b", low):
        contract.ordering = "descending"
    elif re.search(r"\bascending\b|\blowest to highest\b", low):
        contract.ordering = "ascending"
    if q.count("?") > 1 or re.search(r"\b(and also|as well as|respectively)\b", low):
        contract.multi_part = True


async def _compile_contract(question: str, output_schema: Any, deadline: float) -> Contract:
    contract = Contract(question, output_schema)
    _deterministic_contract(contract)
    if _money() < CONTRACT_MIN_USD or _seconds(deadline) < 170.0:
        return contract
    schema_text = json.dumps(output_schema, ensure_ascii=False) if output_schema is not None else "null"
    system = (
        "You are a query-contract compiler, not an answerer. Convert the question into "
        "a verification contract. Return exactly one JSON object and no prose. Do not solve "
        "the question. Distinguish output-format instructions from entity-selection conditions."
    )
    user = f"QUESTION:\n{question}\n\nOUTPUT_SCHEMA:\n{schema_text[:8000]}\n\nReturn keys:\n"
    user += """{
  "answer_kind": "person|organization|work|place|number|date|list|comparison|fact|other",
  "constraints": ["atomic condition that must be proved"],
  "named_sources": ["source explicitly named by the question"],
  "entities": ["entities explicitly named in the question"],
  "measures": ["unit/metric that must be preserved exactly"],
  "ordering": "ordering requirement or empty",
  "strict_output": false,
  "complete_set": false,
  "superlative": false,
  "multi_part": false,
  "temporal": false,
  "computed": false,
  "requested_count": false,
  "question_parts": ["distinct sub-question"],
  "risk_flags": ["likely way an answer could be subtly wrong"]
}"""
    payload = await _chat(
        CONTRACT_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        2200,
        0.0,
        CONTRACT_TIMEOUT,
    )
    data = _json_obj(_llm_text(payload))
    if data is not None:
        contract.apply(data)
    return contract


# ---------------------------------------------------------------------------
# Mission planning
# ---------------------------------------------------------------------------


def _fallback_missions(contract: Contract, wave: int, cap: int) -> list[Mission]:
    q = contract.question
    raw: list[tuple[str, str]] = []
    if wave == 1:
        raw.append(("enumerator", f"Identify the complete candidate pool relevant to: {q}"))
        raw.append(("primary", f"Find the best primary or official source directly supporting the central answer to: {q}"))
        raw.append(("verifier", f"Verify the hardest numerical/date/name condition in: {q}"))
        raw.append(("source", f"Locate any source explicitly named in: {q}"))
        if contract.superlative:
            raw.append(("ranking", f"Verify the ranking/superlative over the full comparison pool for: {q}"))
        if contract.complete_set:
            raw.append(("exclusion", f"Find evidence that tests likely non-qualifying candidates, not only winners, for: {q}"))
        if contract.temporal:
            raw.append(("time", f"Verify the exact reference period/date required by: {q}"))
        if contract.computed:
            raw.append(("calculation", f"Find every source value required to compute the requested result in: {q}"))
    else:
        raw.append(("gap", f"Find missing decisive evidence for: {q}"))
        raw.append(("counterexample", f"Search for evidence that could falsify the current tentative answer to: {q}"))
        raw.append(("authority", f"Replace secondary evidence with a primary source for: {q}"))
    out: list[Mission] = []
    for idx, pair in enumerate(raw[:cap], 1):
        role, objective = pair
        mission = Mission(idx, objective)
        mission.role = role
        mission.queries = [q, f"{q} official source", f"{q} primary source"][:MISSION_QUERY_CAP]
        out.append(mission)
    return out


async def _plan_missions(
    contract: Contract,
    vault: Vault,
    deadline: float,
    wave: int,
    cap: int,
    gap_context: str = "",
) -> list[Mission]:
    if _seconds(deadline) < 120.0 or _money() < 0.025:
        return _fallback_missions(contract, wave, cap)
    system = (
        "You are a research mission scheduler. You do not answer questions. Break the "
        "verification problem into independent missions that can run in parallel. Each mission "
        "must test one falsifiable requirement. Avoid redundant broad searches. For set or "
        "superlative questions, include missions that establish the whole pool and exclusions. "
        "For named-source questions, include a mission that targets that exact source. Return JSON only."
    )
    existing = vault.mission_digest()[-12000:] if vault.missions else "(none)"
    user = f'''QUESTION:\n{contract.question}\n\nCONTRACT:\n{contract.block()}\n\nWAVE: {wave}\n\nPREVIOUS MISSIONS:\n{existing}\n\nGAP CONTEXT:\n{gap_context[:12000]}\n\nReturn exactly:\n{{"missions":[{{"role":"enumerator|primary|verifier|ranking|exclusion|time|calculation|counterexample|authority|gap","objective":"one falsifiable objective","queries":["precise search query", "optional second", "optional third"],"focus_terms":["terms to locate inside pages"],"candidate":"candidate if applicable","condition":"condition being tested","prefer_primary":true,"must_resolve":true}}]}}\n\nReturn at most {cap} missions.'''
    payload = await _chat(
        MISSION_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        3600,
        0.05,
        MISSION_PLAN_TIMEOUT,
    )
    data = _json_obj(_llm_text(payload))
    raw = data.get("missions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return _fallback_missions(contract, wave, cap)
    out: list[Mission] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        objective = item.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            continue
        mission = Mission(len(vault.missions) + len(out) + 1, objective)
        mission.apply(item)
        if not mission.queries:
            mission.queries = [contract.question]
        out.append(mission)
        if len(out) >= cap:
            break
    if not out:
        return _fallback_missions(contract, wave, cap)
    return out


# ---------------------------------------------------------------------------
# Search and fetch primitives used by mission workers
# ---------------------------------------------------------------------------


def _loosen(query: str) -> str:
    value = re.sub(r"\bsite:\S+", " ", query or "", flags=re.I)
    value = value.replace('"', " ").replace("'", " ")
    return _space(value)


async def _search(query: str, vault: Vault, mission_number: int) -> list[int]:
    q = _space(query)
    if not q:
        return []
    if q not in vault.searches:
        vault.searches.append(q)
    attempts = [q]
    loose = _loosen(q)
    if loose and loose != q:
        attempts.append(loose)
    payload = None
    for provider in _search_order():
        for attempt in attempts:
            try:
                result = await search_web(
                    attempt,
                    provider=provider,
                    num=SEARCH_RESULTS,
                    timeout=MISSION_SEARCH_TIMEOUT,
                )
                if getattr(result, "results", None):
                    payload = result
                    break
            except Exception:
                continue
        if payload is not None:
            break
    if payload is None:
        return []
    _budget_note(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    results = list(getattr(payload, "results", None) or [])
    if not receipt:
        return []
    numbers: list[int] = []
    for item in results:
        result_id = getattr(item, "result_id", None)
        note = str(getattr(item, "note", None) or "")
        if not isinstance(result_id, str) or not result_id or not note.strip():
            continue
        title = str(getattr(item, "title", None) or "")
        url = str(getattr(item, "url", None) or "")
        number = vault.add_source(
            receipt,
            result_id,
            note,
            title,
            url,
            [(0, min(len(note), SEARCH_NOTE_SHOW))],
            "search",
            mission_number,
        )
        if number and number not in numbers:
            numbers.append(number)
    return numbers


async def _fetch(url: str, focus: str, question: str, vault: Vault, mission_number: int) -> int:
    target = (url or "").strip()
    if not target:
        return 0
    if target not in vault.fetches:
        vault.fetches.append(target)
    payload = None
    for provider in _search_order():
        for _attempt in (0, 1):
            try:
                result = await fetch_page(
                    target,
                    provider=provider,
                    timeout=MISSION_FETCH_TIMEOUT,
                )
                if getattr(result, "results", None):
                    payload = result
                    break
            except Exception:
                continue
        if payload is not None:
            break
    if payload is None:
        return 0
    _budget_note(payload)
    receipt = str(getattr(payload, "receipt_id", "") or "")
    results = list(getattr(payload, "results", None) or [])
    if not receipt or not results:
        return 0
    item = results[0]
    result_id = getattr(item, "result_id", None)
    note = str(getattr(item, "note", None) or "")
    if not isinstance(result_id, str) or not result_id or not note.strip():
        return 0
    title = str(getattr(item, "title", None) or target)
    result_url = str(getattr(item, "url", None) or target)
    if len(note) <= 7500:
        shown = [(0, len(note))]
    else:
        shown = [(0, min(FETCH_HEAD, len(note)))]
        for span in _best_windows(note, f"{question}\n{focus}"):
            shown.append(span)
        shown = _merge_spans(shown, len(note))
    return vault.add_source(
        receipt,
        result_id,
        note,
        title,
        result_url,
        shown,
        "fetch",
        mission_number,
    )


def _top_fetch_urls(numbers: list[int], vault: Vault, mission: Mission) -> list[tuple[int, str]]:
    ranked: list[tuple[int, int, str]] = []
    for number in numbers:
        row = vault.source(number)
        if row is None:
            continue
        url = str(row.get("url") or "")
        if not url:
            continue
        score = _to_int(row.get("authority"), 0)
        if mission.prefer_primary:
            score *= 3
        ranked.append((-score, number, url))
    ranked.sort()
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for _, number, url in ranked:
        host = _host(url)
        key = host + "|" + url
        if key in seen:
            continue
        seen.add(key)
        out.append((number, url))
        if len(out) >= FETCH_PER_MISSION:
            break
    return out


def _source_excerpt(number: int, vault: Vault, focus: str, limit: int = 3500) -> str:
    row = vault.source(number)
    if row is None:
        return ""
    text = str(row.get("text") or "")
    if not text:
        return ""
    windows = _best_windows(text, focus, min(limit, 3800), 1)
    if windows:
        start, end = windows[0]
        return text[start:end]
    return text[:limit]


async def _mission_extract(mission: Mission, contract: Contract, vault: Vault, deadline: float) -> None:
    if not mission.source_numbers or _seconds(deadline) < 40.0 or _money() < 0.012:
        return
    blocks: list[str] = []
    for number in mission.source_numbers[:10]:
        row = vault.source(number)
        if row is None:
            continue
        excerpt = _source_excerpt(number, vault, f"{mission.objective} {' '.join(mission.focus_terms)}")
        blocks.append(
            f"[{number}] TITLE: {row.get('title','')}\nURL: {row.get('url','')}\n{excerpt}"
        )
    evidence = "\n\n".join(blocks)
    if not evidence:
        return
    system = (
        "You are a mission evidence extractor. Decide only the assigned objective from the "
        "provided source excerpts. Return JSON only. Never invent a source number. Select exact "
        "verbatim quotes that directly prove or disprove the objective; quotes must appear in the "
        "excerpt. If evidence is inconclusive, say so rather than guessing."
    )
    user = f'''QUESTION:\n{contract.question}\n\nMISSION:\n{mission.block()}\n\nEVIDENCE:\n{evidence[:30000]}\n\nReturn:\n{{"status":"supported|refuted|inconclusive","finding":"concise factual finding","candidate":"candidate if any","condition":"condition tested","proof":[{{"source":1,"quote":"exact quote"}}],"new_candidates":["candidate discovered incidentally"]}}'''
    payload = await _chat(
        MISSION_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        1800,
        0.0,
        min(26.0, MISSION_PLAN_TIMEOUT),
    )
    data = _json_obj(_llm_text(payload))
    if data is None:
        return
    status = str(data.get("status") or "inconclusive")[:40]
    finding = _space(str(data.get("finding") or ""))
    candidate = _space(str(data.get("candidate") or mission.candidate))
    condition = _space(str(data.get("condition") or mission.condition))
    proof = data.get("proof")
    refs: list[int] = []
    if isinstance(proof, list):
        for item in proof:
            if not isinstance(item, dict):
                continue
            number = _to_int(item.get("source"), 0)
            quote = item.get("quote")
            if number <= 0 or not isinstance(quote, str):
                continue
            if vault.retain(number, quote):
                refs.append(number)
    if finding:
        mission.findings.append(finding[:900])
        vault.add_claim(finding, status, refs, candidate, condition)
    if candidate:
        vault.add_candidate(candidate)
    for item in _unique_strings(data.get("new_candidates"), 10, 180):
        vault.add_candidate(item)
    mission.status = status


async def _run_mission(mission: Mission, contract: Contract, vault: Vault, deadline: float) -> None:
    if _seconds(deadline) < 55.0:
        mission.status = "skipped-time"
        return
    mission.status = "searching"
    numbers: list[int] = []
    # Query searches are independent; run them concurrently within the mission.
    tasks = [_search(query, vault, mission.number) for query in mission.queries[:MISSION_QUERY_CAP]]
    if tasks:
        search_tasks = [asyncio.create_task(item) for item in tasks]
        done, pending = await asyncio.wait(search_tasks)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                result = task.result()
            except Exception:
                result = []
            if isinstance(result, list):
                for number in result:
                    if isinstance(number, int) and number > 0 and number not in numbers:
                        numbers.append(number)
    fetch_targets = _top_fetch_urls(numbers, vault, mission)
    fetch_tasks = []
    for _, url in fetch_targets:
        focus = " ".join([mission.objective, mission.condition] + mission.focus_terms)
        fetch_tasks.append(_fetch(url, focus, contract.question, vault, mission.number))
    if fetch_tasks:
        scheduled_fetches = [asyncio.create_task(item) for item in fetch_tasks]
        done, pending = await asyncio.wait(scheduled_fetches)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                item = task.result()
            except Exception:
                item = 0
            if isinstance(item, int) and item > 0 and item not in numbers:
                numbers.append(item)
    mission.source_numbers = numbers[:16]
    if not mission.source_numbers:
        mission.status = "no-evidence"
        return
    mission.status = "extracting"
    try:
        await _mission_extract(mission, contract, vault, deadline)
    except Exception:
        mission.status = "evidence-collected"
    if mission.status == "extracting":
        mission.status = "evidence-collected"


async def _run_wave(missions: list[Mission], contract: Contract, vault: Vault, deadline: float) -> None:
    for mission in missions:
        vault.missions.append(mission)
    index = 0
    while index < len(missions):
        if _seconds(deadline) < 55.0:
            break
        group = missions[index : index + MISSION_CONCURRENCY]
        tasks = [asyncio.create_task(_run_mission(mission, contract, vault, deadline)) for mission in group]
        done, pending = await asyncio.wait(tasks)
        for task in pending:
            task.cancel()
        for task in done:
            try:
                task.result()
            except Exception:
                pass
        index += MISSION_CONCURRENCY


# ---------------------------------------------------------------------------
# Global adjudication and gap generation
# ---------------------------------------------------------------------------


def _fallback_gap_context(contract: Contract, vault: Vault) -> str:
    notes: list[str] = []
    if contract.complete_set and not vault.candidates:
        notes.append("The complete candidate pool is not established.")
    if contract.superlative:
        notes.append("Verify that the winning value is compared against the full relevant pool.")
    if contract.named_sources:
        notes.append("The question names these sources and they need direct targeting: " + ", ".join(contract.named_sources))
    if contract.temporal:
        notes.append("Check the exact date/reference period rather than a nearby year.")
    if contract.computed:
        notes.append("Verify every input to the requested computation and preserve units.")
    return "\n".join(notes)


async def _adjudicate(contract: Contract, vault: Vault, deadline: float) -> dict[str, Any]:
    fallback = {
        "tentative_answer": "",
        "answer_confidence": 0.0,
        "gaps": [_fallback_gap_context(contract, vault)] if _fallback_gap_context(contract, vault) else [],
        "followup_missions": [],
        "claims": [],
    }
    if _money() < ADJUDICATE_MIN_USD or _seconds(deadline) < 80.0:
        return fallback
    system = (
        "You are the adjudicator of independent research missions. Reconstruct the answer from "
        "evidence, not from model memory. Check every contract constraint. For complete-set questions, "
        "prove the pool and every inclusion/exclusion. For superlatives, verify the comparison universe. "
        "For computations, verify each operand and units. Return JSON only. Source numbers must refer to "
        "the supplied evidence."
    )
    user = f'''QUESTION:\n{contract.question}\n\nCONTRACT:\n{contract.block()}\n\nMISSION RESULTS:\n{vault.mission_digest()[-26000:]}\n\nCLAIM RECORDS:\n{vault.claim_digest()[-18000:]}\n\nEVIDENCE:\n{vault.evidence_digest(42000)}\n\nReturn:\n{{"tentative_answer":"best evidence-supported answer or empty","answer_confidence":0.0,"claims":[{{"claim":"atomic claim","status":"supported|refuted|uncertain","sources":[1],"candidate":"","condition":""}}],"gaps":["specific unresolved fact"],"followup_missions":[{{"role":"gap|counterexample|authority|verifier","objective":"one unresolved fact","queries":["precise query"],"focus_terms":["term"],"candidate":"","condition":"","prefer_primary":true,"must_resolve":true}}]}}'''
    payload = await _chat(
        ADJUDICATOR_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        4200,
        0.0,
        ADJUDICATE_TIMEOUT,
    )
    data = _json_obj(_llm_text(payload))
    if data is None:
        return fallback
    raw_claims = data.get("claims")
    if isinstance(raw_claims, list):
        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            claim = item.get("claim")
            if not isinstance(claim, str):
                continue
            sources = item.get("sources")
            refs: list[int] = []
            if isinstance(sources, list):
                for value in sources:
                    number = _to_int(value, 0)
                    if 1 <= number <= len(vault.sources) and number not in refs:
                        refs.append(number)
            vault.add_claim(
                claim,
                str(item.get("status") or "uncertain"),
                refs,
                str(item.get("candidate") or ""),
                str(item.get("condition") or ""),
            )
    return data


def _missions_from_adjudication(data: dict[str, Any], vault: Vault, cap: int) -> list[Mission]:
    raw = data.get("followup_missions")
    if not isinstance(raw, list):
        return []
    out: list[Mission] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        objective = item.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            continue
        mission = Mission(len(vault.missions) + len(out) + 1, objective)
        mission.apply(item)
        if mission.queries:
            out.append(mission)
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Skeptic: independent attempt to falsify the current answer
# ---------------------------------------------------------------------------


async def _skeptic_plan(contract: Contract, vault: Vault, adjudication: dict[str, Any], deadline: float) -> list[Mission]:
    if _money() < SKEPTIC_MIN_USD or _seconds(deadline) < STOP_SKEPTIC_LEFT:
        return []
    answer = str(adjudication.get("tentative_answer") or "")
    system = (
        "You are a skeptical verification editor. Your job is NOT to improve prose. Attempt to falsify "
        "the tentative answer by identifying overlooked candidates, boundary cases, wrong years, wrong "
        "units, source-definition mismatches, or counterexamples. Create a few high-value independent "
        "research missions. Return JSON only."
    )
    user = f'''QUESTION:\n{contract.question}\n\nCONTRACT:\n{contract.block()}\n\nTENTATIVE ANSWER:\n{answer[:7000]}\n\nCURRENT CLAIMS:\n{vault.claim_digest()[-16000:]}\n\nCANDIDATES:\n{json.dumps(vault.candidates[:40], ensure_ascii=False)}\n\nReturn:\n{{"missions":[{{"role":"counterexample|exclusion|time|authority|verifier","objective":"specific way to try to disprove the answer","queries":["precise search"],"focus_terms":["term"],"candidate":"","condition":"","prefer_primary":true,"must_resolve":true}}]}}\nAt most {SKEPTIC_MISSIONS} missions.'''
    payload = await _chat(
        SKEPTIC_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        2600,
        0.05,
        SKEPTIC_TIMEOUT,
    )
    data = _json_obj(_llm_text(payload))
    raw = data.get("missions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[Mission] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        objective = item.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            continue
        mission = Mission(len(vault.missions) + len(out) + 1, objective)
        mission.apply(item)
        if mission.queries:
            out.append(mission)
        if len(out) >= SKEPTIC_MISSIONS:
            break
    return out


# ---------------------------------------------------------------------------
# Proof selection and final answer
# ---------------------------------------------------------------------------


_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _citation_numbers(text: str, maximum: int) -> list[int]:
    out: list[int] = []
    for match in _CITE_RE.finditer(text or ""):
        number = _to_int(match.group(1), 0)
        if 1 <= number <= maximum and number not in out:
            out.append(number)
    return out


def _citations(marker_text: str, vault: Vault) -> list[CitationRef]:
    refs: list[CitationRef] = []
    spent = 0
    for number in _citation_numbers(marker_text, len(vault.sources)):
        if len(refs) >= MAX_CITATIONS:
            break
        ref, cost = vault.citation(number)
        if ref is None:
            continue
        if spent + cost > MAX_MATERIALIZED_EVIDENCE:
            continue
        refs.append(ref)
        spent += cost
    return refs


def _retain_from_writer_proof(data: dict[str, Any], vault: Vault) -> None:
    proof = data.get("proof_quotes")
    if not isinstance(proof, list):
        return
    for item in proof:
        if not isinstance(item, dict):
            continue
        number = _to_int(item.get("source"), 0)
        quote = item.get("quote")
        if number <= 0 or not isinstance(quote, str):
            continue
        vault.retain(number, quote)


def _answer_text_from_writer(data: dict[str, Any]) -> str:
    answer = data.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    line = data.get("answer_line")
    proof = data.get("proof")
    parts: list[str] = []
    if isinstance(line, str) and line.strip():
        parts.append(line.strip())
    if isinstance(proof, str) and proof.strip():
        parts.append(proof.strip())
    return "\n\n".join(parts)


def _normalize_markers(text: str, maximum: int) -> str:
    def repl(match: re.Match[str]) -> str:
        number = _to_int(match.group(1), 0)
        if 1 <= number <= maximum:
            return f"[{number}]"
        return ""
    return re.sub(r"\[(\d{1,4})\]", repl, text or "")


def _clean_answer(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^```(?:markdown|text)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(r"^(?:Answer|Final answer)\s*:\s*", "", value, flags=re.I)
    return value.strip()


def _usable(text: str) -> bool:
    value = _clean_answer(text)
    if len(value) < 2:
        return False
    low = value.lower()
    bad = (
        "i could not complete",
        "unable to answer",
        "best-effort answer unavailable",
        "i cannot determine",
        "insufficient information to answer",
    )
    if any(phrase in low for phrase in bad):
        return False
    return True


def _evidence_rescue(contract: Contract, vault: Vault, adjudication: dict[str, Any]) -> str:
    tentative = str(adjudication.get("tentative_answer") or "").strip()
    if _usable(tentative):
        return tentative
    supported = [item for item in vault.claims if str(item.get("status") or "").lower() == "supported"]
    if supported:
        lines: list[str] = []
        for item in supported[:10]:
            refs = item.get("sources")
            markers = ""
            if isinstance(refs, list):
                markers = " ".join(f"[{x}]" for x in refs[:3] if isinstance(x, int))
            lines.append(f"{item.get('claim','')} {markers}".strip())
        return "\n".join(lines)
    if vault.sources:
        row = vault.sources[0]
        return _cut(str(row.get("text") or ""), 1600)
    return contract.question[:500]


async def _write_answer(contract: Contract, vault: Vault, adjudication: dict[str, Any], deadline: float) -> tuple[str, str]:
    rescue = _evidence_rescue(contract, vault, adjudication)
    if _seconds(deadline) < STOP_WRITE_LEFT or _money() < WRITE_MIN_USD:
        return rescue, rescue
    system = (
        "You are the final answer compiler. Use ONLY the supplied evidence and adjudicated claims for "
        "load-bearing facts. Sentence one must directly answer the asked kind. Every specific factual "
        "claim must carry [source-number] immediately after that claim. For set questions, show enough "
        "proof to establish completeness and exclusions. For superlatives, prove the comparison universe. "
        "For named-source questions, preserve the source's exact labels/values. For calculations, expose the "
        "inputs and arithmetic succinctly. Do not mention research limitations when the fact is established. "
        "Return JSON only."
    )
    user = f'''QUESTION:\n{contract.question}\n\nCONTRACT:\n{contract.block()}\n\nADJUDICATION:\n{json.dumps(adjudication, ensure_ascii=False)[:18000]}\n\nCLAIMS:\n{vault.claim_digest()[-22000:]}\n\nEVIDENCE:\n{vault.evidence_digest(50000)}\n\nReturn:\n{{"answer":"complete final answer containing [n] markers","proof_quotes":[{{"source":1,"quote":"exact verbatim quote from evidence that supports a claim used in the answer"}}]}}\nUse proof_quotes for the decisive claims and premises so their citation slices contain the actual proof.'''
    payload = await _chat(
        WRITER_MODELS,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        deadline,
        5200,
        0.0,
        WRITER_TIMEOUT,
    )
    data = _json_obj(_llm_text(payload))
    if data is None:
        return rescue, rescue
    _retain_from_writer_proof(data, vault)
    answer = _answer_text_from_writer(data)
    answer = _normalize_markers(_clean_answer(answer), len(vault.sources))
    if not _usable(answer):
        answer = rescue
    return answer, answer


# ---------------------------------------------------------------------------
# Output instruction handling
# ---------------------------------------------------------------------------


def _strict_answer_line(text: str) -> str:
    value = text.strip()
    if not value:
        return value
    first = value.splitlines()[0].strip()
    first = _CITE_RE.sub("", first)
    return _space(first)


def _apply_text_contract(answer: str, contract: Contract) -> str:
    value = answer.strip()
    if contract.strict_output:
        return _strict_answer_line(value)
    if len(value) > MAX_ANSWER:
        return value[: MAX_ANSWER - 4].rstrip() + " …"
    return value


# ---------------------------------------------------------------------------
# Structured-output adapter
# ---------------------------------------------------------------------------


def _schema_kind(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    kind = schema.get("type")
    if isinstance(kind, str):
        return kind
    for key in ("anyOf", "oneOf", "allOf"):
        raw = schema.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("type") != "null":
                    found = _schema_kind(item)
                    if found:
                        return found
    return ""


def _schema_branch(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("type"), str):
        return schema
    for key in ("anyOf", "oneOf", "allOf"):
        raw = schema.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("type") != "null":
                    return _schema_branch(item)
    return schema


def _shape_ok(value: Any, schema: Any, depth: int = 0) -> bool:
    if depth > 7 or not isinstance(schema, dict):
        return True
    if "enum" in schema and isinstance(schema.get("enum"), list):
        return value in schema.get("enum")
    branch = _schema_branch(schema)
    kind = _schema_kind(branch)
    if kind == "object":
        if not isinstance(value, dict):
            return False
        required = branch.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    return False
        props = branch.get("properties")
        if isinstance(props, dict):
            for key, child in props.items():
                if key in value and not _shape_ok(value[key], child, depth + 1):
                    return False
        return True
    if kind == "array":
        if not isinstance(value, list):
            return False
        item_schema = branch.get("items")
        if isinstance(item_schema, dict):
            return all(_shape_ok(item, item_schema, depth + 1) for item in value[:50])
        return True
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


def _strip_markers(text: str) -> str:
    return _space(_CITE_RE.sub("", text or ""))


def _value_lines(answer: str) -> list[str]:
    clean = _CITE_RE.sub("", answer or "")
    out: list[str] = []
    for raw in re.split(r"[\n;]+", clean):
        line = raw.strip().lstrip("-*• ").strip()
        if not line:
            continue
        if len(line) > 180:
            continue
        if line not in out:
            out.append(line)
        if len(out) >= 30:
            break
    return out


def _coerce(answer: str, schema: Any, depth: int = 0) -> Any:
    if depth > 6 or not isinstance(schema, dict):
        return _strip_markers(answer)[:500]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        low = answer.lower()
        for option in enum:
            if isinstance(option, str) and option.lower() in low:
                return option
        return enum[0]
    branch = _schema_branch(schema)
    kind = _schema_kind(branch)
    if kind == "object":
        props = branch.get("properties")
        if not isinstance(props, dict):
            props = {}
        required = branch.get("required")
        keys = required if isinstance(required, list) else list(props.keys())
        out: dict[str, Any] = {}
        for key in keys:
            if isinstance(key, str):
                child = props.get(key)
                out[key] = _coerce(answer, child if isinstance(child, dict) else {}, depth + 1)
        return out
    if kind == "array":
        item_schema = branch.get("items")
        if not isinstance(item_schema, dict):
            item_schema = {}
        lines = _value_lines(answer)
        return [_coerce(line, item_schema, depth + 1) for line in lines[:20]]
    if kind in ("integer", "number"):
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", _CITE_RE.sub("", answer or ""))
        if match is None:
            return 0 if kind == "integer" else 0.0
        raw = match.group(0).replace(",", "")
        try:
            return int(float(raw)) if kind == "integer" else float(raw)
        except Exception:
            return 0 if kind == "integer" else 0.0
    if kind == "boolean":
        low = _strip_markers(answer).lower()
        if re.match(r"^(no|false|none|neither)\b", low):
            return False
        return True
    if kind == "null":
        return None
    return _strip_markers(answer)[:600]


async def _structured(answer: str, question: str, schema: Any, vault: Vault, deadline: float) -> Any:
    if schema is None:
        return None
    if _seconds(deadline) >= MIN_SCHEMA_LEFT and _money() >= SCHEMA_MIN_USD:
        system = (
            "Convert the researched answer into the required JSON schema. Return JSON only: an object "
            "with key 'output'. Preserve exact names, labels, dates, units and numeric values. Do not add "
            "explanatory prose inside scalar fields."
        )
        user = (
            f"QUESTION:\n{question}\n\nANSWER:\n{answer[:16000]}\n\n"
            f"SCHEMA:\n{json.dumps(schema, ensure_ascii=False)[:18000]}\n\n"
            "Return {\"output\": <value matching schema>}"
        )
        payload = await _chat(
            SCHEMA_MODELS,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            deadline,
            2600,
            0.0,
            SCHEMA_TIMEOUT,
        )
        data = _json_obj(_llm_text(payload))
        if data is not None and "output" in data and _shape_ok(data.get("output"), schema):
            return data.get("output")
    forced = _coerce(answer, schema)
    if _shape_ok(forced, schema):
        return forced
    kind = _schema_kind(_schema_branch(schema))
    if kind == "array":
        return []
    if kind == "object":
        return _coerce("", schema)
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return ""


# ---------------------------------------------------------------------------
# Main solver
# ---------------------------------------------------------------------------


async def _solve(query: Query, question: str) -> Response:
    deadline = monotonic() + WALL_SECONDS
    try:
        await _load_tooling()
    except Exception:
        pass

    contract = await _compile_contract(question, query.output_schema, deadline)
    vault = Vault()

    # Wave 1: independent coverage of pool, premises, primary sources and difficult
    # conditions. This is the architectural root: a mission DAG, not a chat tool-loop.
    first = await _plan_missions(contract, vault, deadline, 1, FIRST_WAVE_MISSIONS)
    try:
        await _run_wave(first, contract, vault, deadline)
    except Exception:
        pass

    # Central adjudicator creates explicit claim/gap state.
    try:
        adjudication = await _adjudicate(contract, vault, deadline)
    except Exception:
        adjudication = {
            "tentative_answer": "",
            "answer_confidence": 0.0,
            "gaps": [],
            "followup_missions": [],
            "claims": [],
        }

    # Wave 2 is driven by gaps rather than continuing a monolithic conversation.
    if _seconds(deadline) > STOP_SECOND_WAVE_LEFT and _money() > 0.03:
        second = _missions_from_adjudication(adjudication, vault, SECOND_WAVE_MISSIONS)
        if not second:
            gaps = adjudication.get("gaps")
            gap_text = "\n".join(_unique_strings(gaps, 8, 800)) if isinstance(gaps, list) else ""
            try:
                second = await _plan_missions(contract, vault, deadline, 2, SECOND_WAVE_MISSIONS, gap_text)
            except Exception:
                second = []
        if second:
            try:
                await _run_wave(second, contract, vault, deadline)
            except Exception:
                pass
            try:
                adjudication = await _adjudicate(contract, vault, deadline)
            except Exception:
                pass

    # Independent skeptical red-team. It tries to disprove the current result.
    if _seconds(deadline) > STOP_SKEPTIC_LEFT and _money() > SKEPTIC_MIN_USD:
        try:
            skeptical = await _skeptic_plan(contract, vault, adjudication, deadline)
        except Exception:
            skeptical = []
        if skeptical:
            try:
                await _run_wave(skeptical, contract, vault, deadline)
            except Exception:
                pass
            if _seconds(deadline) > 48.0 and _money() > ADJUDICATE_MIN_USD:
                try:
                    adjudication = await _adjudicate(contract, vault, deadline)
                except Exception:
                    pass

    # Final compile. Marker text is retained separately so citations survive strict
    # output shaping (where the visible first line may intentionally have no markers).
    try:
        answer, marker_text = await _write_answer(contract, vault, adjudication, deadline)
    except Exception:
        answer = _evidence_rescue(contract, vault, adjudication)
        marker_text = answer

    marker_text = _normalize_markers(marker_text, len(vault.sources))
    try:
        refs = _citations(marker_text, vault)
    except Exception:
        refs = []

    answer = _apply_text_contract(_clean_answer(answer), contract)
    if not answer:
        answer = _cut(_evidence_rescue(contract, vault, adjudication), MAX_ANSWER)

    if query.output_schema is not None:
        try:
            output = await _structured(answer, question, query.output_schema, vault, deadline)
            return Response(output=output, citations=refs or None)
        except Exception:
            fallback = _coerce(answer, query.output_schema)
            return Response(output=fallback, citations=refs or None)

    try:
        return Response(text=answer, citations=refs or None)
    except Exception:
        return Response(text=_cut(answer, 60000))


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
        if query.output_schema is not None:
            return Response(output=_coerce(question, query.output_schema))
        return Response(text=_cut(question, 1000))
