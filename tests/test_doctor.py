# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Moutasem Akkad <moutasemalakkad@gmail.com>
"""The preflight and the refusal-to-role map. No cloud, no credentials."""
from context_lift.doctor import (Result, explain, format_report, permissions_in, preflight)


def test_a_refusal_names_the_role_that_would_have_prevented_it():
    """A 403 names the permission and never the role, so an engineer at a
    customer site guesses and asks for too much."""
    message = ("403 Permission 'dataplex.entries.update' denied on resource "
               "'projects/p/locations/us/entryGroups/@bigquery/entries/...'")

    assert permissions_in(message) == ["dataplex.entries.update"]
    assert explain(message) == [
        "dataplex.entries.update: grant roles/dataplex.entryOwner on the entry group being written"]


def test_an_unverified_role_says_so_instead_of_looking_certain():
    """Some rows are marked "(verify)". A report that hides that
    sends someone to ask for a grant nobody has seen work."""
    lines = explain("Permission 'bigquery.jobs.listAll' denied")

    assert "roles/bigquery.resourceViewer" in lines[0]
    assert "not verified by a run here" in lines[0]


def test_several_permissions_in_one_message_all_get_a_line():
    message = ("denied: bigquery.jobs.create; also missing bigquery.tables.getData "
               "on the dataset")

    assert explain(message) == [
        "bigquery.jobs.create: grant roles/bigquery.jobUser on the billing project, the one in bq://",
        "bigquery.tables.getData: grant roles/bigquery.dataViewer on the dataset being read"]


def test_a_permission_nobody_recorded_says_where_to_add_it():
    lines = explain("Permission 'dataplex.somethingNew.frobnicate' denied")

    assert "no role recorded for dataplex.somethingNew.frobnicate." == lines[0]
    assert "PERMISSION_ROLES" in lines[1]


def test_a_message_with_no_permission_asks_for_the_whole_refusal():
    assert explain("it did not work") == [
        "no permission named in that message; paste the whole refusal."]


def test_a_table_name_in_the_message_is_not_read_as_a_permission():
    """`project.dataset.table` has the same shape as a permission. Reading
    one as the other would send someone to grant a role that does not exist."""
    assert permissions_in("table acme.sales.orders was not found") == []


def _preflight(**kwargs):
    base = dict(environ={}, can_import=lambda m: True, have_credentials=lambda: True)
    base.update(kwargs)
    return preflight(**base)


def test_an_unset_forbidden_list_is_a_warning_not_a_failure():
    """Nothing is refused, which is worth saying, but it does not stop a run
    the person asked for."""
    results = _preflight(project="my-project")
    forbidden = next(r for r in results if r.name == "forbidden projects")

    assert forbidden.ok and not forbidden.blocking
    assert "unset" in forbidden.detail


def test_a_project_on_the_forbidden_list_is_a_blocking_failure():
    results = _preflight(project="acme-prod-payments",
                         environ={"CONTEXTLIFT_FORBIDDEN_PROJECTS": "acme-prod,acme-restricted"})
    forbidden = next(r for r in results if r.name == "forbidden projects")

    assert not forbidden.ok and forbidden.blocking
    assert "acme-prod" in forbidden.detail


def test_a_missing_extra_prints_the_command_that_installs_it():
    results = _preflight(needs=("ca",), can_import=lambda m: False)
    extra = next(r for r in results if r.name == "[ca] extra")

    assert not extra.ok
    assert "uv pip install -e '.[ca]'" in extra.detail


def test_credentials_are_only_checked_when_something_needs_the_cloud():
    """An offline run must not fail on a missing credential it never uses."""
    assert not [r for r in _preflight() if r.name == "credentials"]
    assert [r for r in _preflight(needs=("bigquery",)) if r.name == "credentials"]


def test_a_missing_credential_prints_the_login_command():
    results = _preflight(needs=("bigquery",), have_credentials=lambda: False)
    credentials = next(r for r in results if r.name == "credentials")

    assert not credentials.ok
    assert "gcloud auth application-default login" in credentials.detail


def test_an_unwritable_workspace_is_a_failure(tmp_path):
    results = _preflight(workspace=tmp_path)
    assert next(r for r in results if r.name == "workspace").ok

    missing = _preflight(workspace=tmp_path / "not-there")
    assert not next(r for r in missing if r.name == "workspace").ok


def test_the_report_exits_1_only_on_a_blocking_failure():
    green, code = format_report([Result("a", True, "fine")])
    assert code == 0 and "no blocking problem found." in green

    warned, code = format_report([Result("a", False, "worth setting", blocking=False)])
    assert code == 0 and "1 thing(s) worth setting" in warned

    red, code = format_report([Result("a", False, "broken")])
    assert code == 1 and "1 blocking problem(s)" in red


def test_the_report_says_roles_were_not_probed():
    """A green line for a role nobody called would be a guess."""
    text, _ = format_report([Result("a", True, "fine")])

    assert "Roles are not probed here" in text
    assert "--explain" in text


def test_doctor_runs_from_the_command_line(capsys, tmp_path, monkeypatch):
    """The two things a person at a customer site types on day one."""
    from context_lift.cli import main

    monkeypatch.setenv("CONTEXTLIFT_FORBIDDEN_PROJECTS", "acme-prod")

    assert main(["doctor", "--project", "acme-prod-payments"]) == 1
    assert "acme-prod" in capsys.readouterr().out

    # --workspace is a global flag and comes before the subcommand; main()
    # refuses the other order with the corrected line, which is tested elsewhere
    assert main(["--workspace", str(tmp_path), "doctor",
                 "--project", "my-own-project"]) == 0
    assert "no blocking problem found." in capsys.readouterr().out

    assert main(["doctor", "--explain",
                 "Permission 'dataplex.entries.update' denied"]) == 0
    assert "roles/dataplex.entryOwner" in capsys.readouterr().out

    assert main(["doctor", "--needs", "nonsense"]) == 2
    assert "unknown extra(s): nonsense" in capsys.readouterr().out


def test_the_report_names_the_account_not_just_that_credentials_exist():
    """A machine can hold two accounts at once, a personal one and a
    customer's, and one gcloud command switches between them. "credentials
    resolve" cannot tell anybody which is about to be billed."""
    from context_lift.doctor import preflight

    results = preflight(needs=("bigquery",), can_import=lambda m: True,
                        have_credentials=lambda: "someone@example.com",
                        environ={})

    line = next(r for r in results if r.name == "credentials").line()
    assert "someone@example.com" in line
    assert "[ok" in line


def test_a_forbidden_account_fails_the_preflight():
    """The same shape as CONTEXTLIFT_FORBIDDEN_PROJECTS: configuration rather
    than source, because a guard that hardcodes a customer's domain publishes
    it to everyone who reads the repository."""
    from context_lift.doctor import preflight

    results = preflight(needs=("bigquery",), can_import=lambda m: True,
                        have_credentials=lambda: "someone@customer.example",
                        environ={"CONTEXTLIFT_FORBIDDEN_ACCOUNTS": "customer.example"})

    check = next(r for r in results if r.name == "credentials")
    assert not check.ok and check.blocking
    assert "customer.example" in check.detail


def test_no_credentials_still_says_how_to_get_them():
    from context_lift.doctor import preflight

    results = preflight(needs=("bigquery",), can_import=lambda m: True,
                        have_credentials=lambda: None, environ={})

    check = next(r for r in results if r.name == "credentials")
    assert not check.ok
    assert "gcloud auth application-default login" in check.detail


def test_the_configuration_name_is_not_the_account(tmp_path, monkeypatch):
    """One machine had five configurations, an active one called `acme`
    holding a personal account and a `default` holding a customer's. Trusting
    either the name or the default would have reported the wrong one."""
    from context_lift import doctor

    root = tmp_path / ".config" / "gcloud"
    (root / "configurations").mkdir(parents=True)
    (root / "active_config").write_text("acme\n", encoding="utf-8")
    (root / "configurations" / "config_acme").write_text(
        "[core]\naccount = personal@example.com\n", encoding="utf-8")
    (root / "configurations" / "config_default").write_text(
        "[core]\naccount = someone@customer.example\n", encoding="utf-8")
    monkeypatch.setattr(doctor.pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    assert doctor._gcloud_active_account() == "personal@example.com"
