from __future__ import annotations

from dataclasses import dataclass

from .config import ActionConfig, DispatcherConfig, HarnessConfig, ProjectConfig


@dataclass
class RoutingResult:
    harness: str
    model: str
    reasoning: str | None
    routing_source: str


class RoutingError(ValueError):
    pass


def resolve_routing(
    action: dict,
    action_config: ActionConfig,
    project_config: ProjectConfig | None,
    config: DispatcherConfig,
) -> RoutingResult:
    """Resolve harness and model for an action using the priority chain.

    Priority:
    1. Action-explicit: harness/model fields on the action dict.
    2. Project default: default_harness/default_model on the project config.
    3. Action-kind default: default_harness on the action config, or runner compatibility mapping.
    4. Single global fallback: if exactly one harness is configured.

    Raises RoutingError if nothing resolves.
    """
    harnesses = config.harnesses

    # 1. Action explicit — harness/model fields on the action dict.
    action_harness = action.get("harness")
    if action_harness:
        model = action.get("model") or _model_for_harness(action_harness, harnesses, action_config.model)
        reasoning = action.get("reasoning") or _reasoning_for_harness(
            action_harness, harnesses, action_config.reasoning
        )
        return RoutingResult(
            harness=action_harness,
            model=model,
            reasoning=reasoning,
            routing_source="action-explicit",
        )

    # 2. Project default.
    if project_config is not None and project_config.default_harness:
        harness = project_config.default_harness
        model = project_config.default_model or _model_for_harness(harness, harnesses, action_config.model)
        reasoning = project_config.default_reasoning or _reasoning_for_harness(
            harness, harnesses, action_config.reasoning
        )
        return RoutingResult(
            harness=harness,
            model=model,
            reasoning=reasoning,
            routing_source="project-default",
        )

    # 3. Action-kind default: explicit default_harness or runner compatibility mapping.
    action_kind_harness = _harness_from_action_config(action_config)
    if action_kind_harness:
        return RoutingResult(
            harness=action_kind_harness,
            model=action_config.model,
            reasoning=action_config.reasoning,
            routing_source="action-kind-default",
        )

    # 4. Single global fallback — exactly one harness configured.
    if len(harnesses) == 1:
        harness_name, harness_cfg = next(iter(harnesses.items()))
        return RoutingResult(
            harness=harness_name,
            model=harness_cfg.default_model,
            reasoning=harness_cfg.default_reasoning,
            routing_source="global-fallback",
        )

    raise RoutingError(
        f"cannot resolve harness for action type {action_config.name!r}: "
        "set action explicit harness/model, configure project default_harness, "
        "or set action-kind default_harness"
    )


def _harness_from_action_config(action_config: ActionConfig) -> str | None:
    if action_config.default_harness:
        return action_config.default_harness
    if action_config.runner == "local":
        return "claude"
    if action_config.runner in ("fake", "fake-commit"):
        return "fake"
    return None


def _model_for_harness(
    harness: str,
    harnesses: dict[str, HarnessConfig],
    fallback: str,
) -> str:
    cfg = harnesses.get(harness)
    if cfg is not None and cfg.default_model:
        return cfg.default_model
    return fallback


def _reasoning_for_harness(
    harness: str,
    harnesses: dict[str, HarnessConfig],
    fallback: str | None,
) -> str | None:
    cfg = harnesses.get(harness)
    if cfg is not None and cfg.default_reasoning:
        return cfg.default_reasoning
    return fallback
