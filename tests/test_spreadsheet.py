from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from rpa.spreadsheet import column_letter_to_index, read_excel_column


def _write_csv(path: Path, rows: list[dict]) -> Path:
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_reads_column_and_formats_whole_numbers(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [
        {"pr": 11241.0, "name": "alpha"},
        {"pr": 11242, "name": "beta"},
        {"pr": 12.5, "name": "gamma"},
    ])
    values = read_excel_column(str(path), "", "pr")
    assert values == ["11241", "11242", "12.5"]


def test_skip_blanks_default_drops_empty(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [
        {"pr": "a"},
        {"pr": None},
        {"pr": "b"},
    ])
    assert read_excel_column(str(path), "", "pr") == ["a", "b"]


def test_keep_blanks_as_empty_string(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [
        {"pr": "a"},
        {"pr": None},
        {"pr": "b"},
    ])
    assert read_excel_column(str(path), "", "pr", skip_blanks=False) == ["a", "", "b"]


def test_remove_duplicates_preserves_order(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [
        {"pr": "a"}, {"pr": "b"}, {"pr": "a"}, {"pr": "c"}, {"pr": "b"},
    ])
    assert read_excel_column(str(path), "", "pr", remove_duplicates=True) == ["a", "b", "c"]


def test_row_selection_first_n(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [{"pr": str(i)} for i in range(5)])
    values = read_excel_column(str(path), "", "pr", row_selection="first_n", row_count=2)
    assert values == ["0", "1"]


def test_row_selection_range_inclusive(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [{"pr": str(i)} for i in range(5)])
    values = read_excel_column(str(path), "", "pr", row_selection="range", row_start=2, row_end=4)
    assert values == ["1", "2", "3"]


def test_no_headers_uses_letter_and_index(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    pd.DataFrame([["x", "y"], ["z", "w"]]).to_csv(path, index=False, header=False)
    assert read_excel_column(str(path), "", "A", first_row_headers=False) == ["x", "z"]
    assert read_excel_column(str(path), "", "1", first_row_headers=False) == ["y", "w"]


def test_empty_result_returns_empty_list(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [{"pr": None}, {"pr": None}])
    assert read_excel_column(str(path), "", "pr") == []


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_excel_column(str(tmp_path / "nope.csv"), "", "pr")


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_text("pr\n1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_excel_column(str(path), "", "pr")


def test_missing_column_raises(tmp_path: Path) -> None:
    path = _write_csv(tmp_path / "data.csv", [{"pr": "a"}])
    with pytest.raises(ValueError):
        read_excel_column(str(path), "", "missing")


def test_excel_reading_and_missing_sheet(tmp_path: Path) -> None:
    path = tmp_path / "data.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"pr": [11241.0, 12.5]}).to_excel(writer, sheet_name="Sheet1", index=False)
    assert read_excel_column(str(path), "Sheet1", "pr") == ["11241", "12.5"]
    with pytest.raises(ValueError):
        read_excel_column(str(path), "Missing", "pr")


def test_column_letter_to_index() -> None:
    assert column_letter_to_index("A") == 0
    assert column_letter_to_index("B") == 1
    assert column_letter_to_index("Z") == 25
    assert column_letter_to_index("AA") == 26
