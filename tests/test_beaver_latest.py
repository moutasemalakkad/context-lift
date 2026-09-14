import csv
import io
import json

from examples.beaver.prepare_latest import fetch_rows, write_sample


class Response(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_fetch_rows_paginates_and_keeps_token_in_a_header():
    seen = []

    def opener(request):
        seen.append(request)
        offset = 0 if "offset=0" in request.full_url else 2
        rows = [
            {"row": {"id": f"q{i}", "question": f"Q{i}", "sql": "SELECT 1"}}
            for i in range(offset, min(offset + 2, 3))
        ]
        return Response(json.dumps({"num_rows_total": 3, "rows": rows}))

    assert [row["id"] for row in fetch_rows("private-token", opener)] == ["q0", "q1", "q2"]
    assert len(seen) == 2
    assert all(request.get_header("Authorization") == "Bearer private-token"
               for request in seen)
    assert all("private-token" not in request.full_url for request in seen)


def test_write_sample_is_deterministic_and_uses_contextlift_columns(tmp_path):
    rows = [
        {"id": f"q{i}", "question": f"Question {i}", "sql": f"SELECT {i}"}
        for i in range(10)
    ]
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    write_sample(rows, str(first), sample=4, seed=77)
    write_sample(rows, str(second), sample=4, seed=77)

    assert first.read_bytes() == second.read_bytes()
    written = list(csv.DictReader(first.open()))
    assert list(written[0]) == ["id", "question", "gold_sql", "author", "origin"]
    assert len(written) == 4
    assert all(row["author"] == "BEAVER" and "seed 77" in row["origin"]
               for row in written)

