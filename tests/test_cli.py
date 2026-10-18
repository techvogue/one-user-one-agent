"""CLI smoke test on a tiny synthetic pack: load, then inspect a viewer."""

import json

from conftest import make_event, write_jsonl
from test_profiles_catalogue import PROFILE_COLS, make_catalogue, profile_row, write_csv

from ott_agent.__main__ import main


def test_load_then_inspect(tmp_path, capsys):
    pack = tmp_path / "pack"
    (pack / "data").mkdir(parents=True)
    write_jsonl(
        pack / "data" / "events.jsonl",
        [make_event("e1"), make_event("e1", received="2026-09-10T10:01:00.000Z")],
    )
    write_csv(pack / "data" / "profiles_snapshot.csv", PROFILE_COLS, [profile_row()])
    make_catalogue(pack / "data")
    out = tmp_path / "run"
    assert main(["load", "--pack", str(pack), "--out", str(out)]) == 0
    assert (
        json.loads((out / "data_quality_report.json").read_text())["duplicates"]["history"][
            "duplicate_event_ids"
        ]
        == 1
    )
    capsys.readouterr()
    assert (
        main(
            [
                "inspect",
                "--pack",
                str(pack),
                "--out",
                str(out),
                "--viewer",
                "U-1",
                "--at",
                "2026-09-11T00:00:00Z",
                "--json",
                "--save",
            ]
        )
        == 0
    )
    facts = json.loads(capsys.readouterr().out)["facts"]
    assert facts["message_dialect"]["value"] == "d1"
    assert facts["page_dialect"]["value"] is None  # no trigger
    assert main(["inspect", "--pack", str(pack), "--out", str(out), "--viewer", "U-1"]) == 0
    assert "push_consent" in capsys.readouterr().out
