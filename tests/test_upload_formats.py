import unittest

import comment_analysis
from app.core.parsing import _parse_file


class UploadFormatTests(unittest.TestCase):
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
