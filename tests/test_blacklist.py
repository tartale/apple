"""The list of photos that are left out of copies, and why."""
import json

import pytest

from apple.photos import blacklist


@pytest.fixture(autouse=True)
def own_file(tmp_path, monkeypatch):
    monkeypatch.setenv(blacklist.FILE_VARIABLE, str(tmp_path / "photos" / "blacklist.json"))


def test_the_default_place_is_the_photos_folder_of_the_cache_and_the_variable_changes_it(tmp_path, monkeypatch):
    monkeypatch.delenv(blacklist.FILE_VARIABLE)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert blacklist.path() == tmp_path / "cache" / "apple" / "photos" / "blacklist.json"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert str(blacklist.path()).endswith("/.cache/apple/photos/blacklist.json")
    monkeypatch.setenv(blacklist.FILE_VARIABLE, str(tmp_path / "elsewhere.json"))
    assert blacklist.path() == tmp_path / "elsewhere.json"


def test_an_entry_has_a_kind_and_is_written_with_a_version(tmp_path):
    assert blacklist.entries() == [] and blacklist.listed("Lib", "U1") is None
    blacklist.add(blacklist.UNCOPYABLE, "Lib", "U1", "IMG_1012.JPG", "2023-03-27 20:14:00", "Photos failed to import it")
    written = json.loads(blacklist.path().read_text())
    assert written["version"] == 1 and len(written["entries"]) == 1
    entry = written["entries"][0]
    assert (entry["kind"], entry["library"], entry["uuid"], entry["name"], entry["date"], entry["reason"]) == ("uncopyable", "Lib", "U1", "IMG_1012.JPG", "2023-03-27 20:14:00", "Photos failed to import it")
    assert entry["recorded"][:2] == "20"


def test_a_photo_can_be_blacklisted_once_for_each_kind_and_a_kind_is_replaced_not_repeated():
    blacklist.add("uncopyable", "Lib", "U1", "a.jpg", "d", "first")
    blacklist.add("uncopyable", "Lib", "U1", "a.jpg", "d", "second")  # the same kind again replaces it
    blacklist.add("another-reason", "Lib", "U1", "a.jpg", "d", "other")  # a different kind is kept beside it
    blacklist.add("uncopyable", "Other", "U1", "a.jpg", "d", "another library")
    assert [(e["library"], e["kind"], e["reason"]) for e in blacklist.entries()] == [  # in the order recorded
        ("Lib", "uncopyable", "second"), ("Lib", "another-reason", "other"), ("Other", "uncopyable", "another library")]
    assert blacklist.listed("Lib", "U1", "uncopyable")["reason"] == "second" and blacklist.listed("Lib", "U1", "nothing") is None
    assert blacklist.listed("Lib", "U1") is not None  # any kind
    blacklist.remove("uncopyable", "Lib", "U1")
    assert [(e["library"], e["kind"]) for e in blacklist.entries()] == [("Lib", "another-reason"), ("Other", "uncopyable")]


def test_a_file_that_cannot_be_used_is_reported_and_not_overwritten():
    blacklist.path().parent.mkdir(parents=True)
    for text in ("not json", json.dumps({"version": 2, "entries": []}), json.dumps(["a", "list"])):
        blacklist.path().write_text(text)
        with pytest.raises(blacklist.BlacklistError, match="not a blacklist this version understands"):
            blacklist.add("uncopyable", "Lib", "U1", "a.jpg", "d", "r")
        assert blacklist.path().read_text() == text  # left as it was
