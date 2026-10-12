import { mountApprovals } from "./ui/approvals.js";
import { applyAppearance } from "./ui/settings.js";
await applyAppearance();
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.axisTheme) applyAppearance();
});
mountApprovals(document.querySelector("#permission-card"), { popup: true });
