import { el } from "./dom.js";

// This is a lossless display of supplied fields, not a generated summary.
export function fieldLabel(key) {
  const text = String(key).replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function reviewValue(value, depth = 0) {
  if (value === null || value === undefined) return el("span", "muted", "Not supplied");
  if (typeof value !== "object") return el("span", "review-value", String(value));
  if (depth >= 3) return el("pre", "review-value", JSON.stringify(value, null, 2));
  const list = el("dl", "review-fields");
  const entries = Object.entries(value);
  if (!entries.length) return el("span", "muted", Array.isArray(value) ? "None" : "No fields supplied");
  for (const [key, entry] of entries) {
    const label = Array.isArray(value) ? `Item ${Number(key) + 1}` : fieldLabel(key);
    const detail = el("dd");
    detail.append(reviewValue(entry, depth + 1));
    list.append(el("dt", "", label), detail);
  }
  return list;
}

export function reviewField(parent, label, value) {
  const section = el("div", "review-field");
  section.append(el("h3", "", label), reviewValue(value));
  parent.append(section);
  return section;
}
