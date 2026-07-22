from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ActionConfig, DispatcherConfig, HarnessConfig, ProjectConfig


@dataclass
class RoutingResult:
    harness: str
    model: str
    reasoning: str | None
    routing_source: str
    requested_selector: str
    caller_harness: str | None = None
    provider: str | None = None
    transport: str | None = None
    surface: str | None = None
    fallback_model: str | None = None
    fallback_reason: str | None = None


class RoutingError(ValueError):
    pass


def same_provider_fallback(result: RoutingResult, *, reason: str) -> RoutingResult:
    """Build an explicit redispatch route within the already chosen provider."""
    if not result.fallback_model:
        raise RoutingError(
            f"no same-provider fallback is declared for {result.provider or result.harness!r}; "
            "cross-provider fallback is never implicit"
        )
    return RoutingResult(
        harness=result.harness,
        model=result.fallback_model,
        reasoning=result.reasoning,
        routing_source="same-provider-fallback",
        requested_selector=result.requested_selector,
        caller_harness=result.caller_harness,
        provider=result.provider,
        transport=result.transport,
        surface=result.surface,
        fallback_model=None,
        fallback_reason=reason,
    )


def _load_policy(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"cannot load model routing policy {path}: {exc}") from exc
    if policy.get("schema_version") != 1:
        raise RoutingError(f"unsupported model routing policy schema in {path}")
    return policy


def _is_concrete(selector: object) -> bool:
    return bool(selector) and selector != "caller"


def _select_harness(
    action: dict,
    action_config: ActionConfig,
    project_config: ProjectConfig | None,
    config: DispatcherConfig,
) -> tuple[str, str, str]:
    """Return (harness, source, requested selector) in contract order."""
    action_selector = action.get("harness")
    class_selector = action_config.default_harness
    project_selector = project_config.default_harness if project_config else None
    global_selector = config.routing.default_harness

    if _is_concrete(action_selector):
        return str(action_selector), "action-explicit", str(action_selector)
    if _is_concrete(class_selector):
        return str(class_selector), "action-class-override", str(class_selector)
    if _is_concrete(project_selector):
        return str(project_selector), "project-default", str(project_selector)

    requested = next(
        (str(value) for value in (action_selector, class_selector, project_selector, global_selector) if value),
        "",
    )
    caller_requested = "caller" in (action_selector, class_selector, project_selector, global_selector)
    if caller_requested:
        caller = config.routing.trusted_caller_harness
        if caller:
            return caller, "caller-inheritance", requested or "caller"
        if len(config.harnesses) != 1:
            raise RoutingError("caller routing requires trusted_caller_harness when multiple harnesses are configured")

    # Backward-compatible runner mapping is deliberately below caller mode.
    runner_harness = _harness_from_runner(action_config)
    if runner_harness:
        return runner_harness, "runner-compatibility", requested or runner_harness
    if len(config.harnesses) == 1:
        return next(iter(config.harnesses)), "global-fallback", requested or "single-configured-harness"
    raise RoutingError(
        f"cannot resolve harness for action type {action_config.name!r}: "
        "set an explicit/action-class/project harness, configure trusted caller context, "
        "or configure exactly one harness"
    )


def _provider_for_harness(harness: str, config: DispatcherConfig, policy: dict[str, Any] | None) -> str | None:
    harness_cfg = config.harnesses.get(harness)
    if harness_cfg and harness_cfg.provider:
        return harness_cfg.provider
    if harness == config.routing.trusted_caller_harness and config.routing.caller_provider:
        return config.routing.caller_provider
    if policy:
        return policy.get("caller_harness_providers", {}).get(harness)
    return None


def _resolve_alias(
    selector: str,
    harness: str,
    config: DispatcherConfig,
    policy: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
    """Return model, provider, transport, surface, fallback, fallback reason."""
    if not policy or selector not in policy.get("aliases", {}):
        harness_cfg = config.harnesses.get(harness)
        return (
            selector,
            _provider_for_harness(harness, config, policy),
            harness_cfg.transport if harness_cfg else None,
            harness_cfg.surface if harness_cfg else None,
            None,
            None,
        )
    provider = _provider_for_harness(harness, config, policy)
    if not provider:
        raise RoutingError(f"harness {harness!r} requires an explicit provider mapping for alias {selector!r}")
    branch = policy["aliases"][selector].get(provider)
    if not branch:
        raise RoutingError(f"alias {selector!r} has no verified provider branch for {provider!r}")
    if not branch.get("verified"):
        raise RoutingError(f"alias {selector!r} provider branch {provider!r} is not verified")

    required_transport = branch.get("transport")
    allowed_surfaces = branch.get("surfaces") or []
    harness_cfg = config.harnesses.get(harness)
    actual_transport = (
        harness_cfg.transport
        if harness_cfg and harness_cfg.transport
        else config.routing.caller_transport
    )
    actual_surface = (
        harness_cfg.surface
        if harness_cfg and harness_cfg.surface
        else config.routing.caller_surface
    )
    incompatible = (
        (required_transport and actual_transport != required_transport)
        or (allowed_surfaces and actual_surface not in allowed_surfaces)
    )
    if incompatible:
        fallback = branch.get("fallback")
        if not fallback:
            raise RoutingError(
                f"alias {selector!r} is incompatible with transport={actual_transport!r} "
                f"surface={actual_surface!r} and has no same-provider fallback"
            )
        return fallback, provider, actual_transport, actual_surface, None, "transport-incompatible"
    return branch["model"], provider, required_transport or actual_transport, actual_surface, branch.get("fallback"), None


def resolve_routing(
    action: dict,
    action_config: ActionConfig,
    project_config: ProjectConfig | None,
    config: DispatcherConfig,
) -> RoutingResult:
    """Resolve trusted harness identity, logical model alias, and transport."""
    policy = _load_policy(config.routing.policy_path)
    harness, source, requested_selector = _select_harness(action, action_config, project_config, config)
    if harness == "caller":
        raise RoutingError("caller is a selector, not an executable harness")

    harness_cfg = config.harnesses.get(harness)
    explicit_model = action.get("model")
    if explicit_model:
        model_selector = str(explicit_model)
    elif source == "project-default" and project_config and project_config.default_model:
        model_selector = project_config.default_model
    elif source in {"action-explicit", "global-fallback"} and harness_cfg and harness_cfg.default_model:
        model_selector = harness_cfg.default_model
    else:
        model_selector = action_config.model or (harness_cfg.default_model if harness_cfg else "")
    if not model_selector:
        raise RoutingError(f"cannot resolve model selector for harness {harness!r}")

    model, provider, transport, surface, fallback_model, fallback_reason = _resolve_alias(
        model_selector, harness, config, policy
    )
    reasoning = action.get("reasoning")
    if not reasoning and source == "project-default" and project_config:
        reasoning = project_config.default_reasoning
    reasoning = reasoning or _reasoning_for_harness(harness, config.harnesses, action_config.reasoning)
    return RoutingResult(
        harness=harness,
        model=model,
        reasoning=reasoning,
        routing_source=source,
        requested_selector=model_selector,
        caller_harness=config.routing.trusted_caller_harness,
        provider=provider,
        transport=transport,
        surface=surface,
        fallback_model=fallback_model,
        fallback_reason=fallback_reason,
    )


def _harness_from_runner(action_config: ActionConfig) -> str | None:
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
