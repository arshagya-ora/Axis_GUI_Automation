"""Planner and navigator construction, plus the guarded browser tool set.

Two agents share one model instance. The planner has no tools; the navigator
gets a small explicit Python list of the browser tools it actually needs. The
guard in this module is what makes ``BrowserRuntime`` the executor for every
call: budgets, pause/cancel, approval, interruption, and the bounded
``ActionRecord`` all happen here, in front of the unmodified tool functions in
``browser_tools``.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import json
import re
import time
from threading import RLock
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from axis.login import LoginIntent

from pydantic_ai import Agent, InstrumentationSettings, ModelRetry, Tool, ToolFailed, ToolReturn
from pydantic_ai.capabilities import ToolSearch
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.models import Model

import browser_tools as bt
from browser_tools import BrowserRuntime

from .models import ActionRecord, AxisConfig, GoalCheck, NavigatorOutcome, PlanDecision, clip

# Pydantic AI emits model, tool, retry, and run spans through OpenTelemetry.
# Browser/page content and Oracle prompts are deliberately excluded; operators
# still get timings and error attributes without copying sensitive payloads.
Agent.instrument_all(InstrumentationSettings(include_content=False, include_binary_content=False))

# Ordinary navigator tool set. capture/diagnose are deliberately absent.
DEFAULT_TOOL_FUNCTIONS = (
    bt.browser_tabs,
    bt.browser_observe,
    bt.browser_act,
    bt.browser_navigate,
    bt.browser_wait,
    bt.browser_assert,
)
OPTIONAL_TOOL_FUNCTIONS = {
    "browser_capture_evidence": bt.browser_capture_evidence,
    "browser_diagnose": bt.browser_diagnose,
    "browser_downloads": bt.browser_downloads,
    "browser_visual": bt.browser_visual,
}

# Actions that change page state and therefore need later verification.
MUTATING_ACTIONS = frozenset({
    "click", "fill", "type_sequentially", "press", "select", "check", "uncheck",
    "upload", "drag", "type", "accept_dialog", "dismiss_dialog",
})
MUTATING_TOOLS = frozenset({"browser_act", "browser_navigate", "browser_visual"})
# Failures whose real effect on the page is unknown: never repeat these.
UNKNOWN_OUTCOME_CODES = frozenset({"TIMEOUT", "BRIDGE_UNAVAILABLE", "BRIDGE_ERROR"})
NON_RETRYABLE_CODES = frozenset({
    "POLICY_DENIED", "FIREWALL_DENIED", "SENSITIVE_OPERATION_BLOCKED", "UPLOAD_DENIED",
    "APPROVAL_DENIED", "TAB_LIMIT", "BRIDGE_PROTOCOL_MISMATCH",
})
STOP_CODES = frozenset({"RUN_PAUSED", "RUN_CANCELLED"})

# One small, explicit recovery table.  The orchestrator consumes the same
# codes; there is intentionally no workflow or exception framework here.
RECOVERY_POLICY = {
    "STALE_REFERENCE": "observe_and_retry",
    "STALE_SCREENSHOT": "observe_and_retry",
    "REF_NOT_FOUND": "observe_and_retry",
    "TAB_NOT_FOUND": "reconcile_tab",
    "TAB_CLOSED": "reconcile_tab",
    "INVALID_ARGUMENT": "model_retry",
    "ASSERTION_FAILED": "inspect_result",
    "FRAME_TARGET_AMBIGUOUS": "scope_frame",
    "FRAME_GEOMETRY_UNAVAILABLE": "inspect_frame",
    "APPROVAL_DENIED": "replan",
    "POLICY_DENIED": "stop_retry",
    "FIREWALL_DENIED": "stop_retry",
    "BRIDGE_UNAVAILABLE": "transport_retry",
}
LOCAL_RECOVERY_CODES = frozenset({
    'TARGET_NOT_FOUND', 'TARGET_AMBIGUOUS', 'TARGET_DISABLED', 'TARGET_EVIDENCE_UNAVAILABLE',
    'OUTCOME_UNVERIFIED', 'OBSERVATION_REQUIRED', 'STALE_REFERENCE', 'REF_NOT_FOUND', 'STALE_SCREENSHOT',
    'LOCATOR_STRICT_MODE_VIOLATION', 'LOCATOR_REF_NOT_ACTIONABLE',
    'UNSUPPORTED_ELEMENT_PROPERTY',
})
MODEL_RETRY_CODES = frozenset({"INVALID_ARGUMENT", "STALE_REFERENCE", "REF_NOT_FOUND"})
TOOL_FAILED_CODES = frozenset({
    "POLICY_DENIED", "FIREWALL_DENIED", "SENSITIVE_OPERATION_BLOCKED",
    "CAPABILITY_UNAVAILABLE", "TAB_LIMIT", "UPLOAD_DENIED", "BRIDGE_ERROR",
})


def browser_context(runtime: BrowserRuntime) -> Any:
    """A minimal stand-in for ``RunContext`` so the orchestrator can call the
    same tool functions the model calls. They read only ``ctx.deps``."""
    return SimpleNamespace(deps=runtime)


@dataclass
class AxisDeps:
    """What both the guard and the browser tools need. ``browser`` is passed
    straight through to the unmodified tool functions."""

    browser: BrowserRuntime
    gate: "StepGate"
    documents: Any = None


def _command_operation(kwargs: dict[str, Any]) -> str | None:
    command = kwargs.get("command")
    for attribute in ("action", "operation", "condition", "assertion", "capture", "diagnostic"):
        value = getattr(command, attribute, None)
        if isinstance(value, str):
            return value
    mode = kwargs.get("mode")
    return mode if isinstance(mode, str) else None


def _extracted(tool: str, data: dict[str, Any]) -> str | None:
    """The part of a tool result worth carrying in bounded task memory."""
    if tool == "browser_observe":
        if "result" in data:
            return json.dumps(data["result"], ensure_ascii=False, sort_keys=True, default=str)
        return data.get("snapshot")
    if tool == "browser_assert":
        return f"assert {data.get('assertion')} passed={data.get('passed')} expected={data.get('expected')!r}"
    if tool == "browser_tabs":
        tabs = data.get("tabs")
        if not isinstance(tabs, list):
            return None
        # A bare count gave the planner nothing to answer a "list the tabs"
        # style task from, so it kept re-browsing forever hoping for more
        # detail that never came (the raw tool result never reaches it by
        # design). List operations carry the titles/URLs; single-tab
        # operations (create/activate/close) just confirm the one tab.
        if len(tabs) > 1:
            lines = "; ".join(f"{t.get('tab')}: {t.get('title')!r} — {t.get('url')}" for t in tabs)
            return f"{len(tabs)} open tabs: {lines}"
        return f"{len(tabs)} open tabs" if tabs is not None else None
    if tool == "browser_capture_evidence":
        return f"evidence {data.get('evidence')} ({data.get('capture')})"
    if tool == "browser_diagnose":
        return data.get("summary") or f"{data.get('diagnostic')}: {data.get('count')} events"
    if tool == "browser_downloads":
        items = data.get("items")
        if isinstance(items, list):
            return f"{len(items)} downloads: " + "; ".join(str(item.get("filename")) for item in items[:8])
        download = data.get("download")
        return f"download {download.get('state')}: {download.get('filename')}" if isinstance(download, dict) else None
    if tool == "browser_navigate":
        return f"navigated to {data.get('url')!r} ({data.get('title')!r})"
    return None


def _describe_args(kwargs: dict[str, Any]) -> str | None:
    """A compact, safe rendering of one call's arguments for debug tracing.
    Everything here is already alias-safe (tab aliases, refs, URLs, keys) —
    the same values the model itself sent — never a raw browser identifier."""
    parts: list[str] = []
    for key, value in kwargs.items():
        if hasattr(value, "model_dump"):
            value = value.model_dump(exclude_none=True, exclude_defaults=True)
        parts.append(f"{key}={value!r}")
    return ", ".join(parts) if parts else None


def _refusal(tab: Any, code: str, message: str) -> dict[str, Any]:
    """Same envelope shape ``browser_tools`` returns, so a refusal reads to the
    model exactly like any other failed call."""
    return {
        "ok": False,
        "tab": tab if isinstance(tab, str) else None,
        "data": None,
        "error": {"code": code, "message": message, "retryable": False, "detail": None},
    }


def _observed_effect_target(runtime, tab, locator):
    """Bind locator approvals to browser-observed identity, not model labels."""
    result = runtime._rpc('locator.count', {'tabId': runtime.tab(tab).raw_id,
                          **bt._locator_params(locator)}, tab=tab)
    elements = result.get('elements') or []
    count = result.get('count')
    keys = ('role', 'accessibleName', 'text', 'disabled', 'context', 'tagName')
    candidates = [{key: element[key] for key in keys if key in element} for element in elements[:5]]
    detail = {'count': count, 'candidates': candidates}
    if count == 0:
        raise bt.ToolFault('TARGET_NOT_FOUND', 'No target matched. Inspect the relevant region or correct the locator.', True, detail)
    if isinstance(count, int) and count > 1:
        raise bt.ToolFault('TARGET_AMBIGUOUS', 'Multiple targets matched. Scope the locator using the candidate context.', True, detail)
    if count != 1 or len(elements) != 1:
        raise bt.ToolFault('TARGET_EVIDENCE_UNAVAILABLE', 'Target count and identity evidence disagree. Observe again.', True, detail)
    element = elements[0]
    if element.get('disabled') is True:
        raise bt.ToolFault('TARGET_DISABLED', 'The matched control is disabled. Inspect its prerequisites or wait for it to become enabled.', True, detail)
    keys = ('tagName', 'id', 'name', 'role', 'type', 'text', 'ariaLabel', 'placeholder',
            'accessibleName', 'href', 'formAction', 'formMethod', 'disabled', 'editable')
    return {key: element[key] for key in keys if key in element}


@dataclass
class StepGate:
    """Per-navigator-step execution gate. One instance per step.

    Sequencing is Pydantic AI's (`Tool(..., sequential=True)`); this decides
    whether each call in that sequence is allowed to reach the bridge at all.
    """

    max_actions: int
    remaining_total_actions: int
    approval_actions: frozenset[str] = frozenset()
    login_intent: LoginIntent | None = None
    approval: Callable[[str, str | None, dict[str, Any]], bool] | None = None
    effect_authorizer: Callable[[str, str | None, dict[str, Any]], bool] | None = None
    forbidden_actions: frozenset[str] = frozenset()
    mutation_allowed: bool = True
    uncertain_tabs: frozenset[str] = frozenset()
    is_paused: Callable[[], bool] = lambda: False
    is_cancelled: Callable[[], bool] = lambda: False
    on_action: Callable[[ActionRecord, int], None] | None = None
    on_record: Callable[[ActionRecord, bool], None] | None = None
    bind_verification: Callable[[str, Any, str], None] | None = None
    on_dispatch: Callable[[], None] | None = None
    # Called for a call the gate refused before it ever reached the bridge
    # (budget, interruption, pause/cancel, denied approval) — never stored in
    # bounded TaskMemory, but essential for tracing a step's root cause.
    on_refusal: Callable[[str, str | None, str, str, str | None], None] | None = None

    refresh_tabs: set[str] = field(default_factory=set)
    recovery_attempts: int = 0
    actions_used: int = 0
    interrupted: bool = False
    interrupt_reason: str | None = None
    unknown_outcome: bool = False
    non_retryable: bool = False
    stopped: str | None = None
    # Total time this step spent inside the bridge, for run-level attribution.
    browser_ms: int = 0
    # Set when a call changed which tab is active, so the orchestrator can
    # follow it instead of observing the tab the navigator just left.
    active_tab_changed_to: str | None = None
    records: list[ActionRecord] = field(default_factory=list)
    # (mutated, verified) per record, in execution order, so the orchestrator
    # can tell whether a mutation was followed by evidence.
    effects: list[tuple[bool, bool]] = field(default_factory=list)
    refusals: int = 0
    committed: bool = False
    lock: Any = field(default_factory=RLock, repr=False)
    in_flight: tuple[str, dict[str, Any], float] | None = None
    action_postcondition: Any = None
    authorized_origins: dict[str, list[str]] | None = None
    tool_ms: int = 0
    authorization_ms: int = 0
    preflight_ms: int = 0
    assertion_ms: int = 0
    timing_depth: int = 0

    def _interrupt(self, reason: str) -> None:
        if not self.interrupted:
            self.interrupted = True
            self.interrupt_reason = reason

    def _safe_args(self, kwargs: dict[str, Any]) -> str | None:
        return _describe_args(kwargs)

    def reject(self, tool, kwargs, code, message, detail=None):
        """Record pre-dispatch failure without manufacturing a page mutation."""
        operation = _command_operation(kwargs)
        tab = kwargs.get('tab') or getattr(kwargs.get('command'), 'tab', None)
        args = self._safe_args(kwargs)
        self.records.append(ActionRecord(tool=tool, tab=tab, operation=operation, args=args,
            executed=False, execution_success=False, semantic_success=False,
            code=code, message=message, retain_in_memory=True))
        self.effects.append((False, False))
        self.refusals += 1
        if code in LOCAL_RECOVERY_CODES and self.recovery_attempts < 3:
            self.recovery_attempts += 1
            if tab:
                self.refresh_tabs.add(tab)
        else:
            self._interrupt(message)
        if self.on_refusal is not None:
            self.on_refusal(tool, operation, code, message, args)
        result = _refusal(tab, code, message)
        result['error']['detail'] = detail
        result['error']['retryable'] = code in LOCAL_RECOVERY_CODES and not self.interrupted
        return result

    def check(
        self, tool: str, kwargs: dict[str, Any], approval_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return a refusal envelope if this call must not reach the bridge."""
        tab = kwargs.get("tab")
        operation = _command_operation(kwargs)
        args = self._safe_args(kwargs)

        def refuse(code: str, message: str) -> dict[str, Any]:
            return self.reject(tool, kwargs, code, message)

        if self.is_cancelled():
            self.stopped = "cancelled"
            self._interrupt("the run was cancelled")
            return refuse("RUN_CANCELLED", "The run was cancelled; stop and report.")
        if self.is_paused():
            self.stopped = "paused"
            self._interrupt("the run was paused")
            return refuse("RUN_PAUSED", "The run is paused; stop and report.")
        if self.interrupted:
            return refuse(
                "STEP_INTERRUPTED",
                f"Remaining actions were cancelled because {self.interrupt_reason}. "
                "Report what happened; a fresh observation follows.",
            )
        if self.actions_used >= self.max_actions:
            self._interrupt("the per-step action budget was reached")
            return refuse(
                "ACTION_BUDGET_EXHAUSTED",
                f"This step allows at most {self.max_actions} browser actions. Report now.",
            )
        if self.actions_used >= self.remaining_total_actions:
            self._interrupt("the total browser action budget was reached")
            return refuse("ACTION_BUDGET_EXHAUSTED", "The run's browser action budget is exhausted.")
        mutating = tool in MUTATING_TOOLS and (tool == "browser_navigate" or operation in MUTATING_ACTIONS)
        if mutating and (tab in self.uncertain_tabs or self.unknown_outcome):
            return refuse("OUTCOME_UNVERIFIED", "Inspect and verify the previous outcome before another change; do not replay the submission.")
        if mutating and tab in self.refresh_tabs:
            return refuse('OBSERVATION_REQUIRED', 'Observe this tab after the page change or targeting failure before another action.')
        target_text = str((approval_detail or {}).get("target") or "").lower()
        patterns = {
            "download": r"\bdownload\b|source code.*(?:zip|tar)|\.(?:zip|tar|gz|exe|msi|csv|xlsx)(?:\b|$)|/archive/refs/",
            "login": r"\blog[ -]?in\b|\bsign[ -]?in\b",
            "submit": r"\bsubmit\b", "purchase": r"\bpurchase\b|\bbuy\b|\bcheckout\b",
            "modify": r"\b(?:edit|commit|merge|save changes)\b",
        }
        effect_target = tool in MUTATING_TOOLS or (tool == "browser_tabs" and operation == "create")
        forbidden = next(
            (item for item in self.forbidden_actions if item == operation or
             (effect_target and re.search(patterns.get(item, r"\b" + re.escape(item) + r"\b"), target_text))),
            None,
        )
        if "download" in self.forbidden_actions and tool == "browser_downloads":
            forbidden = "download"
        if self.forbidden_actions and tool == "browser_visual" and operation in {"click", "drag", "type"}:
            forbidden = "coordinate interaction without a policy-checkable DOM target"
        if forbidden:
            self.non_retryable = True
            self._interrupt(f"the task forbids {forbidden!r}")
            return refuse("POLICY_DENIED", f"The current task explicitly forbids {forbidden!r}.")
        if mutating and not self.mutation_allowed and not (tool == 'browser_navigate' or (operation == 'click' and re.match(r'^link\b', target_text) and not re.search(r'logout|log out|delete|purchase|checkout|submit|download', target_text))):
            self.non_retryable = True
            self._interrupt("the task is read-only")
            return refuse("POLICY_DENIED", "The current task does not allow page mutations.")
        if self.effect_authorizer is not None:
            authorization_started = time.perf_counter()
            try:
                authorized = self.effect_authorizer(tool, operation, {**(approval_detail or {}),
                    'mandatory_approval': operation in self.approval_actions,
                    'read_only': not self.mutation_allowed,
                    'arguments': {key: value.model_dump(mode='json') if hasattr(value, 'model_dump') else value for key, value in kwargs.items()}})
            finally:
                self.authorization_ms += int((time.perf_counter() - authorization_started) * 1000)
            if not authorized:
                self.stopped = 'paused'
                self._interrupt('the action requires authorization')
                return refuse('APPROVAL_DENIED', 'Action not authorized; the task is paused.')
            if self.is_cancelled() or self.is_paused():
                self.stopped = 'cancelled' if self.is_cancelled() else 'paused'
                return refuse('RUN_PAUSED', 'The task changed while approval was pending.')
        if operation in self.approval_actions and self.effect_authorizer is None:
            approved = False
            if self.approval is not None:
                approved = bool(self.approval(tool, operation, approval_detail or {"tab": tab}))
            if not approved:
                self._interrupt("approval was denied")
                self.non_retryable = True
                return refuse("APPROVAL_DENIED", f"The user did not approve {operation!r}.")
        return None

    def record(
        self, tool: str, kwargs: dict[str, Any], result: dict[str, Any], duration_ms: int = 0,
    ) -> ActionRecord:
        with self.lock:
            if self.committed:
                # Cancellation already preserved this call as outcome-unknown.
                # Its worker must not append a second record after the run ends.
                return self.records[-1]
            self.in_flight = None
            return self._record(tool, kwargs, result, duration_ms)

    def _record(
        self, tool: str, kwargs: dict[str, Any], result: dict[str, Any], duration_ms: int = 0,
    ) -> ActionRecord:
        """Turn one executed call into a bounded record and decide whether the
        rest of this step must be interrupted."""
        operation = _command_operation(kwargs)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        execution_success = bool(result.get("ok"))
        semantic_success = None
        if tool == "browser_assert" and execution_success:
            semantic_success = bool(data.get("passed"))
        success = execution_success and semantic_success is not False
        code = error.get("code") or ("ASSERTION_FAILED" if semantic_success is False else None)
        changed = data.get("whatChanged") if isinstance(data.get("whatChanged"), dict) else {}
        refs_invalidated = bool(data.get("refsInvalidated")) or bool(changed.get("urlChanged"))
        rerendered = bool(changed.get("domChanged")) or bool(changed.get("navigated"))
        mutating = tool in MUTATING_TOOLS and (tool == "browser_navigate" or operation in MUTATING_ACTIONS)
        meaningful_change = True if any(
            bool(changed.get(key))
            for key in ("urlChanged", "domChanged", "navigated", "focusChanged", "newPopups", "a11yDiff")
        ) else None
        mutated = mutating and (success or code in UNKNOWN_OUTCOME_CODES)
        verified = success and tool == "browser_assert" and bool(data.get("passed"))
        if mutated and isinstance(kwargs.get("tab"), str):
            # Delayed navigation may have no immediate change metadata. Observe
            # the action's tab next, even when another tab was previously active.
            self.active_tab_changed_to = kwargs["tab"]

        if success and tool == 'browser_observe' and 'snapshot' in data:
            self.refresh_tabs.discard(kwargs.get('tab'))
        if not success:
            if code in STOP_CODES:
                self.stopped = "cancelled" if code == "RUN_CANCELLED" else "paused"
            if code in LOCAL_RECOVERY_CODES and self.recovery_attempts < 3:
                self.recovery_attempts += 1
                if kwargs.get('tab'):
                    self.refresh_tabs.add(kwargs['tab'])
            elif code in LOCAL_RECOVERY_CODES:
                self._interrupt('the local recovery budget was reached')
            elif code in MODEL_RETRY_CODES:
                # Pydantic AI may issue a corrected call in this same model
                # turn; do not make the gate reject that correction.
                pass
            elif mutating and code in UNKNOWN_OUTCOME_CODES:
                self.unknown_outcome = True
                # Keep reads available, but prohibit further mutations this step.
            elif code in NON_RETRYABLE_CODES:
                self.non_retryable = True
                self._interrupt(f"a non-retryable failure occurred ({code})")
            else:
                self._interrupt(f"an action failed ({code})")
        elif refs_invalidated:
            if isinstance(kwargs.get("tab"), str):
                self.active_tab_changed_to = kwargs["tab"]
            self.refresh_tabs.add(kwargs.get('tab'))
        elif rerendered:
            self.refresh_tabs.add(kwargs.get('tab'))
        elif tool == "browser_act" and isinstance(data.get("openedTabs"), list) and data["openedTabs"]:
            candidates = [alias for alias in data["openedTabs"] if isinstance(alias, str)]
            self.active_tab_changed_to = candidates[-1] if candidates else None
            self._interrupt("the action opened a popup tab")
        elif tool == "browser_wait" and isinstance(data.get("openedTab"), str):
            self.active_tab_changed_to = data["openedTab"]
            self._interrupt("the popup tab is now the active browser context")
        elif tool == "browser_tabs" and operation in {"activate", "close", "create"}:
            # Record which tab is now active so the orchestrator's next
            # automatic observation follows the navigator instead of the tab
            # it left.
            if operation in {"activate", "create"} and success:
                new_tab = result.get("tab") or data.get("tab")
                if isinstance(new_tab, str):
                    self.active_tab_changed_to = new_tab
            # Creating or activating a tab does NOT end the step. Every tool
            # takes an explicit tab alias and BrowserRuntime.resolve_ref keys
            # refs per tab, so a queued action aimed at another tab is either
            # deliberate or safely rejected — it can never silently land on
            # the wrong page. Interrupting here instead cost a whole model
            # round trip per tab, turning "open three tabs and read them"
            # into thirteen steps of create/activate ping-pong.
            # Closing still interrupts: it can destroy the very tab the
            # remaining actions target.
            if operation == "close":
                self._interrupt("a tab was closed")

        record = ActionRecord(
            tool=tool,
            tab=kwargs.get("tab") if isinstance(kwargs.get("tab"), str) else
                result.get("tab") if tool == "browser_tabs" else None,
            operation=operation,
            upload_files=list(getattr(kwargs.get('command'), 'files', [])) if operation == 'upload' else [],
            args=self._safe_args(kwargs),
            executed=True,
            execution_success=execution_success,
            semantic_success=semantic_success,
            code=None if success else (code or ("ASSERTION_FAILED" if semantic_success is False else None)),
            message=None if success else (
                error.get("message") or ("The assertion did not match the current page." if semantic_success is False else None)
            ),
            extracted_data=_extracted(tool, data) if success else None,
            duration_ms=duration_ms,
            refs_invalidated=refs_invalidated,
            meaningful_change=meaningful_change,
            result_data=data,
            retain_in_memory=(
                (not success) or mutating or tool in {"browser_assert", "browser_capture_evidence"}
                # A tabs.list result is the answer to "what tabs are open" — if it
                # only lives in this step's transient NavigatorOutcome, the
                # planner loses it by the next pass and can never accumulate
                # enough to call the task complete.
                or (tool == "browser_tabs" and operation == "list")
                or tool in {"browser_downloads", "browser_observe"}
            ),
        )
        command = kwargs.get("command")
        expected_value = getattr(command, "value", None)
        if isinstance(command, bt.Check):
            expected_value = command.action == "check"
        elif isinstance(command, bt.Select) and len(command.options) == 1:
            selected = (data.get("result") or {}).get("selected") or []
            expected_value = selected[0].get("value") if len(selected) == 1 else command.options[0]
        record.expected_value = expected_value
        record.key = getattr(command, "key", None)
        locator = getattr(command, "locator", None) or kwargs.get("locator")
        if locator is not None:
            identity = {"locator": locator.model_dump(exclude_none=True),
                        "index": getattr(command, "index", kwargs.get("index", 0)),
                        "attribute": getattr(command, "attribute", kwargs.get("attribute"))}
            if tool == "browser_observe":
                identity["extract"] = kwargs.get("extract") or "all_inner_text"
            record.target = json.dumps(identity, sort_keys=True, default=str)
        else:
            target = getattr(command, "target", None)
            record.target = target.model_dump_json(exclude_none=True) if target is not None else operation
        if tool == "browser_act" and self.action_postcondition is not None:
            check = self.action_postcondition
            locator = getattr(check, "locator", None)
            target = next((getattr(locator, key, None) for key in ("name", "label", "text", "placeholder", "selector", "role") if getattr(locator, key, None)), None)
            if check.assertion in {"text", "value", "checked", "title", "url", "visible", "hidden", "enabled", "disabled", "count"}:
                record.postcondition_locator = locator.model_dump(exclude_none=True) if locator else None
                record.postcondition = GoalCheck(assertion=check.assertion, expected=getattr(check, "expected", None), target=target)
        if self.on_record is not None:
            self.on_record(record, mutated)
            if record.evidence_id and isinstance(result.get("data"), dict):
                result["data"]["evidence_id"] = record.evidence_id
        self.records.append(record)
        self.effects.append((mutated, verified))
        if record.success:
            for earlier in self.records[:-1]:
                if (earlier.code in (LOCAL_RECOVERY_CODES - {'OUTCOME_UNVERIFIED'}) | {'INVALID_ARGUMENT'}
                        and earlier.tool == record.tool
                        and (earlier.code == 'INVALID_ARGUMENT' or earlier.operation == record.operation)
                        and earlier.tab == record.tab):
                    earlier.recovered = True
                if (earlier.code == 'OBSERVATION_REQUIRED' and earlier.tab == record.tab
                        and tool == 'browser_observe' and 'snapshot' in data):
                    earlier.recovered = True
        self.actions_used += 1
        self.browser_ms += duration_ms
        if tool == "browser_assert":
            self.assertion_ms += duration_ms
        if self.on_action is not None:
            self.on_action(record, duration_ms)
        if tool == "browser_visual" and operation != "capture" and success:
            self._interrupt("the visual action consumed its screenshot; observe before another interaction")
        return record


def guarded(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap one ``browser_tools`` function so every call goes through the gate.

    ``functools.wraps`` keeps the name, docstring, and signature, so Pydantic
    generates exactly the same tool schema it would for the bare function.
    """

    @functools.wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ctx = args[0] if args else kwargs.pop("ctx")
        rest = args[1:]
        deps: AxisDeps = ctx.deps
        if function is bt.browser_act:
            command = kwargs.get("command")
            postcondition = kwargs.pop("postcondition", None)
            if isinstance(command, bt.ActionBatch):
                results = []
                for step in command.steps:
                    if deps.gate.interrupted:
                        break
                    result = wrapper(ctx, *rest, **{**kwargs, "command": step.command, "postcondition": step.postcondition})
                    results.append(result)
                    if not result.get("ok"):
                        break
                complete = len(results) == len(command.steps) and all(result.get("ok") for result in results)
                return {"ok": complete, "tab": kwargs.get("tab", rest[0] if rest else None),
                        "data": {"results": results, "completed": len(results), "stopped": deps.gate.interrupt_reason},
                        "error": None if complete else {"code": "BATCH_STOPPED", "message": deps.gate.interrupt_reason or "An action or postcondition failed."}}
            if postcondition is not None:
                # Never commit a change when its paired check cannot fit.
                available = min(deps.gate.max_actions, deps.gate.remaining_total_actions) - deps.gate.actions_used
                if available < 2:
                    deps.gate._interrupt('the next complete action and check require two remaining calls')
                    return _refusal(kwargs.get('tab'), 'ACTION_BUDGET_EXHAUSTED',
                                    'Yield before this operation; neither the action nor its check executed.')
                if isinstance(command, bt.Fill) and postcondition.assertion == "value" and postcondition.expected != command.value:
                    raise ModelRetry("A fill postcondition must verify the requested value.")
                if isinstance(command, bt.Check) and postcondition.assertion == "checked" and postcondition.expected != (command.action == "check"):
                    raise ModelRetry("The postcondition must match the requested checked state.")
                action_locator = getattr(getattr(command, "target", None), "locator", None) or getattr(command, "locator", None)
                if postcondition.assertion in {"value", "checked"} and action_locator is not None and action_locator != postcondition.locator:
                    raise ModelRetry("Use the same scoped locator for the action and its field postcondition.")
                deps.gate.action_postcondition = postcondition.as_assertion()
                try:
                    result = wrapper(ctx, *rest, **kwargs)
                finally:
                    deps.gate.action_postcondition = None
                if not result.get("ok") or deps.gate.interrupted:
                    return {**result, "data": {**(result.get("data") or {}), "postconditionSkipped": True}}
                check = guarded(bt.browser_assert)(ctx, tab=kwargs.get("tab", rest[0] if rest else None), command=postcondition.as_assertion())
                passed = bool(check.get("ok") and (check.get("data") or {}).get("passed"))
                return {**result, "ok": passed, "data": {**(result.get("data") or {}), "postcondition": check},
                        "error": None if passed else check.get("error") or {"code": "ASSERTION_FAILED", "message": "Postcondition failed."}}
        preflight_started = time.perf_counter()
        previous_authorization_ms = deps.gate.authorization_ms
        named = dict(kwargs)
        # `tab` is the only positional the tools take after ctx.
        if rest:
            named.setdefault("tab", rest[0])
        command = named.get("command")
        tab = named.get("tab") or getattr(command, "tab", None)
        if (function.__name__ == 'browser_assert' and deps.gate.login_intent
                and isinstance(command, bt.StateAssertion) and command.assertion == 'hidden'
                and command.timeout_ms > 3_000):
            command = command.model_copy(update={'timeout_ms': 3_000})
            kwargs['command'] = command
            named['command'] = command
        if isinstance(command, bt.Upload) and deps.documents:
            try:
                resolved = deps.documents.resolve_uploads(command.files)
            except (ValueError, OSError) as error:
                raise ModelRetry(str(error)) from error
            command = command.model_copy(update={'files': resolved})
            kwargs['command'] = command
            # Keep attachment IDs in model-visible action history. Only the
            # browser executor receives the resolved filesystem paths.
        if (function.__name__ == 'browser_assert' and isinstance(command, bt.StateAssertion)
                and command.assertion == 'visible' and command.locator.role and command.locator.name):
            observation = deps.browser.observations.get(tab)
            matches = []
            if observation:
                for ref in observation.refs:
                    description = deps.browser.describe_ref(ref) or ''
                    match = re.match(r'^([\w-]+)\s+"([^"]+)"', description)
                    if match and match.group(2).casefold() == command.locator.name.casefold():
                        matches.append(match.group(1))
            if len(matches) == 1 and matches[0] != command.locator.role:
                raise ModelRetry('The fresh snapshot identifies this name with role ' + matches[0]
                                 + '. Use its observed semantic role instead of guessing from appearance.')
        target = getattr(command, "target", None)
        target_type = getattr(target, "kind", None)
        target_text = getattr(command, "url", None)
        if target_type == "ref":
            target_text = deps.browser.describe_ref(getattr(target, "ref", ""))
        elif target_type == "locator":
            locator = getattr(target, "locator", None)
            if locator is not None:
                target_text = " ".join(
                    value for value in (
                        locator.name, locator.label, locator.text, locator.placeholder, locator.selector,
                    ) if value
                )
        elif getattr(command, "locator", None) is not None:
            locator = command.locator
            target_type = "locator"
            target_text = next(
                (value for value in (locator.name, locator.label, locator.text, locator.placeholder, locator.selector) if value),
                None,
            )
        if isinstance(command, bt.Press) and not target_text:
            previous = next((r for r in reversed(deps.gate.records) if r.tab == tab and r.success and r.operation == 'fill'), None)
            if previous:
                target_text, target_type = previous.target, 'ref'
        current_url = deps.browser.tabs[tab].url if tab in deps.browser.tabs else None
        detail = {
            "tab": tab,
            "operation": _command_operation(named),
            "current_url": deps.browser.safe_url(current_url),
            "origin": current_url.split("/", 3)[:3] if isinstance(current_url, str) else None,
            "target": target_text,
            "target_type": target_type,
            "upload_filenames": getattr(named.get('command'), "files", None),
            "expected_effect": _command_operation(named),
            "reason": "This action is configured to require user approval.",
        }
        if function.__name__ == 'browser_visual' and _command_operation(named) != 'capture':
            screenshot = deps.browser._visual_screenshot
            if screenshot and screenshot.tab == tab and screenshot.alias == getattr(command, 'screenshot', None):
                detail['target_type'] = 'visual'
                detail['target'] = 'current screenshot interaction'
        if deps.gate.authorized_origins is not None:
            detail["authorized_origins"] = deps.gate.authorized_origins.get(tab, [])
        if isinstance(detail["origin"], list):
            detail["origin"] = "/".join(detail["origin"])
        effect_locator = None
        if deps.gate.effect_authorizer and target_type == 'locator' and function.__name__ == 'browser_act' and _command_operation(named) not in {'hover', 'scroll'}:
            effect_locator = getattr(target, 'locator', None) or getattr(command, 'locator', None)
            try:
                detail['observed_target'] = _observed_effect_target(deps.browser, tab, effect_locator)
            except (ToolFailed, bt.ToolFault) as error:
                code = getattr(error, 'code', 'LOCATOR_STRICT_MODE_VIOLATION')
                return deps.gate.reject(function.__name__, named, code, str(error), getattr(error, 'detail', None))
            observed = detail['observed_target']
            detail['target'] = ' '.join(str(observed.get(key) or '') for key in ('role', 'accessibleName', 'ariaLabel', 'text', 'name', 'type')).strip()
        refusal = deps.gate.check(function.__name__, named, detail)
        if refusal is not None:
            error = refusal.get("error") if isinstance(refusal.get("error"), dict) else {}
            if error.get("code") in TOOL_FAILED_CODES:
                raise ToolFailed(error.get("message") or "This browser operation is not retryable.")
            return refusal
        if function is bt.browser_assert and named.get("goal_target"):
            if deps.gate.bind_verification is None:
                raise ModelRetry("No active planner verification is available to bind.")
            try:
                deps.gate.bind_verification(tab, command, named["goal_target"])
            except ValueError as error:
                raise ModelRetry(str(error)) from error
        if deps.gate.effect_authorizer is not None and tab and function.__name__ in {'browser_act', 'browser_visual'} and _command_operation(named) not in {'capture', 'scroll', 'hover'}:
            deps.browser.reconcile_tabs()
            try:
                live_url = deps.browser.tab(tab).url
            except bt.ToolFault as error:
                return deps.gate.reject(function.__name__, named, error.code, str(error))
            if live_url != current_url:
                deps.gate._interrupt('the destination changed while authorizing the action')
                return deps.gate.reject(function.__name__, named, 'STALE_STATE', 'The destination changed. Observe the current page before continuing.')
            if effect_locator:
                try:
                    current_target = _observed_effect_target(deps.browser, tab, effect_locator)
                except bt.ToolFault as error:
                    return deps.gate.reject(function.__name__, named, error.code, str(error), error.detail)
                if current_target != detail['observed_target']:
                    deps.gate._interrupt('the observed target changed while authorizing the action')
                    return deps.gate.reject(function.__name__, named, 'STALE_STATE', 'The target changed. Observe the page before continuing.')
            if isinstance(command, bt.Upload) and deps.documents:
                try:
                    deps.documents.resolve_uploads(named['command'].files)
                except ValueError as error:
                    deps.gate._interrupt(str(error))
                    return deps.gate.reject(function.__name__, named, 'UPLOAD_DENIED', str(error))
        deps.gate.preflight_ms += max(0, int((time.perf_counter() - preflight_started) * 1000)
                                      - (deps.gate.authorization_ms - previous_authorization_ms))
        browser_ctx = dataclasses.replace(ctx, deps=deps.browser)
        started = time.perf_counter()
        with deps.gate.lock:
            if deps.gate.committed:
                return _refusal(tab, "RUN_CANCELLED", "The step ended before this call could execute.")
            deps.gate.in_flight = (function.__name__, named, started)
        if deps.gate.on_dispatch is not None:
            deps.gate.on_dispatch()
        result = function(browser_ctx, *rest, **kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        record = deps.gate.record(function.__name__, named, result, duration_ms)
        if function is bt.browser_visual and record.success and record.operation == "capture":
            return ToolReturn(return_value=result, content=[deps.browser.visual_content(result["data"]["screenshot"])])
        if not record.success and record.code in MODEL_RETRY_CODES:
            raise ModelRetry(record.error or "Correct the browser tool arguments and try once more.")
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        detail = error.get("detail") if isinstance(error.get("detail"), dict) else {}
        if not record.success and detail.get("consecutiveUiFailures", 0) >= 2:
            raise ToolFailed(record.error or "The element is still unavailable after recovery.")
        terminal_failure = (
            record.executed
            and error.get("retryable") is False
            and RECOVERY_POLICY.get(record.code or "") not in {"reconcile_tab", "replan"}
        )
        if not record.success and (record.code in TOOL_FAILED_CODES or terminal_failure):
            raise ToolFailed(record.error or "This browser operation cannot be retried.")
        return result

    @functools.wraps(wrapper)
    def timed(*args, **kwargs):
        ctx = args[0] if args else kwargs.get('ctx')
        started = time.perf_counter()
        gate = ctx.deps.gate
        gate.timing_depth += 1
        try:
            return wrapper(*args, **kwargs)
        finally:
            gate.timing_depth -= 1
            if gate.timing_depth == 0:
                gate.tool_ms += int((time.perf_counter() - started) * 1000)

    return timed


def validate_browser_args(function: Callable[..., Any]) -> Callable[..., None]:
    """Validate cross-field browser arguments before execution.

    Discriminated Pydantic command models already make missing upload files,
    assertion expectations, and tab-operation parameters impossible.  This
    validator covers the remaining observe combinations that are individually
    well typed but incompatible together.  Policy and live browser checks stay
    in ``StepGate``/``BrowserRuntime`` where they belong.
    """
    signature = inspect.signature(function)

    @functools.wraps(function)
    def validator(*args: Any, **kwargs: Any) -> None:
        bound = signature.bind(*args, **kwargs)
        values = bound.arguments
        if function is bt.browser_observe:
            mode = values.get("mode", "compact")
            locator = values.get("locator")
            extract = values.get("extract")
            attribute = values.get("attribute")
            if mode == "frames" and (locator is not None or extract is not None):
                raise ModelRetry("Frame inspection cannot be combined with locator extraction.")
            if extract is not None and locator is None:
                raise ModelRetry("Choose a locator when requesting an extraction mode.")
            if extract == "attribute" and not attribute:
                raise ModelRetry("Attribute extraction requires the attribute name.")

    return validator


def browser_validation_hooks() -> Hooks:
    """Record schema/args-validator failures that occur before tool execution."""
    hooks = Hooks()
    tool_names = [f.__name__ for f in DEFAULT_TOOL_FUNCTIONS] + list(OPTIONAL_TOOL_FUNCTIONS)

    @hooks.on.tool_validate_error(tools=tool_names)
    def record_validation_failure(ctx: Any, *, call: Any, tool_def: Any, args: Any, error: Exception) -> Any:
        deps: AxisDeps = ctx.deps
        message = clip(str(error)) or "The browser tool arguments are invalid."
        try:
            raw = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        record = ActionRecord(
            tool=call.tool_name,
            tab=raw.get("tab") if isinstance(raw.get("tab"), str) else None,
            args=clip(repr(args)),
            executed=False,
            execution_success=False,
            semantic_success=False,
            code="INVALID_ARGUMENT",
            message=message,
            retain_in_memory=True,
        )
        deps.gate.records.append(record)
        deps.gate.effects.append((False, False))
        if deps.gate.on_refusal is not None:
            deps.gate.on_refusal(call.tool_name, None, "INVALID_ARGUMENT", message, record.args)
        raise ModelRetry(f"Invalid arguments for {tool_def.name}: {message}")

    return hooks


def navigator_tools(config: AxisConfig) -> list[Tool[AxisDeps]]:
    """The navigator's tool list. A plain Python list — no registry, no
    capability framework, no tool-selection agent."""
    selected = list(DEFAULT_TOOL_FUNCTIONS)
    optional: list[Callable[..., Any]] = []
    if config.tools.capture_evidence:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_capture_evidence"])
    if config.tools.diagnose:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_diagnose"])
    if config.tools.downloads:
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_downloads"])
    if config.tools.visual and config.run.visual_mode == "auto":
        optional.append(OPTIONAL_TOOL_FUNCTIONS["browser_visual"])

    def build(function: Callable[..., Any], *, defer: bool) -> Tool[AxisDeps]:
        return Tool(
            guarded(function),
            takes_ctx=True,
            args_validator=validate_browser_args(function),
            sequential=True,
            timeout=config.run.tool_timeout_seconds,
            defer_loading=defer,
        )

    # When tool search is on, the opt-in tools stay hidden until the navigator
    # actually searches for them; the six ordinary tools always stay visible.
    return [build(f, defer=False) for f in selected] + [
        build(f, defer=config.tools.tool_search) for f in optional
    ]


def build_model(config: AxisConfig, *, client: Any | None = None, model_name: str | None = None) -> Model:
    """One provider/model instance, shared by both agents.

    Talks to OCI GenAI's OpenAI-compatible endpoint through the existing
    ``provider_config`` helpers; nothing about auth is duplicated here.
    """
    from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    import provider_config as pc

    if client is None:
        loaded = pc.load_provider_config()
        if config.provider.base_url or config.provider.model:
            loaded = dataclasses.replace(
                loaded,
                base_url=config.provider.base_url or loaded.base_url,
                model=config.provider.model or loaded.model,
            )
        client = pc.build_async_openai_client(loaded)
        model_name = model_name or loaded.model
    if not model_name:
        raise ValueError("A model name is required when an OpenAI client is supplied directly.")
    provider = OpenAIProvider(openai_client=client)
    model_class = OpenAIResponsesModel if config.provider.api == "responses" else OpenAIChatModel
    return model_class(model_name, provider=provider, settings=config.provider.settings or None)


def build_planner(config: AxisConfig, model: Model) -> Agent[None, PlanDecision]:
    """The planner has no tools, by design. Its AgentSpec (prompt, name,
    retries, ...) is loaded straight from ``planner_agent.yaml`` with
    ``Agent.from_file`` — only the shared model instance and the typed output
    are supplied by code."""
    return Agent.from_file(config.planner_spec_path, model=model, output_type=PlanDecision)


def build_navigator(config: AxisConfig, model: Model) -> Agent[AxisDeps, NavigatorOutcome]:
    tools = navigator_tools(config)
    capabilities: list[Any] = [browser_validation_hooks()]
    if config.tools.tool_search:
        capabilities.append(ToolSearch())
    return Agent.from_file(
        config.navigator_spec_path,
        model=model,
        output_type=NavigatorOutcome,
        deps_type=AxisDeps,
        tools=tools,
        capabilities=capabilities,
    )
