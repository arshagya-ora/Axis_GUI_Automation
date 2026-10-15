"""The AXIS orchestrator: planner cadence, navigator steps, budgets, memory.

Ordinary async Python calling two Pydantic AI agents programmatically. Neither
agent calls the other. Browser execution lives in this object; document tasks
also persist scoped coverage, findings, workflow steps, and recovery checkpoints.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import ipaddress
import json
import re
import time
from datetime import datetime
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic_ai import Agent, ModelHTTPError, RunCancelled, UsageLimitExceeded, UsageLimits, format_as_xml

import browser_tools as bt
from browser_tools import BrowserRuntime, ToolFault

from .agents import AxisDeps, RECOVERY_POLICY, UNKNOWN_OUTCOME_CODES, StepGate, StepBudgetYield, browser_context
from .execution import automatic_scope, external_effect, origin
from .login import authorized_login_action, login_destination_matches, parse_login_intent
from .models import (
    ActionRecord,
    AssertionRecord,
    AxisConfig,
    AxisEvent,
    AxisResult,
    BrowserState,
    BrowserStep,
    CompletionVerdict,
    Evidence,
    NavigatorOutcome,
    PlanDecision,
    TabSummary,
    TaskRequirements,
    TaskRunState,
    TaskFact,
    PendingChange,
    GoalCheck,
    clip,
)

EventCallback = Callable[[AxisEvent], None]
ApprovalCallback = Callable[[str, str | None, dict[str, Any]], bool]


def explicit_browser_steps(request: str) -> list[BrowserStep]:
    """Only preseed an unambiguous flat ordered list.

    Restarted numbering and nested lists describe grouped tasks, not a single
    linear workflow. Leave those intact for outcome decomposition by the agent.
    """
    items = list(re.finditer(r'(?m)^([ \t]*)(\d+)[.)][ \t]+([^\n]+)', request))
    if (len(items) < 2 or len(items) > 64
            or [int(item[2]) for item in items] != list(range(1, len(items) + 1))
            or len({len(item[1].expandtabs()) for item in items}) != 1):
        return []
    return [BrowserStep(instruction=request[item.start(3):
            items[index + 1].start() if index + 1 < len(items) else len(request)].strip()[:4000])
            for index, item in enumerate(items)]

# A schemeless/internal page a real content script cannot attach to, even
# under a "*" host permission — chrome:// pages (no real origin) and
# about:blank (no origin until it navigates) both hit
# "Extension manifest must request permission to access this host." from the
# bridge. Never treat either as an observable or a landing tab.
_UNOBSERVABLE_PREFIXES = ("chrome://", "about:")
# The fallback destination when no observable tab exists. Must be a real
# http(s) page a content script can attach to — "about:blank" cannot be, see
# above. Matches the Google flow the project's own smoke test already treats
# as a first-class target (scripts/browser_foundation_smoke.py).
_FALLBACK_TAB_URL = "https://www.google.com/webhp?hl=en&pws=0"
_GUI_ACTION = re.compile(r"\b(open|click|fill|enter|configure|apply|update|create|change|set|select|enable|disable|submit|perform|do)\b", re.I)
_GUI_TARGET = re.compile(r"\b(?:current|open|this) (?:page|tab|website)\b|\b(?:gui|web app(?:lication)?|website)\b", re.I)
_NAMED_GUI = re.compile(r"\b([A-Z][A-Za-z0-9_-]*(?:\s+[A-Z][A-Za-z0-9_-]*){0,4})\s+GUI\b")
_CURRENT_TAB = re.compile(
    r"\b(?:current(?:ly)?(?: open)?|this|active|focused|open) tab\b|\bopened the tab\b", re.I,
)


def gui_action_request(request: str) -> bool:
    """Explicit page work must not be completed from model knowledge."""
    if re.match(r'\s*(?:what|why|how|explain|describe|summari[sz]e|review|analy[sz]e)\b', request, re.I):
        return False
    return bool(_GUI_ACTION.search(request) and _GUI_TARGET.search(request))


def named_gui(request: str) -> str | None:
    match = _NAMED_GUI.search(request) or re.search(r'\b([A-Z][A-Za-z0-9_-]*)\s+tab\b', request)
    if not match:
        return None
    words = match.group(1).split()
    while words and words[0].casefold() in {'from', 'in', 'inside', 'on', 'using', 'the', 'within'}:
        words.pop(0)
    return ' '.join(words) or None


def _unobservable(url: str | None) -> bool:
    return (url or "").startswith(_UNOBSERVABLE_PREFIXES)

# Bridge faults that end the run rather than feeding another planner pass.
FATAL_BRIDGE_CODES = frozenset({"BRIDGE_UNAVAILABLE", "FIREWALL_DENIED"})

# The spec keeps browser_capture_evidence out of the ordinary tool set and adds
# it only "when evidence or an artifact is requested" — this is how a request
# is recognised in the task text, so a user who asks for a screenshot actually
# gets the tool that can take one.
_EVIDENCE_REQUEST = re.compile(
    r"screen\s?shot|screengrab|screen capture|\bproof\b|\bpdf\b|\bartifact\b|"
    r"save .*(image|picture)",
    re.IGNORECASE,
)
_SCREENSHOT_REQUEST = re.compile(
    r"screen\s?shot|screengrab|screen capture|save .*(image|picture)", re.IGNORECASE,
)
_CONSOLE_REQUEST = re.compile(r"\bbrowser console\b|\bconsole (?:log|message|error|warning|output)s?\b", re.IGNORECASE)
_NETWORK_REQUEST = re.compile(
    r"\bnetwork (?:activity|request|response|error|status|detail)s?\b|\bhttp status\b|"
    r"\brequest(?:'s)? (?:status|details?)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_IP_URL = re.compile(r"(?<![\w./:])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s<>\"'`]*)?(?![\w.])")
_CURRENT_PAGE_DEPENDENCY = re.compile(r"\bcurrent page\b|\bthis page\b|\bfrom here\b|\bcompare\b", re.IGNORECASE)
_REQUIRED_TEXT = re.compile(
    r"(?:assert|verify|contains?|subject(?: is|:)?|exact text(?: is|:)?)\s+(?:that\s+)?[\"“`]([^\"”`]{1,300})[\"”`]",
    re.IGNORECASE,
)
_NEGATED = re.compile(r"\b(?:do not|don't|never|avoid|without)\s+[^.!?;\n]+", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(send|submit|delete|purchase|buy|post|transfer|upload|download|log in|login|sign in|modify)\b",
    re.IGNORECASE,
)
_DOWNLOAD_REQUEST = re.compile(
    r"\bdownload(?:ed|ing|s)?\b|\bexport\b.*\b(file|csv|xlsx|pdf|zip)\b|"
    r"\bsave\b.*\b(file|report|attachment|csv|xlsx|pdf|zip)\b",
    re.IGNORECASE,
)


def evidence_requested(*texts: str | None) -> bool:
    return any(_EVIDENCE_REQUEST.search(text) for text in texts if text)


def downloads_requested(*texts: str | None) -> bool:
    for text in texts:
        if not text:
            continue
        for clause in re.split(r"[.!?;\n]", _NEGATED.sub("", text)):
            if (re.search(r"\b(research|investigate|compare|explain|support|handling)\b", clause, re.I)
                    and not re.match(r"\s*(?:please\s+)?(?:download|export|save)\b", clause, re.I)):
                continue
            if _DOWNLOAD_REQUEST.search(clause):
                return True
    return False


def extract_requirements(*texts: str | None) -> TaskRequirements:
    combined = "\n".join(text for text in texts if text)
    urls = list(dict.fromkeys(match.rstrip(".,);]") for match in _URL.findall(combined)))
    for match in _IP_URL.findall(combined):
        url = 'http://' + match.rstrip('.,);]')
        try:
            parsed = urlsplit(url)
            ipaddress.IPv4Address(parsed.hostname)
            parsed.port  # Validate the port as well as the address.
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    required = list(dict.fromkeys(_REQUIRED_TEXT.findall(combined)))
    forbidden = {match.lower() for clause in _NEGATED.findall(combined)
                 for match in _FORBIDDEN.findall(clause)}
    forbidden = {"login" if item in {"log in", "sign in"} else item for item in forbidden}
    def minimum(noun: str) -> int:
        match = re.search(r"at least\s+(\d+)\s+(?:\w+\s+){0,3}" + noun + r"\b", combined, re.I)
        return min(int(match[1]), 128) if match else 0
    return TaskRequirements(
        literal_urls=urls,
        required_text=required,
        require_screenshot=bool(_SCREENSHOT_REQUEST.search(combined)),
        require_download=downloads_requested(combined) and "download" not in forbidden,
        require_console_check=bool(_CONSOLE_REQUEST.search(combined)),
        require_network_check=bool(_NETWORK_REQUEST.search(combined)),
        mutation_allowed=not bool(re.search(r"\bread[- ]only\b|\bdo not (?:change|modify)\b", combined, re.IGNORECASE)),
        forbidden_actions=forbidden,
        prohibitions=_NEGATED.findall(combined)[:16],
        min_sources=minimum("pages"),
        min_tabs=minimum("tabs"),
        min_actions=minimum("actions"),
        require_search=bool(re.search(r"\buse\s+(?:google|bing|duckduckgo|(?:a|another) search engine)\b", combined, re.I)),
    )


def diagnostics_requested(requirements: TaskRequirements) -> bool:
    return requirements.require_console_check or requirements.require_network_check


def _merge_requirements(current: TaskRequirements, added: TaskRequirements) -> TaskRequirements:
    return TaskRequirements(
        literal_urls=list(dict.fromkeys(current.literal_urls + added.literal_urls)),
        required_text=list(dict.fromkeys(current.required_text + added.required_text)),
        final_url_pattern=added.final_url_pattern or current.final_url_pattern,
        require_screenshot=current.require_screenshot or added.require_screenshot,
        require_download=current.require_download or added.require_download,
        require_console_check=current.require_console_check or added.require_console_check,
        require_network_check=current.require_network_check or added.require_network_check,
        mutation_allowed=current.mutation_allowed and added.mutation_allowed,
        forbidden_actions=current.forbidden_actions | added.forbidden_actions,
        prohibitions=list(dict.fromkeys(current.prohibitions + added.prohibitions))[:16],
        min_sources=max(current.min_sources, added.min_sources),
        min_tabs=max(current.min_tabs, added.min_tabs),
        min_actions=max(current.min_actions, added.min_actions),
        require_search=current.require_search or added.require_search,
    )


def verify_completion(state: TaskRunState, browser_state: BrowserState | None) -> CompletionVerdict:
    """Deterministically decide whether a model completion claim is admissible."""
    reasons: list[str] = []
    latest_assertions: dict[tuple[str, str, str, str, str], AssertionRecord] = {}
    for assertion in state.assertions:
        if assertion.superseded_by or not assertion.required:
            continue
        key = (assertion.goal_id or "", assertion.tab or "", assertion.target or "",
               assertion.assertion, repr(assertion.expected))
        latest_assertions[key] = assertion
    login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
    current_url = browser_state.url if browser_state else None
    app_reached = bool(login and current_url and urlsplit(current_url).hostname == login.app_host)
    login_submit_sequence = max((action.sequence for action in state.actions
        if action.success and action.tool == 'browser_act' and action.operation == 'click'
        and re.search(r'\b(?:log[ -]?in|sign[ -]?in)\b', action.target or '', re.I)), default=0)
    positive_login_check = bool(app_reached and any(
        assertion.passed and assertion.assertion in {'visible', 'text', 'title'}
        and assertion.sequence > login_submit_sequence
        and not re.search(r'\b(?:log[ -]?in|sign[ -]?in|password)\b',
                          str(assertion.target or '') + ' ' + str(assertion.expected or ''), re.I)
        for assertion in state.assertions
    ))
    def obsolete_url_failure(item: AssertionRecord) -> bool:
        return bool(login and positive_login_check and item.assertion == 'url'
                    and item.expected in state.requirements.literal_urls
                    and any(later.passed and later.assertion == 'url'
                            and later.tab == item.tab and later.sequence > item.sequence
                            for later in state.assertions))
    if any(not item.passed and not obsolete_url_failure(item) for item in latest_assertions.values()):
        reasons.append("A required assertion is still failing.")
    if login and not positive_login_check:
        reasons.append('The authenticated application page needs a positive visible or title check.')
    if state.unverified_mutations:
        reasons.append(f"{state.unverified_mutations} page mutation(s) remain unverified.")
    if state.pending_uploads:
        reasons.append('Verify the visible, completed attachment receipt for: ' + ', '.join(item['filename'] for item in state.pending_uploads.values()))
    requirements = state.requirements
    if len(state.source_register) < requirements.min_sources:
        reasons.append(f"Reviewed {len(state.source_register)} of {requirements.min_sources} required source pages.")
    if len(state.task_tabs) < requirements.min_tabs:
        reasons.append(f"Used {len(state.task_tabs)} of {requirements.min_tabs} required tabs.")
    if state.successful_actions < requirements.min_actions:
        reasons.append(f"Only {state.successful_actions} successful actions; {requirements.min_actions} required.")
    if requirements.require_search and not state.search_observed:
        reasons.append("The requested interactive search-engine research has not been observed.")
    if any(step.result is None for step in state.browser_steps):
        reasons.append("Requested browser steps remain unverified.")
    if state.remaining_work:
        reasons.append("Research checklist remains incomplete: " + "; ".join(state.remaining_work[:6]))
    reasons.extend(state.source_note_errors)
    if requirements.require_screenshot and not state.evidence_by_kind("screenshot"):
        reasons.append("The requested screenshot is missing.")
    if requirements.require_download and not state.downloads:
        reasons.append("The requested download is missing.")
    if requirements.require_console_check and not any(e.kind == "console" for e in state.diagnostic_evidence):
        reasons.append("Console diagnostics were requested but not collected.")
    if requirements.require_network_check and not any(e.kind == "network" for e in state.diagnostic_evidence):
        reasons.append("Network diagnostics were requested but not collected.")
    trusted = [item for item in state.evidence if item.verified and item.id and item.kind != "navigator"]
    searchable = "\n".join(
        [browser_state.snapshot if browser_state and browser_state.evidence_id else ""]
        + [item.detail for item in trusted if item.kind in {"observation", "extraction"}]
    ).casefold()
    for required in requirements.required_text:
        if required.casefold() not in searchable:
            reasons.append(f"Required text has not been verified: {required!r}.")
    for literal_url in requirements.literal_urls:
        opened = any(action.tool == 'browser_navigate' and action.operation == 'open'
                     and action.success and action.args and literal_url in action.args
                     for action in state.actions)
        if literal_url not in state.visited_urls and not opened:
            reasons.append(f"Required URL has not been visited or verified: {literal_url!r}.")
    if requirements.final_url_pattern and not re.search(requirements.final_url_pattern, current_url or ""):
        reasons.append("The final URL condition is not met.")
    unresolved_refusals: dict[tuple[str, str | None, str | None], ActionRecord] = {}
    for record in state.actions:
        if record.recovered:
            continue
        key = (record.tool, record.operation, record.tab)
        if not record.executed and record.code not in {"STEP_INTERRUPTED", "ACTION_BUDGET_EXHAUSTED", "RUN_PAUSED", "RUN_CANCELLED", "OUTCOME_UNVERIFIED"}:
            unresolved_refusals[key] = record
        elif record.success:
            unresolved_refusals.pop(key, None)
    if unresolved_refusals:
        reasons.append("A refused browser action remains unresolved.")
    if state.goal_number and not any(item.kind in {"observation", "extraction", "assertion"} for item in trusted):
        reasons.append("The browser task has no recorded page evidence.")
    return CompletionVerdict(valid=not reasons, reasons=reasons)


class _Stop(Exception):
    """Internal: carry a finished :class:`AxisResult` out of a nested step."""

    def __init__(self, result: AxisResult):
        super().__init__(result.reason)
        self.result = result


class _VisionUnavailable(Exception):
    """The first image request was explicitly rejected before any action."""


class AxisOrchestrator:
    """Drives one task (and its follow-ups) across a live browser runtime."""

    def __init__(
        self,
        *,
        browser: BrowserRuntime,
        planner: Agent[Any, PlanDecision],
        navigator: Agent[Any, NavigatorOutcome],
        config: AxisConfig,
        on_event: EventCallback | None = None,
        approval: ApprovalCallback | None = None,
        navigator_factory: Callable[..., Agent[Any, NavigatorOutcome]] | None = None,
    ):
        self.browser = browser
        self.planner = planner
        self.navigator = navigator
        self.config = config
        self.on_event = on_event
        self.approval = approval
        # Rebuilds the navigator with the opt-in tools enabled. Without one the
        # tool set is fixed for the whole run (the default the tests use).
        self.navigator_factory = navigator_factory

        self.state: TaskRunState | None = None
        self.usage_limits = UsageLimits(request_limit=config.run.max_model_requests)

        self._prestart_paused = False
        self._capture_enabled = config.tools.capture_evidence
        self._diagnose_enabled = config.tools.diagnose
        self._downloads_enabled = config.tools.downloads
        self._visual_enabled = False
        self._base_navigator = navigator
        self.documents = None  # Set by the UI runner for tasks with attachments.
        self.attachment_session = None
        self.recovery_counts = None
        self.browser_checkpoint = None

    @property
    def memory(self) -> TaskRunState | None:
        return self.state

    @property
    def usage(self):
        return self.state.usage if self.state else None

    @property
    def browser_actions(self) -> int:
        return self.state.counters.browser_actions if self.state else 0

    @property
    def planner_passes(self) -> int:
        return self.state.counters.planner_passes if self.state else 0

    @property
    def tab(self) -> str | None:
        return self.state.bound_tab if self.state else None

    @tab.setter
    def tab(self, value: str | None) -> None:
        if self.state is not None:
            self.state.bound_tab = value
            if value and value not in self.state.task_tabs:
                self.state.task_tabs = (self.state.task_tabs + [value])[-128:]

    # -- timing ----------------------------------------------------------

    def _elapsed_ms(self) -> int:
        started = self.state.started_monotonic if self.state else time.monotonic()
        return int((time.monotonic() - started) * 1000)

    # -- control ---------------------------------------------------------

    def pause(self) -> None:
        if self.state:
            self.state.status = "paused"
            gate = getattr(self, '_active_gate', None)
            if gate is None:
                self.state.cancellation_token.cancel()
            else:
                with gate.lock:
                    if gate.in_flight is None:
                        self.state.cancellation_token.cancel()
        else:
            self._prestart_paused = True

    def resume(self) -> None:
        self._prestart_paused = False
        if self.state and self.state.status == "paused":
            self.state.cancellation_token = type(self.state.cancellation_token)()
            self.state.status = "active"

    def cancel(self) -> None:
        if self.state:
            self.state.status = "cancelled"
            self.state.cancellation_token.cancel()

    # -- entry points ----------------------------------------------------

    async def start_task(self, task: str, *, task_id: str | None = None,
                         reference_context: dict[str, Any] | None = None) -> AxisResult:
        """Start an independent task while reusing the connected browser."""
        attachments = self.documents or self.attachment_session
        if attachments:
            task = attachments.input_prompt(task)
            self.attachment_session = attachments
            self.documents = attachments if attachments.catalog() else None
        self.state = self.config.new_memory(task, extract_requirements(task))
        if attachments:
            self.state.original_request = task
        self.state.reference_context = reference_context or {}
        if not self.documents:
            self.state.browser_steps = explicit_browser_steps(task)
        for tab in self.browser.tabs.values():
            tab.task_created = False
        if task_id is not None:
            self.state.task_id = task_id
        if attachments:
            from axis.models import AttachmentRef
            self.state.attachments = [AttachmentRef(attachment_id=a['id'], role=a['role']) for a in attachments.store.catalog(self.state.task_id)]
        if self.documents:
            with self.documents.store.history.transaction():
                saved = self.documents.store.history.task(self.state.task_id)
                if not saved.get('instruction'):
                    saved['instruction'] = task
                    self.documents.store.history.save_task(saved)
            self.documents.refresh_scope()
            checkpoint = self.documents.store.history.task(self.state.task_id).get('document_checkpoint', {})
            if checkpoint:
                self.state.counters.total_steps = checkpoint.get('total_steps', 0)
                self.state.counters.browser_actions = checkpoint.get('browser_actions', 0)
                self.state.counters.mutation_count = checkpoint.get('mutation_count', 0)
                self.state.counters.verified_mutation_count = checkpoint.get('verified_mutation_count', 0)
                self.state.successful_actions = checkpoint.get('successful_actions', 0)
                self.state.usage.requests = checkpoint.get('model_requests', 0)
                self.state.usage.input_tokens = checkpoint.get('input_tokens', 0)
                self.state.usage.output_tokens = checkpoint.get('output_tokens', 0)
                self.state.remaining_work = checkpoint.get('remaining_work', [])
                self.state.sequence = checkpoint.get('sequence', 0)
                self.state.evidence_number = checkpoint.get('evidence_number', 0)
                self.state.goal_number = checkpoint.get('goal_number', 0)
                self.state.pending_uploads = checkpoint.get('pending_uploads', {})
                self.state.pending_changes = {key:PendingChange.model_validate(value) for key,value in checkpoint.get('pending_changes', {}).items()}
                self.state.last_interaction_by_tab = checkpoint.get('last_interaction_by_tab', {})
                self.state.last_edit_by_page = checkpoint.get('last_edit_by_page', {})
                self.state.current_goal_id = checkpoint.get('current_goal_id')
                self.state.current_goal = checkpoint.get('current_goal')
                if checkpoint.get('current_verification'):
                    self.state.current_verification = GoalCheck.model_validate(checkpoint['current_verification'])
                self.state.assertions = [AssertionRecord.model_validate(value) for value in checkpoint.get('assertions', [])]
                self.state.extra_steps = checkpoint.get('extra_steps', 0)
                self.state.extra_model_requests = checkpoint.get('extra_model_requests', 0)
                self.state.extra_browser_actions = checkpoint.get('extra_browser_actions', 0)
                self.state.document_auto_steps = checkpoint.get('auto_steps', 0)
                self.state.document_extension_progress = checkpoint.get('extension_progress', 0)
                self.state.last_error = 'Resume from document coverage and findings. Observe the website before repeating any browser change.'
        self.browser.task_started_wallclock = self.state.started_wallclock
        self.browser.visual_enabled = False
        self.browser.invalidate_visual()
        self.state.status = "paused" if self._prestart_paused else "active"
        if self.recovery_counts:
            self.state.counters.total_steps = self.recovery_counts.get('total_steps', 0)
            self.state.counters.browser_actions = self.recovery_counts.get('browser_actions', 0)
            self.state.usage.requests = self.recovery_counts.get('model_requests', 0)
            self.state.last_error = 'Recovered workflow. Reobserve browser state before retrying any running step.'
            self.recovery_counts = None
        if self.browser_checkpoint and not self.documents:
            self._restore_browser_checkpoint(self.browser_checkpoint)
            self.state.requirements = extract_requirements(task)
            self.browser_checkpoint = None
        self._set_navigator_tools()
        try:
            return await self._loop()
        except RunCancelled:
            return self._result("paused" if self.state and self.state.status == "paused" else "cancelled",
                reason="The run is paused; state is preserved." if self.state and self.state.status == "paused" else "The user cancelled the run.")
        finally:
            self._close_telemetry()

    async def run(self, task: str) -> AxisResult:
        """Compatibility alias for start_task()."""
        return await self.start_task(task)

    async def resume_run(self) -> AxisResult:
        """Re-enter a paused run without replacing its identity, memory or budgets.

        The caller must wait for the previous entry point to return first.
        """
        if self.state is None or self.state.status != "paused":
            raise ValueError("Only a paused live task can be resumed.")
        self.resume()
        try:
            return await self._loop()
        except RunCancelled:
            return self._result("paused" if self.state and self.state.status == "paused" else "cancelled",
                reason="The run is paused; state is preserved." if self.state and self.state.status == "paused" else "The user cancelled the run.")
        finally:
            self._close_telemetry()

    async def continue_task(self, follow_up: str) -> AxisResult:
        """Append a follow-up and force an immediate planner pass.

        Keeps bounded task context, extracted data, completed goals, and the
        live browser runtime with its open tabs. The browser is only re-bound
        if the previous run lost its tab.
        """
        if self.state is None:
            return await self.start_task(follow_up)
        state = self.state
        attachments = self.documents or self.attachment_session
        if attachments:
            follow_up = attachments.input_prompt(follow_up, follow_up=True)
            self.attachment_session = attachments
            self.documents = attachments if attachments.catalog() else None
        state.add_followup(follow_up)
        if attachments:
            state.follow_ups[-1] = follow_up
        added = extract_requirements(follow_up)
        state.requirements = _merge_requirements(state.requirements, added)
        application = named_gui(follow_up)
        if (_CURRENT_TAB.search(follow_up) or added.literal_urls or re.search(r"\btab_\d+\b", follow_up)
                or (application and application != state.bound_application)):
            state.bound_tab = None
            state.bound_application = None
        if added.require_download:
            state.downloads.clear()
            state.started_wallclock = time.time()
            self.browser.task_started_wallclock = state.started_wallclock
        if added.require_screenshot:
            state.evidence = [item for item in state.evidence if item.kind != "screenshot"]
        state.last_error = None
        state.document_synthesis_only = False
        if self.documents:
            # New user input starts a fresh traversal attempt without erasing
            # coverage or repeating the same internal-error counter forever.
            self.documents.progress(0, [])
            self.documents.last_result = None
            self.documents.refresh_scope()
        state.counters.consecutive_failures = 0
        state.counters.no_progress_steps = 0
        state.counters.repeated_actions = 0
        state.counters.repeated_observations = 0
        state.progress_by_tab.clear()
        state.made_progress = False
        state.counters.navigator_steps_since_plan = self.config.run.planner_interval_steps
        state.current_goal = None
        state.latest_navigator_outcome = None
        state.status = "active"
        state.completion_rejected = False
        state.repair_attempted = False
        if added.literal_urls:
            state.direct_navigation_done = False
        state.started_monotonic = time.monotonic()
        state.model_ms = state.browser_ms = state.plan_ms = state.step_ms = state.tool_model_ms = 0
        state.preflight_ms = state.authorization_ms = state.assertion_ms = 0
        state.cancellation_token = type(state.cancellation_token)()
        try:
            self._set_navigator_tools()
            if state.bound_tab is not None:
                self._reconcile_bound_tab()
            return await self._loop()
        except RunCancelled:
            return self._result("paused" if self.state and self.state.status == "paused" else "cancelled",
                reason="The run is paused; state is preserved." if self.state and self.state.status == "paused" else "The user cancelled the run.")
        finally:
            self._close_telemetry()

    def _set_navigator_tools(self) -> None:
        if self.state is not None:
            self.browser.forbidden_actions = frozenset(self.state.requirements.forbidden_actions)
        state = self.state
        assert state is not None
        self._sync_navigator_tools(
            capture=evidence_requested(state.original_request, *state.follow_ups),
            diagnose=diagnostics_requested(state.requirements),
            downloads=state.requirements.require_download,
            exact=True,
        )

    def _sync_navigator_tools(
        self, *, capture: bool = False, diagnose: bool = False, downloads: bool = False,
        visual: bool = False,
        exact: bool = False,
    ) -> None:
        """Turn on an opt-in tool when the run actually needs it.

        ``browser_capture_evidence`` is added when the user asked for a
        screenshot/artifact, and ``browser_diagnose`` after a genuine failure
        worth investigating — matching the spec's "add only when requested"
        rule while making sure a task that asks for a screenshot is not left
        without the one tool that can take it.
        """
        want_capture = (capture or self.config.tools.capture_evidence) if exact else (
            self._capture_enabled or capture or self.config.tools.capture_evidence
        )
        want_diagnose = (diagnose or self.config.tools.diagnose) if exact else (
            self._diagnose_enabled or diagnose or self.config.tools.diagnose
        )
        want_downloads = (downloads or self.config.tools.downloads) if exact else (
            self._downloads_enabled or downloads or self.config.tools.downloads
        )
        want_visual = visual and self.config.run.visual_mode == "auto" and not (self.state and self.state.vision_disabled)
        previous_visual = self._visual_enabled
        if (want_capture, want_diagnose, want_downloads, want_visual) == (
            self._capture_enabled, self._diagnose_enabled, self._downloads_enabled, self._visual_enabled,
        ):
            return
        self._capture_enabled, self._diagnose_enabled, self._downloads_enabled = (
            want_capture, want_diagnose, want_downloads,
        )
        self._visual_enabled = want_visual
        if self.navigator_factory is None:
            return
        options = dict(capture=want_capture, diagnose=want_diagnose, downloads=want_downloads)
        if want_visual or previous_visual:
            options["visual"] = want_visual
        self.navigator = self.navigator_factory(**options)
        self._emit(
            "status", phase="tools_changed", capture=want_capture,
            diagnose=want_diagnose, downloads=want_downloads,
            visual=want_visual,
        )

    # -- events ----------------------------------------------------------

    def _emit(self, kind: str, **detail: Any) -> None:
        if self.on_event is None:
            return
        detail.setdefault("at_ms", self._elapsed_ms())
        try:
            self.on_event(AxisEvent(kind=kind, detail=detail))
        except Exception:  # a caller's callback must never break a run
            pass

    # -- budgets and stopping --------------------------------------------

    def _run_limits(self):
        state = self.state
        run = self.config.run
        if state is None:
            return run
        return run.model_copy(update={
            "max_model_requests": run.max_model_requests + state.extra_model_requests,
            "max_browser_actions": run.max_browser_actions + state.extra_browser_actions,
            "max_total_steps": run.max_total_steps + state.extra_steps,
        })

    async def extend_budget(self, requests: int, *, steps: int = 0, actions: int = 0) -> AxisResult:
        """Explicitly add task budget while preserving evidence and lifetime usage."""
        if self.state is None or self.state.status != "limit_reached":
            raise ValueError("Budget extension requires a task stopped at its limit.")
        if requests <= 0 or steps < 0 or actions < 0:
            raise ValueError("Requests must be positive; steps and actions must be non-negative.")
        self.state.extra_model_requests += requests
        self.state.extra_steps += steps
        self.state.extra_browser_actions += actions
        self.state.status = "active"
        self.state.last_error = "The user extended this task's budget. Resume the remaining work using retained sources."
        try:
            # A budget can stop immediately after the planner chose a goal,
            # before its first observation. Resume that already-authorized
            # step instead of asking a planner to complete an unread page.
            if (self.state.current_goal and self.state.last_browser_state is None
                    and self.state.latest_navigator_outcome is None):
                await self._navigator_burst()
            return await self._loop()
        except _Stop as stopped:
            return stopped.result
        finally:
            self._close_telemetry()

    def _partial_answer(self, reason: str = "The execution budget was reached.", *, budget_exhausted: bool = True) -> str:
        state = self.state
        assert state is not None
        lines = ["Task incomplete. " + reason,
                 "Remaining work: " + "; ".join(state.remaining_work or [state.current_goal or "Final verification and synthesis"])]
        for index, step in enumerate(state.browser_steps, 1):
            if step.result is not None:
                lines.append(f"Step {index}: {step.result}")
        for fact in state.facts:
            if fact.tool == "source_note":
                lines.append(fact.value)
        if state.source_register:
            lines.append("Reviewed sources (this list does not imply every requested claim was verified):")
            lines.extend(f"- {item['title']}: {url}" for url, item in state.source_register.items())
        lines.append("Use /extend REQUESTS [STEPS ACTIONS] to resume with these sources and existing tabs." if budget_exhausted else
                     "Use /continue with recovery instructions to retain these sources and existing tabs.")
        return clip("\n".join(lines), self.config.run.final_answer_max_chars) or ""

    def _synthesis_reserve(self) -> int:
        if self.state.requirements.min_sources or self.state.requirements.require_search:
            return self.config.run.synthesis_reserve_requests
        if gui_action_request(self._current_request()) or (self.documents and any(
                use.get('purpose') == 'execute' for use in self.documents._uses().values())):
            return 0
        return self.config.run.synthesis_reserve_requests

    def _stop_reason(self) -> AxisResult | None:
        state = self.state
        assert state is not None
        run = self._run_limits()
        if state.status == "cancelled":
            return self._result("cancelled", reason="The user cancelled the run.")
        if state.status == "paused":
            return self._result("paused", reason="The run is paused; task and browser state are preserved.")
        if state.counters.total_steps >= run.max_total_steps:
            if self.documents:
                covered = sum(item['read'] for item in self.documents.coverage())
                room = self.config.run.document_extra_steps - state.document_auto_steps
                if (room > 0 and covered > state.document_extension_progress
                        and self.documents.progress().get('attempts', 0) == 0
                        and int(state.usage.requests) < run.max_model_requests - self._synthesis_reserve()):
                    extension = min(room, self.config.run.document_extension_steps)
                    state.extra_steps += extension
                    state.document_auto_steps += extension
                    state.document_extension_progress = covered
                    self._emit('status', phase='document_budget_extended', steps=extension)
                else:
                    state.document_synthesis_only = True
            else:
                return self._result("limit_reached", reason=f"Reached the maximum of {run.max_total_steps} steps.")
        if int(state.usage.requests) >= run.max_model_requests:
            return self._result("limit_reached", reason="Reached the maximum number of model requests.")
        if state.counters.browser_actions >= run.max_browser_actions:
            return self._result("limit_reached", reason="Reached the maximum number of browser actions.")
        if state.counters.consecutive_failures >= run.max_consecutive_failures:
            return self._result("failed", reason=f"{state.counters.consecutive_failures} consecutive failures.")
        if state.counters.no_progress_steps >= 3:
            return self._result("failed", reason="NO_PROGRESS: Three equivalent attempts produced no useful change.")
        return None

    def _result(self, status: str, *, answer: str | None = None, evidence: list[str] | None = None,
                reason: str = "", recoverable: bool = False, choices: list[str] | None = None) -> AxisResult:
        state = self.state
        if state and answer is None and (status == "limit_reached" or
                                        (status == "failed" and (state.source_register or state.browser_steps))):
            answer = self._partial_answer(reason or "Execution stopped before completion.", budget_exhausted=status == "limit_reached")
        if state:
            state.status = status  # type: ignore[assignment]
        result = AxisResult(
            status=status,  # type: ignore[arg-type]
            answer=answer,
            evidence=evidence or [],
            reason=reason,
            recoverable=recoverable,
            clarification_choices=choices or [],
            total_steps=state.counters.total_steps if state else 0,
            planner_passes=state.counters.planner_passes if state else 0,
            browser_actions=state.counters.browser_actions if state else 0,
            model_requests=int(state.usage.requests) if state else 0,
            time_to_first_action_ms=state.first_action_ms if state else None,
            duration_ms=self._elapsed_ms(),
            # Agent.run includes time spent executing tools. Subtract the
            # entire guarded tool duration, including authorization and preflight.
            model_ms=max(0, state.model_ms - state.tool_model_ms) if state else 0,
            browser_ms=state.browser_ms if state else 0,
            preflight_ms=state.preflight_ms if state else 0,
            authorization_ms=state.authorization_ms if state else 0,
            assertion_ms=state.assertion_ms if state else 0,
            redundant_observations=state.counters.redundant_observations if state else 0,
            redundant_actions=state.counters.redundant_actions if state else 0,
            redundant_assertions=state.counters.redundant_assertions if state else 0,
            recovery_actions=state.counters.recovery_actions if state else 0,
            input_tokens=int(state.usage.input_tokens) if state else 0,
            output_tokens=int(state.usage.output_tokens) if state else 0,
            limitations=["The configured provider rejected image input; visual recovery was disabled for this run."]
            if state and state.vision_disabled else [],
        )
        self._emit(
            "final", status=result.status, reason=result.reason,
            duration_ms=result.duration_ms, model_ms=result.model_ms, browser_ms=result.browser_ms,
            debug_artifacts=None,
        )
        if self.documents and state:
            self.documents.checkpoint(state, self.config.run.model_dump())
        return result

    def _close_telemetry(self) -> None:
        state = self.state
        if state and state.debug_telemetry_started:
            artifacts = self.browser.stop_debug_telemetry()
            state.debug_telemetry_started = False
            if artifacts:
                state.evidence.append(Evidence(kind="observation", detail=f"Debug telemetry: {artifacts}"))

    # -- main loop -------------------------------------------------------

    async def _loop(self) -> AxisResult:
        state = self.state
        assert state is not None
        state.direct_execution = state.direct_execution and self.config.run.direct_browser_execution and not bool(self.documents)
        document_progress = self.documents.progress() if self.documents else {}
        document_stalls = document_progress.get('attempts', 0)
        document_results = set(document_progress.get('signatures', []))
        if self.documents and self.documents.last_result is None and not any(item.kind == 'document' for item in state.evidence):
            # A reconstructed task needs fresh source content in model context.
            # Preserve failures, but permit re-reading evidence lost with the process.
            document_results.clear()
        while True:
            stop = self._stop_reason()
            if stop is not None:
                return stop
            if state.direct_execution:
                state.current_goal = self._current_request()
                state.current_goal_id = state.current_goal_id or "direct"
                try:
                    await self._navigator_burst()
                except _Stop as stopped:
                    return stopped.result
                # Strategic help is requested by the browser agent, or by a
                # bounded failure/no-progress guard, rather than another classifier.
                state.direct_execution = False
            try:
                plan = await self._plan()
            except _Stop as stopped:
                return stopped.result

            request = self._current_request()
            if gui_action_request(request) and plan.decision == 'complete' and not any(
                action.success and action.tool in {'browser_act', 'browser_navigate', 'browser_visual'}
                for action in state.actions):
                routing_error = 'This request requires work in the named browser application. Select a matching tab and perform the action before answering.'
                if state.last_error == routing_error:
                    return self._result('failed', reason=routing_error, recoverable=True)
                state.last_error = routing_error
                continue

            state.counters.planner_passes += 1
            state.counters.navigator_steps_since_plan = 0
            self._emit("planner_decision", **plan.model_dump(), duration_ms=state.plan_ms)
            if self.documents and plan.attachment_uses:
                try:
                    from axis.attachments.scope import user_messages
                    self.documents.configure_uses(plan.attachment_uses, state.original_request + '\n' + '\n'.join(user_messages(self.documents.store, state.task_id)))
                except ValueError as error:
                    from axis.attachments.runtime import DocumentRestriction, DocumentScopeError
                    if isinstance(error, DocumentRestriction):
                        return self._result('needs_user', reason=str(error), answer=str(error))
                    if isinstance(error, DocumentScopeError):
                        return self._result('failed', reason='Invalid attachment scope: ' + str(error), recoverable=True)
                    state.last_error = str(error)
                    document_stalls += 1
                    self.documents.progress(document_stalls, document_results)
                    if document_stalls >= 3:
                        return self._result('failed', reason='Document scope could not be validated: ' + str(error), recoverable=True)
                    continue
            if self.documents and plan.document_notes:
                rejected = self.documents.retain_notes(plan.document_notes)
                if rejected:
                    self._emit('status', phase='document_notes_rejected', rejections=rejected,
                               error='; '.join(f"Section {item['section_id']}: {item['reason']}" for item in rejected))

            if plan.decision == "complete":
                if self.documents:
                    errors = self.documents.completion_errors()
                    if errors:
                        state.last_error = "Completion rejected: " + " ".join(errors)
                        if state.document_synthesis_only:
                            return self._result('limit_reached', answer=plan.final_answer,
                                reason='Document budget exhausted; this is a partial answer. ' + state.last_error[:500])
                        document_stalls += 1
                        self.documents.progress(document_stalls, document_results)
                        self._emit('status', phase='document_error', error=state.last_error[:600], attempt=document_stalls, operation='completion')
                        if document_stalls >= 3:
                            return self._result('failed', reason=state.last_error[:600], answer=state.last_error[:600], recoverable=True)
                        continue
                if plan.final_answer and len(plan.final_answer) > self.config.run.final_answer_max_chars:
                    state.last_error = f"Shorten the answer to {self.config.run.final_answer_max_chars} characters while preserving required sources."
                    continue
                verdict = verify_completion(state, state.last_browser_state)
                if not self._current_goal_verified():
                    verdict.valid = False
                    verdict.reasons.append("The current goal still needs its declared scoped verification.")
                if not verdict.valid:
                    state.completion_rejected = True
                    state.last_error = "Completion rejected: " + " ".join(verdict.reasons)
                    state.current_goal = state.current_goal or "Verify every unresolved completion requirement."
                    continue
                return self._result(
                    "completed", answer=plan.final_answer, evidence=plan.evidence, reason=plan.reason,
                )
            if plan.decision == "ask_user":
                return self._result("needs_user", answer=plan.final_answer, evidence=plan.evidence,
                                    reason=plan.reason, choices=plan.clarification_choices)
            if plan.decision == "fail":
                return self._result("failed", evidence=plan.evidence, reason=plan.reason)

            if plan.decision == "document":
                if state.document_synthesis_only:
                    return self._result('limit_reached', reason='Document execution budget reached; saved findings and remaining work are available.')
                try:
                    if not self.documents or not plan.document_request:
                        raise ValueError("Document access requires task attachments and an executable document_request.")
                    result = await asyncio.to_thread(self.documents.execute, plan.document_request)
                    normalized = {k:v for k,v in result.items() if k not in {'new_sections', 'coverage'}}
                    if 'items' in normalized:
                        normalized['items'] = sorted(normalized['items'], key=lambda item: str(item.get('id', '')))
                    import hashlib
                    signature = hashlib.sha256(json.dumps(normalized, sort_keys=True, default=str).encode()).hexdigest()
                    if signature in document_results and not (plan.document_request.reread and plan.document_request.reason):
                        raise ValueError('The document request returned no new information. Use the retained result or choose a different operation.')
                    document_results.add(signature)
                    document_stalls = 0
                    self.documents.progress(document_stalls, document_results)
                    if result.get('operation') in {'read', 'enumerate'}:
                        for item in result['items']:
                            evidence = state.add_evidence('document', item['text'])
                            evidence.attachment_id = item['attachment_id']
                            evidence.location = item['location']
                    state.last_error = None
                    self._emit('status', phase='document_read', operation=result.get('operation', plan.document_request.operation),
                               coverage=self.documents.coverage())
                except (ValueError, RuntimeError) as error:
                    from axis.attachments.runtime import DocumentRestriction, DocumentScopeError
                    state.last_error = str(error)[:600]
                    document_stalls += 1
                    if self.documents:
                        self.documents.progress(document_stalls, document_results)
                    self._emit('status', phase='document_error', error=state.last_error,
                               attempt=document_stalls, operation=plan.document_request.operation if plan.document_request else 'missing')
                    if isinstance(error, DocumentRestriction):
                        return self._result('needs_user', reason=state.last_error, answer=state.last_error)
                    if isinstance(error, DocumentScopeError):
                        return self._result('failed', reason='Invalid attachment scope: ' + state.last_error, recoverable=True)
                    if document_stalls >= 3:
                        reason = 'Document work stopped after three attempts without progress: ' + state.last_error
                        return self._result('failed', reason=reason, answer=reason, recoverable=True)
                state.counters.total_steps += 1
                self.documents.checkpoint(state, self.config.run.model_dump())
                continue

            if self._synthesis_reserve() and (state.remaining_work or state.requirements.min_sources) and (
                    self._run_limits().max_model_requests - int(state.usage.requests)
                    <= self._synthesis_reserve()):
                return self._result("limit_reached", reason="Research budget reached its synthesis reserve; progress is preserved.")

            try:
                self._apply_browse_plan(plan)
            except ValueError as error:
                from axis.attachments.runtime import DocumentRestriction, DocumentScopeError
                state.last_error = str(error)
                if isinstance(error, DocumentRestriction):
                    return self._result('needs_user', reason=state.last_error, answer=state.last_error)
                if isinstance(error, DocumentScopeError):
                    return self._result('failed', reason='Invalid attachment scope: ' + state.last_error, recoverable=True)
                continue

            try:
                await self._navigator_burst()
            except _Stop as stopped:
                return stopped.result

    def _apply_browse_plan(self, plan):
        """Validate a workflow transition before changing active goal or evidence identity."""
        state = self.state
        step = None
        if self.documents:
            workflow = self.documents.workflow()
            if workflow['next_steps'] and not plan.workflow_step_id:
                raise ValueError('Choose the first pending workflow_step_id for browser execution.')
            if plan.workflow_step_id:
                if plan.verification is None:
                    raise ValueError('Workflow execution requires a concrete verification postcondition.')
                step = self.documents.validate_step(plan.workflow_step_id)
        if plan.verification and (plan.verification.locator or plan.verification.tab):
            saved = GoalCheck.model_validate_json(step['verification']) if step and step.get('verification') else None
            if plan.verification != state.current_verification and plan.verification != saved:
                raise ValueError('Only the navigator can bind an observed verification locator; retain the declared outcome.')
        running_id = step.get('goal_id') if step else None
        keep_goal = bool(state.current_goal_id and (state.pending_changes or
                         (state.current_verification and not self._current_goal_verified()) or
                         plan.next_goal == state.current_goal))
        if step:
            keep_goal = bool(running_id)
        goal_id = running_id or (state.current_goal_id if keep_goal else f'goal_{state.goal_number + 1}')
        check = state.current_verification if keep_goal else plan.verification
        if step and step.get('verification'):
            check = GoalCheck.model_validate_json(step['verification'])
        if check is None:
            check = plan.verification
        proposed = plan.verification
        if check and proposed and check.locator and not proposed.locator and not proposed.tab:
            proposed = proposed.model_copy(update={'locator': check.locator, 'tab': check.tab})
        if proposed != check and keep_goal and state.current_goal_id == goal_id:
            if self._refine_verification(proposed):
                check = state.current_verification
        if step and proposed != check:
            raise ValueError('Retain the saved verification postcondition when recovering this step: ' + check.model_dump_json())
        if step:
            self.documents.begin(step['id'], goal_id, check)
        if not keep_goal:
            state.goal_number += 1
        state.current_goal_id = goal_id
        state.current_verification = check
        for pending in state.pending_changes.values():
            if pending.goal_id == goal_id and pending.verification is None:
                pending.verification = check
        state.current_plan_summary = plan.plan_summary
        state.current_goal = self._preserve_literal_urls(plan.next_goal)
        state.current_success_condition = plan.success_condition
        state.repair_attempted = False

    async def _navigator_burst(self) -> None:
        """Run navigator steps until the planner is due again."""
        state = self.state
        assert state is not None
        while True:
            stop = self._stop_reason()
            if stop is not None:
                raise _Stop(stop)

            browser_state = await self._browser_state()
            # The preceding action's effect is only known once the next page
            # observation arrives, including asynchronously rendered updates.
            self._update_progress(browser_state, [])
            stop = self._stop_reason()
            if stop is not None:
                raise _Stop(stop)
            if state.counters.no_progress_steps == 2 and state.counters.navigator_steps_since_plan:
                state.last_error = "NO_PROGRESS: Two equivalent attempts made no useful change; choose a different strategy."
                return
            outcome, gate = await self._navigate(browser_state)

            state.counters.total_steps += 1
            state.counters.navigator_steps_since_plan += 1
            state.latest_navigator_outcome = outcome
            self._commit_gate(gate)
            check = state.current_verification
            if check and check.assertion == 'value':
                self._refine_verification(check.model_copy(update={'assertion': 'text'}))
            # Follow a tab the navigator switched to, so the next observation
            # targets the page it actually moved to rather than the one it
            # left — otherwise it must burn a whole step re-activating.
            control_codes = {'ACTION_BUDGET_EXHAUSTED', 'STEP_INTERRUPTED', 'RUN_PAUSED', 'RUN_CANCELLED'}
            failures = [record for record in gate.records if not record.success and not record.recovered
                        and record.code not in control_codes
                        and not (record.code == 'STALE_STATE' and not record.executed)
                        and not (record.code == 'INVALID_ARGUMENT' and any(
                            later.success and later.tool == record.tool
                            for later in gate.records[gate.records.index(record) + 1:]))]
            # A local execution window is a scheduler yield, not a task failure.
            # Real faults, user stop conditions and requests for information win.
            budget_boundary = (gate.budget_scope is not None and not failures
                and not gate.non_retryable and not gate.stopped and not outcome.stop_condition_met
                and outcome.blocker not in {'environment', 'missing_information', 'scope_change'})
            budget_yield = budget_boundary and gate.budget_scope == 'step'
            if budget_boundary and outcome.status in {'failed', 'blocked', 'needs_plan', 'ask_user'}:
                outcome.status = 'continue'
                outcome.reason = ('Step browser-call budget reached; continue the remaining work with fresh evidence.'
                                  if budget_yield else 'Run browser-call budget reached; preserve remaining work.')
                outcome.final_answer = None
            if outcome.stop_condition_met:
                outcome.status = 'failed'
            completion_proposed = outcome.status in {'goal_reached', 'task_complete'}
            recoverable = {"STALE_REFERENCE", "REF_NOT_FOUND", "LOCATOR_ACTIONABILITY_TIMEOUT", "LOCATOR_NOT_FOUND", "LOCATOR_STRICT_MODE_VIOLATION", "FRAME_TARGET_AMBIGUOUS", "FRAME_GEOMETRY_UNAVAILABLE"}
            tab = browser_state.tab
            if tab:
                targeting = [r for r in failures if r.tab == tab and r.code in recoverable]
                if targeting:
                    state.visual_failures[tab] = state.visual_failures.get(tab, 0) + len(targeting)
                elif not failures:
                    state.visual_failures[tab] = 0
                if ((outcome.needs_visual or state.visual_failures.get(tab, 0) >= 2)
                        and not gate.non_retryable and not outcome.stop_condition_met
                        and self.config.run.visual_mode == "auto"
                        and tab not in state.visual_tabs
                        and not state.vision_disabled):
                    state.visual_tabs.add(tab)
                    outcome.status = "continue"
                    outcome.reason = "Visual recovery requested; a fresh screenshot follows."
                    # A recoverable targeting failure gets this one different
                    # approach before exhausting the existing failure budget.
                    state.counters.consecutive_failures = 0
            # A goal_reached decision with no new mutation is a semantic
            # assessment of the fresh pre-step BrowserState. Do not grant the
            # same status to a step that just changed the page: that change
            # still needs a later observation or passing assertion.
            if outcome.status in {"goal_reached", "task_complete"} and failures:
                outcome.status = "continue"
                outcome.reason = failures[-1].error or "A required browser call did not succeed."
            rejection = self._record_browser_steps(outcome)
            if rejection and outcome.status == "task_complete":
                outcome.status = "continue"
                outcome.reason = rejection
            if outcome.status in {"goal_reached", "task_complete"}:
                pending = [p for p in state.pending_changes.values() if p.goal_id == state.current_goal_id]
                known = {item.id for item in state.evidence if item.verified and item.goal_id == state.current_goal_id}
                if pending:
                    rejection = "Goal verification required: " + "; ".join(
                        f"{p.tab}: observe search results for {p.search_query!r}" if p.search_query is not None else
                        f"{p.tab}: observe URL {p.expected_url!r}" if p.expected_url else
                        f"{p.tab}: run {p.verification.model_dump_json(exclude_none=True)}" if p.verification else
                        f"{p.tab}: action {p.sequence} ({p.target or 'unknown target'}) on {p.source_url or 'unknown page'} has no postcondition. "
                        f'Verify its saved result with browser_assert goal_target="action:{p.sequence}"; do not repeat the action'
                        for p in pending) + "."
                    passed = next((a for a in reversed(state.assertions) if a.passed and not a.superseded_by
                                   and any(a.goal_id == p.goal_id and a.tab == p.tab and a.sequence > p.sequence
                                           for p in pending)), None)
                    if passed and any(p.verification for p in pending):
                        rejection += ' The passing check does not match the required method, expectation or scoped target: ' + passed.model_dump_json(
                            include={'assertion', 'expected', 'target'})
                elif not self._current_goal_verified():
                    rejection = self._goal_verification_rejection()
                elif not known or any(ref not in known for ref in outcome.evidence_ids):
                    rejection = "Unknown or unverified evidence reference; cite runtime evidence IDs."
                if not rejection and self.documents and self.documents.current_step:
                    try:
                        self.documents.verified(state, self._matches_goal_check, accepted=self._accepted_goal_assertions())
                    except ValueError as error:
                        rejection = str(error)
                if rejection:
                    outcome.status = "continue"
                    outcome.reason = rejection
            state.last_error = rejection or (failures[-1].error if failures else
                'Step browser-call budget reached; continue from the current state.' if budget_yield else None)
            if any(RECOVERY_POLICY.get(record.code or "") == "reconcile_tab" for record in failures):
                # Tab liveness is controller state. Reconcile it once instead
                # of asking the model to guess or repeat an internal tab id.
                self._reconcile_bound_tab()
            for item in outcome.evidence:
                state.add_evidence("navigator", item, tab=self.tab, verified=False)
            repeated_check = self._repeated_failed_check(browser_state, gate.records, failures)
            if gate.records:
                self._update_progress(browser_state, gate.records)
            repeated_rejection = False
            if rejection and outcome.status not in {'failed', 'ask_user'} and not budget_yield:
                attempts = state.progress_by_tab.setdefault(tab or '', {})
                signature = (state.current_goal_id,
                             state.current_verification.model_dump_json() if state.current_verification else None, tuple(
                    (key, p.sequence, p.verification.model_dump_json() if p.verification else None)
                    for key, p in state.pending_changes.items()), tuple(
                    sorted({(a.assertion, a.target or '', repr(a.expected), a.passed)
                            for a in state.assertions if a.goal_id == state.current_goal_id})))
                repeated_rejection = completion_proposed and attempts.get('verification_rejection') == signature
                if completion_proposed:
                    attempts['verification_rejection'] = signature
                state.last_error = outcome.reason = rejection + ' Check the assertion method, target identity and scope; repeating the same assertion cannot resolve a mismatch.'
            elif outcome.status in {'goal_reached', 'task_complete'}:
                state.progress_by_tab.setdefault(tab or '', {}).pop('verification_rejection', None)
            self._emit(
                "navigator_step",
                **outcome.model_dump(),
                actions=gate.actions_used,
                interrupted=gate.interrupt_reason,
                duration_ms=state.step_ms,
                browser_ms=gate.browser_ms,
            )

            if gate.stopped == "cancelled":
                raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
            if gate.stopped == "paused":
                raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))
            if outcome.stop_condition_met:
                raise _Stop(self._result('failed', reason=outcome.reason,
                                        answer=outcome.final_answer or outcome.summary))
            if (budget_boundary and gate.budget_scope == 'run'
                    and outcome.status not in {'task_complete', 'goal_reached'}):
                raise _Stop(self._result('limit_reached',
                    reason='The remaining browser-call budget cannot fit the next operation and its required verification.',
                    recoverable=True))
            if repeated_rejection:
                raise _Stop(self._result('failed', reason='VERIFICATION_MISMATCH: The same completion check remains unresolved after recovery. ' + rejection, recoverable=True))
            incompatible = next((r for r in failures if r.code == 'BRIDGE_PROTOCOL_MISMATCH'), None)
            if incompatible is not None:
                raise _Stop(self._result('failed', reason=incompatible.error or incompatible.code, recoverable=True))
            if repeated_check is not None:
                raise _Stop(self._result("failed", reason=self._verification_failure_reason(repeated_check), recoverable=True))

            if failures:
                state.counters.consecutive_failures += 1
            else:
                if any(record.recovered for record in gate.records):
                    state.counters.consecutive_failures = 0
                state.repair_attempted = False

            if outcome.status != "ask_user":
                state.clarification_recovery = False
            if state.direct_execution:
                if outcome.status == "task_complete":
                    verdict = verify_completion(state, state.last_browser_state)
                    action_required = gui_action_request(self._current_request())
                    field_verified = any(check.passed and not check.superseded_by
                        and check.goal_id == state.current_goal_id and check.match not in {'contains', 'regex'}
                        and (check.assertion in {'value', 'checked'} or (check.assertion == 'text'
                             and any(r.evidence_id == check.evidence_id and self._reported_value_property(r) is False
                                     for r in state.actions))) for check in state.assertions)
                    if action_required and not state.counters.mutation_count and not field_verified:
                        verdict.valid = False
                        verdict.reasons.append("Perform the requested browser action before claiming completion.")
                    if (verdict.valid and outcome.final_answer and
                            len(outcome.final_answer) <= self.config.run.final_answer_max_chars):
                        raise _Stop(self._result("completed", answer=outcome.final_answer,
                                                 evidence=outcome.evidence, reason=outcome.reason))
                    state.last_error = "Completion rejected: " + (" ".join(verdict.reasons) or "Provide a concise final_answer.")
                    continue
                if outcome.status == "ask_user" and outcome.blocker == "targeting":
                    if state.clarification_recovery:
                        raise _Stop(self._result("failed", reason=outcome.reason, answer=outcome.final_answer))
                    state.clarification_recovery = True
                    if tab and self.config.run.visual_mode == "auto":
                        state.visual_tabs.add(tab)
                    state.last_error = "Repair the target using fresh scoped evidence; the user has already provided the task."
                    continue
                if outcome.status == "ask_user":
                    raise _Stop(self._result("needs_user", reason=outcome.reason, answer=outcome.final_answer))
                if outcome.status == "failed":
                    raise _Stop(self._result("failed", reason=outcome.reason, answer=outcome.final_answer or outcome.summary))
                if outcome.status in {"needs_plan", "blocked", "goal_reached"} or gate.non_retryable:
                    return
                if state.counters.no_progress_steps >= 2:
                    return
                # A fresh local observation repairs stale targets and checks
                # unknown outcomes without another planner request.
                continue

            # Back to the planner immediately.
            if outcome.status != "continue":
                if outcome.status == "goal_reached" and state.current_goal:
                    state.complete_goal(f"{state.current_goal} -> {outcome.summary}")
                    state.current_goal = None
                    state.current_goal_id = None
                return
            if rejection:
                return
            if gate.unknown_outcome:
                state.last_error = (
                    f"{state.last_error or 'An action had an unknown outcome.'} "
                    "It was not repeated; verify the page before acting again."
                )
                return
            if gate.non_retryable:
                return
            if failures:
                if state.repair_attempted:
                    return  # one repair attempt only
                state.repair_attempted = True
            if state.counters.no_progress_steps >= 2:
                state.last_error = "NO_PROGRESS: Two equivalent attempts made no useful change; choose a different strategy."
                return
            interval = self.config.run.planner_interval_steps
            if state.made_progress and not failures:
                interval = max(interval, self.config.run.planner_max_interval_steps)
            if state.counters.navigator_steps_since_plan >= interval:
                return

    # -- planner ---------------------------------------------------------

    async def _plan(self) -> PlanDecision:
        state = self.state
        assert state is not None
        run = self._run_limits()
        context: dict[str, Any] = {
            "task_id": state.task_id,
            "original_request": state.original_request,
            "follow_ups": state.follow_ups,
            "current_request": self._current_request(),
            "reference_context": state.reference_context,
            "requirements": state.requirements.model_dump(mode="json"),
            "current_plan_summary": state.current_plan_summary,
            "current_goal": state.current_goal,
            "success_condition": state.current_success_condition,
            "verification": state.current_verification.model_dump() if state.current_verification else None,
            "completed_goals": state.completed_goal_summaries,
            "latest_navigator_outcome": (
                state.latest_navigator_outcome.model_dump() if state.latest_navigator_outcome else None
            ),
            "recent_action_results": [record.model_dump(exclude={"extracted_data"}) for record in state.actions[-state.max_recent_actions:]],
            "relevant_extracted_data": state.relevant_extracted_data if not state.facts else [],
            "facts": [fact.model_dump() for fact in state.facts],
            "memory_truncated": state.memory_truncated,
            "pending_changes": [change.model_dump() for change in state.pending_changes.values()],
            "pending_uploads": list(state.pending_uploads.values()),
            "evidence": [item.model_dump(exclude={"detail"} if item.kind in {"observation", "extraction"} else set())
                         for item in state.evidence[-16:] if item.verified and not (self.documents and item.kind == 'document')],
            "source_register": list(state.source_register.values()),
            "remaining_work": state.remaining_work,
            "final_answer_max_chars": run.final_answer_max_chars,
            "synthesis_reserve_requests": self._synthesis_reserve(),
            "last_error": state.last_error,
            "bridge_warning": self.browser.capability_warning,
            "unverified_page_changes": state.unverified_mutations,
            "remaining_steps": max(0, run.max_total_steps - state.counters.total_steps),
            "remaining_browser_actions": max(0, run.max_browser_actions - state.counters.browser_actions),
            "remaining_model_requests": max(0, run.max_model_requests - int(state.usage.requests)),
        }
        if self.attachment_session and self.attachment_session.prompt_files():
            context['files'] = self.attachment_session.prompt_files()
        if self.documents:
            context['documents'] = self.documents.context()
            if self._synthesis_reserve() and run.max_model_requests - int(state.usage.requests) <= self._synthesis_reserve():
                state.document_synthesis_only = True
            context['document_synthesis_only'] = state.document_synthesis_only
        context['execution_mode'] = getattr(self, 'execution_mode', 'automatic')
        context['browser_action_required'] = gui_action_request(context['current_request'])
        context['requested_application'] = named_gui(context['current_request'])
        if self.tab is not None and self.tab in self.browser.tabs:
            context["browser"] = {
                "tab": self.tab,
                "url": self.browser.safe_url(self.browser.tabs[self.tab].url),
                "title": self.browser.tabs[self.tab].title,
            }
        self._emit("status", phase="planner_started", steps_since_plan=state.counters.navigator_steps_since_plan)
        started = time.monotonic()
        login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
        if login:
            context['login_credentials_available'] = True
        decision = await self._run_agent(
            self.planner,
            format_as_xml(context, root_tag="task_context"),
            deps=None,
        )
        state.plan_ms = int((time.monotonic() - started) * 1000)
        self._retain_source_notes(decision)
        return decision

    def _retain_source_notes(self, decision: PlanDecision) -> None:
        state = self.state
        assert state is not None
        state.remaining_work = [clip(item, 200) or "" for item in decision.remaining_work]
        state.source_note_errors = []
        for note in decision.source_notes:
            source = next((e for e in state.evidence if e.id == note.evidence_id and e.verified
                           and e.kind in {"observation", "extraction"} and e.source_url), None)
            text = source.detail if source else ""
            try:
                parsed = json.loads(text)
                def strings(value):
                    if isinstance(value, str):
                        return value
                    if isinstance(value, dict):
                        return " ".join(strings(v) for v in value.values())
                    if isinstance(value, list):
                        return " ".join(strings(v) for v in value)
                    return ""
                text = strings(parsed)
            except (ValueError, TypeError):
                pass
            if source is None or " ".join(note.quote.split()) not in " ".join(text.split()):
                state.last_error = f"Source note rejected: {note.evidence_id} does not contain the quoted text. Read the relevant section."
                state.source_note_errors.append(state.last_error)
                continue
            target = "research:" + note.topic.casefold()
            existing = next((f for f in state.facts if f.tool == "source_note" and f.target == target), None)
            notes = json.loads(existing.value) if existing else []
            item = {"dimension": note.dimension, "quote": note.quote, "url": source.source_url,
                    "evidence_id": source.id, "tab": source.tab}
            notes = [n for n in notes if (n["url"], n["dimension"]) != (item["url"], item["dimension"])]
            notes.append(item)
            while len(json.dumps(notes, ensure_ascii=False)) > 3_800:
                notes.pop(0)
                state.memory_truncated = True
            state.retain_fact(TaskFact(value=json.dumps(notes, ensure_ascii=False), target=target,
                evidence_id=source.id, goal_id=source.goal_id, tool="source_note", priority=3))

    # -- navigator -------------------------------------------------------

    def _commit_gate(self, gate: StepGate) -> None:
        """Persist a step gate exactly once, including cancelled runs."""
        with gate.lock:
            if gate.committed:
                return
            if gate.in_flight:
                tool, kwargs, started = gate.in_flight
                gate.record(tool, kwargs, {"ok": False, "error": {"code": "BRIDGE_ERROR",
                    "message": "The run ended while the browser call was in flight; its outcome is unknown. Do not replay it."}},
                    int((time.perf_counter() - started) * 1000))
            state = self.state
            assert state is not None
            state.preflight_ms += gate.preflight_ms
            state.authorization_ms += gate.authorization_ms
            state.assertion_ms += gate.assertion_ms
            state.record_actions(gate.records)
            state.counters.browser_actions += gate.actions_used
            for record, (mutated, verified) in zip(gate.records, gate.effects):
                if not record.sequence:
                    self._register_action(record, mutated)
            if gate.active_tab_changed_to and gate.active_tab_changed_to in self.browser.tabs:
                self.tab = gate.active_tab_changed_to
            gate.committed = True

    def _record_browser_steps(self, outcome):
        state = self.state
        if self.documents:
            return None  # DocumentSession already owns its ordered workflow.
        if outcome.steps and not state.browser_steps:
            state.browser_steps = [BrowserStep(instruction=text[:4000]) for text in outcome.steps]
        evidence = {e.id: e for e in state.evidence if e.verified and e.kind != 'navigator'}
        for update in outcome.completed_steps:
            index = next((i for i, step in enumerate(state.browser_steps) if step.result is None), len(state.browser_steps))
            if 0 < update.step <= len(state.browser_steps):
                previous = state.browser_steps[update.step - 1]
                if previous.result is not None and previous.evidence_ids == update.evidence_ids:
                    continue
            if update.step > index + 1 or update.step > len(state.browser_steps):
                return f'Received step {update.step}; next incomplete step is {index + 1 if index < len(state.browser_steps) else "none"}. Report only completed steps in order.'
            proof = [evidence.get(identifier) for identifier in update.evidence_ids]
            if not proof or any(item is None for item in proof):
                return 'Step results need verified runtime evidence, not an unsupported summary.'
            # This is progress reporting. Pending outcomes are checked once at
            # task completion, not again against every unrelated step update.
            step = state.browser_steps[update.step - 1]
            step.result, step.evidence_ids = update.result, update.evidence_ids
        return None

    def checkpoint_browser(self):
        """Persist bounded task state, never live refs or screenshots."""
        state = self.state
        if not state or self.documents:
            return None
        fields = ('browser_steps', 'pending_changes', 'assertions', 'evidence', 'facts', 'change_history',
                  'current_goal', 'current_goal_id', 'current_verification', 'goal_number',
                  'sequence', 'evidence_number', 'visited_urls', 'successful_actions', 'last_interaction_by_tab',
                  'requirements', 'counters', 'direct_navigation_done', 'bound_application', 'last_edit_by_page')
        saved = state.model_dump(mode='json', include=set(fields))
        saved['model_requests'] = int(state.usage.requests)
        tab = self.browser.tabs.get(state.bound_tab)
        if tab and not tab.closed:
            saved['tab'] = {'id': tab.raw_id, 'url': tab.url, 'title': tab.title}
            saved['alias'] = state.bound_tab
            saved['origins'] = state.authorized_origins.get(state.bound_tab, [])
        saved['tabs'] = {alias: {'id': tab.raw_id, 'url': tab.url, 'title': tab.title}
            for alias in set([state.bound_tab, *state.task_tabs, *state.authorized_origins])
            if alias and (tab := self.browser.tabs.get(alias)) and not tab.closed}
        saved['scopes'] = state.authorized_origins
        gate = getattr(self, '_active_gate', None)
        if gate and gate.in_flight:
            tool, kwargs, _ = gate.in_flight
            command = kwargs.get('command')
            operation = getattr(command, 'action', None) or getattr(command, 'operation', None)
            if tool in {'browser_act', 'browser_visual', 'browser_navigate'}:
                check = gate.action_postcondition
                locator = getattr(check, 'locator', None)
                target = next((getattr(locator, key, None) for key in ('name', 'label', 'selector', 'role') if getattr(locator, key, None)), None)
                verification = GoalCheck(assertion=check.assertion, expected=getattr(check, 'expected', None), target=target) if check else state.current_verification
                saved['uncertain_action'] = {'tab': kwargs.get('tab'), 'operation': operation,
                    'verification': verification.model_dump() if verification else None}
        return saved

    def _restore_browser_checkpoint(self, saved):
        state = self.state
        restored = TaskRunState.model_validate({key: value for key, value in saved.items()
            if key in TaskRunState.model_fields and key not in {'task_id', 'status'}})
        for key in saved:
            if key in TaskRunState.model_fields and key not in {'task_id', 'status'}:
                setattr(state, key, getattr(restored, key))
        state.usage.requests = saved.get('model_requests', 0)
        tabs = saved.get('tabs') or ({saved['alias']: saved['tab']} if saved.get('tab') else {})
        aliases = {old: self.browser._register(tab) for old, tab in tabs.items()}
        state.bound_tab = aliases.get(saved.get('alias'))
        state.task_tabs = list(aliases.values())
        scopes = saved.get('scopes') or {saved.get('alias'): saved.get('origins', [])}
        state.authorized_origins = {aliases[old]: grants for old, grants in scopes.items() if old in aliases}
        for item in [*state.pending_changes.values(), *state.evidence, *state.assertions, *state.facts]:
            if item.tab in aliases:
                item.tab = aliases[item.tab]
        checks = [state.current_verification, *(p.verification for p in state.pending_changes.values())]
        for check in checks:
            if check and check.tab in aliases:
                check.tab = aliases[check.tab]
        state.last_interaction_by_tab = {aliases.get(tab, tab): sequence
                                         for tab, sequence in state.last_interaction_by_tab.items()}
        state.last_error = 'Resumed saved progress. Inspect current state and resolve pending outcomes before repeating a change.'
        if saved.get('uncertain_action'):
            uncertain = saved['uncertain_action']
            if isinstance(uncertain, dict) and state.bound_tab:
                state.pending_changes['restart:uncertain'] = PendingChange(tab=state.bound_tab,
                    goal_id=state.current_goal_id, sequence=state.next_sequence(), verification=uncertain.get('verification'), uncertain=True)
            state.last_error += ' The service stopped during an action; its outcome is unknown. Do not resubmit it.'

    def _authorize_effect(self, tool, operation, detail):
        """CLI and UI use the same task-scope policy; callbacks only supply UX."""
        mandatory = detail.get('mandatory_approval', False)
        login = self._login_intent(detail.get('tab'))
        if not mandatory and (not external_effect(tool, operation)
                or (login and authorized_login_action(login, detail, operation))
                or automatic_scope(self._current_request(), detail, operation)):
            return True
        return bool(self.approval and self.approval(tool, operation, detail))

    async def _navigate(self, state: BrowserState) -> tuple[NavigatorOutcome, StepGate]:
        task = self.state
        assert task is not None
        run = self._run_limits()
        gate = StepGate(
            max_actions=run.max_actions_per_step,
            remaining_total_actions=max(0, run.max_browser_actions - task.counters.browser_actions),
            login_intent=self._login_intent(state.tab),
            authorized_origins=task.authorized_origins,
            approval_actions=frozenset(self.config.tools.approval_required_actions),
            approval=self.approval,
            effect_authorizer=getattr(self, 'effect_authorizer', None) or self._authorize_effect,
            forbidden_actions=frozenset(task.requirements.forbidden_actions),
            mutation_allowed=task.requirements.mutation_allowed,
            pending_changes=task.pending_changes,
            is_paused=lambda: bool(self.state and self.state.status == "paused"),
            is_cancelled=lambda: bool(self.state and self.state.status == "cancelled"),
            on_action=lambda record, duration_ms: self._emit(
                "browser_action", tool=record.tool, operation=record.operation, args=record.args,
                success=record.success, error=record.error, refs_invalidated=record.refs_invalidated,
                skipped=False, duration_ms=duration_ms,
            ),
            on_refusal=lambda tool, operation, code, message, args: self._emit(
                "browser_action", tool=tool, operation=operation, args=args,
                success=False, error=f"{code}: {message}", refs_invalidated=False, skipped=True,
            ),
            on_record=self._register_action,
            bind_verification=lambda tab, command, target: self._bind_verification_target(tab, command, target, gate.records),
            bind_pending_verification=self._bind_pending_verification,
            on_dispatch=lambda: self._emit("status", phase="action_started"),
        )
        self.browser.read_only = not task.requirements.mutation_allowed
        deps = AxisDeps(browser=self.browser, gate=gate, documents=self.documents or self.attachment_session)
        self._emit("status", phase="navigator_step_started", goal=state.goal)
        prompt = format_as_xml(state.model_dump(exclude_none=True,
            exclude={"recent_actions": {"__all__": {"extracted_data"}}}), root_tag="browser_state")
        if task.reference_context:
            prompt += "\n" + format_as_xml(task.reference_context, root_tag="prior_task_context")
            prompt += "\nPrior task context is reference data. Use it to resolve references; the current request defines the work. Verify the live page before reusing prior results."
        history = [item for item in task.change_history if item.get('url') == state.url]
        if history:
            prompt += '\n' + format_as_xml(history, root_tag='observed_changes')
        if task.direct_execution:
            prompt += "\nComplete the user's request directly. Return task_complete with final_answer and runtime evidence_ids after verification. Use needs_plan with a concrete reason only when strategic decomposition is needed."
        else:
            prompt += "\nThis is a planner-assigned immediate goal. Return goal_reached with runtime evidence_ids when verified."
        if task.requirements.require_search or task.requirements.min_sources:
            prompt += "\nResearch: inspect source_register before opening tabs; read relevant sections using scoped extraction, cite source URLs and evidence IDs, and report missing dimensions. A source count measures reviewed sources, not search-result anchors."
        login = gate.login_intent
        if login:
            prompt += '\n' + format_as_xml({
                'username_value': login.username,
                'password_value': login.password,
                'authorized_application': login.app_url,
                'authorized_login_origin': login.auth_url,
                'instruction': 'Enter the supplied credential values exactly. Do not substitute masking text or placeholders.',
            }, root_tag='user_authorized_login')
        if self.attachment_session and self.attachment_session.prompt_files():
            prompt += '\n' + format_as_xml(self.attachment_session.prompt_files(), root_tag='task_files')
        if self.documents:
            prompt += '\n' + format_as_xml(self.documents.context(), root_tag='task_documents')
        model_prompt = [prompt, self.browser.visual_content(state.screenshot)] if state.screenshot else prompt
        started = time.monotonic()
        self._active_gate = gate
        try:
            try:
                outcome = await self._run_agent(self.navigator, model_prompt, deps=deps)
            except StepBudgetYield:
                outcome = NavigatorOutcome(status='continue', reason='Browser call budget reached; resume from runtime evidence.')
            except _VisionUnavailable:
                task.vision_disabled = True
                task.visual_tabs.clear()
                self.browser.visual_enabled = False
                self.browser.invalidate_visual()
                self._sync_navigator_tools(visual=False)
                task.last_error = "The configured provider does not support image input; continuing with text."
                self._emit("status", phase="vision_unavailable", reason=task.last_error)
                outcome = await self._run_agent(self.navigator, prompt + "\n" + task.last_error, deps=deps)
        except BaseException:
            self._active_gate = None
            task.browser_ms += gate.browser_ms
            task.tool_model_ms += gate.tool_ms
            task.step_ms = int((time.monotonic() - started) * 1000)
            self._commit_gate(gate)
            raise
        task.browser_ms += gate.browser_ms
        self._active_gate = None
        task.tool_model_ms += gate.tool_ms
        task.step_ms = int((time.monotonic() - started) * 1000)
        return outcome, gate

    async def _run_agent(self, agent: Agent[Any, Any], prompt: Any, *, deps: Any) -> Any:
        """One model request, with the shared usage record and limits.

        Pause/cancel are checked here, immediately before the request.
        """
        state = self.state
        assert state is not None
        if state.status == "cancelled":
            raise _Stop(self._result("cancelled", reason="The user cancelled the run."))
        if state.status == "paused":
            raise _Stop(self._result("paused", reason="The run is paused; state is preserved."))
        started = time.monotonic()
        try:
            result = await self._model_request(agent, prompt, deps)
        except UsageLimitExceeded as exc:
            raise _Stop(self._result("limit_reached", reason=clip(str(exc)) or "Usage limit exceeded.")) from exc
        except ModelHTTPError as exc:
            status = getattr(exc, "status_code", None)
            if (isinstance(prompt, list) and deps is not None and deps.gate.actions_used == 0
                    and status in {400, 422} and re.search(
                        r"(?:image|vision|multimodal).*?(?:not supported|unsupported|not allowed)|"
                        r"(?:does not support|unsupported|cannot accept).*?(?:image|vision|multimodal)",
                        str(exc.body), re.IGNORECASE | re.DOTALL)):
                raise _VisionUnavailable() from exc
            reason = (
                "Provider authentication failed."
                if status in (401, 403)
                else f"The model provider returned an error ({status})."
            )
            transient = status in {429, 500, 502, 503, 504}
            raise _Stop(self._result("failed", reason=reason, recoverable=transient and self.documents is not None)) from exc
        except RunCancelled:
            if state.status != "paused":
                state.status = "cancelled"
            raise
        finally:
            state.model_ms += int((time.monotonic() - started) * 1000)
        return result.output

    async def _model_request(self, agent, prompt, deps):
        """Retry a model request only while no browser tool has executed.

        Client retries stay disabled: one layer owns backoff, accounting,
        cancellation, and public retry feedback. Never restart an agent run
        which may already have changed a website.
        """
        import random
        state = self.state
        for attempt in range(self.config.run.provider_retries + 1):
            before = int(state.usage.requests)
            try:
                return await agent.run(prompt, deps=deps, usage=state.usage,
                    usage_limits=UsageLimits(request_limit=self._run_limits().max_model_requests),
                    cancellation_token=state.cancellation_token)
            except ModelHTTPError as error:
                state.usage.requests = max(int(state.usage.requests), before + 1)
                if (error.status_code not in {429, 500, 502, 503, 504}
                        or attempt >= self.config.run.provider_retries
                        or (deps is not None and (deps.gate.actions_used or deps.gate.records))
                        or int(state.usage.requests) >= self._run_limits().max_model_requests):
                    raise
                delay = self.config.run.provider_retry_delay * 2**attempt
                delay += random.uniform(0, delay * .25)
                response = getattr(error.__cause__, 'response', None)
                retry_after = getattr(response, 'headers', {}).get('retry-after')
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except (ValueError, TypeError):
                        from email.utils import parsedate_to_datetime
                        try:
                            delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                delay = min(30.0, max(0.0, delay))
                self._emit('status', phase='provider_retry', attempt=attempt+1,
                           max_attempts=self.config.run.provider_retries, delay_seconds=round(delay, 1))
                if self.documents:
                    self.documents.checkpoint(state, self.config.run.model_dump())
                deadline = time.monotonic() + delay
                while True:
                    if state.status in {'paused', 'cancelled'}:
                        raise _Stop(self._result(state.status, reason='Provider retry interrupted by the task control.'))
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(.1, remaining))

    # -- browser ---------------------------------------------------------

    def _reconcile_bound_tab(self) -> list[dict[str, Any]]:
        """Refresh tab liveness while retaining a valid task-scoped alias."""
        state = self.state
        assert state is not None
        previous_alias = state.bound_tab
        previous = self.browser.tabs.get(previous_alias) if previous_alias else None
        previous_identity = (
            self.browser.safe_url(previous.url), previous.title
        ) if previous is not None else (None, None)
        ctx = browser_context(self.browser)
        listed = bt.browser_tabs(ctx, bt.ListTabs(operation="list"))
        self._emit(
            "status", phase="tab_list", success=listed["ok"],
            tabs=len(listed["data"]["tabs"]) if listed["ok"] else None,
            error=self._envelope_error(listed),
        )
        if not listed["ok"]:
            raise _Stop(self._result(
                "failed", reason=self._envelope_error(listed) or "The browser bridge failed to list tabs.",
            ))
        tabs = listed["data"]["tabs"]
        live = {item["tab"] for item in tabs}
        if state.bound_tab not in live:
            state.bound_tab = None
            previous_url, previous_title = previous_identity
            matches = [
                item for item in tabs
                if previous_url and item.get("url") == previous_url
                and (not previous_title or item.get("title") == previous_title)
            ]
            if len(matches) == 1:
                # Chrome may replace a tab underneath us while retaining its
                # public identity. Rebind to the unique alias; raw ids never
                # leave BrowserRuntime.
                self.tab = matches[0]["tab"]
        return tabs

    def _ensure_tab(self) -> str:
        """Bind (once) to a live, observable tab. Never restarts a browser
        that is fine.

        A schemeless/internal page (``chrome://...``, ``about:blank``) cannot
        host a content script even though our firewall allows navigating
        there — the bridge's own extension-side host-permission check refuses
        to read it. Whole-browser access can land on one of those as the
        "focused" tab, or such a tab can already be open from an earlier run,
        so any unobservable candidate is skipped in favor of an ordinary tab,
        falling back to a freshly created one already pointed at a real page.
        """
        state = self.state
        assert state is not None
        ctx = browser_context(self.browser)
        tabs = self._reconcile_bound_tab()
        application = next((name for request in reversed([state.original_request, *state.follow_ups])
                            if (name := named_gui(request))), None)
        if self.tab is not None and (not application or state.bound_application == application):
            cached = self.browser.tabs.get(self.tab)
            if cached is not None and not cached.closed and not _unobservable(cached.url):
                initial_origin = origin(cached.url)
                if initial_origin:
                    state.authorized_origins.setdefault(self.tab, [initial_origin])
                return self.tab

        def observable(candidates: list[dict[str, Any]]) -> str | None:
            return next((t["tab"] for t in candidates if not _unobservable(t.get("url"))), None)

        request = self._tab_request()
        chosen = None
        aliases = list(dict.fromkeys(re.findall(r"\btab_\d+\b", request)))
        if len(aliases) == 1:
            chosen = next((t['tab'] for t in tabs if t['tab'] == aliases[0]
                           and not _unobservable(t.get('url'))), None)
            if chosen is None:
                raise _Stop(self._result('needs_user', reason='The requested tab is closed or cannot be read.',
                                        answer='Select an open, readable tab.'))
        focused = None
        if self.config.browser.initial_tab == "focused" or _CURRENT_TAB.search(request):
            try:
                focused = self.browser.focused_tab()
            except ToolFault as fault:
                raise _Stop(self._result('failed', reason=f'{fault.code}: {fault}')) from fault
        urls = extract_requirements(request).literal_urls
        if len(urls) == 1 and not _CURRENT_TAB.search(request):
            matches = [t for t in tabs if origin(t.get('url')) == origin(urls[0])
                       and not _unobservable(t.get('url'))]
            chosen = chosen or next((t['tab'] for t in matches if t['tab'] == focused), None) or observable(matches)
        if focused and not _unobservable(self.browser.tabs[focused].url):
            chosen = chosen or focused
        if _CURRENT_TAB.search(request) and chosen is None:
            raise _Stop(self._result('needs_user', reason='The focused tab cannot be read.',
                                    answer='Open the requested page in the focused tab.'))
        if chosen is None:
            chosen = observable(tabs) or (tabs[0]["tab"] if tabs else None)
        needs_new_tab = chosen is None or (
            self.browser.tabs.get(chosen) and _unobservable(self.browser.tabs[chosen].url)
        )
        if needs_new_tab:
            if (application or aliases) and not self._direct_url():
                raise _Stop(self._result('needs_user', reason='No readable tab is open for the requested application.',
                                        answer='Open the requested application in a browser tab.'))
            created = bt.browser_tabs(ctx, bt.CreateTab(operation="create", url=self._direct_url() or _FALLBACK_TAB_URL))
            self._emit(
                "status", phase="tab_create", success=created["ok"],
                tab=created.get("tab"), error=self._envelope_error(created),
            )
            if not created["ok"]:
                raise _Stop(self._result(
                    "failed",
                    reason=self._envelope_error(created) or "The browser bridge refused to open a new tab.",
                ))
            chosen = created["tab"]
        else:
            self._emit("status", phase="tab_selected", tab=chosen, url=self.browser.tabs[chosen].url)
        self.tab = chosen
        state.bound_application = application
        initial_origin = origin(self.browser.tabs[chosen].url)
        if initial_origin:
            state.authorized_origins.setdefault(chosen, [initial_origin])
        return chosen

    @staticmethod
    def _envelope_error(envelope: dict[str, Any]) -> str | None:
        """The full ``code: message`` from a browser_tools envelope's error, so
        a browser-layer failure is never reported to the user or the debug
        trace as an opaque, code-free string."""
        error = envelope.get("error")
        if not isinstance(error, dict):
            return None
        return clip(f"{error.get('code')}: {error.get('message')}")

    def _tab_summaries(self) -> list[TabSummary]:
        state = self.state
        assert state is not None
        candidates = list(dict.fromkeys(([self.tab] if self.tab else []) + list(self.browser.tabs)))
        return [
            TabSummary(**self.browser.public_tab(alias))
            for alias in candidates
            if alias in self.browser.tabs and not self.browser.tabs[alias].closed
            and not _unobservable(self.browser.tabs[alias].url)
        ][:64]

    def _tab_request(self) -> str:
        state = self.state
        assert state is not None
        requests = [state.original_request, *state.follow_ups]
        return next((request for request in reversed(requests)
                     if _CURRENT_TAB.search(request) or named_gui(request)
                     or extract_requirements(request).literal_urls
                     or re.search(r"\btab_\d+\b", request)), state.original_request)

    def _current_request(self) -> str:
        state = self.state
        assert state is not None
        if not state.follow_ups:
            return state.original_request
        return state.original_request + '\n\nUser updates to this task (in order):\n' + '\n'.join(state.follow_ups)

    def _preserve_literal_urls(self, goal: str | None) -> str | None:
        state = self.state
        assert state is not None
        if not goal or not state.requirements.literal_urls:
            return goal
        missing = [url for url in state.requirements.literal_urls if url not in goal]
        if len(state.requirements.literal_urls) == 1 and missing:
            return clip(f"{goal} Target URL: {missing[0]}")
        return goal

    def _direct_url(self) -> str | None:
        state = self.state
        assert state is not None
        request = self._tab_request()
        if _CURRENT_TAB.search(request):
            return None
        urls = extract_requirements(request).literal_urls
        if state.direct_navigation_done or len(urls) != 1:
            return None
        if _CURRENT_PAGE_DEPENDENCY.search(self._current_request()):
            return None
        return urls[0]

    def _login_intent(self, tab: str):
        state = self.state
        assert state is not None
        login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
        if login and state.login_auth_tab == tab and state.login_auth_url:
            login = replace(login, auth_url=state.login_auth_url)
        return login

    def _maybe_direct_navigate(self, tab: str) -> None:
        state = self.state
        assert state is not None
        url = self._direct_url()
        if not url:
            return
        current = self.browser.tabs.get(tab)
        if current and current.url and current.url.rstrip("/") == url.rstrip("/"):
            state.direct_navigation_done = True
            return
        command = bt.Open(operation='open', url=url)
        gate = StepGate(max_actions=1,
                        remaining_total_actions=max(0, self._run_limits().max_browser_actions-state.counters.browser_actions),
                        login_intent=parse_login_intent('\n'.join([state.original_request, *state.follow_ups])),
                        approval_actions=frozenset(self.config.tools.approval_required_actions), approval=self.approval,
                        effect_authorizer=getattr(self, 'effect_authorizer', None),
                        forbidden_actions=frozenset(state.requirements.forbidden_actions),
                        is_paused=lambda: state.status == 'paused', is_cancelled=lambda: state.status == 'cancelled')
        refusal = gate.check('browser_navigate', {'tab':tab,'command':command},
                             {'tab':tab,'target':url,'current_url':current.url if current else None,'expected_effect':'navigate'})
        if refusal:
            raise _Stop(self._result(gate.stopped or 'needs_user', reason=self._envelope_error(refusal)))
        navigation_started = time.monotonic()
        result = bt.browser_navigate(
            browser_context(self.browser), tab, command,
        )
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        record = ActionRecord(
            tool="browser_navigate", tab=tab, operation="open", args=f"tab={tab!r}, url={url!r}",
            executed=True, execution_success=bool(result.get("ok")), semantic_success=None,
            code=error.get("code"), message=error.get("message"), refs_invalidated=bool(result.get("ok")),
            duration_ms=int((time.monotonic() - navigation_started) * 1000),
            meaningful_change=bool(result.get("ok")), retain_in_memory=True,
            extracted_data=f"navigated to {url!r}" if result.get("ok") else None,
        )
        state.record_actions([record])
        state.browser_ms += record.duration_ms
        state.counters.browser_actions += 1
        self._register_action(record, record.success)
        if not record.success:
            state.last_error = record.error
            return
        state.direct_navigation_done = True
        login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
        reached = result.get('data', {}).get('url')
        if login and reached and urlsplit(reached).scheme in {'http', 'https'}:
            state.login_auth_url = reached
            state.login_auth_tab = tab
            state.login_auth_host = urlsplit(reached).hostname

    def _arm_diagnostics(self, tab: str) -> None:
        state = self.state
        assert state is not None
        if not diagnostics_requested(state.requirements) or tab in state.diagnostics_armed_tabs:
            return
        raw = self.browser.tabs.get(tab)
        if raw is None:
            return
        armed = True
        for required, method in (
            (state.requirements.require_console_check, "console.read"),
            (state.requirements.require_network_check, "network.read"),
        ):
            if required:
                try:
                    self.browser._rpc(method, {"tabId": raw.raw_id, "limit": 1}, tab=tab)
                except ToolFault as fault:
                    state.last_error = f"Could not arm {method}: {fault.code}."
                    armed = False
        if armed:
            state.diagnostics_armed_tabs.add(tab)

    async def _recover_stalled_load(self, tab: str) -> bool:
        """Try one reload, then one fresh entry from the user's URL."""
        state = self.state
        assert state is not None
        attempt = state.load_recoveries.get(tab, 0)
        if attempt >= 2:
            return False
        urls = extract_requirements(self._current_request()).literal_urls or state.requirements.literal_urls
        if attempt == 1 and len(urls) != 1:
            return False
        command = (bt.History(operation="reload") if attempt == 0
                   else bt.Open(operation="open", url=urls[0]))
        state.load_recoveries[tab] = attempt + 1
        current = self.browser.tabs.get(tab)
        target = urls[0] if attempt == 1 else current.url if current else None
        gate = StepGate(
            max_actions=1,
            remaining_total_actions=max(0, self._run_limits().max_browser_actions - state.counters.browser_actions),
            login_intent=parse_login_intent('\n'.join([state.original_request, *state.follow_ups])),
            approval_actions=frozenset(self.config.tools.approval_required_actions), approval=self.approval,
            effect_authorizer=getattr(self, 'effect_authorizer', None),
            forbidden_actions=frozenset(state.requirements.forbidden_actions),
            is_paused=lambda: state.status == 'paused', is_cancelled=lambda: state.status == 'cancelled',
        )
        refusal = gate.check('browser_navigate', {'tab': tab, 'command': command},
                             {'tab': tab, 'target': target, 'current_url': current.url if current else None,
                              'expected_effect': 'navigate'})
        if refusal:
            raise _Stop(self._result(gate.stopped or 'needs_user', reason=self._envelope_error(refusal)))
        started = time.monotonic()
        error: dict[str, Any] = {}
        try:
            params: dict[str, Any] = {'tabId': self.browser.tab(tab).raw_id, 'wait': False}
            if attempt == 1:
                params['url'] = urls[0]
            result = await asyncio.to_thread(
                self.browser._rpc, 'page.reload' if attempt == 0 else 'page.navigate',
                params, tab=tab, url=urls[0] if attempt == 1 else None,
            ) or {}
            raw_tab = result.get('tab') if isinstance(result.get('tab'), dict) else {}
            current = self.browser.tab(tab)
            current.url = raw_tab.get('url', urls[0] if attempt == 1 else current.url)
            current.title = raw_tab.get('title', current.title)
            self.browser.invalidate(tab)
        except ToolFault as fault:
            error = {'code': fault.code, 'message': str(fault)}
        record = ActionRecord(
            tool='browser_navigate', tab=tab, operation=command.operation,
            args=f"tab={tab!r}, url={self.browser.safe_url(target)!r}", executed=True,
            execution_success=not error, code=error.get('code'), message=error.get('message'),
            refs_invalidated=not error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        state.record_actions([record])
        state.browser_ms += record.duration_ms
        state.counters.browser_actions += 1
        self._register_action(record, False)
        self._emit('status', phase='load_recovery', tab=tab, operation=command.operation,
                   attempt=attempt + 1, success=record.success, error=record.error)
        if not record.success:
            state.last_error = record.error
        return record.success

    async def _browser_state(self) -> BrowserState:
        """A fresh, bounded observation before every navigator step.

        Calls ``BrowserRuntime.observe`` directly — the same method the
        ``browser_observe`` tool calls — so observation logic exists once.
        """
        state = self.state
        assert state is not None
        run = self._run_limits()
        tab = self._ensure_tab()
        self._arm_diagnostics(tab)
        await asyncio.to_thread(self._maybe_direct_navigate, tab)
        if self.config.tools.debug_recording and not state.debug_telemetry_started:
            self.browser.start_debug_telemetry(tab)
            state.debug_telemetry_started = bool(self.browser.debug_recording or self.browser.debug_trace)

        observation: dict[str, Any] | None = None
        last: ToolFault | None = None
        observe_started = time.perf_counter()
        reconciled = False
        for _ in range(4):  # tab reconciliation and at most two load repairs
            try:
                observation = await asyncio.to_thread(self.browser.observe, tab, run.observation_mode, run.observation_max_nodes)
                break
            except ToolFault as fault:
                last = fault
                if RECOVERY_POLICY.get(fault.code) == "reconcile_tab" and not reconciled:
                    reconciled = True
                    self.tab = None
                    tab = self._ensure_tab()
                    self._arm_diagnostics(tab)
                    continue
                if fault.code == 'PAGE_WAIT_FOR_LOAD_TIMEOUT' and await self._recover_stalled_load(tab):
                    continue
                break
        observe_ms = int((time.perf_counter() - observe_started) * 1000)
        state.browser_ms += observe_ms
        self._emit(
            "status", phase="observed", tab=tab, success=observation is not None,
            duration_ms=observe_ms,
        )
        if observation is None:
            reason = clip(str(last)) if last else "The browser could not be observed."
            raise _Stop(self._result("failed", reason=reason or "The browser could not be observed."))
        want_visual = (tab in state.visual_tabs or run.include_screenshot or self.config.tools.visual)
        want_visual = want_visual and run.visual_mode == "auto" and not state.vision_disabled
        self.browser.visual_enabled = want_visual
        self._sync_navigator_tools(visual=want_visual)
        visual = self._screenshot(tab) if want_visual else None
        browser_state = BrowserState(
            tab=tab,
            url=observation.get("url"),
            title=observation.get("title"),
            tabs=self._tab_summaries(),
            scroll=observation.get("scroll"),
            snapshot=observation.get("snapshot", ""),
            truncated=bool(observation.get("truncated")) or len(observation.get("snapshot", "")) > 12_000,
            screenshot=visual.get("screenshot") if visual else None,
            visual=visual,
            refs_fresh=tab in self.browser.observations,
            unverified_page_changes=state.unverified_mutations,
            site_pattern=observation.get("sitePattern"),
            runtime_warning=observation.get("runtimeWarning"),
            recent_actions=state.actions[-4:],
            goal=state.current_goal,
            success_condition=state.current_success_condition,
            verification=state.current_verification,
            remaining_steps=max(0, run.max_total_steps - state.counters.total_steps),
            remaining_actions=min(
                run.max_actions_per_step, max(0, run.max_browser_actions - state.counters.browser_actions)
            ),
            note=state.last_error,
            requirements=state.requirements,
            goal_id=state.current_goal_id,
            facts=sorted(state.facts, key=lambda f: (
                f.tool == "source_note", f.goal_id == state.current_goal_id, f.tab == tab, f.priority), reverse=True)[:6],
            source_register=list(state.source_register.values()),
            remaining_work=state.remaining_work,
            remaining_model_requests=max(0, run.max_model_requests - int(state.usage.requests)),
            memory_truncated=state.memory_truncated,
            pending_changes=list(state.pending_changes.values()),
            browser_steps=state.browser_steps,
            verified_checks=self._verified_checks(),
        )
        state.last_browser_state = browser_state
        login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
        if (login and state.direct_navigation_done and not state.login_auth_host
                and login_destination_matches(login, browser_state.url or '')):
            state.login_auth_host = urlsplit(browser_state.url or '').hostname
        self._record_search(browser_state.url)
        evidence = state.add_evidence("observation", f"Title: {browser_state.title or ''}\n{browser_state.snapshot}", tab=tab, source_url=browser_state.url)
        browser_state.evidence_id = evidence.id
        self._remember_fields(tab, observation, evidence.id)
        state.retain_fact(TaskFact(value=evidence.detail, tab=tab, source_url=browser_state.url,
                                   goal_id=state.current_goal_id, evidence_id=evidence.id,
                                   target="page", priority=0))
        self._verify_navigation(tab, browser_state.url, evidence.sequence)
        browser_state.unverified_page_changes = state.unverified_mutations
        browser_state.pending_changes = list(state.pending_changes.values())
        browser_state.memory_truncated = state.memory_truncated
        return browser_state

    def _register_action(self, record: ActionRecord, mutated: bool) -> None:
        state = self.state
        assert state is not None
        if mutated and record.executed and state.first_action_ms is None:
            state.first_action_ms = max(0, self._elapsed_ms() - record.duration_ms)
        if state.repair_attempted and record.executed:
            state.counters.recovery_actions += 1
        record.sequence = state.next_sequence()
        record.goal_id = state.current_goal_id
        if record.tab and record.invalidates_verification:
            state.last_interaction_by_tab[record.tab] = record.sequence
        if record.tool == "browser_downloads" and record.tab is None:
            record.tab = state.bound_tab
        if record.target:
            reference = re.search(r'"ref"\s*:\s*"(ref_\d+)"', record.target)
            if reference:
                record.target = self.browser.describe_ref(reference[1]) or record.target
        tab = self.browser.tabs.get(record.tab) if record.tab else None
        record.source_url = self.browser.safe_url(tab.url) if tab else None
        field_edit = record.operation in {'fill', 'select', 'check', 'uncheck', 'type_sequentially'}
        field_click = record.operation == 'click' and re.search(
            r'\b(?:combobox|listbox|option|checkbox|switch|radio|data-row|header-row|row)\b', record.target or '')
        if mutated and record.tool == 'browser_act' and record.tab and (field_edit or field_click):
            observed = state.field_observations.get(record.tab)
            if (observed and observed['url'] == record.source_url
                    and not any(item['url'] == observed['url'] for item in state.change_history)
                    and len(state.change_history) < 8):
                state.change_history.append({'url': observed['url'], 'before': observed['fields'],
                    'before_evidence_id': observed['evidence_id'], 'before_complete': observed['complete'],
                    'after': [], 'after_evidence_id': None})
        if record.success:
            if record.tool == 'browser_tabs' and record.operation in {'activate', 'create'} and tab:
                initial_origin = origin(tab.url)
                if initial_origin:
                    state.authorized_origins.setdefault(record.tab, [initial_origin])
            login = self._login_intent(record.tab) if record.tab else None
            changed = record.result_data.get('whatChanged') or {}
            source = changed.get('fromUrl') or record.source_url
            if (login and record.tool == 'browser_act' and record.operation == 'click'
                    and changed.get('toUrl') and authorized_login_action(login, {
                        'current_url': source, 'target_type': 'locator', 'target': record.target,
                        'arguments': {'command': {'value': record.expected_value}}}, 'click')
                    and re.match(r'^link\b', record.target or '', re.I)):
                state.login_auth_url = changed['toUrl']
                state.login_auth_host = urlsplit(changed['toUrl']).hostname
                state.login_auth_tab = record.tab
            state.successful_actions += 1
            if record.tab and record.tab not in state.task_tabs:
                state.task_tabs = (state.task_tabs + [record.tab])[-128:]
        if mutated and record.tab:
            target_role = (record.target or '').split(' ', 1)[0]
            try:
                target_role = json.loads(record.target or '{}').get('locator', {}).get('role', target_role)
            except (ValueError, TypeError, AttributeError):
                pass
            changed = record.result_data.get('whatChanged') or {}
            # HTML controls can lack an ARIA role. Use their observed navigation
            # container, never a control name or DOM-change flag, to recognize
            # navigation. A save can rerender or redirect too.
            context = re.search(r' context="([^"]*)', record.target or '')
            navigation_context = bool(context and re.search(
                r'(?:^| > )(?:navigation|menubar|tablist)\b', context[1]))
            contextual_navigation = navigation_context and target_role in {'a', 'link', 'menuitem', 'tab'}
            route_change = (record.operation == 'click' and target_role in {'menuitem', 'tab'}
                            and (changed.get('urlChanged') or changed.get('navigated')))
            navigation = (record.success and not record.postcondition
                          and (record.tool == 'browser_navigate' or route_change or (
                              record.operation == 'click' and (contextual_navigation or (
                                  not state.current_verification and target_role in {'link', 'menuitem', 'tab'})))))
            key = f"{record.goal_id}:{record.tab}"
            if navigation:
                key += ":navigation"
            if record.operation in {"fill", "check", "uncheck", "select"} and not self._search_target(record.target):
                key += ":" + (record.target or record.operation)
            login = self._login_intent(record.tab)
            login_stage = None
            if record.success and record.tool == 'browser_act' and login and authorized_login_action(login, {
                    'current_url': record.source_url, 'target_type': 'locator',
                    'target': record.target, 'arguments': {'command': {'value': record.expected_value}}},
                    record.operation or ''):
                login_stage = ('submit' if record.operation == 'click' else 'input') if not re.match(r'^link\b', record.target or '', re.I) else None
                if login_stage == 'submit':
                    navigation = False  # Authentication needs its positive result check.
                    key += ':login-submit'
            previous = state.pending_changes.get(key)
            search_query = None
            search_host = None
            search_submitted = False
            engine = self._search_location(record.source_url)
            if record.success and record.tool == "browser_act" and (not previous or previous.search_query is not None):
                if record.operation == "fill" and isinstance(record.expected_value, str) and engine and self._search_target(record.target):
                    search_query, search_host = record.expected_value, engine[0]
                elif (record.operation == "press" and (record.key or "").casefold() == "enter" and previous
                      and previous.search_query is not None and (record.target == "press" or self._search_target(record.target))):
                    search_query, search_host = previous.search_query, previous.search_host
                    search_submitted = True
            check = None if search_query is not None else record.postcondition or state.current_verification
            if (check is None or (not record.postcondition and check.assertion in {"value", "checked"})) and record.operation in {"fill", "select", "check", "uncheck"} and record.expected_value is not None:
                # Straightforward input goals have a deterministic target/value
                # postcondition even with an older planner specification.
                target = record.target
                names = re.findall(r'"([^"\n]+)"', target or "")
                if target and not target.startswith("{") and names:
                    target = names[0]
                elif target and target.startswith("{"):
                    locator = json.loads(target).get("locator", {})
                    target = next((locator[k] for k in ("name", "label", "text", "placeholder", "selector") if locator.get(k)), None)
                check = GoalCheck(assertion="checked" if record.operation in {"check", "uncheck"} else "value", expected=record.expected_value, target=target) if target else None
            verification_locator = record.postcondition_locator
            if field_edit and check and check.assertion in {"value", "checked"} and not verification_locator:
                try:
                    verification_locator = json.loads(record.target or "{}").get("locator")
                except (ValueError, TypeError):
                    pass
            state.pending_changes[key] = PendingChange(
                goal_id=record.goal_id, tab=record.tab, sequence=record.sequence,
                source_url=record.source_url, target=record.target,
                count=(previous.count if previous else 0) + 1,
                expected_url=(record.result_data.get('whatChanged') or {}).get('toUrl') if (record.success
                    and login and record.operation == 'click' and re.match(r'^link\b', record.target or '', re.I)) else
                    record.source_url if record.success and record.tool == "browser_navigate" and
                    (not previous or previous.search_query is not None or previous.expected_url) else None,
                search_query=search_query, search_host=search_host, search_submitted=search_submitted,
                verification=None if search_submitted or navigation else check,
                verification_locator=verification_locator, login_stage=login_stage,
                navigation=navigation, uncertain=record.code in UNKNOWN_OUTCOME_CODES,
            )
            state.counters.mutation_count += 1
            if not navigation and record.source_url:
                state.last_edit_by_page[record.source_url] = record.sequence
        self._collect_record_evidence(record)

    @staticmethod
    def _search_location(url: str | None) -> tuple[str, str] | None:
        if not url:
            return None
        parts = urlsplit(url)
        host = parts.hostname or ""
        engine = bool(re.fullmatch(r"(?:www\.)?google\.[a-z.]+", host)) or host in {
            "bing.com", "www.bing.com", "duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com"}
        if engine and parts.scheme in {"http", "https"} and parts.path.rstrip("/") in {"", "/search", "/webhp", "/html"}:
            return host, dict(parse_qsl(parts.query)).get("q", "")
        return None

    @staticmethod
    def _search_target(target: str | None) -> bool:
        if not target:
            return False
        if target.startswith("{"):
            locator = json.loads(target).get("locator", {})
            return (locator.get("role") == "searchbox" or
                    any(str(locator.get(k, "")).casefold() in {"search", "search google", "search the web"}
                        for k in ("name", "label", "placeholder")) or
                    locator.get("selector") in {'[name="q"]', 'input[name="q"]', 'textarea[name="q"]'})
        return bool(re.search(r'\b(?:combobox|textbox|searchbox)\s+"(?:Search|Search Google|Search the web)"', target, re.I))

    def _verify_navigation(self, tab: str | None, url: str | None, sequence: int) -> None:
        state = self.state
        assert state is not None
        search = self._search_location(url)
        for key, pending in list(state.pending_changes.items()):
            if pending.tab != tab or pending.goal_id != state.current_goal_id or sequence <= pending.sequence:
                continue
            url_matches = bool(pending.expected_url and pending.expected_url == url)
            query_matches = (pending.search_submitted and bool(pending.search_query) and search is not None and
                             search == (pending.search_host, pending.search_query))
            if url_matches or query_matches or (pending.navigation and url):
                state.counters.verified_mutation_count += pending.count
                state.counters.consecutive_failures = 0
                del state.pending_changes[key]

    def _verify_login_result(self, record: ActionRecord) -> None:
        """A verified authenticated result supersedes its consumed login inputs."""
        state = self.state
        assert state is not None
        login = parse_login_intent('\n'.join([state.original_request, *state.follow_ups]))
        if (not login or not record.tab or record.operation not in {'visible', 'text', 'title'}
                or not record.source_url or not login_destination_matches(login, record.source_url)
                or re.search(r'\b(?:log[ -]?in|sign[ -]?in|password)\b',
                             (record.target or '') + ' ' + str(record.result_data.get('expected') or ''), re.I)):
            return
        submissions = [pending for pending in state.pending_changes.values()
                       if pending.login_stage == 'submit' and pending.tab == record.tab
                       and pending.goal_id == record.goal_id and pending.sequence < record.sequence]
        if not submissions:
            return
        submitted_at = max(pending.sequence for pending in submissions)
        for key, pending in list(state.pending_changes.items()):
            if (pending.login_stage and pending.tab == record.tab and pending.goal_id == record.goal_id
                    and pending.sequence <= submitted_at):
                state.counters.verified_mutation_count += pending.count
                del state.pending_changes[key]

    def _remember_fields(self, tab: str | None, data: dict, evidence_id: str) -> None:
        if not tab or not data.get('fields') or not data.get('url'):
            return
        observed = {'url': data['url'], 'fields': data['fields'], 'evidence_id': evidence_id,
                    'complete': data.get('fields_complete', False)}
        self.state.field_observations[tab] = observed
        for change in self.state.change_history:
            if change['url'] == data['url']:
                change['after'] = data['fields']
                change['after_evidence_id'] = evidence_id
                change['after_complete'] = observed['complete']

    def _record_search(self, url: str | None) -> None:
        search = self._search_location(url)
        if self.state is not None and search and search[1]:
            self.state.search_observed = True

    def _collect_record_evidence(self, record: ActionRecord) -> None:
        state = self.state
        assert state is not None
        if not record.executed:
            return
        data = record.result_data
        attachments = self.documents or self.attachment_session
        if attachments and record.operation == 'upload' and (record.success or record.code in {'TIMEOUT', 'BRIDGE_ERROR', 'BRIDGE_UNAVAILABLE'}):
            for identifier in record.upload_files:
                item = attachments.store.get(identifier)
                state.pending_uploads[identifier] = dict(filename=item['filename'], tab=record.tab,
                    source_url=record.source_url, sequence=record.sequence)
        def evidence(kind: str, detail: str, verified: bool = True) -> Evidence:
            item = state.add_evidence(kind, detail, tab=record.tab, source_url=record.source_url,
                                      sequence=record.sequence, verified=verified)
            record.evidence_id = item.id
            return item

        if record.tool == "browser_assert" and record.execution_success:
            assertion = AssertionRecord(
                assertion=record.operation or "unknown",
                passed=record.semantic_success is True,
                expected=data.get("expected"),
                tab=record.tab,
                message=record.message,
                target=record.target,
                goal_id=record.goal_id,
                sequence=record.sequence,
                match=data.get("match"),
                required=(self._matches_goal_check(state.current_verification, record)
                          or any(self._matches_goal_check(p.verification, record) for p in state.pending_changes.values())),
            )
            actual = data.get("actual")
            if isinstance(actual, dict):
                actual = actual.get("actual", actual)
            detail = (f"{assertion.assertion} passed={assertion.passed} expected={assertion.expected!r} "
                      f"actual={actual!r} count={data.get('count')} "
                      f"reason={data.get('message') or record.message or ''} target={record.target}")
            item = evidence("assertion", detail, assertion.passed)
            assertion.evidence_id = item.id
            state.assertions = (state.assertions + [assertion])[-128:]
            if assertion.passed:
                for identifier, upload in list(state.pending_uploads.items()):
                    # A page title or selected file input is not a website receipt.
                    expected = str(assertion.expected or '')
                    target = assertion.target or ''
                    receipt = (assertion.assertion == 'text' and upload['filename'] in expected) or (
                        assertion.assertion == 'count' and isinstance(assertion.expected, int) and assertion.expected > 0
                        and upload['filename'] in target)
                    if (receipt and assertion.tab == upload['tab'] and record.sequence > upload['sequence']
                            and record.source_url == upload['source_url'] and 'input' not in target.lower()):
                        del state.pending_uploads[identifier]
            for key, pending in list(state.pending_changes.items()):
                if self._assertion_verifies_change(pending, assertion):
                    state.counters.verified_mutation_count += pending.count
                    state.counters.consecutive_failures = 0
                    del state.pending_changes[key]
            if assertion.passed:
                for earlier in state.assertions:
                    if (not earlier.passed and not earlier.superseded_by
                            and earlier.goal_id == assertion.goal_id and earlier.tab == assertion.tab
                            and earlier.sequence < assertion.sequence
                            and earlier.assertion == assertion.assertion and earlier.expected == assertion.expected
                            and (earlier.match or 'exact') == (assertion.match or 'exact')
                            and self._assertion_target_refines(earlier.target, assertion.target,
                                                               allow_descendant=assertion.assertion == 'text')):
                        earlier.superseded_by = item.id
                        state.counters.consecutive_failures = 0
                self._verify_login_result(record)
        elif record.success and record.tool in {"browser_observe", "browser_navigate", "browser_tabs"}:
            if record.tool == "browser_observe":
                self._record_search(record.source_url)
                # Locator extractions do not read the live URL. Cached tab
                # metadata alone cannot verify a delayed navigation.
                self._verify_navigation(record.tab, data.get("url"), record.sequence)
            kind = "navigation" if record.tool == "browser_navigate" else (
                "observation" if record.tool == "browser_observe" and "snapshot" in data else "extraction")
            content = record.extracted_data
            if content is not None:
                item = evidence(kind, content)
                if record.tool == 'browser_observe':
                    self._remember_fields(record.tab, data, item.id)
                if (record.tool == "browser_observe" and (kind == "extraction" or record.operation == "text")
                        and len(re.findall(r"[a-zA-Z]", content)) >= 40 and record.source_url):
                    state.source_register[record.source_url] = {
                        "url": record.source_url,
                        "title": clip(self.browser.tabs[record.tab].title, 150) or record.source_url,
                        "tab": record.tab, "evidence_id": item.id,
                    }
                    while len(state.source_register) > 128 or len(json.dumps(state.source_register)) > 8_000:
                        del state.source_register[next(iter(state.source_register))]
                        state.memory_truncated = True
                state.retain_fact(TaskFact(value=content, tab=record.tab, source_url=record.source_url,
                                          goal_id=record.goal_id, evidence_id=item.id,
                                          tool=record.tool,
                                          target=record.target or "page", priority=0 if kind == "observation" else 1))
        elif record.tool == "browser_capture_evidence" and record.success:
            capture = data.get("capture")
            if capture in {"page_screenshot", "element_screenshot"}:
                if data.get("saved"):
                    evidence("screenshot", f"Screenshot {data.get('evidence')}: {data.get('path') or ''}")
        elif record.tool == "browser_downloads" and record.success:
            candidates = data.get("items", [data.get("download")])
            for candidate in candidates if isinstance(candidates, list) else []:
                if not isinstance(candidate, dict) or candidate.get("state") != "complete" or candidate.get("exists") is False:
                    continue
                try:
                    started = datetime.fromisoformat(candidate.get("startTime", "").replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError, AttributeError):
                    continue
                if started < state.started_wallclock or candidate.get("taskEligible") is False:
                    continue
                if not candidate.get("filename"):
                    continue
                check = state.current_verification
                if check is None or check.assertion != "download" or not isinstance(check.expected, str) or not check.expected:
                    continue
                filename = candidate["filename"].replace("\\", "/").rsplit("/", 1)[-1]
                if check.expected not in {filename, candidate.get("url"), candidate.get("finalUrl")}:
                    continue
                item = evidence("download", json.dumps(candidate, ensure_ascii=False))
                state.downloads = (state.downloads + [item])[-16:]
                for key, pending in list(state.pending_changes.items()):
                    if (pending.goal_id == state.current_goal_id and pending.verification == check
                            and pending.tab == record.tab
                            and record.sequence > pending.sequence):
                        state.counters.verified_mutation_count += pending.count
                        del state.pending_changes[key]
        elif record.tool == "browser_diagnose" and record.success and record.operation in {"console", "network"}:
            detail = str(data.get("events", []))[:4_000]
            item = evidence(record.operation, detail or "No events were returned.")
            state.diagnostic_evidence = (state.diagnostic_evidence + [item])[-32:]
            state.retain_fact(TaskFact(value=detail, tab=record.tab, source_url=record.source_url,
                                      goal_id=record.goal_id, evidence_id=item.id,
                                      tool=record.tool, target=record.operation, priority=1))

    def _verification_failure_reason(self, record: ActionRecord) -> str:
        data = record.result_data
        reason = (f"VERIFICATION_UNRESOLVED: {record.operation} check on {record.tab} "
                  f"expected {data.get('expected')!r}, actual {data.get('actual')!r}; "
                  f"target={record.target}. The same check failed after recovery. "
                  f"{data.get('message') or record.message or record.code or ''}")
        if record.operation == 'value' and self.browser.capability_warning:
            reason += ' ' + self.browser.capability_warning
        return clip(reason, 1600) or reason

    def _repeated_failed_check(self, browser_state, records, failures):
        """Bound identical assertion retries, including across needs_plan returns."""
        state = self.state
        for record in records:
            if record.tool != 'browser_assert':
                continue
            entry = state.progress_by_tab.setdefault(record.tab or '', {})
            identity = (record.goal_id, record.operation, record.target, repr(record.result_data.get('expected')))
            if record.success:
                previous = entry.get('failed_check')
                if previous and previous['identity'] == identity:
                    entry.pop('failed_check', None)
                continue
            if record not in failures or record.code not in {'ASSERTION_FAILED', 'UNSUPPORTED_ELEMENT_PROPERTY'}:
                continue
            interaction = max((r.sequence for r in state.actions if r.tab == record.tab
                and r.goal_id == record.goal_id and r.sequence < record.sequence
                and r.invalidates_verification), default=0)
            attempt = {'identity': identity, 'interaction': interaction,
                       'page': self._page_fingerprint(browser_state, [])}
            if entry.get('failed_check') == attempt:
                return record
            entry['failed_check'] = attempt
        return None

    def _verified_checks(self):
        """A compact view of existing evidence; navigating does not erase task outcomes."""
        state = self.state
        evidence = {e.id: e for e in state.evidence}
        latest = {}
        for a in state.assertions:
            e = evidence.get(a.evidence_id)
            if (a.goal_id != state.current_goal_id or not e or not e.source_url
                    or a.sequence <= state.last_edit_by_page.get(e.source_url, 0)):
                continue
            key = (e.source_url, a.tab, a.target, a.assertion, repr(a.expected))
            latest[key] = (a, e)
        return [dict(page=e.source_url, tab=a.tab, assertion=a.assertion,
                     target=a.target, expected=a.expected, evidence_id=a.evidence_id)
                for a, e in latest.values() if a.passed and e.verified and not a.superseded_by][-64:]

    def _bind_pending_verification(self, tab, command, sequence):
        """Supply a missing check explicitly; never replace a declared or uncertain outcome."""
        state = self.state
        pending = next((p for p in state.pending_changes.values()
                        if p.sequence == sequence and p.tab == tab and p.goal_id == state.current_goal_id), None)
        if (not pending or pending.uncertain or pending.navigation or pending.login_stage
                or pending.search_query is not None):
            raise ValueError('Choose a known-successful pending action on this tab; uncertain outcomes retain their existing recovery rules.')
        locator = getattr(command, 'locator', None)
        if (not pending.source_url or pending.source_url != self.browser.safe_url(self.browser.tab(tab).url)
                or locator is None or not any(getattr(locator, k, None) for k in ('name', 'label', 'text', 'selector'))
                or command.assertion not in {'text', 'value', 'checked', 'visible', 'hidden', 'enabled', 'disabled', 'count'}
                or getattr(command, 'contains', False) or getattr(command, 'regex', False)):
            raise ValueError('Verify the saved result on the action\'s original page with a specific exact locator and assertion.')
        expected = getattr(command, 'expected', getattr(command, 'count', True))
        scoped = locator.model_dump(exclude_none=True)
        target = next(scoped[k] for k in ('name', 'label', 'text', 'selector') if scoped.get(k))
        check = GoalCheck(assertion=command.assertion, expected=expected, target=target, locator=scoped, tab=tab)
        if pending.verification is not None and pending.verification != check:
            raise ValueError('This action already has a postcondition. Preserve its method, target and expected result.')
        pending.verification = check
        pending.verification_locator = scoped
        pending.verification_url = pending.source_url

    def _bind_verification_target(self, tab, command, target, records=()):
        """Bind an inferred label before its assertion, using a fresh unique scoped target."""
        state = self.state
        check = state.current_verification
        locator = getattr(command, 'locator', None)
        expected = getattr(command, 'expected', None)
        if (not state.current_goal_id or not check or target != check.target
                or command.assertion != check.assertion or expected != check.expected
                or getattr(command, 'contains', False) or getattr(command, 'regex', False)):
            raise ValueError('Binding must retain the current goal target, assertion and exact expected result.')
        if tab != self.tab or locator is None:
            raise ValueError('Bind the verification on the current goal tab with an observed locator.')
        scoped = locator.model_dump(exclude_none=True)
        if not scoped.get('within') or not any(scoped.get(k) for k in ('name', 'label', 'text', 'has_text', 'placeholder', 'selector')):
            raise ValueError('Use a specific target inside its observed section, form, dialog or row.')
        candidate = ActionRecord(tool='browser_assert', operation=command.assertion, tab=tab,
            target=json.dumps({'locator': scoped}), result_data={'expected': expected, 'match': 'exact'})
        if check.locator:
            if not self._matches_goal_check(check, candidate):
                raise ValueError('Retain the bound verification locator and tab; its scope cannot be replaced.')
            return
        user_text = '\n'.join([state.original_request, *state.follow_ups, *state.requirements.required_text]).casefold()
        if target.casefold() in user_text and not self._matches_goal_check(check, candidate):
            raise ValueError('An explicit user target must keep its literal identity; observe that target.')
        interactions = [r.sequence for r in [*state.actions, *records] if r.goal_id == state.current_goal_id
                        and r.tab == tab and r.invalidates_verification]
        interactions.extend(p.sequence for p in state.pending_changes.values() if p.goal_id == state.current_goal_id and p.tab == tab)
        after = max([state.last_interaction_by_tab.get(tab, 0), *interactions])
        unique = any(a.passed and not a.superseded_by and a.goal_id == state.current_goal_id and a.tab == tab
                     and a.sequence > after and a.assertion == 'count' and a.expected == 1
                     and self._assertion_target_refines(a.target, candidate.target)
                     for a in state.assertions)
        if not unique:
            raise ValueError('First run count=1 on this exact scoped locator after the latest interaction, then bind its check.')
        # Existing per-action scopes stay authoritative, including uncertain submissions.
        pending = [p for p in state.pending_changes.values() if p.goal_id == state.current_goal_id and p.verification == check]
        if any(p.tab != tab or p.uncertain or (p.verification_locator and not self._locator_refines(
                p.verification_locator, scoped, allow_descendant=check.assertion == 'text')) for p in pending):
            raise ValueError('The proposed target conflicts with an outstanding change or its scope.')
        bound = check.model_copy(update={'locator': scoped, 'tab': tab})
        if self.documents:
            self.documents.refine_verification(state.current_goal_id, check, bound)
        state.current_verification = bound
        for p in pending:
            p.verification = bound

    def _current_goal_verified(self) -> bool:
        state = self.state
        check = state.current_verification
        if state.current_goal_id is None or check is None or check.assertion == 'download':
            return True  # Download evidence has its own completion gate.
        return bool(self._accepted_goal_assertions())

    def _goal_verification_rejection(self):
        state = self.state
        check = state.current_verification
        last = next((a for a in reversed(state.assertions) if a.goal_id == state.current_goal_id and a.passed), None)
        detail = last.model_dump_json(include={'assertion', 'expected', 'target', 'tab'}) if last else 'none'
        return ('Goal verification required: declared=' + (check.model_dump_json(exclude_none=True) if check else 'none')
                + '; latest passing assertion=' + detail
                + '. Match the method, exact expectation, tab and scoped target after the latest interaction.'
                + (' For an inferred descriptive target, use count=1 and browser_assert goal_target to bind its observed locator.'
                   if check and check.target and not check.locator else ''))

    def _accepted_goal_assertions(self):
        from axis.models import accepted_goal_assertions
        return accepted_goal_assertions(self.state, self._matches_goal_check)

    def _assertion_verifies_change(self, change, assertion) -> bool:
        record = ActionRecord(tool='browser_assert', operation=assertion.assertion, target=assertion.target, tab=assertion.tab,
                              result_data={'expected': assertion.expected, 'match': assertion.match})
        return (assertion.passed and not assertion.superseded_by
                and assertion.goal_id == change.goal_id and assertion.tab == change.tab
                and assertion.sequence > change.sequence
                and (not change.verification_url or any(e.id == assertion.evidence_id
                    and e.source_url == change.verification_url for e in self.state.evidence))
                and self._matches_goal_check(change.verification, record)
                and (not change.verification_locator or self._locator_refines(
                    change.verification_locator, json.loads(assertion.target or '{}').get('locator'),
                    allow_descendant=assertion.assertion == 'text')))

    @staticmethod
    def _reported_value_property(record: ActionRecord) -> bool | None:
        """Only bridge property metadata is capability evidence, never page prose."""
        if record.tool == 'browser_assert' and record.operation == 'value':
            return False if record.code == 'UNSUPPORTED_ELEMENT_PROPERTY' else None
        if not record.success:
            return None
        if record.tool == 'browser_assert' and record.operation == 'text':
            result = record.result_data.get('actual')
        elif record.tool == 'browser_observe':
            result = record.result_data.get('result')
        else:
            return None
        if not isinstance(result, dict) or result.get('count') != 1:
            return None
        element, elements = result.get('element'), result.get('elements')
        if element is None and isinstance(elements, list) and len(elements) == 1:
            element = elements[0]
        value = element.get('hasValue') if isinstance(element, dict) else None
        return value if isinstance(value, bool) else None

    def _supports_text_verification(self, assertion, after: int) -> bool:
        check = self.state.current_verification
        for record in reversed(self.state.actions):
            if (record.goal_id != assertion.goal_id or record.tab != assertion.tab
                    or not after < record.sequence <= assertion.sequence
                    or not self._matches_goal_check(check, ActionRecord(tool='browser_assert', operation='value',
                        target=record.target, tab=record.tab, result_data={'expected': check.expected}))
                    or not self._assertion_target_refines(record.target, assertion.target, allow_descendant=True)):
                continue
            supported = self._reported_value_property(record)
            if supported is not None:
                return supported is False  # Newer native-control evidence vetoes older metadata.
        return False

    def _refine_verification(self, check: GoalCheck | None) -> bool:
        """Correct an inferred check using matching post-change runtime evidence."""
        state = self.state
        assert state is not None
        old = state.current_verification
        if (not old or not check or old == check
                or old.assertion not in {"text", "title", "value"}
                or (old.target and old.target != check.target)
                or old.locator != check.locator or old.tab != check.tab):
            return False
        user_text = "\n".join([state.original_request, *state.follow_ups,
                               *state.requirements.required_text]).casefold()
        method_change = old.assertion == 'value' and check.assertion == 'text' and old.expected == check.expected
        if old.assertion != check.assertion and not method_change:
            return False
        if method_change and re.search(r'\b(?:value assertion|value property|tohavevalue)\b', user_text):
            return False
        if not method_change and isinstance(old.expected, str) and old.expected.casefold() in user_text:
            return False
        pending = [p for p in state.pending_changes.values() if p.goal_id == state.current_goal_id]
        if not pending and not method_change:
            return False
        replacements = []
        for change in pending:
            if change.uncertain or change.verification != old:
                return False
            corrected = change.model_copy(update={'verification': check})
            replacement = next((a for a in reversed(state.assertions)
                if a.evidence_id and self._assertion_verifies_change(corrected, a)), None)
            if replacement is None:
                return False
            if method_change and not self._supports_text_verification(replacement, change.sequence):
                return False
            replacements.append(replacement)
        if not pending:
            # An already-correct field needs no edit. Still require a fresh,
            # exact check on the active goal's tab after its last interaction.
            after = max((r.sequence for r in state.actions if r.goal_id == state.current_goal_id
                         and r.tab == self.tab and r.tool in {'browser_act', 'browser_navigate', 'browser_visual'}
                         and r.operation != 'capture' and r.executed), default=0)
            replacement = next((a for a in reversed(state.assertions)
                if a.passed and not a.superseded_by and a.evidence_id and a.goal_id == state.current_goal_id
                and a.tab == self.tab and a.sequence > after
                and self._matches_goal_check(check, ActionRecord(tool='browser_assert', operation=a.assertion,
                    target=a.target, tab=a.tab, result_data={'expected': a.expected, 'match': a.match}))
                and self._supports_text_verification(a, after)), None)
            if replacement is None:
                return False
            replacements.append(replacement)
        if self.documents:
            if method_change:
                steps = self.documents.workflow()['next_steps']
                if steps and re.search(r'\b(?:value assertion|value property|tohavevalue)\b', steps[0]['instruction'], re.I):
                    return False
            self.documents.refine_verification(state.current_goal_id, old, check)
        state.current_verification = check
        for change in pending:
            change.verification = check
        for replacement in replacements:
            replacement.required = True
            if method_change:
                for record in state.actions:
                    if (record.tool == 'browser_assert' and record.operation == 'value'
                            and record.code in {'UNSUPPORTED_ELEMENT_PROPERTY', 'ASSERTION_FAILED'}
                            and record.goal_id == replacement.goal_id and record.tab == replacement.tab
                            and record.sequence < replacement.sequence
                            and (record.code == 'UNSUPPORTED_ELEMENT_PROPERTY'
                                 or record.result_data.get('expected') == old.expected)
                            and self._assertion_target_refines(record.target, replacement.target, allow_descendant=True)):
                        record.recovered = True
            for assertion in state.assertions:
                if (not assertion.passed and assertion.goal_id == replacement.goal_id
                        and assertion.tab == replacement.tab
                        and self._assertion_target_refines(assertion.target, replacement.target,
                                                           allow_descendant=method_change)
                        and assertion.assertion == old.assertion and assertion.expected == old.expected):
                    assertion.superseded_by = replacement.evidence_id
        for key, change in list(state.pending_changes.items()):
            if any(self._assertion_verifies_change(change, a) for a in replacements):
                state.counters.verified_mutation_count += change.count
                del state.pending_changes[key]
        state.counters.consecutive_failures = 0
        self._emit("verification_refined", verification=check.model_dump(),
                   evidence_ids=[a.evidence_id for a in replacements])
        return True

    @staticmethod
    def _locator_refines(original: dict | None, corrected: dict | None, *, allow_descendant: bool = False) -> bool:
        if not original or not corrected:
            return False
        if original == corrected:
            return True
        if not isinstance(original, dict) or not isinstance(corrected, dict):
            return False
        if allow_descendant and AxisOrchestrator._locator_refines(original, corrected.get('within'), allow_descendant=True):
            return True
        for key, value in original.items():
            if value is None or (key == 'exact' and value is False):
                continue
            if key == 'within':
                if not AxisOrchestrator._locator_refines(value, corrected.get(key), allow_descendant=True):
                    return False
            elif corrected.get(key) != value:
                return False
        return True

    @staticmethod
    def _assertion_target_refines(original: str | None, corrected: str | None, *, allow_descendant: bool = False) -> bool:
        try:
            before, after = json.loads(original or '{}'), json.loads(corrected or '{}')
            return (before.get('attribute') == after.get('attribute')
                    and before.get('index', 0) == after.get('index', 0)
                    and AxisOrchestrator._locator_refines(before.get('locator'), after.get('locator'),
                                                          allow_descendant=allow_descendant))
        except (ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _matches_goal_check(check: GoalCheck | None, record: ActionRecord) -> bool:
        if check is None or check.assertion != record.operation:
            return False
        actual = record.result_data.get("expected")
        if record.result_data.get("match") in {"regex", "contains"}:
            return False
        if check.expected is not None and actual != check.expected:
            return False
        if check.locator:
            try:
                identity = json.loads(record.target or '{}')
                return (record.tab == check.tab and identity.get('attribute') is None
                        and identity.get('index', 0) == 0
                        and AxisOrchestrator._locator_refines(check.locator, identity.get('locator')))
            except (ValueError, TypeError, AttributeError):
                return False
        if check.target:
            try:
                identity = json.loads(record.target or "{}")
                locator = identity.get("locator", {})
                targets = [str(v).casefold() for k, v in locator.items()
                            if k in {"name", "label", "text", "has_text", "placeholder", "selector", "role"}]
                scope = locator.get('within') if check.assertion == 'text' else None
                while isinstance(scope, dict):
                    targets.extend(str(v).casefold() for k, v in scope.items()
                                   if k in {"name", "label", "text", "has_text", "placeholder", "selector", "role"})
                    scope = scope.get('within')
                if check.target.casefold() not in targets:
                    return False
            except (ValueError, TypeError, AttributeError):
                return False
        return True

    @staticmethod
    def _normalized_url(url: str | None) -> str:
        if not url:
            return ""
        parts = urlsplit(url)
        query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))

    @classmethod
    def _page_fingerprint(cls, browser_state: BrowserState, records: list[ActionRecord]) -> str:
        text = re.sub(r"\[ref_\d+\]", "[ref]", browser_state.snapshot)
        text = " ".join(text.split())
        return hashlib.sha256(f"{cls._normalized_url(browser_state.url)}|{text}".encode()).hexdigest()

    def _update_progress(self, browser_state: BrowserState, records: list[ActionRecord]) -> None:
        state = self.state
        assert state is not None
        page_fingerprint = self._page_fingerprint(browser_state, records)
        tab = browser_state.tab or ""
        entry = state.progress_by_tab.setdefault(tab, {})
        if not records:
            state.made_progress = False
            pending = entry.pop("pending", None)
            if pending:
                changed = pending["page"] != page_fingerprint
                if changed or pending["positive"]:
                    entry["streak"] = 0
                    state.made_progress = True
                    for other in state.progress_by_tab.values():
                        other["streak"] = 0
                elif pending["action"] == entry.get("last_action"):
                    entry["streak"] = entry.get("streak", 0) + 1
                else:
                    entry["streak"] = 1
                entry["last_action"] = pending["action"]
            if entry.get("page") == page_fingerprint:
                state.counters.redundant_observations += 1
            state.counters.repeated_observations = (
                state.counters.repeated_observations + 1 if entry.get("page") == page_fingerprint else 0)
            entry["page"] = page_fingerprint
            state.counters.no_progress_steps = entry.get("streak", 0)
            return
        for record in records:
            record_tab = record.tab or tab
            tracked = state.progress_by_tab.setdefault(record_tab, {})
            if record.success and record.tool in {"browser_observe", "browser_assert"}:
                new_assertion = False
                if record.tool == "browser_assert":
                    key = (record.operation, record.target, repr(record.result_data.get("expected")),
                           state.counters.mutation_count, page_fingerprint)
                    new_assertion = tracked.get("last_assertion") != key
                    if not new_assertion:
                        state.counters.redundant_assertions += 1
                    tracked["last_assertion"] = key
                text = record.extracted_data
                positive = new_assertion if record.tool == "browser_assert" else (text is not None and text != tracked.get("extracted"))
                if record.tool == "browser_observe" and "snapshot" in record.result_data:
                    observed = BrowserState(tab=record_tab, url=record.source_url or browser_state.url,
                                            snapshot=record.result_data["snapshot"])
                    fingerprint = self._page_fingerprint(observed, [])
                    positive = fingerprint != tracked.get("page")
                    tracked["page"] = fingerprint
                tracked["extracted"] = text
                if positive:
                    state.made_progress = True
                    tracked["streak"] = 0
                    state.counters.no_progress_steps = 0
                    if "pending" in tracked:
                        tracked["pending"]["positive"] = True
            if (record.tool not in {"browser_act", "browser_navigate", "browser_visual"}
                    or record.operation == "capture" or record.recovered
                    or (record.code == 'STALE_STATE' and not record.executed)
                    or record.code in {'STEP_INTERRUPTED', 'ACTION_BUDGET_EXHAUSTED', 'RUN_PAUSED', 'RUN_CANCELLED'}):
                continue
            # Stable target descriptions distinguish two controls whose freshly
            # minted references happen to have different numbers each step.
            target = record.target or record.args or ""
            target = re.sub(r"\b(ref|screenshot)_\d+\b", r"\1", target)
            payload = re.sub(r"\b(ref|screenshot)_\d+\b", r"\1", record.args or "")
            # A refusal is an attempt too, even if the model changes its target syntax.
            action = record.code if not record.success else f"{record.tool}|{record.operation}|{target}|{payload}"
            if action == tracked.get("last_action") and tracked.get("page") == page_fingerprint:
                state.counters.redundant_actions += 1
            tracked["pending"] = {"page": tracked.get("page", page_fingerprint),
                                  "action": action, "positive": record.meaningful_change is True}

    def _screenshot(self, tab: str) -> dict[str, Any] | None:
        """Capture bounded visual input through the same execution gate."""
        state = self.state
        assert state is not None
        gate = StepGate(max_actions=1,
                        remaining_total_actions=self._run_limits().max_browser_actions - state.counters.browser_actions,
                        approval_actions=frozenset(self.config.tools.approval_required_actions),
                        approval=self.approval, forbidden_actions=frozenset(state.requirements.forbidden_actions),
                        mutation_allowed=state.requirements.mutation_allowed,
                        is_paused=lambda: state.status == "paused", is_cancelled=lambda: state.status == "cancelled",
                        on_record=self._register_action)
        kwargs = {"tab": tab, "command": bt.VisualCapture(operation="capture")}
        current_url = self.browser.safe_url(self.browser.tab(tab).url)
        refused = gate.check("browser_visual", kwargs, {
            "tab": tab, "operation": "capture", "current_url": current_url,
            "expected_effect": "capture", "reason": "This action is configured to require user approval.",
        })
        if refused:
            self._commit_gate(gate)
            state.last_error = "Visual recovery unavailable: " + (self._envelope_error(refused) or "capture refused")
            state.visual_tabs.discard(tab)
            self._emit("status", phase="vision_unavailable", reason=state.last_error)
            return None
        started = time.monotonic()
        result = bt.browser_visual(browser_context(self.browser), **kwargs)
        elapsed = int((time.monotonic() - started) * 1000)
        gate.record("browser_visual", kwargs, result, elapsed)
        self._commit_gate(gate)
        state.browser_ms += elapsed
        if not result["ok"]:
            state.last_error = "Visual recovery unavailable: " + (self._envelope_error(result) or "capture failed")
            state.visual_tabs.discard(tab)
            self._emit("status", phase="vision_unavailable", reason=state.last_error)
            return None
        return result["data"]


__all__ = ["AxisOrchestrator", "EventCallback", "ApprovalCallback"]
