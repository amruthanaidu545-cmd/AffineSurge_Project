from app.diffing import classify_change, diff_summary

OLD = "four AA alkaline batteries provide approximately 300 measurement cycles. Icon once capacity falls below 15%."


def test_wording_only_change_is_minor():
    assert classify_change(OLD, OLD.replace("provide approximately", "give roughly")) == "minor"


def test_threshold_change_is_critical_and_reported():
    d = diff_summary(OLD, OLD.replace("300", "250").replace("15%", "10%"))
    assert d["classification"] == "critical"
    assert "300" in d["critical_removed"] and "250" in d["critical_added"] and "15%" in d["critical_removed"]


def test_error_code_list_change_is_critical():
    assert classify_change("limited to the E1–E5 error conditions", "limited to the E1–E6 error conditions") == "critical"


def test_modal_and_negation_words_are_critical():
    assert classify_change("E3 cannot be silenced", "E3 can be silenced") == "critical"
    assert classify_change("the device shall abort", "the device should abort") == "critical"


def test_known_blind_spot_reordered_condition_is_missed():
    # documented limitation: same numbers/symbols/modal words, different meaning
    a = "Stage 1: systolic 130–139 or diastolic 80–89"
    b = "Stage 1: diastolic 130–139 or systolic 80–89"
    assert classify_change(a, b) == "minor"
