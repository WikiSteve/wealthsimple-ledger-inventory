import json
import os
import queue
from pathlib import Path
from unittest.mock import patch

import pytest

from src.full_account_inventory import RunState, ensure_dirs, import_exports
from scripts.wealthsimple_gui import (
    ACTIVITY_EXPORT_DOWNLOADER,
    RUNNER,
    Bundle,
    WealthsimpleAuditApp,
    browser_control_ready,
    browser_control_launch_command,
    browser_python,
    build_capture_command,
    build_fresh_activity_capture_command,
    discover_bundles,
    latest_export_downloads,
    latest_automated_activity_export,
    holdings_csv_from_stdout_line,
    report_path,
    run_dir_from_stdout_line,
    save_export_paths,
)
from src.browser_control_preflight import inspect_browser_control


def test_capture_command_uses_read_only_runner():
    command = build_capture_command("FULL")
    assert command[-2:] == ["--mode", "FULL"]
    assert command[1].endswith("scripts/run_full_inventory.py")


def test_capture_command_attaches_only_explicit_export_paths():
    activity = Path("/tmp/activities-export.csv")
    holdings = Path("/tmp/holdings-report.csv")
    command = build_capture_command("FULL", activity, holdings)
    assert command[-6:] == [
        "--mode", "FULL", "--activity-export", str(activity), "--holdings-export", str(holdings),
    ]


def test_fresh_activity_capture_downloads_then_runs_the_standard_audit():
    command = build_fresh_activity_capture_command("FULL")
    assert command == [
        str(browser_python()),
        str(ACTIVITY_EXPORT_DOWNLOADER),
        "--activities-12-months",
        "--download-holdings",
        "--run-audit",
        "--audit-mode",
        "FULL",
    ]
    assert "--activity-export" not in command


def test_fresh_activity_capture_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        build_fresh_activity_capture_command("TURBO")


def test_process_reader_waits_for_return_code_before_completion_sentinel():
    class FakeProcess:
        def __init__(self):
            self.stdout = iter(["first line\n"])
            self.returncode = None
            self.waited = False

        def wait(self):
            self.waited = True
            self.returncode = 0
            return 0

    app = object.__new__(WealthsimpleAuditApp)
    app.process = FakeProcess()
    app.output_queue = queue.Queue()

    WealthsimpleAuditApp._read_process_output(app)

    assert app.process.waited is True
    assert app.process.returncode == 0
    assert app.output_queue.get_nowait() == "first line\n"
    assert app.output_queue.get_nowait() is None


def test_browser_preflight_fails_closed_when_endpoint_is_unavailable():
    def unavailable(*_args, **_kwargs):
        raise OSError("not running")

    with patch("urllib.request.urlopen", unavailable):
        assert browser_control_ready() is False


def test_browser_launch_button_uses_only_the_local_browser_service_command():
    command = browser_control_launch_command()
    assert len(command) == 1
    assert command[0].endswith("/scripts/browser-control-launch.sh")


def test_browser_preflight_rejects_stale_browser_major():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "Browser": "Chrome/149.0.7827.200",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/example",
            }).encode()

    completed = type("Completed", (), {"stdout": "ChromeDriver 150.0.7871.186", "stderr": ""})()
    with patch("urllib.request.urlopen", return_value=Response()), patch(
        "subprocess.run", return_value=completed
    ):
        status = inspect_browser_control(chromedriver="/usr/bin/chromedriver")
    assert status.ready is False
    assert "browser 149, driver 150" in status.message
    assert "./scripts/browser-control-stop.sh && ./scripts/browser-control-launch.sh" in status.message


def test_browser_preflight_accepts_matching_attached_major():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({
                "Browser": "Chrome/150.0.7871.186",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9223/devtools/browser/example",
            }).encode()

    completed = type("Completed", (), {"stdout": "ChromeDriver 150.0.7871.186", "stderr": ""})()
    with patch("urllib.request.urlopen", return_value=Response()), patch(
        "subprocess.run", return_value=completed
    ):
        status = inspect_browser_control(chromedriver="/usr/bin/chromedriver")
    assert status.ready is True
    assert "browser/driver 150" in status.message


def test_run_directory_is_extracted_from_runner_zip_line():
    assert run_dir_from_stdout_line("/tmp/wealthsimple-full-account-inventory-20260709T120000-0300.zip\n") == Path(
        "/tmp/wealthsimple-full-account-inventory-20260709T120000-0300"
    )
    assert run_dir_from_stdout_line("WARN\n") is None


def test_fresh_holdings_path_is_extracted_from_downloader_output():
    path = Path("/tmp/downloads/holdings-report-2026-08-06.csv")
    assert holdings_csv_from_stdout_line(f"HOLDINGS_CSV={path}\n") == path
    assert holdings_csv_from_stdout_line("other output\n") is None


def test_discover_bundles_orders_newest_first(tmp_path: Path):
    first = tmp_path / "wealthsimple-full-account-inventory-old"
    second = tmp_path / "wealthsimple-full-account-inventory-new"
    for directory, status, generated_at in (
        (first, "OK", "2026-07-09T20:30:00-03:00"),
        (second, "WARN", "2026-07-09T20:35:00-03:00"),
    ):
        directory.mkdir()
        (directory / "manifest.json").write_text('{"status": "' + status + '", "generated_at": "' + generated_at + '"}')
    (first / "manifest.json").touch()
    bundles = discover_bundles(tmp_path)
    assert [bundle.directory for bundle in bundles] == [second, first]


def test_report_path_prefers_chatgpt_summary(tmp_path: Path):
    bundle_dir = tmp_path / "wealthsimple-full-account-inventory-test"
    bundle_dir.mkdir()
    bundle = Bundle(bundle_dir, {"status": "OK"})
    assert report_path(bundle).name == "inventory-report.md"
    (bundle_dir / "all-accounts-summary.md").write_text("# Summary")
    assert report_path(bundle).name == "all-accounts-summary.md"


# --------------------------------------------------------------------------
# Export attachment: exact command construction, selection validation, and the
# guarantee that convenience features cannot abort a capture.
# --------------------------------------------------------------------------


def test_capture_command_attaches_each_export_independently():
    activity = Path("/tmp/activities-export-2026-07-28.csv")
    holdings = Path("/tmp/holdings-report-2026-07-28.csv")
    base = [str(browser_python()), str(RUNNER), "--mode", "FULL"]
    assert build_capture_command("FULL") == base
    assert build_capture_command("FULL", activity, None) == base + ["--activity-export", str(activity)]
    assert build_capture_command("FULL", None, holdings) == base + ["--holdings-export", str(holdings)]
    assert build_capture_command("FULL", activity, holdings) == (
        base + ["--activity-export", str(activity), "--holdings-export", str(holdings)]
    )
    # nothing else is ever appended, so no path can be attached implicitly
    assert len(build_capture_command("FULL", activity, holdings)) == 8


def test_capture_command_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        build_capture_command("TURBO")


def test_selected_export_requires_an_existing_csv(tmp_path: Path):
    good = tmp_path / "activities-export-2026-07-28.csv"
    good.write_text("transaction_date\n2026-07-28\n", encoding="utf-8")
    assert WealthsimpleAuditApp._selected_export(str(good), "Activity export") == good
    assert WealthsimpleAuditApp._selected_export("   ", "Activity export") is None
    assert WealthsimpleAuditApp._selected_export("", "Activity export") is None
    with pytest.raises(ValueError, match="does not exist"):
        WealthsimpleAuditApp._selected_export(str(tmp_path / "missing.csv"), "Activity export")
    # the chooser offers an "All files" filter, so a wrong pick is rejected here
    other = tmp_path / "statement.pdf"
    other.write_bytes(b"%PDF-1.4 binary")
    with pytest.raises(ValueError, match="must be a .csv export"):
        WealthsimpleAuditApp._selected_export(str(other), "Activity export")
    directory = tmp_path / "folder.csv"
    directory.mkdir()
    with pytest.raises(ValueError, match="does not exist"):
        WealthsimpleAuditApp._selected_export(str(directory), "Holdings export")


def test_saving_the_selection_never_raises_when_the_location_is_unwritable(tmp_path: Path):
    target = tmp_path / "state.json"
    assert save_export_paths("/tmp/a.csv", "/tmp/h.csv", target) is True
    assert json.loads(target.read_text()) == {
        "activity_export": "/tmp/a.csv", "holdings_export": "/tmp/h.csv",
    }
    # a path whose parent cannot be created must be reported, not raised:
    # persistence is a convenience and must never abort a capture
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert save_export_paths("/tmp/a.csv", "", blocker / "nested" / "state.json") is False


def test_latest_export_downloads_picks_the_newest_of_each_kind(tmp_path: Path):
    older = tmp_path / "activities-export-2026-07-01.csv"
    newer = tmp_path / "activities-export-2026-07-28.csv"
    holdings = tmp_path / "holdings-report-2026-07-28.csv"
    unrelated = tmp_path / "receipts.csv"
    for path in (older, newer, holdings, unrelated):
        path.write_text("x\n", encoding="utf-8")
    os.utime(older, (1_600_000_000, 1_600_000_000))
    os.utime(newer, (1_700_000_000, 1_700_000_000))
    found = latest_export_downloads(tmp_path)
    assert found == {"activity": newer, "holdings": holdings}
    assert latest_export_downloads(tmp_path / "no-such-dir") == {}
    assert latest_export_downloads(tmp_path / "receipts.csv") == {}


def test_latest_automated_activity_export_uses_the_newest_download(tmp_path: Path):
    old_dir = tmp_path / "wealthsimple-csv-downloads-old"
    new_dir = tmp_path / "wealthsimple-csv-downloads-new"
    old_dir.mkdir()
    new_dir.mkdir()
    old = old_dir / "activities-export-2026-07-01.csv"
    new = new_dir / "activities-export-2026-07-28.csv"
    old.write_text("x\n", encoding="utf-8")
    new.write_text("x\n", encoding="utf-8")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    assert latest_automated_activity_export(tmp_path) == new
    assert latest_automated_activity_export(tmp_path / "missing") is None


def test_unreadable_export_is_ignored_rather_than_aborting_the_capture(tmp_path: Path):
    # Reachable from the GUI because the chooser allows "All files".
    ensure_dirs(tmp_path)
    binary = tmp_path / "statement.csv"
    binary.write_bytes(b"\xff\xfe\x00not-utf8\x80")
    state = RunState(out_dir=tmp_path, mode="FULL")
    provenance = import_exports(state, binary, None)
    assert provenance == {}
    assert any("could not be parsed and was ignored" in warning for warning in state.warnings)
