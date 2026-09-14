from __future__ import annotations

import csv
import importlib.util
import json
import pathlib
import sys


SCRIPT = pathlib.Path(__file__).parents[1] / "examples" / "bird" / "prepare_enterprise.py"
QUICKSTART = pathlib.Path(__file__).parents[1] / "examples" / "bird" / "enterprise_quickstart.py"


def load_module():
    spec = importlib.util.spec_from_file_location("bird_enterprise", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def load_quickstart():
    spec = importlib.util.spec_from_file_location("bird_enterprise_quickstart", QUICKSTART)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def load_sibling(name):
    directory = SCRIPT.parent
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_source_urls_are_immutable():
    module = load_module()
    url = module._url(module.QUERY_REPO, module.QUERY_REVISION, "questions file.json")
    assert module.QUERY_REVISION in url
    assert "/resolve/main/" not in url
    assert url.endswith("questions%20file.json")


def test_write_inputs_preserves_question_evidence_and_sql(tmp_path):
    module = load_module()
    rows = [{
        "query_id": 7,
        "database_id": "financial",
        "query": "Which accounts qualify?",
        "answer": "SELECT account_id FROM account",
        "evidence": "qualified means active = 1",
        "difficulty": "moderate",
    }]
    counts = module.write_inputs(rows, tmp_path)
    assert counts["financial"] == 1
    assert sum(counts.values()) == 1
    output = list(csv.DictReader(
        (tmp_path / "work" / "financial" / "golds.csv").open()))
    assert output == [{
        "id": "bird_0007",
        "question": "Which accounts qualify?",
        "gold_sql": "SELECT account_id FROM account",
        "evidence": "qualified means active = 1",
        "difficulty": "moderate",
        "source": "BIRD validation benchmark",
    }]
    evidence = json.loads(
        (tmp_path / "work" / "financial" / "evidence.json").read_text())
    assert evidence == {"bird_0007": "qualified means active = 1"}


def test_catalog_description_keeps_business_names_and_values(tmp_path):
    module = load_module()
    path = tmp_path / "account.csv"
    path.write_text(
        "original_column_name,column_name,column_description,value_description\n"
        "status,account state,current lifecycle state,1 = active; 0 = closed\n"
        "account_id,account_id,account_id,\n",
        encoding="utf-8",
    )
    text = module._read_description(path, "account")
    assert "Catalog documentation for table account:" in text
    assert "business name: account state" in text
    assert "current lifecycle state" in text
    assert "values: 1 = active; 0 = closed" in text
    assert "account_id" not in text


def test_quickstart_validates_and_discloses_scope():
    module = load_quickstart()
    result = module.load_result()
    output = module.render(result)
    assert "37.4%  ->  56.9%" in output
    # The per-question reading is what gets quoted: 15 fixed - 3 broke over
    # 65 questions. The answer-level +19.5 is shown beside it and labelled
    # narrower, because 195 answers are 65 questions asked three times.
    assert "+18.5 points" in output
    assert "over 65 questions" in output
    assert "+19.5" in output and "which is narrower" in output
    assert "65/256 reserved questions" in output
    assert "oracle-routing" in output
    assert "3 stable regressions" in output


def test_quickstart_rejects_inconsistent_receipt(tmp_path):
    module = load_quickstart()
    result = module.load_result()
    result["treatment_correct"] += 1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    try:
        module.load_result(path)
    except ValueError as error:
        assert "treatment percentage" in str(error)
    else:
        raise AssertionError("an inconsistent receipt was accepted")


def test_context_policy_classes_are_frozen_and_mutually_exclusive():
    module = load_sibling("context_policy")
    assert module.context_class("rate = amount / total") == "formula"
    assert module.context_class("A means active") == "mapping"
    assert module.context_class("status = 'A'") == "literal"
    assert module.context_class("Use the account table") == "other"


def test_merge_reports_excludes_unpaired_question_symmetrically(tmp_path):
    module = load_sibling("merge_reports")
    for database in module.DATABASES:
        directory = tmp_path / "work" / database / "reports"
        directory.mkdir(parents=True)
        for prefix in ("base", "docs"):
            for repetition in range(1, 4):
                rows = [{"id": f"{database}_kept", "execution_correct": True}]
                if database == module.DATABASES[0]:
                    rows.append({
                        "id": "unpaired",
                        "execution_correct": None if prefix == "base" else False,
                    })
                (directory / f"{prefix}_rep{repetition}.json").write_text(
                    json.dumps({"n_total": len(rows), "results": rows}))
    out = tmp_path / "merged"
    common, excluded = module.merge_reports(
        tmp_path, "reports", "base", "docs", out)
    assert len(common) == len(module.DATABASES)
    assert excluded == {"unpaired"}
    pairing = json.loads((out / "pairing.json").read_text())
    assert pairing["excluded_unpaired_ids"] == ["unpaired"]
    for path in out.glob("*_rep*.json"):
        assert all(row["id"] != "unpaired"
                   for row in json.loads(path.read_text())["results"])


def test_the_bird_result_quotes_the_paired_per_question_interval():
    """Three answers to one question are not three independent facts.

    The recorded result first carried only an answer-level interval,
    [12.1, 26.9] over 195 answers, a half-width of 7.4 points. The paired
    (McNemar) interval over the 65 questions is [6.5, 30.4], half-width 12.0.
    Both are kept so the gap is visible; the per-question one is quoted.
    docs/METHODOLOGY.md: where the two disagree, that gap is the result.
    """
    import json
    import math
    import pathlib

    result = json.loads(
        pathlib.Path("examples/bird/enterprise_result.json").read_text())
    n = result["targeted_questions"]
    fixed, broke = result["question_fixed"], result["question_broke"]

    # The point estimate is fixed minus broke over the questions, not the
    # difference of two answer-level rates.
    assert result["question_lift_points"] == round(100 * (fixed - broke) / n, 1)

    # And the interval is the paired one, recomputed here from the counts so
    # a hand-edited JSON cannot drift away from its own numbers.
    se = math.sqrt(broke + fixed - (fixed - broke) ** 2 / n) / n
    diff = (fixed - broke) / n
    low, high = result["question_lift_ci_points"]
    assert low == round((diff - 1.96 * se) * 100, 1)
    assert high == round((diff + 1.96 * se) * 100, 1)

    # The answer-level pair is kept, and is narrower. That is the whole point
    # of showing both.
    a_low, a_high = result["answer_lift_ci_points"]
    assert (a_high - a_low) < (high - low)
