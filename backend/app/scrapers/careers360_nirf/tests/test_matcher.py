from app.scrapers.careers360_nirf.matcher import match_seed, normalize_name, parse_sitemap, verify_location

from .conftest import fx, make_seed


def test_normalize_name_variants():
    assert normalize_name("S.R.M. Institute of Science & Technology") == "srm institute of science and technology"
    assert normalize_name("Siksha `O` Anusandhan") == "siksha o anusandhan"
    assert normalize_name("Birla Institute of Technology & Science -Pilani") == \
        "birla institute of technology and science pilani"


def test_sitemap_keeps_only_profile_urls(index):
    assert len(index) == 9          # the /articles/ URL is ignored
    assert len(parse_sitemap(fx("sitemap_sample.xml"))) == 10


def test_matched_exact_name(index):
    m = match_seed(make_seed(), index)
    assert m.status == "matched"
    assert m.url.endswith("/university/indian-institute-of-technology-madras")
    assert m.score == 1.0
    assert all(c.score < 1.0 for c in m.candidates[1:])      # the department page is a worse candidate


def test_matched_with_dotted_and_odd_names(index):
    srm = match_seed(make_seed(name="S.R.M. Institute of Science and Technology", city="Chennai"), index)
    assert srm.status == "matched" and "srm-institute" in srm.url
    sosa = match_seed(make_seed(name="Siksha `O` Anusandhan", city="Bhubaneswar", state="Odisha"), index)
    assert sosa.status == "matched" and sosa.url.endswith("siksha-o-anusandhan-bhubaneswar")


def test_ambiguous_records_candidates_and_never_guesses(index):
    m = match_seed(make_seed(id="X", name="Example Institute of Technology", city="Mumbai", state="Maharashtra"), index)
    assert m.status == "ambiguous"
    assert m.url is None
    assert {c.url.rsplit("-", 1)[-1] for c in m.candidates} == {"pune", "nagpur"}


def test_city_in_profile_url_breaks_a_tie(index):
    m = match_seed(make_seed(id="X", name="Example Institute of Technology", city="Pune", state="Maharashtra"), index)
    assert m.status == "matched" and m.url.endswith("technology-pune")


def test_unmatched(index):
    m = match_seed(make_seed(name="Zzyzx Quantum Academy", city="Nowhere", state="Nowhere"), index)
    assert m.status == "unmatched" and m.url is None


def test_unrelated_similar_college_is_not_accepted(index):
    m = match_seed(make_seed(name="Indian Institute of Technology Kanpur", city="Kanpur", state="Uttar Pradesh"), index)
    assert m.status in ("unmatched", "ambiguous") and m.url is None


def test_verify_location():
    s = make_seed()
    assert verify_location(s, "Chennai", "Tamil Nadu") == (True, True)
    assert verify_location(s, "Mohali", "Tamil Nadu") == (True, False)
    assert verify_location(s, "Chennai", "Karnataka")[0] is False
    assert verify_location(s, None, None) == (None, None)


def test_tied_rank_seeds_are_matched_independently(index):
    a = make_seed(id="A", rank=27)
    b = make_seed(id="B", name="Indian Institute of Technology Delhi", city="New Delhi", state="Delhi", rank=27)
    assert match_seed(a, index).url != match_seed(b, index).url
