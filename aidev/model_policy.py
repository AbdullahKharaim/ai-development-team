"""Owner-approved Codex model / reasoning-effort selection.

The policy lives in live/live_config.json ("codex_policy"). Trusted code, not a
model, picks the selection:

- The task file carries the owner's complexity class (simple | normal | complex).
- The planner (Claude) may recommend a class. A recommendation can only RAISE the
  class; a lower recommendation is recorded and ignored, so a task the owner marked
  "normal" is never silently downgraded.
- The class maps to an effort via the policy; model and effort must be in the
  policy's allow-lists AND in the installed CLI's model catalog
  (`codex debug models`, captured by live/readiness.py). Anything else raises
  ModelPolicyError before Codex is invoked. There is no fallback model.

The Codex CLI does not validate `-m` or `model_reasoning_effort` itself (unknown
values parse fine), which is why this check exists.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .protocol import COMPLEXITIES

KNOWN_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


class ModelPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSelection:
    model: str
    effort: str
    complexity: str
    complexity_source: str        # "task" | "planner-raised"
    rationale: str
    policy_sha256: str

    def to_dict(self) -> dict:
        return asdict(self)


def load_policy(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"model", "effort_by_complexity", "allowed_models",
                                                 "allowed_efforts"}:
        raise ModelPolicyError("codex_policy needs model, effort_by_complexity, allowed_models, "
                               "allowed_efforts")
    ebc = raw["effort_by_complexity"]
    if not isinstance(ebc, dict) or set(ebc) != set(COMPLEXITIES):
        raise ModelPolicyError(f"effort_by_complexity must map exactly {COMPLEXITIES}")
    for e in list(ebc.values()) + list(raw["allowed_efforts"]):
        if e not in KNOWN_EFFORTS:
            raise ModelPolicyError(f"unknown reasoning effort {e!r}")
    if raw["model"] not in raw["allowed_models"]:
        raise ModelPolicyError("policy model is not in allowed_models")
    for c, e in ebc.items():
        if e not in raw["allowed_efforts"]:
            raise ModelPolicyError(f"effort {e!r} for {c!r} is not in allowed_efforts")
    if KNOWN_EFFORTS.index(ebc["simple"]) > KNOWN_EFFORTS.index(ebc["normal"]):
        raise ModelPolicyError("simple tasks must not get more effort than normal ones")
    if KNOWN_EFFORTS.index(ebc["normal"]) > KNOWN_EFFORTS.index(ebc["complex"]):
        raise ModelPolicyError("normal tasks must not get more effort than complex ones")
    return raw


def policy_sha256(policy: dict) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def select(policy: dict, task_complexity: str, recommended: str | None = None,
           recommendation_rationale: str = "") -> ModelSelection:
    if task_complexity not in COMPLEXITIES:
        raise ModelPolicyError(f"task complexity {task_complexity!r} invalid")
    complexity, source = task_complexity, "task"
    note = f"owner classified the task as {task_complexity!r}"
    if recommended is not None:
        if recommended not in COMPLEXITIES:
            raise ModelPolicyError(f"planner complexity {recommended!r} invalid")
        if COMPLEXITIES.index(recommended) > COMPLEXITIES.index(task_complexity):
            complexity, source = recommended, "planner-raised"
            note += f"; planner raised it to {recommended!r}: {recommendation_rationale.strip()[:300]}"
        elif recommended != task_complexity:
            note += (f"; planner suggested {recommended!r} (lower) - ignored, no automatic downgrade")
    effort = policy["effort_by_complexity"][complexity]
    return ModelSelection(policy["model"], effort, complexity, source,
                          f"{note}; policy maps {complexity!r} -> {effort!r}", policy_sha256(policy))


def validate(selection: ModelSelection, policy: dict, catalog: dict[str, list[str]]) -> None:
    """`catalog`: {model slug: [supported efforts]} from `codex debug models`."""
    if selection.model not in policy["allowed_models"]:
        raise ModelPolicyError(f"model {selection.model!r} is not approved by the owner policy")
    if selection.effort not in policy["allowed_efforts"]:
        raise ModelPolicyError(f"effort {selection.effort!r} is not approved by the owner policy")
    if selection.model not in catalog:
        raise ModelPolicyError(f"model {selection.model!r} is not in the installed Codex CLI catalog")
    if selection.effort not in catalog[selection.model]:
        raise ModelPolicyError(f"effort {selection.effort!r} is not supported by {selection.model!r} "
                               f"(catalog: {catalog[selection.model]})")
