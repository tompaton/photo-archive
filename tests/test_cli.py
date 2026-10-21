from conftest import image

from photo_archive_v3.cli import main


def test_cli_workflow_and_stale_noninteractive(workspace, capsys):
    source, _, _, database = workspace
    image(source / "20260102_030405.jpg")
    prefix = ["--db", str(database)]
    for command in ("scan", "analyze"):
        assert main(prefix + [command]) == 0
    output = capsys.readouterr()
    assert "Scan 1:" in output.err and "Analyze 1/1" in output.err
    assert (
        main(prefix + ["list", "--queue", "ready", "--camera", "CAM", "--year", "2026"])
        == 0
    )
    assert "ready" in capsys.readouterr().out
    assert main(prefix + ["filename-rule", "--pattern", "never", "--skip-final"]) == 0
    capsys.readouterr()
    assert main(prefix + ["sync"]) == 1
    assert "out-of-date" in capsys.readouterr().err
    assert main(prefix + ["sync", "--dry-run"]) == 1
    assert "out-of-date" in capsys.readouterr().err
    assert main(prefix + ["sync", "--dry-run", "--allow-stale"]) == 0
    assert "1 would archive" in capsys.readouterr().out
    assert main(prefix + ["sync", "--allow-stale"]) == 0
    assert "1 archived" in capsys.readouterr().out
    assert main(prefix + ["config", "cameras"]) == 0
    assert "min_date" in capsys.readouterr().out


def test_invalid_rule_not_saved(workspace, capsys):
    _, _, _, database = workspace
    assert main(["--db", str(database), "filename-rule", "--pattern", "["]) == 1
    assert "Error" in capsys.readouterr().err
