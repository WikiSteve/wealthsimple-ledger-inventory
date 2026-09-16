"""Read-only evidence for the Activity sidebar; absent controls fail closed."""

from __future__ import annotations

import re
from typing import Any

FILTER_GROUPS = ("Quick filters", "Account", "Type", "Holdings", "Timeframe", "Status")
SELECTION_GROUPS = ("Account", "Type", "Holdings", "Status")

# Shared with Clear click scoping: action and clear_present must agree.
CLEAR_LABEL_RE = re.compile(r"^Clear(?: all)?$|^Clear filters$|^Clear search$", re.IGNORECASE)
CLEAR_LABEL_CANONICAL = ("Clear", "Clear all", "Clear filters", "Clear search")

FILTER_SNAPSHOT_SCRIPT = """
const search = document.querySelector('[data-testid="filter-search"]');
let root = search;
while (root && !(root.innerText || '').trim().startsWith('Filters')) root = root.parentElement;
if (!root) return null;
const names = arguments[0];
const buttons = Array.from(root.querySelectorAll('button'));
return {
  search: search.value,
  groups: names.map(name => {
    const matches = buttons.filter(b => b.innerText.trim() === name);
    if (matches.length !== 1) return {name, ambiguous: true};
    const b = matches[0], panel = b.parentElement;
    return {name, expanded: b.getAttribute('aria-expanded'),
      checkboxes: Array.from(panel.querySelectorAll('input[type="checkbox"]')).map(e => e.checked),
      radios: Array.from(panel.querySelectorAll('input[type="radio"]')).map(e => ({value:e.value, checked:e.checked})),
      pressed: Array.from(panel.querySelectorAll('[aria-pressed]')).map(e => e.getAttribute('aria-pressed'))};
  }),
  clear_present: buttons.some(b => /^Clear(?: all)?$/i.test(b.innerText.trim()) || /^Clear filters$/i.test(b.innerText.trim()) || /^Clear search$/i.test(b.innerText.trim()))
};
"""

# Sidebar-scoped Clear click: never search the whole document.
FILTER_CLEAR_CLICK_SCRIPT = """
const label = arguments[0], exact = arguments[1];
const search = document.querySelector('[data-testid="filter-search"]');
let root = search;
while (root && !(root.innerText || '').trim().startsWith('Filters')) root = root.parentElement;
if (!root) return {ok:false, reason:'no_sidebar'};
const els = Array.from(root.querySelectorAll('a,button,[role="button"],[role="menuitem"]'));
const normalize = (t) => (t || '').trim().replace(/\\s+/g, ' ');
const el = els.find(e => {
  const text = normalize(e.innerText || e.getAttribute('aria-label') || '');
  if (exact) return text === label;
  return text.toLowerCase() === label.toLowerCase();
});
if (!el) return {ok:false, reason:'not_found'};
const text = normalize(el.innerText || el.getAttribute('aria-label') || '');
const href = el.href || el.getAttribute('href');
el.scrollIntoView({block:'center'});
el.click();
return {ok:true, text, href};
"""


def clear_label_matches(text: str | None) -> bool:
    """True when text is a recognized filter Clear control label."""
    if not isinstance(text, str):
        return False
    return bool(CLEAR_LABEL_RE.match(text.strip()))


def _pressed_all_default(values: Any) -> bool | None:
    """Return True/False when pressed evidence is recognized; None if absent."""
    if not isinstance(values, list) or not values:
        return None
    if any(not isinstance(v, str) or v != "false" for v in values):
        return False
    return True


def _checkboxes_all_default(values: Any) -> bool | None:
    """Return True/False when checkbox evidence is recognized; None if absent."""
    if not isinstance(values, list) or not values:
        return None
    if any(v is not False for v in values):
        return False
    return True


def selection_group_at_default(group: dict[str, Any]) -> bool:
    """At least one recognized idiom present, and every recognized idiom default."""
    if not isinstance(group, dict) or group.get("ambiguous"):
        return False
    checkbox_state = _checkboxes_all_default(group.get("checkboxes"))
    pressed_state = _pressed_all_default(group.get("pressed"))
    if checkbox_state is None and pressed_state is None:
        return False
    if checkbox_state is False or pressed_state is False:
        return False
    return True


def confirms_unfiltered(snapshot: Any) -> bool:
    """Accept only a complete recognized sidebar in its explicit default state.

    Selection groups may present checkbox-only, toggle-only, or mixed evidence.
    When both idioms exist, all recognized controls must agree at default.
    Unrecognized, malformed, conflicting, ambiguous, or missing evidence fails closed.
    """
    if not isinstance(snapshot, dict) or snapshot.get("search") != "" or snapshot.get("clear_present") is not False:
        return False
    groups = snapshot.get("groups")
    if not isinstance(groups, list) or [g.get("name") for g in groups] != list(FILTER_GROUPS):
        return False
    for group in groups:
        if not isinstance(group, dict) or group.get("ambiguous") or group.get("expanded") != "true":
            return False
        name = group.get("name")
        if name == "Quick filters":
            if _pressed_all_default(group.get("pressed")) is not True:
                return False
        elif name == "Timeframe":
            radios = group.get("radios")
            if not isinstance(radios, list) or not radios:
                return False
            all_radios = [r for r in radios if isinstance(r, dict)]
            if len(all_radios) != len(radios):
                return False
            if len([r for r in all_radios if r.get("value") == "all"]) != 1:
                return False
            if any(r.get("checked") is not (r.get("value") == "all") for r in all_radios):
                return False
        elif name in SELECTION_GROUPS:
            if not selection_group_at_default(group):
                return False
        else:
            return False
    return True
