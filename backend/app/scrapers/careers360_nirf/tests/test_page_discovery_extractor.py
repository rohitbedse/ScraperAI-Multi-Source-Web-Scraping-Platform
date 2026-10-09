import pytest

from app.scrapers.careers360_nirf.extractor import (
    CollegeContext, CourseContext, admission_map_from, build_course, classify_degree, coerce, course_dedupe_key,
    domain_map_from, extract_college, extract_destinations, extract_rankings, html_to_text, normalize_duration,
    normalize_exams, normalize_fees, required_pages, unbound_keys)
from app.scrapers.careers360_nirf.page_discovery import (
    LayoutChanged, clean_url, course_detail, degree_filters, discover_pages, listing, parse_state, with_page)
from app.scrapers.careers360_nirf.schemas import Parameter

from .conftest import BASE, fx, make_seed


# ------------------------------------------------------------------ page discovery
def test_parse_state_ignores_statements_after_the_state():
    """Live pages continue the same <script> with more `window.x = ...;` statements."""
    html = ('<script>window.INITIAL_STATE={"a":{"b":[1,2]},"c":"x;y"};\n'
            '  window.nonCriticalCssString="<link>";</script>')
    assert parse_state(html) == {"a": {"b": [1, 2]}, "c": "x;y"}


def test_parse_state_blanks_js_function_literals():
    """A serialized failed request on live listing pages contains `"adapter":function httpAdapter(config) {...}`."""
    fn = ('function httpAdapter(config) {\n'
          '  var s = "}{ not a brace"; if (a) { return new Promise(function (r) { r(1); }); }\n}')
    html = '<script>window.INITIAL_STATE={"err":{"config":{"adapter":' + fn + ',"timeout":0}},"ok":[1,{"x":2}]};</script>'
    assert parse_state(html) == {"err": {"config": {"adapter": None, "timeout": 0}}, "ok": [1, {"x": 2}]}
    with pytest.raises(LayoutChanged):                      # still a clear failure when it cannot be repaired
        parse_state('<script>window.INITIAL_STATE={"a":function (x) {;</script>')


def test_parse_state_handles_undefined_and_errors():
    st = parse_state('<script>window.INITIAL_STATE={"a":undefined,"b":[1,undefined]}</script>')
    assert st == {"a": None, "b": [1, None]}
    with pytest.raises(LayoutChanged):
        parse_state("<html>no state</html>")
    with pytest.raises(LayoutChanged):
        parse_state("<script>window.INITIAL_STATE={broken</script>")


def test_discover_pages_from_site_navigation():
    pages = discover_pages(parse_state(fx("iitm_overview.html")), BASE)
    assert pages["courses"] == f"{BASE}/courses"
    assert pages["admission"] == f"{BASE}/admission"
    assert {"overview", "fees", "cutoff", "placement"} <= set(pages)


def test_degree_filters_and_listing():
    st = parse_state(fx("iitm_courses.html"))
    degs = degree_filters(st)
    assert [d["label"] for d in degs][:2] == ["B.E /B.Tech", "M.E /M.Tech."]
    assert "?" not in degs[0]["url"]                       # tracking-style query dropped
    rows, total = listing(st)
    assert len(rows) == 5 and total == 2
    assert with_page(degs[0]["url"], 1) == degs[0]["url"]
    assert with_page(degs[0]["url"] + "?x=1", 3).endswith("be-btech-idpg?page=3")
    assert clean_url("https://a.b/c?d=e") == "https://a.b/c"


def test_course_detail_extraction_point():
    d = course_detail(parse_state(fx("iitm_course_detail.html")))
    assert d["id"] == 6001 and "JEE" in d["admission_procedure"]


def test_required_pages_follow_the_yaml(params):
    assert required_pages(params) == {"overview", "courses", "admission", "course_detail"}
    slim = params.model_copy(deep=True)
    slim.course_parameters.fields = [f for f in slim.course_fields if f.key in ("course_name", "fees")]
    assert required_pages(slim) == {"overview", "courses"}          # no detail/admission pages needed


def test_unbound_keys_and_path_parameters(params):
    assert unbound_keys(params) == []
    extra = params.model_copy(deep=True)
    extra.college_parameters.append(Parameter(key="founded", label="Founded", type="int"))
    assert unbound_keys(extra) == ["founded"]
    extra.college_parameters[-1] = Parameter(key="founded", label="Founded", type="int",
                                             path="collegOverview.overview.institute_data.year_of_establishment")
    assert unbound_keys(extra) == []
    ctx = CollegeContext(seed=make_seed(), profile_url=BASE, overview=parse_state(fx("iitm_overview.html")))
    assert extract_college(ctx, extra.college_parameters)["founded"] == 1959      # new parameter, no code change


# --------------------------------------------------------------------- college fields
@pytest.fixture
def ctx():
    return CollegeContext(seed=make_seed(), profile_url=BASE, overview=parse_state(fx("iitm_overview.html")))


def test_college_fields(ctx, params):
    c = extract_college(ctx, params.college_parameters)
    assert c["standard_college_name"] == "Indian Institute of Technology Madras"
    assert c["standard_university_name"] == "Indian Institute of Technology Madras"
    assert c["college_category"] == "Govt"
    assert c["source"] == "Careers360" and c["url"] == BASE
    assert c["location"] == "Chennai, Tamil Nadu"
    assert c["description"].startswith("Indian Institute of Technology Madras") and "<" not in c["description"]


def test_category_unknown_is_null_not_guessed(ctx, params):
    ctx.header["ownership_value"] = {"id": 9, "value": "Deemed (Trust)"}
    assert extract_college(ctx, params.college_parameters)["college_category"] is None
    ctx.header["ownership_value"] = {"id": 2, "value": "Private"}
    assert extract_college(ctx, params.college_parameters)["college_category"] == "Private"


def test_missing_values_become_none(ctx, params):
    ctx.overview["collegOverview"]["overview"]["institute_data"]["about_college"] = "  "
    ctx.header["current_location"] = {}
    c = extract_college(ctx, params.college_parameters)
    assert c["description"] is None and c["location"] is None


def test_rankings_nirf_from_seed_qs_the_only_from_page(ctx, params):
    r = extract_rankings(ctx, params.ranking_category)
    assert r == {"nirf_ranking": 1, "qs_world_ranking": None, "times_higher_education_ranking": None}
    ctx.overview["collegOverview"]["overviewRanking"]["QS"] = {"latest_year_ranking": [{"college_ranking": [
        {"ranking__ranking_authority": "QS World University Rankings", "overall_rank": "227",
         "ranking__year": 2025}]}]}
    r = extract_rankings(ctx, params.ranking_category)
    assert r["qs_world_ranking"] == "227" and r["nirf_ranking"] == 1


def test_study_destination_only_from_page_evidence(ctx, params):
    d = extract_destinations(ctx, params.study_destination)
    assert d["study_destination_india"] is True
    assert [v for k, v in d.items() if k != "study_destination_india"] == [None] * 4      # never False / guessed


# ------------------------------------------------------------------ course normalizing
def test_normalize_duration():
    assert normalize_duration("48 Months") == ("4 years", 48)
    assert normalize_duration("2 Years") == ("2 years", 24)
    assert normalize_duration("18 Months") == ("18 months", 18)
    assert normalize_duration("1 year") == ("1 year", 12)
    assert normalize_duration("") == (None, None) and normalize_duration("varies") == (None, None)


def test_normalize_fees():
    assert normalize_fees(873608, "INR") == ("INR 873,608", 873608, "INR")
    assert normalize_fees(0, "INR") == (None, None, None)
    assert normalize_fees(None, None) == (None, None, None)
    assert normalize_fees("abc", "INR") == (None, None, None)


def test_normalize_exams_dedupes_and_keeps_raw():
    names, raw = normalize_exams([{"name": "Joint Entrance Exam Advanced", "short_name": "JEE Advanced"},
                                  {"name": "JEE Advanced again", "short_name": "jee advanced"},
                                  {"name": "Graduate Aptitude Test in Engineering", "short_name": "GATE"}])
    assert names == ["JEE Advanced", "GATE"] and len(raw) == 3
    assert normalize_exams([]) == (None, []) and normalize_exams(None) == (None, [])


def test_classify_degree():
    assert classify_degree("B.E /B.Tech") == "UG" and classify_degree("BS") == "UG" and classify_degree("MBBS") == "UG"
    assert classify_degree("M.E /M.Tech.") == "PG" and classify_degree("MBA") == "PG" and classify_degree("M.Sc.") == "PG"
    assert classify_degree("Ph.D") == "PhD" and classify_degree("Doctor of Philosophy") == "PhD"
    assert classify_degree("B.Tech M.Tech") is None and classify_degree("Diploma") is None
    assert classify_degree("") is None and classify_degree(None) is None


def test_coerce_and_html_to_text():
    assert coerce(87, "str") == "87" and coerce("1,200", "int") == 1200 and coerce("x", "int") is None
    assert coerce("a", "list") == ["a"] and coerce("", "str") is None and coerce("yes", "bool") is None
    assert html_to_text("<p>A&nbsp;<b>b</b></p><ul><li>c</li></ul>") == "A b c" and html_to_text("") is None


def _rows():
    st = parse_state(fx("iitm_degree_btech_p1.html"))
    return st, listing(st)[0]


def test_build_course_from_list_row_marks_page_only_fields_missing(params):
    _, rows = _rows()
    cctx = CourseContext(domain_map=domain_map_from(parse_state(fx("iitm_courses.html"))))
    c = build_course(rows[0], "B.E /B.Tech", cctx, params.course_fields)
    assert c.fields["course_name"] == "B.Tech Electrical Engineering"
    assert c.fields["course_duration"] == "4 years" and c.raw["course_duration"] == "48 Months"
    assert c.fields["fees"] == "INR 873,608" and c.normalized["fees_amount"] == 873608
    assert c.fields["entrance_exams_accepted"] == ["JEE Advanced"]
    assert c.fields["course_intake"] == "154"
    assert c.fields["domain"] == "Engineering and Architecture"
    # not on a listing row -> null and recorded, not invented
    assert {"eligibility", "course_details", "research_areas", "admission_process"} <= set(c.missing_fields)
    assert c.fields["eligibility"] is None


def test_build_course_with_detail_and_admission_map(params):
    _, rows = _rows()
    detail = course_detail(parse_state(fx("iitm_course_detail.html")))
    cctx = CourseContext(admission_map=admission_map_from(parse_state(fx("iitm_admission.html"))))
    c = build_course(rows[0], "B.E /B.Tech", cctx, params.course_fields, detail)
    assert c.fields["eligibility"] and "<" not in c.fields["eligibility"]
    assert c.fields["admission_process"].startswith("Admissions are done")
    assert "eligibility" not in c.missing_fields
    # the admission map alone fills admission_process for courses whose detail page was not opened
    c2 = build_course(rows[1], "B.E /B.Tech", cctx, params.course_fields)
    assert c2.fields["admission_process"] and c2.fields["eligibility"] is None


def test_duplicate_key_ignores_case_and_spacing(params):
    _, rows = _rows()
    cctx = CourseContext()
    a = build_course(rows[0], "x", cctx, params.course_fields)
    b = build_course({**rows[0], "id": 1, "course_name": "  b.tech  ELECTRICAL engineering"}, "x", cctx,
                     params.course_fields)
    assert course_dedupe_key("UG", a) == course_dedupe_key("UG", b)
    assert course_dedupe_key("UG", a) != course_dedupe_key("PG", a)


def test_zero_intake_and_fees_are_null(params):
    _, rows = _rows()
    c = build_course({**rows[0], "approved_intake": 0, "total_fees": 0, "duration": ""}, "x", CourseContext(),
                     params.course_fields)
    assert c.fields["course_intake"] is None and c.fields["fees"] is None and c.fields["course_duration"] is None
    assert {"course_intake", "fees", "course_duration"} <= set(c.missing_fields)
