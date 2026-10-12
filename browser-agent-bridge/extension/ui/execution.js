import { el, button, icon } from "./dom.js";
import { reviewField } from "./review-fields.js";

export function renderExecutionApproval(root, task, api, onError) {
  const pending = task?.pending_approval;
  root.hidden = !pending;
  if (!pending) { root.replaceChildren(); delete root.dataset.approval; return; }
  if (root.dataset.approval === pending.id) return;
  root.dataset.approval = pending.id;
  const card = el("div", "permission-card execution-card");
  card.append(el("h2", "", "Approve website change"));
  const body = el("div", "permission-body");
  body.tabIndex = 0;
  body.setAttribute("role", "region");
  body.setAttribute("aria-label", "Change details");
  body.append(el("p", "metadata", task.title));
  for (const action of pending.actions || []) {
    const change = el("section", "review-action");
    reviewField(change, "Action", action.operation).classList.add("review-compact");
    reviewField(change, "Target", action.target || "Target not established").classList.add("review-compact");
    reviewField(change, "Destination", action.current_url || action.origin || "Destination not established").classList.add("review-compact");
    // Lead with the complete proposed text when the action supplies it. Retain
    // every remaining parameter below and the original action in exact data.
    const proposedText = action.arguments?.command?.value;
    reviewField(change, "Proposed change", typeof proposedText === "string" ? proposedText : action.arguments);
    if (action.approval_reason) reviewField(change, "Why approval is needed", action.approval_reason);
    if (action.observed_target) reviewField(change, "Observed target", action.observed_target);
    if (action.files?.length) reviewField(change, "Files", action.files);
    reviewField(change, "Verification", action.verification);
    if (typeof proposedText === "string") reviewField(change, "Change parameters", action.arguments);
    const details = el("details");
    details.append(el("summary", "", "Exact technical data"));
    details.append(el("pre", "permission-params", JSON.stringify(action, null, 2)));
    change.append(details);
    body.append(change);
  }
  card.append(body);
  const hint = el("p", "review-scroll-hint", "Scroll to review all change details");
  hint.id = `review-hint-${pending.id}`;
  hint.prepend(icon("down"));
  body.setAttribute("aria-describedby", hint.id);
  card.append(hint);
  const message = el("p", "feedback");
  message.setAttribute("role", "status");
  message.hidden = true;
  const controls = el("div", "permission-actions");
  const decide = async decision => {
    controls.querySelectorAll("button").forEach(node => node.disabled = true);
    try {
      await api.request(`/api/tasks/${task.id}/approvals/${pending.id}`, { method: "POST", body: { decision } });
      message.textContent = decision === "approve" ? "Approved; checking task state…" : "Declined; pausing task…";
      message.hidden = false;
    } catch (cause) {
      onError(cause.message);
      controls.querySelectorAll("button").forEach(node => node.disabled = false);
    }
  };
  controls.append(button("Approve change", () => decide("approve"), null, "primary"),
    button("Decline", () => decide("deny")));
  card.append(message, controls);
  root.replaceChildren(card);
}
