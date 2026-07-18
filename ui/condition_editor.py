"""Reusable action-specific condition form for If and Repeat Until steps."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)


CONDITION_OPTIONS = [
    ("Image Exists", "image_exists"),
    ("Image Does Not Exist", "image_not_exists"),
    ("Window Exists", "window_exists"),
    ("File or Folder Exists", "path_exists"),
    ("Variable", "variable"),
]


class ConditionEditor(QWidget):
    changed = Signal()

    def __init__(self, data: dict | None = None, fixed_type: str | None = None, variables=None, parent=None) -> None:
        super().__init__(parent)
        self.fixed_type = fixed_type
        self.variables = sorted(variables or [])
        data = data or {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.type_combo = QComboBox()
        self.type_combo.setToolTip("Choose the plain-language test that controls this block.")
        for label, value in CONDITION_OPTIONS:
            self.type_combo.addItem(label, value)
        initial_type = fixed_type or str(data.get("condition_type", "variable"))
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData(initial_type)))
        if fixed_type is None:
            form = QFormLayout()
            form.addRow("Condition", self.type_combo)
            layout.addLayout(form)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        self._build_image_page(data)
        self._build_window_page(data)
        self._build_path_page(data)
        self._build_variable_page(data)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        self._connect_changes()
        self._type_changed()

    def _build_image_page(self, data: dict) -> None:
        page = QWidget(); form = QFormLayout(page)
        self.image = QLineEdit(str(data.get("image", "")))
        self.image.setPlaceholderText("Select a saved target screenshot")
        self.image.setToolTip("The flow checks the current desktop for this image without clicking it.")
        browse = QPushButton("Browse…"); browse.clicked.connect(self._browse_image)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(self.image, 1); row.addWidget(browse)
        wrap = QWidget(); wrap.setLayout(row)
        self.confidence = QDoubleSpinBox(); self.confidence.setRange(0.01, 1.0); self.confidence.setDecimals(2)
        self.confidence.setSingleStep(0.05); self.confidence.setValue(float(data.get("confidence", 0.86) or 0.86))
        self.confidence.setToolTip("Higher values require a closer visual match. 0.86 is a practical default.")
        form.addRow("Target image", wrap); form.addRow("Match accuracy", self.confidence)
        self.pages.addWidget(page)

    def _build_window_page(self, data: dict) -> None:
        page = QWidget(); form = QFormLayout(page)
        self.window_title = QLineEdit(str(data.get("window_title", "")))
        self.window_title.setPlaceholderText("Part of the window title")
        self.window_title.setToolTip("A partial title is enough, for example 'Invoice - Excel'.")
        self.window_case = QCheckBox("Match uppercase/lowercase exactly")
        self.window_case.setChecked(bool(data.get("case_sensitive", False)))
        form.addRow("Window title contains", self.window_title); form.addRow("", self.window_case)
        self.pages.addWidget(page)

    def _build_path_page(self, data: dict) -> None:
        page = QWidget(); form = QFormLayout(page)
        self.path = QLineEdit(str(data.get("path", "")))
        self.path.setPlaceholderText("Select a file or folder")
        browse_file = QPushButton("File…"); browse_file.clicked.connect(lambda: self._browse_path(False))
        browse_folder = QPushButton("Folder…"); browse_folder.clicked.connect(lambda: self._browse_path(True))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addWidget(self.path, 1); row.addWidget(browse_file); row.addWidget(browse_folder)
        wrap = QWidget(); wrap.setLayout(row)
        self.path_type = QComboBox()
        for label, value in (("File or Folder", "either"), ("File", "file"), ("Folder", "folder")):
            self.path_type.addItem(label, value)
        self.path_type.setCurrentIndex(max(0, self.path_type.findData(str(data.get("path_type", "either")))))
        form.addRow("Path", wrap); form.addRow("Must be", self.path_type)
        self.pages.addWidget(page)

    def _build_variable_page(self, data: dict) -> None:
        page = QWidget(); form = QFormLayout(page)
        self.variable = QComboBox(); self.variable.setEditable(True); self.variable.addItems(self.variables)
        self.variable.setToolTip("Choose a project, runtime, built-in, or output variable.")
        self.variable.setCurrentText(str(data.get("variable", "")))
        self.operator = QComboBox()
        for label, value in (("Equals", "equals"), ("Contains", "contains"), ("Is Empty", "is_empty")):
            self.operator.addItem(label, value)
        self.operator.setCurrentIndex(max(0, self.operator.findData(str(data.get("operator", "equals")))))
        self.compare_value = QLineEdit(str(data.get("value", "")))
        self.compare_value.setPlaceholderText("Value to compare with")
        self.variable_case = QCheckBox("Match uppercase/lowercase exactly")
        self.variable_case.setChecked(bool(data.get("case_sensitive", False)))
        self.operator.currentIndexChanged.connect(self._operator_changed)
        form.addRow("Variable", self.variable); form.addRow("Comparison", self.operator)
        form.addRow("Value", self.compare_value); form.addRow("", self.variable_case)
        self.pages.addWidget(page)
        self._operator_changed()

    def _connect_changes(self) -> None:
        for line in (self.image, self.window_title, self.path, self.compare_value):
            line.textChanged.connect(self.changed)
        self.variable.currentTextChanged.connect(self.changed)
        for combo in (self.path_type, self.operator):
            combo.currentIndexChanged.connect(self.changed)
        self.confidence.valueChanged.connect(self.changed)
        self.window_case.toggled.connect(self.changed)
        self.variable_case.toggled.connect(self.changed)

    def _type_changed(self, _index: int | None = None) -> None:
        kind = self.condition_type()
        page = 0 if kind in {"image_exists", "image_not_exists"} else 1 if kind == "window_exists" else 2 if kind == "path_exists" else 3
        self.pages.setCurrentIndex(page)
        self.changed.emit()

    def _operator_changed(self, _index: int | None = None) -> None:
        self.compare_value.setEnabled(self.operator.currentData() != "is_empty")

    def condition_type(self) -> str:
        return self.fixed_type or str(self.type_combo.currentData())

    def data(self) -> dict:
        kind = self.condition_type()
        if kind in {"image_exists", "image_not_exists"}:
            return {"condition_type": kind, "image": self.image.text().strip(), "confidence": self.confidence.value()}
        if kind == "window_exists":
            return {"condition_type": kind, "window_title": self.window_title.text().strip(), "case_sensitive": self.window_case.isChecked()}
        if kind == "path_exists":
            return {"condition_type": kind, "path": self.path.text().strip(), "path_type": self.path_type.currentData()}
        return {
            "condition_type": "variable", "variable": self.variable.currentText().strip(),
            "operator": self.operator.currentData(), "value": self.compare_value.text(),
            "case_sensitive": self.variable_case.isChecked(),
        }

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Condition Image", filter="Images (*.png *.jpg *.jpeg *.bmp)")
        if path:
            self.image.setText(path)

    def _browse_path(self, folder: bool) -> None:
        if folder:
            path = QFileDialog.getExistingDirectory(self, "Select Folder", self.path.text())
            if path:
                self.path_type.setCurrentIndex(self.path_type.findData("folder"))
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select File", self.path.text())
            if path:
                self.path_type.setCurrentIndex(self.path_type.findData("file"))
        if path:
            self.path.setText(path)
