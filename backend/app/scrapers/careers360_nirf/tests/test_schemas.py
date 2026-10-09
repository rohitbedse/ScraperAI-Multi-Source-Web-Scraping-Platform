import json

import pytest

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.schemas import DataFileError, load_parameters, load_seed


def test_real_seed_file_loads_and_keeps_tied_ranks():
    seeds = load_seed()
    assert len(seeds) == 100
    ranks = [s.rank for s in seeds]
    assert ranks.count(27) == 2 and ranks.count(64) == 2          # ties preserved, never renumbered
    assert 28 not in ranks and 65 not in ranks
    assert isinstance(seeds[0].id, str) and seeds[0].city and seeds[0].state


def test_real_parameters_file_loads():
    p = load_parameters()
    assert p.degree_levels == ["UG", "PG", "PhD"]
    assert [f.key for f in p.course_fields][:2] == ["course_name", "domain"]
    assert len(p.study_destination) == 5 and len(p.ranking_category) == 3


def test_seed_missing_empty_malformed(tmp_path):
    with pytest.raises(DataFileError, match="not found"):
        load_seed(tmp_path / "nope.json")
    empty = tmp_path / "e.json"
    empty.write_text("  ")
    with pytest.raises(DataFileError, match="empty"):
        load_seed(empty)
    bad = tmp_path / "b.json"
    bad.write_text("{not json")
    with pytest.raises(DataFileError, match="malformed JSON"):
        load_seed(bad)
    notlist = tmp_path / "n.json"
    notlist.write_text('{"a": 1}')
    with pytest.raises(DataFileError, match="non-empty JSON list"):
        load_seed(notlist)


def test_seed_invalid_entries_reported_with_position(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"id": "A", "name": "X", "city": "C", "state": "S", "location": "C, S",
                              "score": 1.0, "rank": 0}]))
    with pytest.raises(DataFileError, match=r"entry #1.*rank"):
        load_seed(f)


def test_seed_duplicate_id_rejected_but_duplicate_rank_allowed(tmp_path):
    row = {"id": "A", "name": "X", "city": "C", "state": "S", "location": "C, S", "score": 1.0, "rank": 27}
    f = tmp_path / "s.json"
    f.write_text(json.dumps([row, {**row, "id": "B"}]))
    assert [s.rank for s in load_seed(f)] == [27, 27]
    f.write_text(json.dumps([row, row]))
    with pytest.raises(DataFileError, match="duplicate id"):
        load_seed(f)


def test_parameters_missing_empty_malformed_invalid(tmp_path):
    with pytest.raises(DataFileError, match="not found"):
        load_parameters(tmp_path / "x.yaml")
    f = tmp_path / "p.yaml"
    f.write_text("")
    with pytest.raises(DataFileError, match="empty"):
        load_parameters(f)
    f.write_text("a: [unclosed")
    with pytest.raises(DataFileError, match="malformed YAML"):
        load_parameters(f)
    f.write_text("- just\n- a list\n")
    with pytest.raises(DataFileError, match="expected a mapping"):
        load_parameters(f)
    f.write_text("college_parameters: []\n")
    with pytest.raises(DataFileError, match="invalid content"):
        load_parameters(f)


def test_parameters_bad_type_and_duplicate_key(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("""
college_parameters:
  - {key: a, label: A, type: banana}
course_parameters:
  degree_levels: [UG]
  fields: [{key: a, label: A, type: str}]
""")
    with pytest.raises(DataFileError, match="type must be one of"):
        load_parameters(f)
    f.write_text(f.read_text().replace("banana", "str"))
    with pytest.raises(DataFileError, match="duplicate parameter key"):
        load_parameters(f)


def test_data_paths_come_from_config():
    assert config.SEED_FILE.name == "nirf_seed.json" and config.PARAMETERS_FILE.name == "parameters.yaml"
