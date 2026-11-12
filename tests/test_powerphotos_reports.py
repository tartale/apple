"""The report PowerPhotos writes for a copy."""
import sqlite3

from apple.photos import powerphotos_reports as reports


def bundle(folder, name, results=(), end=2.0, cancelled=0, error=None, operation_log=None):
    path = folder / name
    path.mkdir(parents=True)
    connection = sqlite3.connect(path / "Assets.db")
    connection.execute("create table assets (libraryIdentifier text, uuid text, filename text, status integer, result integer, resultDescription text)")
    connection.execute("create table reportMedatata (operationType integer, wasCancelled integer, startDate real, endDate real, error blob)")
    for uuid, result, description in results:
        connection.execute("insert into assets values ('L', ?, 'x.jpg', 1, ?, ?)", (uuid, result, description))
    connection.execute("insert into reportMedatata values (1, ?, 1.0, ?, ?)", (cancelled, end, error))
    connection.commit()
    connection.close()
    if operation_log is not None:
        (path / "operation.log").write_text(operation_log, encoding="utf-8")
    return path


def test_a_new_report_for_the_library_is_found_by_its_name(tmp_path, monkeypatch):
    monkeypatch.setenv(reports.REPORTS_VARIABLE, str(tmp_path))
    bundle(tmp_path, "2026-11-11-171415 Copy 1 photo to library “photos-2023”.ppreport")
    before = reports.snapshot()
    assert reports.find(before, "photos-2023") is None
    older = bundle(tmp_path, "2026-11-11-172132 Copy 1 photo to library “photos-2023”.ppreport")
    newer = bundle(tmp_path, "2026-11-11-172421 Copy 1 photo to library “photos-2023”.ppreport")
    bundle(tmp_path, "2026-11-11-172500 Copy 1 photo to library “photos-2024”.ppreport")
    assert older.exists() and reports.find(before, "photos-2023") == newer  # the newest of the new ones, and only for that library
    assert reports.find(before, "nowhere") is None


def test_failures_are_read_by_photo_identifier_with_the_error_text(tmp_path):
    path = bundle(tmp_path, "r.ppreport", [
        ("AAAA/L0/001", 8, 'Photos failed to import the photo “IMG_1012.JPG” (Code=3302)'),
        ("BBBB", 6, ""), ("CCCC", 2, None), ("DDDD", 9, "  analysis\n failed "), ("EEEE", 1, "")])
    report = reports.read(path)
    assert report.finished and report.error is None
    assert report.failed == {
        "AAAA": 'Photos failed to import the photo “IMG_1012.JPG” (Code=3302)',
        "CCCC": "PowerPhotos skipped it (its resources are missing, or it is unsupported)",
        "DDDD": "analysis failed",
    }


def test_an_operation_that_has_not_ended_is_not_finished_and_one_that_was_cancelled_is(tmp_path):
    assert not reports.read(bundle(tmp_path, "running.ppreport", end=None)).finished
    assert reports.read(bundle(tmp_path, "cancelled.ppreport", end=None, cancelled=1)).finished


def test_the_operation_error_is_its_description_and_what_to_do(tmp_path):
    blob = '{"code":10,"errorDescription":"The contents of the library “photos-2024”\\ncould not be read.","recoverySuggestion":"Please try opening it in Photos."}'.encode()
    report = reports.read(bundle(tmp_path, "r.ppreport", error=blob))
    assert report.error == "The contents of the library “photos-2024” could not be read. Please try opening it in Photos."
    assert reports.error_text(b"not json: something went wrong here") == "not json: something went wrong here"
    assert reports.error_text(None) is None


def test_the_operation_log_says_what_stopped_an_operation_that_recorded_no_error(tmp_path):
    log = '2026-11-12 10:00:00.000  Session “Copy” started — 1 source library\n2026-11-12 10:00:02.000  Error shown: Photos is open.  Quit it.\n'
    assert reports.read(bundle(tmp_path, "a.ppreport", operation_log=log)).error == "Photos is open. Quit it."
    failed = '2026-11-12 10:00:00.000  Session “Copy” failed (the library is locked) after 2s — 0 photos\n'
    assert reports.read(bundle(tmp_path, "b.ppreport", operation_log=failed)).error == "the library is locked"
    assert reports.read(bundle(tmp_path, "c.ppreport", operation_log="Session “Copy” completed after 2s\n")).error is None
    assert reports.read(bundle(tmp_path, "d.ppreport", end=None, operation_log=log)).error is None  # still running: nothing is concluded yet


def test_a_report_that_cannot_be_read_is_none(tmp_path):
    (tmp_path / "broken.ppreport").mkdir()
    assert reports.read(tmp_path / "broken.ppreport") is None


def test_no_reports_folder_has_no_reports(tmp_path, monkeypatch):
    monkeypatch.setenv(reports.REPORTS_VARIABLE, str(tmp_path / "missing"))
    assert reports.snapshot() == set() and reports.find(set(), "x") is None


def test_the_error_may_be_stored_as_text_as_it_is_in_a_real_report(tmp_path):
    text = '{"code":10,"errorDescription":"The contents of the library could not be read.","recoverySuggestion":"Open it in Photos."}'
    path = bundle(tmp_path, "r.ppreport", error=text)  # a str, so sqlite stores text and reads it back as str
    assert reports.read(path).error == "The contents of the library could not be read. Open it in Photos."


def test_a_report_in_an_unexpected_shape_is_none_and_never_an_exception(tmp_path):
    path = bundle(tmp_path, "odd.ppreport", error=12345)  # an integer where an error should be
    assert reports.read(path) is None or reports.read(path).error in (None, "12345", "an error")
    path = tmp_path / "worse.ppreport"
    path.mkdir()
    connection = sqlite3.connect(path / "Assets.db")
    connection.execute("create table reportMedatata (x)")
    connection.execute("create table assets (x)")
    connection.commit()
    connection.close()
    assert reports.read(path) is None  # the columns are not there
