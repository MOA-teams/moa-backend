import json

import pytest
from pydantic import ValidationError

from scripts.burst_replay import ReplayError, load_plan, main, normalize_base


@pytest.fixture
def seed_file(tmp_path):
    rows = [
        {
            "burst_id": f"B-{i:03d}",
            "offset_seconds": offset,
            "customer_id": f"U-{1900 + i}",
            "title": "합성 문의",
            "message": "저장 문제가 있습니다.",
            "customer_category": "기능 오류",
            "env": {"os": "iOS 18", "app_version": "2.4.0", "error_code": code},
            "attachments": [],
        }
        for i, offset, code in [(1, 0, "E-502"), (2, 300, None), (3, 600, "E-413")]
    ]
    path = tmp_path / "burst_inquiries.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def test_source_ids_are_metadata_and_no_code_is_inferred_from_labels(seed_file):
    plan = load_plan(seed_file)
    assert plan[1]["body"]["env"]["error_code"] is None
    for row in plan:
        assert set(row["body"]) == {
            "message",
            "source_customer_id",
            "customer_category",
            "env",
            "attachments",
        }
        assert row["body"]["source_customer_id"].startswith("U-")
        assert row["body"]["env"]["os"] == "iOS 18"


@pytest.mark.parametrize(
    "change", ["labels", "extra", "attachment", "duplicate", "invalid_env"]
)
def test_unexpected_seed_fields_fail_before_http(seed_file, change):
    rows = json.loads(seed_file.read_text())
    if change == "labels":
        new = seed_file.parent / "labels" / seed_file.name
        new.parent.mkdir()
        seed_file = new
    elif change == "extra":
        rows[0]["group_id"] = "G-001"
    elif change == "attachment":
        rows[0]["attachments"] = ["ATT-0001"]
    elif change == "duplicate":
        rows.append(rows[0])
    else:
        rows[0]["env"]["error_code"] = "x" * 101
    seed_file.write_text(json.dumps(rows))
    with pytest.raises((ReplayError, ValidationError)):
        load_plan(seed_file)


def test_dry_run_needs_no_auth_and_writes_no_state(seed_file, tmp_path, capsys):
    state = tmp_path / "state.json"
    assert main(["--seed-file", str(seed_file), "--state", str(state)]) == 0
    assert "DRY 3건" in capsys.readouterr().out
    assert not state.exists() and not state.with_suffix(".json.lock").exists()


@pytest.mark.parametrize(
    "base",
    [
        "https://user:password@host",
        "https://host/api/v1",
        "https://host?secret=x",
        "file:///tmp/x",
    ],
)
def test_invalid_api_base_is_rejected(base):
    with pytest.raises(ReplayError, match="INVALID_API_BASE_URL"):
        normalize_base(base)
