import copy
import importlib.util
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parent.parent
PATH = REPO / "examples" / "beaver" / "quickstart.py"
SPEC = importlib.util.spec_from_file_location("beaver_quickstart", PATH)
quickstart = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quickstart)


def test_committed_receipt_is_consistent_and_renders_the_decision():
    result = quickstart.load_result()
    output = quickstart.render(result)

    assert "5.3%  ->  15.0%" in output
    assert "+9.7 points, 95% CI +2.0 to +18.3" in output
    assert "REAL GAIN (McNemar p=0.0391): 8 fixed, 1 broke (dw_247)" in output
    assert "3 template twins banned" in output
    assert "not a clean win" in output


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("gate_flagged", 933, "gate counts"),
        ("reserved", 70, "split counts"),
        ("paired_answers", 206, "three repetitions"),
        ("baseline_percent", 9.9, "baseline percentage"),
        ("treatment_percent", 9.9, "treatment percentage"),
        ("lift_points", 9.9, "lift"),
        ("question_broke", 2, "broken ids"),
    ],
)
def test_receipt_reader_refuses_inconsistent_summaries(tmp_path, field, value, message):
    result = copy.deepcopy(quickstart.load_result())
    result[field] = value
    path = tmp_path / "result.json"
    import json
    path.write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        quickstart.load_result(path)
