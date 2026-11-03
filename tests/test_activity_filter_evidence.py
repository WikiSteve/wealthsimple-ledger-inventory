from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.activity_filter_evidence import FILTER_GROUPS, confirms_unfiltered


PINNED = Path("/tmp/wealthsimple-full-account-inventory-20260916T160652-0300")


def defaults():
    return {"layout": "group", "search": "", "clear_present": False, "groups": [
        {"name": name, "expanded": "true", "checkboxes": [False],
         "pressed": ["false"], "radios": [{"value": "all", "checked": True},
                                             {"value": "last-week", "checked": False}]}
        for name in FILTER_GROUPS
    ]}


def test_explicit_defaults_without_clear():
    assert confirms_unfiltered(defaults())


@pytest.mark.parametrize("name", FILTER_GROUPS)
def test_missing_or_collapsed_section_is_unknown(name):
    s = defaults()
    next(g for g in s["groups"] if g["name"] == name)["expanded"] = "false"
    assert not confirms_unfiltered(s)
    s["groups"] = [g for g in s["groups"] if g["name"] != name]
    assert not confirms_unfiltered(s)


@pytest.mark.parametrize("name", ["Account", "Type", "Holdings", "Status"])
def test_selected_checkbox_or_malformed_evidence(name):
    s = defaults()
    g = next(g for g in s["groups"] if g["name"] == name)
    for value in ([True], [None], ["false"]):
        g["checkboxes"] = value
        assert not confirms_unfiltered(s)


@pytest.mark.parametrize("name", ["Account", "Type", "Holdings", "Status"])
def test_toggle_only_empty_checkboxes_accepted(name):
    s = defaults()
    g = next(g for g in s["groups"] if g["name"] == name)
    g["checkboxes"] = []
    g["pressed"] = ["false", "false"]
    assert confirms_unfiltered(s)


@pytest.mark.parametrize("name", ["Account", "Type", "Holdings", "Status"])
def test_neither_idiom_fails_closed(name):
    s = defaults()
    g = next(g for g in s["groups"] if g["name"] == name)
    g["checkboxes"] = []
    g["pressed"] = []
    assert not confirms_unfiltered(s)


def test_indexes_filter_must_be_explicitly_default():
    s = defaults()
    group = next(g for g in s["groups"] if g["name"] == "Indexes")
    group["checkboxes"] = [True]
    assert not confirms_unfiltered(s)


def test_current_sidebar_without_search_uses_accessible_filter_group(fixture_browser, tmp_path):
    from src.activity_filter_evidence import FILTER_SNAPSHOT_SCRIPT
    page = tmp_path / "filters.html"
    groups = []
    for name in FILTER_GROUPS:
        controls = ('<input type="radio" value="all" checked><input type="radio" value="last-week">'
                    if name == "Timeframe" else '<button aria-pressed="false">Choice</button>')
        groups.append(f'<div><button aria-expanded="true">{name}</button>{controls}</div>')
    page.write_text('<div role="group" aria-label="Filters"><h2>Filters</h2>' + ''.join(groups) + '</div>')
    fixture_browser.get(page.as_uri())
    assert confirms_unfiltered(fixture_browser.execute_script(FILTER_SNAPSHOT_SCRIPT, list(FILTER_GROUPS)))
    fixture_browser.execute_script("document.querySelector('[aria-pressed]').setAttribute('aria-pressed','true')")
    assert not confirms_unfiltered(fixture_browser.execute_script(FILTER_SNAPSHOT_SCRIPT, list(FILTER_GROUPS)))


def test_quick_timeframe_search_and_clear_fail_closed():
    s = defaults()
    variants = []
    for key, value in [("search", "BN"), ("clear_present", True)]:
        v = deepcopy(s); v[key] = value; variants.append(v)
    v = deepcopy(s); v["groups"][0]["pressed"] = ["true"]; variants.append(v)
    v = deepcopy(s); next(g for g in v["groups"] if g["name"] == "Timeframe")["radios"][0]["checked"] = False; variants.append(v)
    v = deepcopy(s); next(g for g in v["groups"] if g["name"] == "Timeframe")["radios"][1]["checked"] = True; variants.append(v)
    assert all(not confirms_unfiltered(v) for v in variants)
    assert not confirms_unfiltered(None)


@pytest.mark.skipif(not PINNED.exists(), reason="pinned bundle not present")
def test_live_toggle_only_snapshot_from_pinned_bundle():
    snapshot = json.loads((PINNED / "logs" / "activity-filter-state.json").read_text())["snapshot"]
    assert confirms_unfiltered(snapshot)
