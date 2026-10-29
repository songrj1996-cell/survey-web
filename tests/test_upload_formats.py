import io
import unittest
import zipfile

import comment_analysis
import openpyxl
from app.core.parsing import _parse_file, parse_file_with_cell_metadata


def _numeric_identity_xlsx(raw_numbers):
    """Keep raw XLSX numeric encodings that openpyxl would simplify on save."""
    workbook = openpyxl.Workbook()
    workbook.active.append(["WhatsApp"])
    for index in range(len(raw_numbers)):
        workbook.active.append([100 + index])
    original = io.BytesIO()
    workbook.save(original)
    workbook.close()
    output = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                for index, raw in enumerate(raw_numbers):
                    content = content.replace(
                        f"<v>{100 + index}</v>".encode(), f"<v>{raw}</v>".encode(),
                    )
            target.writestr(name, content)
    return output.getvalue()


class UploadFormatTests(unittest.TestCase):
    def test_annotation_numbers_drop_float_suffix_and_expand_scientific_notation(self):
        content = _numeric_identity_xlsx(["12345678901.0", "1.2345678902E+10", "1E+20"])
        rows = _parse_file("responses.xlsx", content, normalize_integer_numbers=True)
        self.assertEqual([row[0] for row in rows[1:]], [
            "12345678901", "12345678902", "100000000000000000000",
        ])
        # Other upload callers retain the existing conversion behavior.
        unchanged = _parse_file("responses.xlsx", content)
        self.assertEqual([row[0] for row in unchanged[1:]], [
            "12345678901.0", "12345678902.0", "1e+20",
        ])

    def test_annotation_preserves_text_ids_fractions_and_empty_cells(self):
        values = ["0012345678901", "+12345678901", "12345678901.0", "1.2345E+10",
                  "123456789012345678901", 1.25, True, None]
        workbook = openpyxl.Workbook()
        workbook.active.append(["WhatsApp", "Answer"])
        for value in values:
            workbook.active.append([value, "sample"])
        content = io.BytesIO()
        workbook.save(content)
        workbook.close()
        rows = _parse_file("responses.xlsx", content.getvalue(), normalize_integer_numbers=True)
        self.assertEqual([row[0] for row in rows[1:]], [
            "0012345678901", "+12345678901", "12345678901.0", "1.2345E+10",
            "123456789012345678901", "1.25", "True", "",
        ])

    def test_group_annotation_keeps_precision_and_formula_warnings(self):
        content = _numeric_identity_xlsx([
            "12345678901.0", "1.2345678902E+10", "999999999999999.0", "1E+15", "1.25",
        ])
        rows, issues = parse_file_with_cell_metadata("responses.xlsx", content)
        self.assertEqual([row[0] for row in rows[1:]], [
            "12345678901", "12345678902", "999999999999999", "1000000000000000", "1.25",
        ])
        self.assertEqual(set(issues), {"4:0", "5:0"})
        self.assertIn("精度", issues["4:0"])
        workbook = openpyxl.Workbook()
        workbook.active.append(["WhatsApp"])
        workbook.active.append(['="12345678901"'])
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        _, formula_issues = parse_file_with_cell_metadata("responses.xlsx", buffer.getvalue())
        self.assertIn("公式", formula_issues["1:0"])

    def test_annotation_csv_remains_text_without_numeric_coercion(self):
        content = b"WhatsApp\n0012345678901\n+12345678901\n12345678901.0\n1.2345E+10\n"
        rows = _parse_file("responses.csv", content, normalize_integer_numbers=True)
        self.assertEqual(rows, _parse_file("responses.csv", content))
        self.assertEqual(parse_file_with_cell_metadata("responses.csv", content), (rows, {}))

    def test_shared_parser_rejects_legacy_xls(self):
        with self.assertRaisesRegex(ValueError, r"\.csv.*\.xlsx"):
            _parse_file("responses.xls", b"legacy")

    def test_comment_parsers_reject_legacy_xls(self):
        with self.assertRaisesRegex(ValueError, r"\.csv.*\.xlsx"):
            comment_analysis.parse_comment_file(b"legacy", "comments.xls")
        with self.assertRaisesRegex(ValueError, r"\.csv.*\.xlsx"):
            list(comment_analysis.preprocess_comment_file("unused.xls", "comments.xls"))


if __name__ == "__main__":
    unittest.main()
