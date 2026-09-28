from src.agent_tools.document_tools import _find_edit_span, _plan_edit_spans


def test_word_find_ignores_extra_blank_lines_only_at_unique_location():
    document = "Week 1 (Date):\n\nObjective(s) of the activities :\n\nContents :\n"
    find = "Week 1 (Date):\nObjective(s) of the activities :\nContents :"
    span = _find_edit_span(document, find)
    assert span is not None
    assert document[slice(*span)] == document[:-1]


def test_repeated_template_anchor_requires_line_number():
    document = "Contents :\n\nContents :\n"
    assert _find_edit_span(document, "Contents :") is None
    span = _find_edit_span(document, "3\tContents :")
    assert span is not None
    assert document[slice(*span)] == "Contents :"
    assert span[0] == len("Contents :\n\n")


def test_stale_line_number_does_not_select_wrong_repeated_field():
    document = "Contents :\n\nContents :\n"
    assert _find_edit_span(document, "2\tContents :") is None


def test_whitespace_tolerance_does_not_change_words():
    assert _find_edit_span("Week 1 (Date):\n", "Week 2 (Date):") is None


def test_repeated_logbook_blocks_map_in_document_order():
    entry = "Week 1 (Date): \nObjective(s) of the activities :\n\nContents :"
    document = "\nNext entry\n".join([entry] * 3)
    find = "Week 1 (Date): \nObjective(s) of the activities :\n\n\n\nContents :"
    edits = [{"find": find, "replace": f"Entry {i}"} for i in range(1, 4)]
    planned, skipped = _plan_edit_spans(document, edits)
    assert skipped == 0
    assert [document[slice(*span)] for span, _ in planned] == [entry] * 3
    assert [edit["replace"] for _, edit in planned] == ["Entry 1", "Entry 2", "Entry 3"]


def test_repeated_logbook_blocks_do_not_guess_when_count_differs():
    document = "Contents :\nContents :\nContents :"
    edits = [{"find": "Contents :", "replace": "one"}] * 2
    planned, skipped = _plan_edit_spans(document, edits)
    assert planned == []
    assert skipped == 2
