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
    mode = m.parse("exact,filename=full,date=within=1d,filesize=within=2.5%")
    assert (mode.filename, mode.date, mode.date_within, mode.filesize, mode.size_within) == ("full", "within", timedelta(days=1), "within", 0.025)
    assert m.parse("filename=exact") == m.Mode(filename="exact")
    assert m.parse("extension=ignore").extension == "ignore" and m.parse("exact,extension=full").extension == "full"
    assert m.parse("date=ignore,exact") == m.EXACT  # a whole mode after a field starts over
    assert m.parse("date=within=90m").date_within == timedelta(minutes=90)
    assert m.parse("filesize=within=512bytes").size_bytes == 512 and m.parse("filesize=within=1byte").size_bytes == 1 and m.parse("filesize=within=20b").size_bytes == 20
    assert m.parse("filesize=within=512bytes,filesize=within=2%").size_bytes is None  # the last one wins


@pytest.mark.parametrize("text", ["filename", "filename=prefix=8", "filename=regex=^(a)", "filename=sideways", "extension", "extension=any", "date=within",
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
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234-edited.jpg"): 1}), held)  # another name
    assert not m.absent(Counter({("2018-03-01 10:00:00", "1000", "IMG_1234-edited.jpg"): 1}), held, m.parse("filename=ignore"))
    assert m.absent(Counter({("2018-03-01 10:00:00", "1000", "a.jpg"): 1}), Counter({("2018-03-01 10:00:00", "1000", "a.jpg.jpg"): 1}))  # the last extension only


def test_the_removed_filename_options_are_errors_that_say_what_there_is():
    for text in ("filename=prefix=8", "filename=regex=^(a)"):
        with pytest.raises(ValueError, match="use full, exact or ignore"):
            m.parse(text)


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
    assert m.TOLERANT.date_source == "modified" and m.EXACT.date_source == "modified" and m.parse("").date_source == "modified"
    assert m.parse("date=exif").date_source == "exif" and m.parse("date=modified").date_source == "modified"
    assert m.parse("date=exif,date=within=1h") == m.Mode(date_source="exif", date_within=timedelta(hours=1))  # a source and a comparison are separate parts
    assert m.parse("date=exif,exact").date_source == "modified"  # a whole mode starts over


def ids(**kinds):
    return {kind: {value} for kind, value in kinds.items()}


def test_photos_with_the_same_id_are_the_same_photo_whatever_their_name_date_and_size():
    held = ("2006-09-30 06:51:40", "1492992", "IMAG0044.JPG")
    other = ("2005-09-30 06:51:40", "999", "renamed.jpg")
    held_ids = {held: ids(stable="ASLNFDmS")}
    assert not m.absent(Counter({other: 1}), Counter({held: 1}), wanted_ids={other: ids(stable="ASLNFDmS")}, held_ids=held_ids)
    assert m.absent(Counter({other: 1}), Counter({held: 1}))  # without ids, nothing about them agrees


def test_photos_with_different_ids_are_not_the_same_photo_though_everything_else_agrees():
    held = ("2006-09-30 06:51:40", "1492992", "IMAG0044.JPG")
    near = ("2006-09-30 06:51:41", "1492992", "IMAG0044.JPG")
    assert m.absent(Counter({near: 1}), Counter({held: 1}), wanted_ids={near: ids(stable="B")}, held_ids={held: ids(stable="A")})
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), wanted_ids={near: ids(stable="A")}, held_ids={held: ids(stable="A")})


def test_an_id_that_only_one_side_has_does_not_decide():
    held = ("2006-09-30 06:51:40", "1492992", "IMAG0044.JPG")
    near = ("2006-09-30 06:51:50", "1492992", "IMAG0044.JPG")
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), wanted_ids={near: ids(stable="A")})  # the held photo has no id: the other parts agree
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), held_ids={held: ids(stable="A")})


def test_ids_of_different_kinds_do_not_conflict_and_a_shared_value_among_several_is_enough():
    held = ("2006-09-30 06:51:40", "1492992", "IMAG0044.JPG")
    near = ("2006-09-30 06:51:50", "1492992", "IMAG0044.JPG")
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), wanted_ids={near: ids(cloud="X")}, held_ids={held: ids(stable="A")})  # no kind in common: the other parts decide
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), wanted_ids={near: {"stable": {"A", "B"}}}, held_ids={held: {"stable": {"B", "C"}}})


def test_identity_ignore_and_the_exact_mode_use_the_other_parts_alone():
    held = ("2006-09-30 06:51:40", "1492992", "IMAG0044.JPG")
    other = ("2005-09-30 06:51:40", "999", "renamed.jpg")
    kwargs = dict(wanted_ids={other: ids(stable="A")}, held_ids={held: ids(stable="A")})
    assert m.absent(Counter({other: 1}), Counter({held: 1}), m.parse("identity=ignore"), **kwargs)
    assert m.absent(Counter({other: 1}), Counter({held: 1}), m.EXACT, **kwargs)
    assert m.EXACT.identity == "ignore" and m.TOLERANT.identity == "auto" and m.parse("exact,identity=auto").identity == "auto"
    with pytest.raises(ValueError):
        m.parse("identity=maybe")


def test_pairs_say_how_each_was_decided_and_prefer_the_photo_with_the_same_id():
    first = ("2006-09-30 06:51:40", "100", "a.jpg")
    close = ("2006-09-30 06:51:41", "100", "a.jpg")  # nearer in date
    far = ("2005-09-30 06:51:40", "100", "renamed.jpg")  # a year off, but the same id
    matched, only_first, only_second = m.pairs(Counter({first: 1}), Counter({close: 1, far: 1}), first_ids={first: ids(stable="S")}, second_ids={far: ids(stable="S")})
    assert matched == [(first, far, "stable")] and only_second == Counter({close: 1})
    plain, _, _ = m.pairs(Counter({first: 1}), Counter({close: 1}))
    assert plain == [(first, close, "attributes")]


def test_dedupe_leaves_out_a_photo_with_the_id_of_an_earlier_one():
    one = ("2006-09-30 06:51:40", "100", "IMAG0044.JPG")
    two = ("2006-09-30 06:51:40", "100", "IMAG0044_ASLN.JPG")
    assert m.dedupe([one, two]) == [one, two]
    assert m.dedupe([one, two], ids={one: ids(stable="S"), two: ids(stable="S")}) == [one]
    assert m.dedupe([one, two], ids={one: ids(stable="S"), two: ids(stable="T")}) == [one, two]


def test_content_is_a_part_of_the_mode_off_by_default_and_a_hash_never_shows_photos_to_be_different():
    assert m.TOLERANT.content == "ignore" and m.parse("content=hash").content == "hash" and m.parse("content=hash,exact").content == "ignore"
    with pytest.raises(ValueError):
        m.parse("content=md5")
    held = ("2006-09-30 06:51:40", "100", "a.jpg")
    other = ("2005-01-01 00:00:00", "999", "b.jpg")
    mode = m.parse("content=hash")
    same = m.absent(Counter({other: 1}), Counter({held: 1}), mode, {other: {"sha256": {"H"}}}, {held: {"sha256": {"H"}}})
    assert not same
    near = ("2006-09-30 06:51:41", "100", "a.jpg")
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), mode, {near: {"sha256": {"X"}}}, {held: {"sha256": {"H"}}})  # other bytes do not veto
    assert m.absent(Counter({other: 1}), Counter({held: 1}), m.TOLERANT, {other: {"sha256": {"H"}}}, {held: {"sha256": {"H"}}})  # not read unless asked


def test_dimensions_is_a_part_of_the_mode_off_by_default_and_can_only_show_photos_to_be_different():
    assert m.TOLERANT.dimensions == "ignore" and m.parse("dimensions=exact").dimensions == "exact"
    with pytest.raises(ValueError):
        m.parse("dimensions=about")
    held = ("2018-03-01 10:00:00", "100", "a.jpg")
    near = ("2018-03-01 10:00:10", "100", "a.jpg")
    different = ({near: {"dims": {"1000x800"}}}, {held: {"dims": {"2000x1500"}}})
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), m.TOLERANT, *different)  # not looked at
    assert m.absent(Counter({near: 1}), Counter({held: 1}), m.parse("dimensions=exact"), *different)
    same = ({near: {"dims": {"1000x800"}}}, {held: {"dims": {"1000x800"}}})
    assert not m.absent(Counter({near: 1}), Counter({held: 1}), m.parse("dimensions=exact"), *same)
    far = ("2005-01-01 00:00:00", "5", "z.jpg")
    assert m.absent(Counter({far: 1}), Counter({held: 1}), m.parse("dimensions=exact"), {far: {"dims": {"1000x800"}}}, {held: {"dims": {"1000x800"}}})  # a size is not an id
