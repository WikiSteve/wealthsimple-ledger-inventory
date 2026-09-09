from copy import deepcopy

import pytest

from src.activity_filter_evidence import FILTER_GROUPS, confirms_unfiltered


def defaults():
    return {"search": "", "clear_present": False, "groups": [
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
def test_selected_or_missing_checkbox_evidence(name):
    s = defaults()
    g = next(g for g in s["groups"] if g["name"] == name)
    for value in ([True], [], [None], ["false"]):
        g["checkboxes"] = value
        assert not confirms_unfiltered(s)


def test_quick_timeframe_search_and_clear_fail_closed():
    s = defaults()
    variants = []
    for key, value in [("search", "BN"), ("clear_present", True)]:
        v = deepcopy(s); v[key] = value; variants.append(v)
    v = deepcopy(s); v["groups"][0]["pressed"] = ["true"]; variants.append(v)
    v = deepcopy(s); v["groups"][4]["radios"][0]["checked"] = False; variants.append(v)
    v = deepcopy(s); v["groups"][4]["radios"][1]["checked"] = True; variants.append(v)
    assert all(not confirms_unfiltered(v) for v in variants)
    assert not confirms_unfiltered(None)
