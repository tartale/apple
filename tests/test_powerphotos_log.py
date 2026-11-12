"""PowerPhotos' own log of a failed import, and the list of photos that cannot be copied."""
import json

from apple.photos import powerphotos_log as log
from apple.photos import uncopyable

# An entry as PowerPhotos 2026 wrote it (taken from a real PowerPhotos.buffer): binary framing, the text, binary again.
REAL_ENTRY = (
    "\x00" * 29 + " K<\x8a\xf0\x01\x00\x00\xda\x01\x00\x00\x00\x00\x00\x00\x00\xc2\xa8\xfa\xd9\x9d\x82\xb0\x06\x00\x01\xf2\xd7\xa3\x01"
    'Got PhotoKit error Error Domain=PHPhotosErrorDomain Code=3302 "Invalid resource (code 3302)" UserInfo={_PHResourceUrlsErrorKey=(\n'
    '    "file:///Volumes/home/Pictures/scratch3.photoslibrary/private/com.fatcatsoftware.PowerPhotos/assetresourcecache/Batch/40779445-2A40-4CAC-A58A-A4759F990C85/IMG_1012.JPG",\n'
    '    "file:///Volumes/home/Pictures/scratch3.photoslibrary/private/com.fatcatsoftware.PowerPhotos/assetresourcecache/Batch/FF2E5E9A-8DB8-4C43-A8EB-72666540A8C0/FullSizeRender.jpeg\xc4@>P"\x01\x00\x00\xdb\x01'
    + "\x00" * 8 + "\x01Could not read the photo's adjustment data to check its format"
)


def write_log(tmp_path, monkeypatch, text):
    path = tmp_path / "PowerPhotos.buffer"
    path.write_bytes(text.encode("latin-1"))
    monkeypatch.setenv(log.LOG_VARIABLE, str(path))
    return path


def test_a_failed_import_is_read_from_the_binary_framed_log(tmp_path, monkeypatch):
    write_log(tmp_path, monkeypatch, REAL_ENTRY)
    [failure] = log.failures()
    assert (failure.code, failure.message) == ("3302", "Invalid resource (code 3302)")
    assert failure.names == ("IMG_1012.JPG",)  # the photo, not the pieces of an edited photo (FullSizeRender, Adjustments)
    assert len(failure.urls) == 2


def test_only_the_failures_that_were_not_in_the_log_before_are_new(tmp_path, monkeypatch):
    write_log(tmp_path, monkeypatch, REAL_ENTRY)
    before = log.failures()
    assert log.new_failures(before) == []
    write_log(tmp_path, monkeypatch, REAL_ENTRY + REAL_ENTRY.replace("40779445", "99999999").replace("IMG_1012", "IMG_2000"))
    [new] = log.new_failures(before)
    assert new.names == ("IMG_2000.JPG",)


def test_a_missing_or_unreadable_log_has_no_failures(tmp_path, monkeypatch):
    monkeypatch.setenv(log.LOG_VARIABLE, str(tmp_path / "nothing.buffer"))
    assert log.failures() == [] and log.new_failures([]) == []


def test_file_names_with_escapes_are_read(tmp_path, monkeypatch):
    write_log(tmp_path, monkeypatch, REAL_ENTRY.replace("IMG_1012.JPG", "My%20Photo.JPG"))
    assert log.failures()[0].names == ("My Photo.JPG",)


def test_uncopyable_photos_are_remembered_per_library_and_forgotten(tmp_path, monkeypatch):
    monkeypatch.setenv(uncopyable.PATH_VARIABLE, str(tmp_path / "list.json"))
    assert uncopyable.entries() == {} and uncopyable.listed("Default Library", "U1") is None
    uncopyable.add("Default Library", "U1", "IMG_1012.JPG", "2023-03-27 20:14:00", "PhotoKit error 3302")
    assert uncopyable.listed("Default Library", "U1")["name"] == "IMG_1012.JPG" and uncopyable.listed("Other", "U1") is None
    assert list(json.loads((tmp_path / "list.json").read_text())) == ["Default Library\tU1"]
    uncopyable.remove("Default Library", "U1")
    assert uncopyable.entries() == {}


def test_a_url_with_no_file_name_in_it_names_no_photo_and_does_not_break_the_reading(tmp_path, monkeypatch):
    cut_off = REAL_ENTRY.replace("IMG_1012.JPG", "").replace("FullSizeRender.jpeg\xc4@>P", "Adjustments.plist")
    write_log(tmp_path, monkeypatch, cut_off)
    [failure] = log.failures()  # the first URL ends in a folder, the second is a part of an edited photo
    assert failure.names == () and failure.code == "3302"
    write_log(tmp_path, monkeypatch, REAL_ENTRY.split("Got PhotoKit")[0] + 'Got PhotoKit error Error Domain=PHPhotosErrorDomain Code=1 "x" UserInfo={_PHResourceUrlsErrorKey=(\n    "file:///a/b/"\x01')
    assert log.failures()[0].names == ()
