"""Test sector_mapping.yaml parsing and lookup."""
import re
import yaml
from pathlib import Path

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
