"""apple.photos.matching: when two photos are the same photo."""
from collections import Counter
from datetime import timedelta

import pytest

from apple.photos import matching as m

A = ("2018-03-01 10:00:00", "1000", "IMG_0001.JPG")


def test_nothing_is_the_default_tolerant_mode():
    assert m.parse("") == m.TOLERANT == m.parse("tolerant")
    assert m.TOLERANT.filename == "full" and m.TOLERANT.date_within == timedelta(hours=2) and m.TOLERANT.size_within == 0.01


def test_a_mode_is_read_from_left_to_right_starting_from_the_default():
    mode = m.parse("exact,filename=prefix=8,date=within=1d,filesize=within=2.5%")
    assert (mode.filename, mode.prefix, mode.date, mode.date_within, mode.filesize, mode.size_within) == ("prefix", 8, "within", timedelta(days=1), "within", 0.025)
    assert m.parse("filename=exact") == m.Mode(filename="exact")
    assert m.parse("extension=ignore").extension == "ignore" and m.parse("exact,extension=full").extension == "full"
    assert m.parse("date=ignore,exact") == m.EXACT  # a whole mode after a field starts over
    assert m.parse("date=within=90m").date_within == timedelta(minutes=90)
    assert m.parse("filesize=within=512bytes").size_bytes == 512 and m.parse("filesize=within=1byte").size_bytes == 1 and m.parse("filesize=within=20b").size_bytes == 20
    assert m.parse("filesize=within=512bytes,filesize=within=2%").size_bytes is None  # the last one wins


@pytest.mark.parametrize("text", ["filename", "filename=prefix", "filename=prefix=0", "filename=prefix=x", "filename=sideways", "extension", "extension=any", "date=within",
                                  "date=within=2", "date=within=2w", "date=near", "filesize=within=1", "filesize=within=1.5bytes", "filesize=within=x%", "exif=maybe", "date=sometimes", "strict"])
def test_what_is_not_a_mode_says_what_is_wrong(text):
    with pytest.raises(ValueError):
        m.parse(text)


def test_the_default_ignores_case_and_jpeg_and_allows_two_hours_and_one_percent():
    held = Counter({A: 1})
    near = ("2018-03-01 11:59:59", "1009", "img_0001.jpeg")
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "img_0001.jpg"): 1}), held)
    assert not m.absent(Counter({near: 1}), held)  # .jpeg is .jpg, case ignored, just under two hours and 1%
    assert m.absent(Counter({("2018-03-01 12:00:01", "1000", "IMG_0001.JPG"): 1}), held)  # more than two hours
    assert m.absent(Counter({("2018-03-01 10:00:00", "1011", "IMG_0001.JPG"): 1}), held)  # more than 1%


def test_exact_wants_the_same_date_size_and_name_as_written():
    held = Counter({A: 1})
    assert not m.absent(Counter({A: 1}), held, m.EXACT)
    for other in (("2018-03-01 10:00:01", "1000", "IMG_0001.JPG"), ("2018-03-01 10:00:00", "1001", "IMG_0001.JPG"), ("2018-03-01 10:00:00", "1000", "img_0001.jpg")):
        assert m.absent(Counter({other: 1}), held, m.EXACT)


def test_a_prefix_of_the_name_is_enough_with_filename_prefix():
    held = Counter({A: 1})
    edited = ("2018-03-01 10:00:00", "1000", "IMG_0001-edited.JPG")
    assert m.absent(Counter({edited: 1}), held)
    assert not m.absent(Counter({edited: 1}), held, m.parse("filename=prefix=8"))
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_0002.JPG"): 1}), held, m.parse("filename=prefix=8"))
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_0002.JPG"): 1}), held, m.parse("filename=prefix=7"))


def test_a_field_set_to_ignore_does_not_count():
    held = Counter({A: 1})
    assert not m.absent(Counter({("2020-01-01 00:00:00", "1000", "IMG_0001.JPG"): 1}), held, m.parse("date=ignore"))
    assert not m.absent(Counter({("2018-03-01 10:00:00", "5", "IMG_0001.JPG"): 1}), held, m.parse("filesize=ignore"))
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "OTHER.JPG"): 1}), held, m.parse("filename=ignore"))


def test_an_unknown_size_does_not_tell_photos_apart_unless_sizes_must_be_exact():
    held = Counter({A: 1})
    placeholder = ("2018-03-01 10:00:00", "0", "IMG_0001.JPG")
    assert not m.absent(Counter({placeholder: 1}), held)
    assert m.absent(Counter({placeholder: 1}), held, m.EXACT)


def test_pairing_matches_each_photo_with_at_most_one_other_and_the_extra_copies_are_left_over():
    first = Counter({A: 3, ("2018-03-02 10:00:00", "1", "b.jpg"): 1})
    second = Counter({("2018-03-01 10:30:00", "1000", "IMG_0001.JPG"): 1, ("2018-03-09 10:00:00", "1", "c.jpg"): 1})
    only_first, only_second, both = m.pair(first, second)
    assert both == 1 and only_first == Counter({A: 2, ("2018-03-02 10:00:00", "1", "b.jpg"): 1}) and only_second == Counter({("2018-03-09 10:00:00", "1", "c.jpg"): 1})


def test_exact_pairing_is_plain_counting():
    first, second = Counter({A: 2}), Counter({A: 1, ("x", "1", "y"): 1})
    assert m.pair(first, second, m.EXACT) == (Counter({A: 1}), Counter({("x", "1", "y"): 1}), 1)


def test_date_ignore_pairs_by_size_and_name_as_a_clip_imported_twice_with_a_shifted_time():
    first = Counter({("2018-03-01 10:00:00", "1000", "a.mov"): 1})
    second = Counter({("2019-07-01 10:00:00", "1000", "a.mov"): 1})
    assert m.pair(first, second, m.parse("exact,date=ignore")) == (Counter(), Counter(), 1)


def test_a_size_can_differ_by_a_number_of_bytes_whatever_the_size():
    held = Counter({("2018-03-01 10:00:00", "1000000", "a.jpg"): 1, ("2018-03-01 10:00:00", "100", "b.jpg"): 1})
    mode = m.parse("filesize=within=50bytes")
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000050", "a.jpg"): 1}), held, mode)  # 50 bytes of a million: far under 1%
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000051", "a.jpg"): 1}), held, mode)
    assert m.absent(Counter({("2018-03-01 10:00:00", "151", "b.jpg"): 1}), held, mode)  # 51 bytes off a hundred: over the 50
    assert not m.absent(Counter({("2018-03-01 10:00:00", "150", "b.jpg"): 1}), held, mode)  # 50 bytes off a hundred: 50%, but within the 50 bytes


def test_a_regex_can_hold_commas_and_ends_at_the_next_part():
    mode = m.parse(r"filename=regex=^(img_\d{1,4}),date=exact")
    assert (mode.filename, mode.pattern, mode.date) == ("regex", r"^(img_\d{1,4})", "exact")
    assert m.parse("exact,filename=regex=a=b").pattern == "a=b"  # an = inside is part of the pattern


@pytest.mark.parametrize("text", ["filename=regex", "filename=regex=", "filename=regex=(unclosed"])
def test_what_is_not_a_regex_is_an_error(text):
    with pytest.raises(ValueError):
        m.parse(text)


def test_a_regex_compares_what_it_matches_in_the_name_case_ignored():
    held = Counter({("2018-03-01 10:00:00", "1000", "20180301_100000_a.jpg"): 1})
    mode = m.parse(r"filename=regex=^(\d{8}_\d{6})")
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "20180301_100000_b.JPG"): 1}), held, mode)  # what follows the time is not compared
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "20180301_100001_a.jpg"): 1}), held, mode)  # another second
    ignoring_case = m.parse(r"filename=regex=(img_\d+)")
    other = Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.jpg"): 1})
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "img_1234_edited.JPG"): 1}), other, ignoring_case)


def test_a_regex_uses_the_whole_match_without_a_group_and_the_whole_name_for_a_name_it_does_not_match():
    held = Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.jpg"): 1})
    mode = m.parse(r"filename=regex=IMG_\d+")
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234-edited.jpg"): 1}), held, mode)
    unmatched = Counter({("2018-03-01 10:00:00", "1000", "holiday.jpg"): 1})
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "HOLIDAY.JPG"): 1}), unmatched, mode)  # read as full reads it
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "beach.jpg"): 1}), unmatched, mode)
    optional = m.parse("filename=regex=(zz)?a")  # the group takes no part in these names: the whole match is compared
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "ab.jpg"): 1}), Counter({("2018-03-01 10:00:00", "1000", "ac.jpg"): 1}), optional)


def test_the_name_is_compared_without_its_extension_and_the_extension_on_its_own():
    held = Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.HEIC"): 1})
    heic_as_jpg = Counter({("2018-03-01 10:00:00", "1000", "img_1234.jpg"): 1})
    assert m.absent(heic_as_jpg, held)  # another extension is another photo, by default
    assert not m.absent(heic_as_jpg, held, m.parse("extension=ignore"))
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_9999.heic"): 1}), held, m.parse("extension=ignore"))  # the name still counts
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "img_1234.heic"): 1}), held)  # case ignored by default
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "img_1234.heic"): 1}), held, m.parse("extension=exact"))
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.heic"): 1}), held, m.EXACT)
    jpeg = Counter({("2018-03-01 10:00:00", "1000", "a.JPEG"): 1})
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "a.jpg"): 1}), jpeg)  # .jpeg is .jpg
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "a.jpg"): 1}), jpeg, m.parse("extension=exact"))


def test_filename_modes_look_at_the_name_without_its_extension():
    held = Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.jpg"): 1})
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234-edited.jpg"): 1}), held, m.parse("filename=prefix=8"))
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234.jpg"): 1}), Counter({("2018-03-01 10:00:00", "1000", "IMG_12.jpg"): 1}), m.parse("filename=prefix=8"))  # IMG_12 has 6 characters, not 8
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "a.jpg"): 1}), Counter({("2018-03-01 10:00:00", "1000", "a.jpg.jpg"): 1}), m.parse("filename=regex=^(.*)$"))  # the last extension only


def finder(*photos, mode=m.TOLERANT):
    return m.Finder(mode, list(photos))


def test_the_closest_photo_is_the_one_that_differs_in_the_fewest_parts():
    held = [("2018-03-01 10:00:00", "1000", "a.jpg"), ("2018-03-01 10:00:00", "5000", "other.jpg"), ("2019-01-01 00:00:00", "1000", "a.jpg")]
    wanted = ("2018-03-01 10:00:00", "2000", "a.jpg")
    near = finder(*held).closest(wanted)
    assert near.photo == held[0] and near.differs == ("size by 1,000 bytes (50.0%)",) and near.gap == 0 and near.bytes_apart == 1000


def test_what_differs_is_said_in_words_for_each_part():
    held = ("2018-03-01 10:00:00", "1000", "IMG_1.jpg")
    near = finder(held).closest(("2018-03-04 14:30:00", "1000", "IMG_2.heic"))
    assert near.differs == ("name", "extension", "date by 3d 4h")  # the two largest units
    assert finder(held).closest(("2018-03-01 10:00:30", "1000", "IMG_1.jpg")).differs == ()  # a match: nothing differs


def test_the_same_date_and_size_under_another_name_is_found_though_nothing_shares_the_name():
    held = ("2018-03-01 10:00:00", "1000", "IMG_1.jpg")
    near = finder(held, ("2020-01-01 00:00:00", "5", "z.jpg")).closest(("2018-03-01 10:00:00", "1000", "IMG_1_copy.jpg"))
    assert near.photo == held and near.differs == ("name",)


def test_nothing_in_the_library_means_no_closest_photo():
    assert finder().closest(("2018-03-01 10:00:00", "1000", "a.jpg")) is None


def test_a_gap_is_told_in_two_units():
    assert [m.span(s) for s in (0, 40, 3725, 90000, 345600)] == ["0s", "40s", "1h 2m", "1d 1h", "4d"]


def test_the_date_of_a_folder_file_comes_from_exif_unless_the_mode_says_modified():
    assert m.TOLERANT.date_source == "exif" and m.EXACT.date_source == "exif"
    assert m.parse("date=modified").date_source == "modified" and m.parse("date=exif").date_source == "exif"
    assert m.parse("date=modified,date=within=1h") == m.Mode(date_source="modified", date_within=timedelta(hours=1))  # a source and a comparison are separate parts
    assert m.parse("date=modified,exact").date_source == "exif"  # a whole mode starts over
    assert m.parse("filename=regex=a,b,date=modified").pattern == "a,b"  # a comma before date= ends a regex too
