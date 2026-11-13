"""The warehouse tooling targets ``$EDAI1_WH_CONTAINER``, defaulting to the original container."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

import data_platform.storage.warehouse.__main__ as cli
from data_platform.storage.warehouse import CONTAINER, CONTAINER_ENV, container_name
from data_platform.storage.warehouse.postgres import Psql

OFFICIAL = "edai1-wh5gb-evidence-postgres"


class Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, **_):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "1", "")


def test_default_container_is_unchanged(monkeypatch):
    monkeypatch.delenv(CONTAINER_ENV, raising=False)
    assert CONTAINER == "edai1-wh5gb-postgres"
    assert container_name() == CONTAINER
    assert Psql().container == CONTAINER


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_variable_keeps_the_default(monkeypatch, value):
    monkeypatch.setenv(CONTAINER_ENV, value)
    assert Psql().container == CONTAINER


def test_variable_selects_the_official_container(monkeypatch):
    monkeypatch.setenv(CONTAINER_ENV, OFFICIAL)
    assert container_name() == OFFICIAL
    assert Psql().container == OFFICIAL


def test_variable_is_read_when_psql_is_created_not_at_import(monkeypatch):
    monkeypatch.delenv(CONTAINER_ENV, raising=False)
    before = Psql()
    monkeypatch.setenv(CONTAINER_ENV, OFFICIAL)
    assert (before.container, Psql().container) == (CONTAINER, OFFICIAL)


def test_every_sql_path_execs_into_the_selected_container(monkeypatch):
    monkeypatch.setenv(CONTAINER_ENV, OFFICIAL)
    runner = Recorder()
    psql = Psql(runner=runner)
    psql.run("SELECT 1")
    psql.scalar("SELECT 1")
    psql.copy_in("\\copy t FROM STDIN", io.BytesIO(b""))
    assert len(runner.calls) == 3
    for args in runner.calls:
        assert args[:4] == ["docker", "exec", "-i", OFFICIAL]
        assert CONTAINER not in args
        assert args[4] == "psql"
        assert args[9:13] == ["-U", "warehouse", "-d", "warehouse"]


def test_explicit_container_argument_still_wins(monkeypatch):
    monkeypatch.setenv(CONTAINER_ENV, OFFICIAL)
    assert Psql(container="other").container == "other"


def test_cli_database_commands_use_and_report_the_selected_container(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(CONTAINER_ENV, OFFICIAL)
    seen: dict[str, str] = {}

    def fake_load(snapshot_dir: Path, psql: Psql):
        seen["container"] = psql.container
        return {"rows": 0}

    monkeypatch.setattr("data_platform.storage.warehouse.load.load", fake_load)
    assert cli.main(["load", "--snapshot-dir", str(tmp_path)]) == 0
    assert seen["container"] == OFFICIAL
    assert f"warehouse target container: {OFFICIAL}" in capsys.readouterr().err
