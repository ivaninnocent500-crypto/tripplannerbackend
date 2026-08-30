"""
Day Narrative Service
=======================

Produces the "Why this day?" text shown in the UI.

Non-negotiable rule
---------------------
The deterministic ExplanationEngine facts are the response. AI-generated
prose is a strictly optional, time-bounded UPGRADE applied on top of
those facts -- never a replacement path the response waits on, and
never a source of facts the deterministic engine didn't already
establish.

    ExplanationEngine.explain(cabinet)
            |
            v
    hardcoded facts ---------------------------> ALWAYS returned,
            | immediately, if AI
            | is slow/unavailable/
            | errors
            v
    (bounded-time attempt to turn facts into prose via AIGateway)
            |
            v
    if AI succeeded within the deadline -> prose replaces the raw
      fact bullets in the RESPONSE TEXT ONLY
    if AI did not succeed in time -> hardcoded facts stand, unchanged

This mirrors the project's standing principle (see route_geography.py,
ItineraryPlanningEngine.py): never let an optional enhancement
silently degrade into fabricated or blocking behavior. The AI call
here is explicitly instructed to work FROM the deterministic facts
only -- it is never given free rein to invent a reason for a
scheduling decision that isn't already backed by ExplanationEngine's
output.

Timeout defense is two layers deep, on purpose:
  1. This module's own thread-based deadline (ai_deadline_seconds)
     bounds how long THIS CALLER waits before giving up and using the
     hardcoded facts.
  2. AIGateway.generate_json()'s request_options timeout (see
     gateway.py) is passed through and forwarded into the Gemini SDK
     call itself, so a genuinely hung request is cancelled at the
     transport level, not merely abandoned by this module's caller
     while the underlying connection stays open in the background.
Layer 1 alone (before gateway.py had a real provider wired in) could
only ever protect the RESPONSE from being slow; it could not prevent
a leaked thread/connection under load. Both layers are needed.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any

from app.ai.gateway import AIGateway, get_ai_gateway
from app.engines.explanation import ExplanationEngine
from app.engines.resilience import call_engine

logger = logging.getLogger(__name__)


# AI prose generation gets a hard ceiling well under typical HTTP
# client/gateway timeouts (usually 10-30s). This is deliberately short
# -- "why this day" text is a nice-to-have, not worth the response
# feeling slow for. Tune via the deadline_seconds parameter if a
# specific deployment needs different headroom; do not remove the
# bound entirely.
DEFAULT_AI_NARRATIVE_DEADLINE_SECONDS = 2.5

# A narrative below this length or above this length is treated as
# suspicious (empty completion, or the model rambling/hallucinating
# well past what "why this day" prose should look like) and the
# hardcoded facts are used instead rather than trusting the AI output
# blindly.
MIN_NARRATIVE_CHARS = 20
MAX_NARRATIVE_CHARS = 600


@dataclass
class DayNarrativeResult:
    """
    What the API layer actually sends to the client for "Why this day?".
    """

    # Always populated. These are ExplanationEngine's deterministic
    # fact bullets -- the ground truth, regardless of what happened
    # with AI narrative generation.
    facts: list[dict[str, str]] = field(default_factory=list)

    # The text actually shown in the "Why this day?" UI slot. Equal to
    # a hardcoded rendering of `facts` unless `narrative_source` is
    # "ai", in which case it's the AI's prose -- but even then, the
    # prose was generated FROM `facts`, never independently.
    narrative_text: str = ""

    # "deterministic" | "ai". Exposed so the API/UI layer can, if it
    # wants, badge AI-generated text differently -- never required to,
    # but the information isn't hidden.
    narrative_source: str = "deterministic"

    # True if ExplanationEngine itself failed and call_engine's
    # fallback was used. In that case facts/narrative_text reflect the
    # fallback ({"facts": [], "generated_by": "unavailable"}), and the
    # AI upgrade is never attempted -- there is nothing verified to
    # generate prose from.
    facts_degraded: bool = False

    cabinet_id: str | None = None


def _render_hardcoded_narrative(facts: list[dict[str, str]]) -> str:
    """
    The actual hardcoded fallback text. Deliberately plain -- this is
    what every "Why this day?" panel shows whenever AI narrative isn't
    available, so it needs to stand on its own as acceptable copy, not
    read like a degraded placeholder.
    """

    if not facts:
        return "This day was planned using verified route and schedule data."

    sentences = [fact.get("detail", "").strip() for fact in facts if fact.get("detail")]
    sentences = [s for s in sentences if s]

    if not sentences:
        return "This day was planned using verified route and schedule data."

    return " ".join(sentences)


def _build_ai_narrative_prompt(facts: list[dict[str, str]]) -> str:
    """
    Constrains the AI call to ONLY the verified facts. The prompt is
    explicit that no fact outside this list may be introduced --
    matching the project's standing "AI must never be in a position to
    silently wave through" a claim nothing verified.

    Requests JSON output matching AIGateway.generate_json()'s contract
    -- AIGateway's Gemini model is configured in JSON-mode via its
    system_instruction (see gateway.py), which already forbids
    inventing facts beyond what's given; this prompt supplies the
    specific facts and the specific response shape for this call.
    """

    fact_lines = "\n".join(
        f"- {fact.get('heading', '')}: {fact.get('detail', '')}"
        for fact in facts
        if fact.get("detail")
    )

    return (
        "FACTS (verified, already computed by the itinerary system):\n"
        f"{fact_lines}\n\n"
        "TASK: Rewrite these facts as 2-3 warm, concise sentences "
        "explaining why this day was planned this way. Use ONLY the "
        "facts listed above. Do not add any claim, reason, location, "
        "time, or number that is not already stated in these facts.\n\n"
        'Respond with exactly this JSON shape: {"narrative": "string"}'
    )


def _attempt_ai_narrative(
    ai_gateway: AIGateway,
    facts: list[dict[str, str]],
    deadline_seconds: float,
) -> str | None:
    """
    Runs the AI narrative call with a hard wall-clock deadline.

    Returns the generated text on success, or None on ANY failure mode
    -- timeout, exception, disabled gateway, or output that fails the
    sanity checks below. None always means "use the hardcoded
    narrative instead"; this function never raises.
    """

    if not ai_gateway.is_available():
        return None

    if not facts:
        # Nothing verified to build prose from -- do not call the AI
        # with an empty fact set, since that invites it to invent
        # content to fill the gap.
        return None

    prompt = _build_ai_narrative_prompt(facts)

    def _call() -> str:
        # Real Gemini call via AIGateway.generate_json(), matching
        # gateway.py's JSON-mode contract: the prompt requests
        # {"narrative": "string"}, and AIGateway's system_instruction
        # already constrains the model to the given facts. The
        # per-call timeout is passed straight through to
        # generate_json(), which forwards it into the SDK's own
        # request_options -- so a slow request is cancelled at the
        # transport level, not just abandoned by this thread.
        result = ai_gateway.generate_json(prompt, timeout_seconds=deadline_seconds)
        if not result.available:
            return ""
        text = result.data.get("narrative", "")
        return str(text).strip()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_call)

    try:
        text = future.result(timeout=deadline_seconds)
    except concurrent.futures.TimeoutError:
        logger.info(
            "AI narrative generation exceeded %.1fs deadline; "
            "using hardcoded facts instead.",
            deadline_seconds,
        )
        # Do NOT use `with executor:` here -- its __exit__ calls
        # shutdown(wait=True), which would block THIS call (the one
        # that just gave up waiting) until the slow background call
        # finishes, defeating the entire point of the timeout. Using
        # wait=False lets this function return immediately; the
        # worker thread finishes on its own time in the background,
        # and we never read its result after this point, so it cannot
        # affect the response we're about to send.
        executor.shutdown(wait=False)
        return None
    except Exception as exc:
        logger.warning(
            "AI narrative generation failed: %s. Using hardcoded "
            "facts instead.",
            exc,
        )
        executor.shutdown(wait=False)
        return None
    else:
        executor.shutdown(wait=False)

    if not (MIN_NARRATIVE_CHARS <= len(text) <= MAX_NARRATIVE_CHARS):
        logger.info(
            "AI narrative output failed length sanity check (%d chars); "
            "using hardcoded facts instead.",
            len(text),
        )
        return None

    return text


class DayNarrativeService:
    """
    The single entry point the API layer should call for "Why this
    day?" text. Wraps ExplanationEngine (hardcoded, always-on) with an
    optional, bounded-time AI prose upgrade.
    """

    def __init__(
        self,
        db: Any,
        ai_gateway: AIGateway | None = None,
        ai_deadline_seconds: float = DEFAULT_AI_NARRATIVE_DEADLINE_SECONDS,
    ):
        self.db = db
        self.ai_gateway = ai_gateway or get_ai_gateway()
        self.ai_deadline_seconds = ai_deadline_seconds
        self.explanation_engine = ExplanationEngine()

    def explain_day(self, cabinet: Any) -> DayNarrativeResult:
        """
        Returns a DayNarrativeResult for the given cabinet. Always
        returns promptly (bounded by ai_deadline_seconds at most) and
        always returns usable narrative_text, regardless of AI
        availability.
        """

        cabinet_id = str(getattr(cabinet, "id", "")) or None

        # STEP 1: hardcoded facts, via the existing resilience wrapper.
        # This is the same call_engine pattern already used elsewhere
        # in the pipeline -- if ExplanationEngine itself throws, we
        # degrade to an empty fact set rather than propagate the
        # exception, and the AI step below is skipped entirely because
        # there would be nothing verified to build prose from.
        engine_result = call_engine(
            "ExplanationEngine",
            lambda: self.explanation_engine.explain(cabinet),
            fallback={"facts": [], "generated_by": "unavailable"},
            db=self.db,
        )

        facts = engine_result.value.get("facts", [])
        hardcoded_text = _render_hardcoded_narrative(facts)

        result = DayNarrativeResult(
            facts=facts,
            narrative_text=hardcoded_text,
            narrative_source="deterministic",
            facts_degraded=engine_result.degraded,
            cabinet_id=cabinet_id,
        )

        if engine_result.degraded:
            # No verified facts to hand the AI -- ship the hardcoded
            # fallback sentence as-is, do not attempt AI narrative.
            return result

        # STEP 2: optional, bounded-time AI upgrade. This can only
        # ever IMPROVE narrative_text's wording -- it never changes
        # `facts`, never runs if step 1 degraded, and never blocks
        # past ai_deadline_seconds.
        ai_text = _attempt_ai_narrative(
            self.ai_gateway,
            facts,
            self.ai_deadline_seconds,
        )

        if ai_text:
            result.narrative_text = ai_text
            result.narrative_source = "ai"

        return result


def explain_day(
    db: Any,
    cabinet: Any,
    *,
    ai_gateway: AIGateway | None = None,
    ai_deadline_seconds: float = DEFAULT_AI_NARRATIVE_DEADLINE_SECONDS,
) -> DayNarrativeResult:
    """
    Functional convenience wrapper.
    """

    return DayNarrativeService(
        db,
        ai_gateway=ai_gateway,
        ai_deadline_seconds=ai_deadline_seconds,
    ).explain_day(cabinet)


__all__ = [
    "DayNarrativeResult",
    "DayNarrativeService",
    "explain_day",
    "DEFAULT_AI_NARRATIVE_DEADLINE_SECONDS",
]

