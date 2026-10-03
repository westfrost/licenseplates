from driveway_lpr.plates import PlateRules, Read, apply_template, format_plate, normalize, similarity, vote

RULES = PlateRules([r"^[A-Z]{2}\d{5}$", r"^[A-Z0-9]{2,7}$"], ["LLDDDDD"])


def test_normalize_strips_spaces_and_symbols():
    assert normalize(" ab-12 345_ ") == "AB12345"


def test_template_fixes_letter_digit_confusion():
    assert apply_template("A812345", "LLDDDDD") == "AB12345"
    assert apply_template("AB12O45", "LLDDDDD") == "AB12045"
    assert apply_template("A812O45", "LLDDDDD") is None  # two changes: too unsure
    assert apply_template("A812O45", "LLDDDDD", max_changes=2) == "AB12045"
    assert apply_template("AB1234", "LLDDDDD") is None


def test_clean_keeps_valid_and_fixes_near_misses():
    assert RULES.clean("AB 12 345") == "AB12345"
    assert RULES.clean("8B12345") == "BB12345"
    assert RULES.clean("5AU5341") == "5AU5341"  # Czech plate must not be "corrected"
    assert RULES.clean("HEJMOR") == "HEJMOR"  # personalised plate untouched


def test_validity():
    assert RULES.is_strict("AB12345")
    assert not RULES.is_strict("HEJMOR")
    assert RULES.is_valid("HEJMOR")
    assert not RULES.is_valid("X")


def test_vote_majority_per_character():
    reads = [Read("AB12345", 0.9), Read("AB12346", 0.6), Read("AB12345", 0.8), Read("A812345", 0.5)]
    v = vote(reads, RULES)
    assert v.text == "AB12345"
    assert v.reads == 4
    assert ("AB12346", 1) in v.alternatives


def test_vote_prefers_strict_length():
    reads = [Read("AB1234", 0.7), Read("AB12345", 0.6), Read("AB12345", 0.5)]
    assert vote(reads, RULES).text == "AB12345"


def test_vote_empty():
    assert vote([], RULES) is None


def test_format_and_similarity():
    assert format_plate("AB12345") == "AB 12 345"
    assert format_plate("HEJMOR") == "HEJMOR"
    assert similarity("AB12345", "AB12345") == 1.0
    assert 0.8 < similarity("AB12345", "AB12346") < 1.0
