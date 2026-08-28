"""Test sector_mapping.yaml parsing and lookup."""
import re
import yaml
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetchers.sector_mapper as sector_mapper

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
MAPPING_PATH = CONFIG_DIR / "sector_mapping.yaml"


def test_mapping_file_exists():
    assert MAPPING_PATH.exists(), f"{MAPPING_PATH} not found"


def test_mapping_is_valid_yaml():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict)
    assert len(data) > 0, "mapping is empty"


def test_mapping_format():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    for ths_name, em_names in data.items():
        assert isinstance(ths_name, str) and ths_name, \
            f"bad key: {ths_name}"
        assert isinstance(em_names, list) and len(em_names) > 0, \
            f"bad values for {ths_name}"
        for em_name in em_names:
            assert isinstance(em_name, str) and em_name, \
                f"bad em_name in {ths_name}: {em_name}"


def test_common_sectors_present():
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    essential = {"半导体", "人工智能", "新能源汽车", "光伏", "证券", "银行", "白酒"}
    missing = essential - set(data.keys())
    assert not missing, f"missing essential sectors: {missing}"


def test_no_duplicate_keys():
    """Scan raw YAML for duplicate top-level keys.

    yaml.safe_load silently keeps last duplicate — this catches it.
    """
    raw = MAPPING_PATH.read_text(encoding="utf-8")
    keys = re.findall(r"^(?!#)(\S+):", raw, re.MULTILINE)
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"duplicate keys found: {dupes}"


def test_load_mapping_can_explicitly_return_stale_copy(tmp_path, monkeypatch):
    cache_file = tmp_path / "stock_sector_map.json"
    monkeypatch.setattr(sector_mapper, "MAP_CACHE_FILE", cache_file)
    stale = {"meta": {"built_at": "2020-01-01T00:00:00"},
             "mapping": {"600519": [{"code": "BK0477", "name": "白酒",
                                       "type": "industry"}]}}
    cache_file.write_text(json.dumps(stale), encoding="utf-8")

    assert sector_mapper.load_mapping() is None
    loaded = sector_mapper.load_mapping(allow_stale=True)

    assert loaded["mapping"] == stale["mapping"]
    assert loaded["meta"]["stale"] is True
    assert loaded["meta"]["age_hours"] > 0
    assert stale["meta"].get("stale") is None


def test_empty_mapping_is_never_usable_stale(tmp_path, monkeypatch):
    cache_file = tmp_path / "stock_sector_map.json"
    monkeypatch.setattr(sector_mapper, "MAP_CACHE_FILE", cache_file)
    cache_file.write_text(json.dumps({
        "meta": {"built_at": "2020-01-01T00:00:00"}, "mapping": {},
    }), encoding="utf-8")

    assert sector_mapper.load_mapping(allow_stale=True) is None
