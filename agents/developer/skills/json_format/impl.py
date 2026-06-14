"""Example skill: validate and pretty-print a JSON string.

A minimal, deterministic skill (no LLM, no I/O) that demonstrates the contract.
Public, so other agents can adopt it (see src/skill_registry.py).
"""
import json

from src.skills import BaseSkill, SkillContext, SkillResult


class JsonFormatSkill(BaseSkill):
    name = "json_format"
    version = "0.1.0"
    description = "Validate and pretty-print a JSON string."
    params = {
        "text": "The JSON string to validate and format.",
        "indent": "Number of spaces to indent (default 2).",
    }

    def run(self, ctx: SkillContext, text: str = "", indent: int = 2, **_) -> SkillResult:
        try:
            obj = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(ok=False, output=f"invalid JSON: {exc}")
        pretty = json.dumps(obj, indent=int(indent), ensure_ascii=False, sort_keys=True)
        return SkillResult(ok=True, output=pretty, data={"keys": _shape(obj)})


def _shape(obj) -> object:
    if isinstance(obj, dict):
        return sorted(obj.keys())
    if isinstance(obj, list):
        return f"list[{len(obj)}]"
    return type(obj).__name__
