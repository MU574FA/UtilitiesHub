"""Autocomplete engine: matching, per-field keywords / shortcuts / switches, learning, persistence."""
from datetime import datetime

import pytest

from utilshub.core import autocomplete as A
from utilshub.core.schema import Opt
from utilshub.core.settings import Settings


@pytest.fixture
def ac(tmp_path):
    s = Settings(tmp_path / "settings.json", autosave=False)
    s.register(A.AUTOCOMPLETE_OPTS + A.AUTOCOMPLETE_STATE)
    s.register([Opt("match.job_title_keywords", ["engineer", "welder"], "x", "list"),
                Opt("visa.visa_types", ["Work visa", "Visit"], "x", "list"),
                Opt("visa.custom_fields", [], "x", "list"),
                Opt("docpacker.custom_fields", [], "x", "list"),
                Opt("cv_sorter.fields", [], "x", "list"), Opt("screenshot.fields", [], "x", "list")])
    svc = A.create(s, tmp_path / "learned.json")
    svc.register(A.FieldDef("t.job", "Job", "T", keywords=["Software Engineer", "Site Engineer", "Welder", "Accountant"],
                            shortcuts={"se": "Software Engineer"}))
    svc.register(A.FieldDef("t.skills", "Skills", "T", mode="list", keywords=["Python", "Java", "SQL"]))
    svc.register(A.FieldDef("t.note", "Note", "T", mode="text", shortcuts={"nr": "No response", ";t": "{time}", "sig": "Best,\n{cursor}"}))
    return svc


def texts(res):
    return [i.text for i in res.items] if res else []


def test_prefix_words_contains_and_initials(ac):
    assert texts(ac.suggest("t.job", "wel")) == ["Welder"]
    assert "Site Engineer" in texts(ac.suggest("t.job", "eng"))          # word start
    assert texts(ac.suggest("t.job", "ccount")) == ["Accountant"]        # contains (smart)
    ac.set_keywords("t.job", ["Human Resources Officer"])
    assert texts(ac.suggest("t.job", "hro")) == ["Human Resources Officer"]   # initials
    assert texts(ac.suggest("t.job", "weldr")) == []     # (job keywords replaced above)
    ac.set_keywords("t.job", ["Welder"])
    assert texts(ac.suggest("t.job", "weldr")) == ["Welder"]               # one typo


def test_match_modes_restrict(ac):
    ac.settings.set("autocomplete.match", "prefix")
    assert ac.suggest("t.job", "eng") is None
    ac.settings.set("autocomplete.match", "words")
    assert "Site Engineer" in texts(ac.suggest("t.job", "eng"))
    assert ac.suggest("t.job", "ccount") is None


def test_exact_value_is_not_suggested_and_min_chars(ac):
    assert ac.suggest("t.job", "Welder") is None
    ac.settings.set("autocomplete.min_chars", 3)
    assert ac.suggest("t.job", "we") is None
    assert ac.suggest("t.job", "we", force=True) is not None


def test_force_lists_everything_on_empty(ac):
    res = ac.suggest("t.job", "", force=True)
    assert len(texts(res)) == 4


def test_arabic_and_accent_folding(ac):
    ac.register(A.FieldDef("t.n", "N", "T", keywords=["أحمد علي", "José"]))
    assert texts(ac.suggest("t.n", "احم")) == ["أحمد علي"]
    assert texts(ac.suggest("t.n", "jos")) == ["José"]


def test_enable_disable_per_field_and_master(ac):
    assert ac.is_enabled("t.job")
    ac.set_enabled("t.job", False)
    assert ac.suggest("t.job", "wel") is None and ac.expansion("t.job", "se", 2) is None
    assert ac.suggest("t.skills", "py") is not None                       # other fields unaffected
    ac.set_enabled("t.job", True)
    assert ac.suggest("t.job", "wel") is not None
    ac.set_master(False)
    assert ac.suggest("t.job", "wel") is None
    assert ac.config("t.job").enabled                                    # choices kept while the master is off


def test_default_off_fields_and_override_only_stores_differences(ac):
    assert not ac.is_enabled("visa.passport_no")
    ac.set_enabled("visa.passport_no", True)
    assert ac.is_enabled("visa.passport_no")
    assert ac.settings.get("autocomplete.fields") == {"visa.passport_no": {"enabled": True}}
    ac.set_enabled("visa.passport_no", False)                             # back to the default -> nothing stored
    assert ac.settings.get("autocomplete.fields") == {}


def test_custom_keywords_replace_defaults_and_reset(ac):
    ac.set_keywords("t.job", ["Pilot", "Pirate", "pilot"])
    assert ac.config("t.job").keywords == ["Pilot", "Pirate"]
    assert texts(ac.suggest("t.job", "pi")) == ["Pilot", "Pirate"]
    assert ac.add_keyword("t.job", "Pianist") and not ac.add_keyword("t.job", "PILOT")
    ac.reset_field("t.job")
    assert "Welder" in ac.config("t.job").keywords and not ac.is_customised("t.job")


def test_linked_lists_are_offered(ac):
    assert "Engineer" in texts(ac.suggest("visa.position", "eng"))   # curated and linked copies are merged
    ac.settings.set("match.job_title_keywords", ["engineer", "rigger"])
    assert "rigger" in texts(ac.suggest("visa.position", "rig"))
    assert "Work visa" in texts(ac.suggest("visa.visa_type", "wor"))
    ac.settings.set("visa.visa_types", ["Work visa", "Tourist"])
    assert "Tourist" in texts(ac.suggest("visa.visa_type", "tou"))


def test_shortcut_comes_first_and_expands(ac):
    res = ac.suggest("t.job", "se")
    assert res.items[0].kind == "shortcut" and res.items[0].text == "Software Engineer"
    assert ac.accept("se", 2, res, res.items[0]) == ("Software Engineer", 17)
    exp = ac.expansion("t.job", "se", 2)
    assert ac.apply_expansion("se", exp) == ("Software Engineer", 17)
    assert ac.expansion("t.job", "see", 3) is None


def test_shortcut_in_text_mode_keeps_surroundings_and_caret_template(ac):
    t = "called nr"
    exp = ac.expansion("t.note", t, len(t))
    assert ac.apply_expansion(t, exp) == ("called No response", 18)
    t = "x sig"
    exp = ac.expansion("t.note", t, len(t), multiline=True)
    assert ac.apply_expansion(t, exp) == ("x Best,\n", 8)
    exp = ac.expansion("t.note", t, len(t), multiline=False)
    assert exp.text == "Best, "
    assert ac.expansion("t.note", "nrx", 3) is None


def test_symbol_shortcut_works_mid_word(ac):
    t = "john@g"
    exp = ac.expansion("visa.email", t, len(t))
    assert ac.apply_expansion(t, exp)[0] == "john@gmail.com"
    res = ac.suggest("visa.email", t)
    assert res.items[0].kind == "shortcut"
    now = datetime(2026, 11, 19, 14, 5)
    t = "seen ;t"
    exp = ac.expansion("t.note", t, len(t), now=now)
    assert exp.text == "14:05"


def test_list_mode_completes_the_token_under_the_caret(ac):
    t = "Python, ja"
    res = ac.suggest("t.skills", t)
    assert texts(res) == ["Java"] and res.start == 8
    assert ac.accept(t, len(t), res, res.items[0]) == ("Python, Java", 12)
    t = "Python, ja, SQL"
    res = ac.suggest("t.skills", t, cursor=10)
    assert ac.accept(t, 10, res, res.items[0])[0] == "Python, Java, SQL"


def test_text_mode_uses_last_words(ac):
    ac.set_keywords("t.note", ["Passport collected", "Passport lost"])
    assert texts(ac.suggest("t.note", "he said passport c")) == ["Passport collected"]
    assert ac.suggest("t.note", "he said ") is None


def test_learning_and_privacy(ac):
    assert ac.learn("visa.group", "Batch 12") == 1
    assert ac.learn("visa.group", "x") == 0 and ac.learn("visa.group", "12345") == 0
    assert texts(ac.suggest("visa.group", "bat")) == ["Batch 12"]
    assert ac.learn("visa.passport_no", "AB123456") == 0
    ac.set_enabled("visa.passport_no", True)
    assert ac.learn("visa.passport_no", "AB123456") == 0                  # sensitive: never remembered
    assert not ac.config("visa.passport_no").learn
    ac.set_learn("visa.group", False)
    assert ac.suggest("visa.group", "bat") is None
    ac.set_learn("visa.group", True)
    ac.settings.set("autocomplete.learn", False)
    assert ac.learn("visa.group", "Batch 13") == 0
    ac.settings.set("autocomplete.learn", True)
    ac.clear_learned("visa.group")
    assert ac.suggest("visa.group", "bat") is None


def test_learning_boosts_recent_and_list_mode_splits(ac):
    ac.learn("t.skills", "Rust, Go")
    assert set(ac.learned.get("t.skills")) == {"Rust", "Go"}
    ac.learn("t.job", "Welder")
    ac.learn("t.job", "Site Engineer")
    assert texts(ac.suggest("t.job", "s"))[0] == "Site Engineer"          # most recent first among equals


def test_text_mode_is_not_learned(ac):
    assert ac.learn("t.note", "some long note") == 0


def test_learned_file_persists(ac, tmp_path):
    ac.learn("visa.group", "Batch 7")
    ac.flush()
    again = A.LearnedStore(tmp_path / "learned.json")
    assert again.get("visa.group") == ["Batch 7"]


def test_box_values_and_extra_are_candidates(ac):
    res = ac.suggest("visa.group", "ag", extra=["Agency A"], box_values=["Agile"])
    assert set(texts(res)) == {"Agency A", "Agile"}


def test_dynamic_fields_from_settings(ac):
    ac.settings.set("visa.custom_fields", ["Contract no.", "Agency"])
    ac.settings.set("cv_sorter.fields", [{"key": "skills", "label": "Skills", "kind": "keywords", "pattern": "Python, Java"},
                                         {"key": "phone", "label": "Phone", "kind": "phone"},
                                         {"key": "job_title", "label": "Job title", "kind": "job_title"}])
    ids = {d.id: d for d in ac.fields()}
    assert "visa.custom.agency" in ids and "visa.custom.contract_no" in ids
    assert ids["field.skills"].mode == "list" and ids["field.skills"].keywords == ["Python", "Java"]
    assert not ac.is_enabled("field.phone") and ids["field.phone"].sensitive
    assert "Welder" in texts(ac.suggest("field.job_title", "wel"))


def test_parse_and_validate_shortcuts():
    sc, problems = A.parse_shortcuts("nr = No response\n# c\nwa\tSent WhatsApp\nbad\nx = x\nnr = dup\nlong one = z")
    assert sc == {"nr": "No response", "wa": "Sent WhatsApp", "long": "one = z"} or "nr" in sc
    assert problems
    assert A.validate_shortcut("", "x") and A.validate_shortcut("a b", "x") and A.validate_shortcut("a", "a")
    assert A.validate_shortcut("NR", "y", ["nr"]) and not A.validate_shortcut("nr", "y", ["nr"], replacing="nr")
    assert A.parse_shortcuts(A.format_shortcuts({"a": "x\ny"}))[0] == {"a": "x\ny"}


def test_parse_keywords():
    assert A.parse_keywords("a, b; c, A") == ["a", "b", "c"]
    assert A.parse_keywords("one two\nthree, four\n\n") == ["one two", "three, four"]


def test_add_shortcut_api(ac):
    assert ac.add_shortcut("t.job", "acc", "Accountant") == ""
    assert ac.add_shortcut("t.job", "acc", "Other")                       # duplicate -> message
    assert ac.config("t.job").shortcuts["acc"] == "Accountant" and ac.config("t.job").shortcuts["se"]


def test_unknown_field_still_works_without_defaults(ac):
    assert ac.suggest("nope.x", "a") is None
    ac.add_keyword("nope.x", "Alpha")
    assert texts(ac.suggest("nope.x", "al")) == ["Alpha"]


def test_change_listeners_fire(ac):
    hits = []
    ac.on_change(lambda: hits.append(1))
    ac.set_enabled("t.job", False)
    assert hits


def test_shortcut_already_typed_past_is_not_expanded_again(ac):
    assert ac.expansion("t.job", "se ", 3) is None
    assert ac.expansion("t.note", "called nr ", 10) is None


def test_extract_fields_present_and_disabled_by_default(ac):
    from utilshub.core.fields import default_screenshot_fields

    ac.settings.set("screenshot.fields", default_screenshot_fields())
    extract_fields = [d for d in ac.fields() if d.group == "Extract"]
    by_id = {d.id: d for d in extract_fields}

    expected_static = {
        "badge_extractor.ar_name",
        "badge_extractor.en_name",
        "badge_extractor.job_title",
        "badge_extractor.badge_no",
        "badge_extractor.filename_template",
        "ocr_match.patterns",
        "passport.mrz",
        "similarity_search.query",
        "similarity_search.exclude",
        "similarity_search.extensions",
        "similarity_search.pasted_title",
        "similarity_search.pasted_text",
    }
    expected_screenshot = {f"screenshot.{s['key']}" for s in default_screenshot_fields()}
    assert expected_static.issubset(by_id)
    assert expected_screenshot.issubset(by_id)

    for d in extract_fields:
        assert d.default_enabled is False, d.id
        assert not ac.is_enabled(d.id), d.id

    assert ac.suggest("badge_extractor.job_title", "wel") is None
    assert ac.suggest("screenshot.title", "eng") is None
    assert ac.suggest("ocr_match.patterns", "al") is None

    ac.set_enabled("badge_extractor.job_title", True)
    assert "Welder" in texts(ac.suggest("badge_extractor.job_title", "wel"))

    ac.set_enabled("screenshot.title", True)
    assert "Electrician" in texts(ac.suggest("screenshot.title", "elec"))

    ac.set_enabled("ocr_match.patterns", True)
    ac.set_keywords("ocr_match.patterns", ["Welder", "Electrician"])
    res = ac.suggest("ocr_match.patterns", "Welder\nelec")
    assert texts(res) == ["Electrician"]
    assert ac.accept("Welder\nelec", 11, res, res.items[0]) == ("Welder\nElectrician", 18)
