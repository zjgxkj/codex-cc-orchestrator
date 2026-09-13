"""Deterministic return boundary. Keep caller order; never infer semantics."""

from typing import Any

SUMMARY_LIMIT = 3000
ARRAY_LIMITS = {
    "files_changed": (100, 1024),
    "validation": (20, 500),
    "evidence": (30, 800),
    "unmet_criteria": (100, 1000),
}

OUTPUT_BUDGET_APPEND = """
No code/diff/log dumps, repeated tests or hidden reasoning. Within schema limits,
order: blockers/failures, unmet acceptance, key file:line/evidence, failed checks,
key passed checks, routine success. Preserve unmet criteria and useful paths.
"""


def _format(review: bool) -> dict:
    status = "verdict" if review else "status"
    fields = ("unmet_criteria", "evidence") if review else ("files_changed", "validation")
    properties = {
        status: {"type": "string", "enum": ["PASS", "FAIL"] if review else ["COMPLETED", "BLOCKED", "FAILED"]},
        "summary": {"type": "string", "maxLength": SUMMARY_LIMIT},
    }
    for field in fields:
        count, length = ARRAY_LIMITS[field]
        properties[field] = {"type": "array", "maxItems": count,
                             "items": {"type": "string", "maxLength": length}}
    return {"type": "json_schema", "schema": {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}}


EXEC_OUTPUT_FORMAT = _format(False)
REVIEW_OUTPUT_FORMAT = _format(True)


def compact_payload(payload: dict, kind: str) -> dict:
    """Cap known prose fields; preserve control fields and trusted metadata.

    omitted[field] counts removed array items; omitted[field + '_chars']
    counts removed characters in retained strings. Reapplying is idempotent.
    The caller supplies a typed envelope, never arbitrary model envelope keys.
    """
    if kind not in {"execution", "review"}:
        raise ValueError("invalid result kind")
    result = dict(payload)
    omitted = dict(result.get("omitted") or {})
    def cut(text, field, limit):
        if isinstance(text, str) and len(text) > limit:
            key = field + "_chars"
            omitted[key] = omitted.get(key, 0) + len(text) - limit
            return text[:limit]
        return text
    result["summary"] = cut(result.get("summary"), "summary", SUMMARY_LIMIT)
    fields = ("unmet_criteria", "evidence") if kind == "review" else ("files_changed", "validation")
    for field in fields:
        items = result.get(field)
        if not isinstance(items, list):
            continue
        count, length = ARRAY_LIMITS[field]
        if len(items) > count:
            omitted[field] = omitted.get(field, 0) + len(items) - count
        result[field] = [cut(item, field, length) for item in items[:count]]
    result["omitted"] = omitted
    result["output_truncated"] = bool(omitted) or bool(result.get("output_truncated"))
    return result


def compact_error(error: dict[str, Any]) -> dict:
    """Cap exception diagnostics as well as success output. No raw repr dumps."""
    omitted = dict(error.get("omitted") or {})
    def bound(value, path, depth=0):
        if isinstance(value, str):
            limit = 2000 if path == "message" else 500
            if len(value) > limit:
                omitted[path + "_chars"] = len(value) - limit
            return value[:limit]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= 2:
            omitted[path] = 1
            return "[omitted nested details]"
        if isinstance(value, dict):
            if len(value) > 12:
                omitted[path] = len(value) - 12
            # Fixed-size metadata keys too: arbitrary error detail keys can be huge.
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index == 12:
                    break
                key = str(key)
                if len(key) > 80:
                    omitted[path + ".keys_chars"] = omitted.get(path + ".keys_chars", 0) + len(key) - 80
                result[key[:80]] = bound(item, path + "." + key[:80], depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            if len(value) > 12:
                omitted[path] = len(value) - 12
            return [bound(item, path + f"[{i}]", depth + 1) for i, item in enumerate(value[:12])]
        omitted[path] = 1
        return "[unsupported detail]"
    result = {"code": error.get("code", "INTERNAL_ERROR"),
              "retryable": bool(error.get("retryable", False)),
              "message": bound(error.get("message", ""), "message"),
              "details": bound(error.get("details") or {}, "details")}
    result["omitted"] = omitted
    result["output_truncated"] = bool(omitted) or bool(error.get("output_truncated"))
    return result
