"""Read-only evidence for the Activity sidebar; absent controls fail closed."""

FILTER_GROUPS = ("Quick filters", "Account", "Type", "Holdings", "Timeframe", "Status")

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
  clear_present: buttons.some(b => /^Clear(?: all)?$/.test(b.innerText.trim()))
};
"""


def confirms_unfiltered(snapshot):
    """Accept only a complete recognized sidebar in its explicit default state."""
    if not isinstance(snapshot, dict) or snapshot.get("search") != "" or snapshot.get("clear_present") is not False:
        return False
    groups = snapshot.get("groups")
    if not isinstance(groups, list) or [g.get("name") for g in groups] != list(FILTER_GROUPS):
        return False
    for group in groups:
        if group.get("ambiguous") or group.get("expanded") != "true":
            return False
        if group["name"] == "Quick filters":
            values = group.get("pressed", [])
            if not values or any(v != "false" for v in values):
                return False
        elif group["name"] == "Timeframe":
            radios = group.get("radios", [])
            if len([r for r in radios if r.get("value") == "all"]) != 1:
                return False
            if not radios or any(r.get("checked") is not (r.get("value") == "all") for r in radios):
                return False
        else:
            values = group.get("checkboxes", [])
            if not values or any(v is not False for v in values):
                return False
    return True
