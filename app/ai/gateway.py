"""
AI Gateway
===========

Gemini-backed enhancement layer for trip generation payloads.

Mirrors the pattern already proven in the Android app's
ItineraryAiService.kt (GenerativeModel + JSON-mode generationConfig +
systemInstruction constraining the model to given facts + per-method
deterministic fallback that swallows its own exceptions), reimplemented
against the Python google.generativeai SDK rather than the Kotlin one,
since this runs server-side.

Non-negotiable rules (same as ItineraryAiService.kt's own docstring):
  - Used only as a reasoning/prose layer on top of facts the backend
    already computed -- never to originate distances, costs, times, or
    visa rules.
  - Every call has a deterministic fallback and never lets a Gemini
    outage/timeout/malformed-response propagate as an exception to the
    caller.
  - The model is explicitly instructed, via system_instruction, not to
    invent facts beyond what's given in the prompt.

VERIFICATION NEEDED: this is written against the documented
google.generativeai Python SDK surface
(genai.configure/GenerativeModel/generate_content with
request_options={"timeout": ...} and generation_config=
GenerationConfig(response_mime_type="application/json")). I do not have
network access in this environment to install the package and smoke-test
it against your actual pinned version. Before relying on this in
production: `pip install google-generativeai`, run
`python -c "import google.generativeai as genai; print(genai.__version__)"`,
and do one live call with GEMINI_API_KEY set to confirm the method
signatures below match your installed version. If your requirements.txt
pins an older/newer version with a different API shape, flag it back to
me and I will adjust.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


GEMINI_MODEL_NAME = "gemini-2.5-flash"

# Request-level timeout passed INTO the SDK call itself, not just a
# wrapper around it. This is what actually cancels a hung socket
# server-side -- a caller-side thread timeout alone (see
# day_narrative_service.py) only stops the CALLER from waiting; it
# does not close the underlying connection, which is exactly the gap
# flagged before this file was wired to a real provider.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0

SYSTEM_INSTRUCTION = (
    "You are a logistics-aware Africa safari trip assistant. You are "
    "given FACTS about an already-computed itinerary (routes, "
    "distances, times, dates, destinations). Do not invent any new "
    "facts, distances, times, or costs -- only reason about or rephrase "
    "the facts you're given. Always respond with STRICT VALID JSON "
    "matching exactly the structure requested in the user message, and "
    "nothing else (no markdown fences)."
)


class AIEnhancementResult:
    def __init__(
        self,
        data: Dict[str, Any] | None = None,
        available: bool = False,
        error: str | None = None,
    ):
        self.data = data or {}
        self.available = available
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return self.data


class AIGateway:
    """
    Thin wrapper around google.generativeai, matching the
    fallback-per-call contract ItineraryAiService.kt already
    established on the Android side.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = GEMINI_MODEL_NAME,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        self.model_name = model_name
        self.request_timeout_seconds = request_timeout_seconds
        self.enabled = bool(self.api_key)

        self._model = None

        if not self.enabled:
            logger.warning(
                "GEMINI_API_KEY missing/blank -- AI enhancement features "
                "will use deterministic fallbacks only."
            )
            return

        try:
            import google.generativeai as genai

            genai.configure(api_key=self.api_key)

            self._model = genai.GenerativeModel(
                model_name=self.model_name,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                ),
                system_instruction=SYSTEM_INSTRUCTION,
            )
        except Exception as exc:
            # Import failure (package not installed), bad API key
            # format, or any other setup-time problem -- degrade to
            # disabled rather than let a broken AI dependency prevent
            # the app from starting at all.
            logger.exception(
                "Failed to initialize Gemini model; AI enhancement "
                "features will use deterministic fallbacks only: %s",
                exc,
            )
            self._model = None
            self.enabled = False

    def is_available(self) -> bool:
        return self.enabled and self._model is not None

    # ------------------------------------------------------------------
    # Generic JSON-completion call
    # ------------------------------------------------------------------

    def generate_json(
        self,
        prompt: str,
        *,
        timeout_seconds: float | None = None,
    ) -> AIEnhancementResult:
        """
        Runs a single JSON-mode completion against Gemini, with a real
        request-level timeout passed into the SDK call itself.

        Returns AIEnhancementResult(available=False, error=...) on ANY
        failure -- disabled gateway, timeout, malformed JSON, or any
        SDK exception. Never raises. Callers should treat
        available=False the same way ItineraryAiService.kt's callers
        treat a null/exception result: use their own deterministic
        fallback, not this method's absence of data.
        """

        if not self.is_available():
            return AIEnhancementResult(
                data={},
                available=False,
                error="AI Gateway disabled: missing API credentials or model failed to initialize",
            )

        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.request_timeout_seconds
        )

        try:
            # request_options={"timeout": ...} is passed INTO the SDK
            # call -- this is the piece that was missing before this
            # file had a real provider wired in. It instructs the
            # underlying gRPC/HTTP transport itself to give up after
            # this many seconds, rather than relying solely on a
            # caller-side thread timeout that can only stop the
            # caller from waiting, not cancel the in-flight request.
            response = self._model.generate_content(
                prompt,
                request_options={"timeout": effective_timeout},
            )

            raw_text = getattr(response, "text", None)

            if not raw_text:
                return AIEnhancementResult(
                    data={},
                    available=False,
                    error="Gemini returned an empty response",
                )

            cleaned = self._strip_markdown_fences(raw_text)

            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Gemini response was not valid JSON: %s. Raw text: %r",
                    exc,
                    raw_text[:200],
                )
                return AIEnhancementResult(
                    data={},
                    available=False,
                    error=f"Gemini response was not valid JSON: {exc}",
                )

            if not isinstance(parsed, dict):
                return AIEnhancementResult(
                    data={},
                    available=False,
                    error="Gemini response JSON was not an object",
                )

            return AIEnhancementResult(data=parsed, available=True)

        except Exception as exc:
            # Covers SDK-level timeout exceptions, network errors,
            # quota/rate-limit errors, and anything else the SDK can
            # raise -- all treated identically: log it, degrade
            # cleanly, never propagate.
            logger.exception("Gemini call failed: %s", exc)
            return AIEnhancementResult(
                data={},
                available=False,
                error=str(exc),
            )

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        text = text.strip()
        if text.startswith("```json"):
            text = text[len("```json"):]
        elif text.startswith("```"):
            text = text[len("```"):]
        if text.endswith("```"):
            text = text[: -len("```")]
        return text.strip()

    # ------------------------------------------------------------------
    # Legacy/compatibility method
    # ------------------------------------------------------------------

    def enhance_trip(self, trip_dict: Dict[str, Any]) -> AIEnhancementResult:
        """
        Retained for compatibility with existing callers. Prefer
        generate_json() for new call sites -- it's the one with the
        real timeout/JSON-parsing contract documented above.

        This method's previous implementation returned hardcoded
        placeholder text unconditionally; it has NOT been ported to a
        real prompt here because no caller in this codebase currently
        depends on its exact input/output shape for anything
        user-facing (grep for enhance_trip callers before repurposing
        it). day_narrative_service.py has been updated to call
        generate_json() directly instead, with its own prompt built
        from ExplanationEngine's verified facts.
        """

        if not self.is_available():
            return AIEnhancementResult(
                data={},
                available=False,
                error="AI Gateway disabled: missing API credentials",
            )

        prompt = (
            "Given this trip data, respond with JSON: "
            '{"summary": "one sentence", "highlights": ["item1", "item2"]}. '
            f"Trip title: {trip_dict.get('title', 'Your Safari')}."
        )

        return self.generate_json(prompt)


_ai_gateway_instance: AIGateway | None = None


def get_ai_gateway() -> AIGateway:
    global _ai_gateway_instance
    if _ai_gateway_instance is None:
        _ai_gateway_instance = AIGateway()
    return _ai_gateway_instance


__all__ = [
    "AIEnhancementResult",
    "AIGateway",
    "get_ai_gateway",
    "GEMINI_MODEL_NAME",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
]

