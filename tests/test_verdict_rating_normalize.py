"""normalize_rating: every consumer sees the closed set; raw text kept as rating_raw."""
import pytest

pytestmark = pytest.mark.unit

import importlib.util, os
_spec = importlib.util.spec_from_file_location(
    "tao_verdict_file", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     os.pardir, "bridge", "tao_verdict.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
normalize_rating = _mod.normalize_rating


@pytest.mark.parametrize("raw, canon", [
    ("Buy", "Buy"), ("BUY", "Buy"), ("strong buy", "Buy"),
    ("Overweight", "Buy"), ("Over Weight", "Buy"), ("Accumulate", "Buy"),
    ("Sell", "Sell"), ("SELL", "Sell"), ("Underweight", "Sell"),
    ("Trim", "Sell"), ("Exit", "Sell"), ("Reduce", "Sell"),
    ("Hold", "Hold"), ("HOLD", "Hold"), ("Neutral", "Hold"),
    ("Market Perform", "Hold"), ("Equal Weight", "Hold"),
    ("REVIEW", "REVIEW"), ("review", "REVIEW"), ("No Opinion", "REVIEW"),
    ("Frog Pattern", "REVIEW"), ("**Buy**", "Buy"), ("_trim_", "Sell"),
])
def test_normalize_mapping(raw, canon):
    assert normalize_rating(raw) == canon


def test_normalize_none_stays_none():
    assert normalize_rating(None) is None


def test_unknown_fails_closed_to_review():
    assert normalize_rating("Anything else") == "REVIEW"
