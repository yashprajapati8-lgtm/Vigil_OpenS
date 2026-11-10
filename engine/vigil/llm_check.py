"""Check the LLM second key end to end: docker compose exec vigil-engine python -m vigil.llm_check"""

import asyncio
import json

from .config import SETTINGS
from .diagnosis import CATEGORIES
from .llm import LLMClient

# Replays the real slow-inventory incident from a live run, including the part that
# fooled the model once: payment's latency appeared 5 s before inventory's.
SAMPLE = {
    "incident": "VGL-CHECK",
    "services": ["payment", "inventory"],
    "calls": [["payment", "inventory"]],
    "evidence": [
        {"id": "E1", "service": "payment", "signal": "up", "value": 1, "note": "process running and answering health checks"},
        {"id": "E2", "service": "payment", "signal": "p95_ms", "value": 2400, "baseline": 72, "z": 130, "onset_s": 0},
        {"id": "E3", "service": "payment", "signal": "error_rate", "value": 0.85, "baseline": 0.0, "z": 170, "onset_s": 0},
        {"id": "E4", "service": "payment", "signal": "cpu_pct", "value": 16.9, "baseline": 14.8, "z": 1.2, "onset_s": 0},
        {"id": "E5", "service": "payment", "signal": "mem_mb", "value": 55.0, "baseline": 54.6, "z": 0.4, "onset_s": 0},
        {"id": "E6", "service": "payment", "signal": "rps", "value": 17.1, "baseline": 19.8, "z": -1.6, "onset_s": 0},
        {"id": "E7", "service": "inventory", "signal": "up", "value": 1, "note": "process running and answering health checks"},
        {"id": "E8", "service": "inventory", "signal": "p95_ms", "value": 2400, "baseline": 24, "z": 400, "onset_s": 0},
        {"id": "E9", "service": "inventory", "signal": "error_rate", "value": 0.0, "baseline": 0.0, "z": 0.0, "onset_s": 0},
        {"id": "E10", "service": "inventory", "signal": "cpu_pct", "value": 6.9, "baseline": 6.2, "z": 0.4, "onset_s": 0},
        {"id": "E11", "service": "inventory", "signal": "rps", "value": 18.8, "baseline": 19.7, "z": -0.6, "onset_s": 0},
        {"id": "E12", "edge": "payment->inventory", "signal": "failed_call_ratio", "value": 1.0},
        {"id": "E13", "edge": "payment->inventory", "signal": "call_p95_ms", "value": 2425},
        {"id": "E14", "service": "payment", "signal": "share_of_errors_that_are_failed_calls_to_inventory", "value": 1.0},
    ],
    "recent_changes": [],
    "categories": CATEGORIES,
}


async def main() -> None:
    llm = LLMClient(SETTINGS.groq_api_key, SETTINGS.groq_model, timeout_s=SETTINGS.llm_timeout_s)
    print(f"model: {SETTINGS.groq_model}; key set: {llm.enabled}")
    result = await llm.diagnose(SAMPLE)
    await llm.close()
    print(json.dumps(result, indent=2))
    if result["status"] == "ok" and result["root_cause_service"] == "inventory" and result["category"] == "stuck_process":
        print("\nPASS: real culprit (inventory) and the right category (stuck_process). Confidence can reach High.")
    elif result["status"] == "ok" and result["root_cause_service"] == "inventory":
        print(f"\nPARTIAL: right culprit, but category {result['category']} (expected stuck_process). Confidence would be Medium.")
    elif result["status"] == "ok":
        print("\nWARNING: valid answer but wrong root. VIGIL would mark confidence Low and not act alone.")
    else:
        print("\nFAIL: VIGIL will run on the blame walk alone (confidence capped at Medium).")


if __name__ == "__main__":
    asyncio.run(main())
