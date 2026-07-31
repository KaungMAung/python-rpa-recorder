"""Pure spreadsheet reading helpers used by the Read Excel Column step."""
from __future__ import annotations

import math
from pathlib import Path

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv"}


def column_letter_to_index(letter: str) -> int:
    """Convert a spreadsheet column label such as ``A`` or ``AA`` to a 0-based index."""
    result = 0
    for char in letter.strip().upper():
        if not ("A" <= char <= "Z"):
            raise ValueError(f"invalid column letter: {letter!r}")
        result = result * 26 + (ord(char) - ord("A") + 1)
    if result <= 0:
        raise ValueError(f"invalid column letter: {letter!r}")
    return result - 1


def _format_value(value) -> str | None:
    """Return a cleaned text value, or ``None`` for a blank/NaN cell."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    numeric = _as_numeric_text(text)
    return numeric if numeric is not None else text


def _as_numeric_text(text: str) -> str | None:
    """Reformat a numeric-looking string (e.g. ``11241.0``) to a tidy number string."""
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number.is_integer():
        return str(int(number))
    return str(number)


def read_excel_column(
    file_path: str,
    sheet_name: str,
    column_header: str,
    *,
    first_row_headers: bool = True,
    row_selection: str = "all",
    row_start: int = 1,
    row_end: int | None = None,
    row_count: int | None = None,
    skip_blanks: bool = True,
    remove_duplicates: bool = False,
) -> list[str]:
    """Read a single column from an Excel/CSV file and return its values as ``list[str]``."""
    import pandas as pd

    path = Path(file_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"spreadsheet file was not found: {file_path}")
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"unsupported spreadsheet type '{suffix or path.name}'; use .xlsx, .xls, or .csv"
        )

    header = 0 if first_row_headers else None
    if suffix == ".csv":
        frame = pd.read_csv(path, header=header, dtype=str, keep_default_na=True)
    else:
        try:
            frame = pd.read_excel(path, sheet_name=sheet_name, header=header, dtype=str)
        except ValueError as exc:
            message = str(exc).lower()
            if "worksheet" in message or "sheet" in message:
                raise ValueError(f"Sheet '{sheet_name}' was not found in {path.name}") from exc
            raise

    series = _select_column(frame, column_header, first_row_headers)
    values = _apply_row_selection(list(series), row_selection, row_start, row_end, row_count)

    result: list[str] = []
    for raw in values:
        formatted = _format_value(raw)
        if formatted is None:
            if not skip_blanks:
                result.append("")
            continue
        result.append(formatted)

    if remove_duplicates:
        seen: set[str] = set()
        deduped: list[str] = []
        for item in result:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        result = deduped
    return result


def _select_column(frame, column_header: str, first_row_headers: bool):
    columns = list(frame.columns)
    if first_row_headers:
        for column in columns:
            if str(column) == str(column_header):
                return frame[column]
        available = ", ".join(str(column) for column in columns) or "(none)"
        raise ValueError(
            f"column '{column_header}' was not found; available columns: {available}"
        )
    header = str(column_header).strip()
    if header.isdigit():
        position = int(header)
    else:
        position = column_letter_to_index(header)
    if position < 0 or position >= len(columns):
        raise ValueError(
            f"column position '{column_header}' is out of range; the file has {len(columns)} columns"
        )
    return frame.iloc[:, position]


def _apply_row_selection(
    values: list, row_selection: str, row_start: int, row_end: int | None, row_count: int | None,
) -> list:
    selection = str(row_selection or "all").strip().lower()
    if selection in {"all", ""}:
        return values
    if selection == "first_n":
        count = int(row_count) if row_count is not None else 0
        if count <= 0:
            return []
        return values[:count]
    if selection == "range":
        start = max(1, int(row_start or 1))
        end = int(row_end) if row_end is not None else len(values)
        if end < start:
            return []
        return values[start - 1:end]
    return values
