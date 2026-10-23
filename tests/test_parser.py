"""Unit tests, one per irregularity found in the CT-200 PDF."""
from app.parser import normalize


def codes(doc, code):
    return [w for w in doc.warnings if w["code"] == code]


def test_duplicate_heading_error_codes_are_distinct_nodes_with_correct_parents(v1):
    dups = [n for n in v1.nodes if n.heading == "Error Codes"]
    assert len(dups) == 2 and dups[0].index != dups[1].index
    assert [v1.nodes[d.parent].heading for d in dups] == [
        "Alarms and Safety Behavior", "Troubleshooting"]
    assert [d.path for d in dups] == ["4/4.2", "7/7.1"]


def test_level_skip_2_1_1_1_attaches_to_2_1_and_is_reported(v1):
    n = v1.by_number("2.1.1.1")[0]
    assert n.level == 4 and v1.nodes[n.parent].number == "2.1"
    assert any(w["number"] == "2.1.1.1" for w in codes(v1, "level_skip"))


def test_out_of_order_3_4_before_3_3_keeps_doc_order_under_section_3(v1):
    n34, n33 = v1.by_number("3.4")[0], v1.by_number("3.3")[0]
    assert n34.ordinal < n33.ordinal
    assert v1.nodes[n34.parent].number == v1.nodes[n33.parent].number == "3"
    assert codes(v1, "out_of_order")


def test_body_size_bold_heading_3_2_not_merged_into_3_1(v1):
    n31, n32 = v1.by_number("3.1")[0], v1.by_number("3.2")[0]
    assert n32.heading == "Cuff Inflation Sequence"
    assert "Cuff Inflation" not in n31.body and "180 mmHg" in n32.body
    assert any(w["number"] == "3.2" for w in codes(v1, "heading_style_deviation"))


def test_numbered_list_items_are_not_headings(v1):
    top = [n.number for n in v1.nodes if n.level == 1]
    assert top == [str(i) for i in range(1, 9)]
    lst = [b for b in v1.by_number("3.3")[0].blocks if b["type"] == "list"][0]
    assert len(lst["items"]) == 5
    assert lst["items"][4].endswith("immediate medical attention")  # indented continuation joined


def test_paragraph_spanning_page_break_is_one_paragraph(v1):
    paras = [b for b in v1.by_number("3.1")[0].blocks if b["type"] == "paragraph"]
    assert len(paras) == 1 and "on the device. Use the profile button" in paras[0]["text"]


def test_tables_extracted_with_wrapped_cells(v1, v2):
    t1 = [b for b in v1.by_number("4.2")[0].blocks if b["type"] == "table"][0]
    t2 = [b for b in v2.by_number("4.2")[0].blocks if b["type"] == "table"][0]
    assert len(t1["rows"]) == 6 and len(t2["rows"]) == 7
    assert t1["rows"][3] == ["E3", "Overpressure condition", "Auto-deflates within 2 seconds, displays E3"]
    assert t1["rows"][1][1] == "Cuff not connected or leak detected"


def test_ligatures_and_nonbreaking_hyphen_normalized(v1):
    assert v1.by_number("2.1")[0].heading == "General Specifications"
    assert "CT-200" in v1.by_number("2.2")[0].body and "‑" not in v1.by_number("2.2")[0].body
    assert normalize("Speciﬁcation CT‑200") == "Specification CT-200"


def test_no_content_lost_or_duplicated(v1, v2):
    for d in (v1, v2):
        assert not [w for w in d.warnings if w["severity"] == "error"]


def test_cross_references_captured(v1):
    assert v1.by_number("3.3")[0].references == ["2.1", "4.3"]
    assert v1.by_number("7.1")[0].references == ["4.2"]
