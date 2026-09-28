from src.agent_tools.document_tools import _find_edit_span


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
