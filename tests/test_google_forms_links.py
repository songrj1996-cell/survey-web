import unittest

from app.core.google_forms_links import (
    GoogleFormsLinkError,
    GoogleFormsLinkErrorCode,
    parse_google_forms_edit_link,
)


FORM_ID = "AbC_123-xYz"


class GoogleFormsLinkTests(unittest.TestCase):
    def test_accepts_editor_links_and_safe_suffixes(self):
        cases = (
            (f"https://docs.google.com/forms/d/{FORM_ID}/edit", FORM_ID),
            (f"https://docs.google.com/forms/d/{FORM_ID}/edit/", FORM_ID),
            (
                f"https://docs.google.com/forms/d/{FORM_ID}/edit?usp=sharing",
                FORM_ID,
            ),
            (
                f"https://docs.google.com/forms/d/{FORM_ID}/edit?usp=drive_link",
                FORM_ID,
            ),
            (
                f"https://docs.google.com/forms/d/{FORM_ID}/edit?usp=sf_link",
                FORM_ID,
            ),
            (
                f"  HTTPS://DOCS.GOOGLE.COM/forms/d/{FORM_ID}/edit  ",
                FORM_ID,
            ),
            ("https://docs.google.com/forms/d/a/edit", "a"),
            (
                "https://docs.google.com/forms/d/"
                + ("A" * 256)
                + "/edit",
                "A" * 256,
            ),
        )
        for link, expected in cases:
            with self.subTest(link=link):
                self.assertEqual(parse_google_forms_edit_link(link), expected)

    def test_rejects_invalid_input_without_echoing_it(self):
        secret = "private-secret-value"
        cases = (
            None,
            "",
            "   ",
            f"https://docs.google.com/forms/d/{secret}/edit\n",
            "x" * 2049,
        )
        for value in cases:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(GoogleFormsLinkError) as caught:
                    parse_google_forms_edit_link(value)  # type: ignore[arg-type]
                self.assertEqual(
                    caught.exception.code,
                    GoogleFormsLinkErrorCode.INVALID_URL,
                )
                self.assertNotIn(secret, str(caught.exception))

    def test_rejects_non_https_links(self):
        for link in (
            f"http://docs.google.com/forms/d/{FORM_ID}/edit",
            f"docs.google.com/forms/d/{FORM_ID}/edit",
        ):
            with self.subTest(link=link):
                self._assert_code(link, GoogleFormsLinkErrorCode.HTTPS_REQUIRED)

    def test_rejects_short_and_non_google_hosts_without_suffix_matching(self):
        self._assert_code(
            f"https://forms.gle/{FORM_ID}",
            GoogleFormsLinkErrorCode.SHORT_LINK_UNSUPPORTED,
        )
        for host in (
            "docs.google.com.evil.example",
            "evil-docs.google.com",
            "google.com",
            "docs.google.com.",
        ):
            with self.subTest(host=host):
                self._assert_code(
                    f"https://{host}/forms/d/{FORM_ID}/edit",
                    GoogleFormsLinkErrorCode.UNSUPPORTED_HOST,
                )

    def test_rejects_credentials_ports_and_fragments(self):
        cases = (
            f"https://user:password@docs.google.com/forms/d/{FORM_ID}/edit",
            f"https://docs.google.com:443/forms/d/{FORM_ID}/edit",
            f"https://docs.google.com/forms/d/{FORM_ID}/edit#responses",
            f"https://docs.google.com/forms/d/{FORM_ID}/edit#",
        )
        for link in cases:
            with self.subTest(link=link):
                self._assert_code(
                    link,
                    GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT,
                )

    def test_rejects_published_and_view_links(self):
        for link in (
            "https://docs.google.com/forms/d/e/PUBLIC_ID/viewform",
            "https://docs.google.com/forms/d/e/PUBLIC_ID/viewform?usp=sharing",
        ):
            with self.subTest(link=link):
                self._assert_code(
                    link,
                    GoogleFormsLinkErrorCode.PUBLISHED_LINK_UNSUPPORTED,
                )

        self._assert_code(
            f"https://docs.google.com/forms/d/{FORM_ID}/viewform",
            GoogleFormsLinkErrorCode.EDIT_LINK_REQUIRED,
        )

    def test_rejects_path_spoofing_and_extra_segments(self):
        cases = (
            f"https://docs.google.com/other/forms/d/{FORM_ID}/edit",
            f"https://docs.google.com/forms/d/{FORM_ID}/edit/extra",
            f"https://docs.google.com/forms/u/0/d/{FORM_ID}/edit",
            f"https://docs.google.com//forms/d/{FORM_ID}/edit",
            f"https://docs.google.com/forms%2Fd%2F{FORM_ID}%2Fedit",
        )
        for link in cases:
            with self.subTest(link=link):
                self._assert_code(
                    link,
                    GoogleFormsLinkErrorCode.EDIT_LINK_REQUIRED,
                )
        self._assert_code(
            f"https://docs.google.com/forms/d/{FORM_ID}%2Fextra/edit",
            GoogleFormsLinkErrorCode.INVALID_FORM_ID,
        )

    def test_rejects_form_ids_outside_existing_api_rule(self):
        for form_id in ("", "bad id", "unicode-问卷", "A" * 257):
            with self.subTest(form_id=form_id):
                self._assert_code(
                    f"https://docs.google.com/forms/d/{form_id}/edit",
                    GoogleFormsLinkErrorCode.INVALID_FORM_ID,
                )

    def test_rejects_unknown_empty_or_duplicate_query_parameters(self):
        cases = (
            f"https://docs.google.com/forms/d/{FORM_ID}/edit?",
            f"https://docs.google.com/forms/d/{FORM_ID}/edit?token=secret",
            f"https://docs.google.com/forms/d/{FORM_ID}/edit?usp=unknown",
            (
                f"https://docs.google.com/forms/d/{FORM_ID}/edit"
                "?usp=sharing&usp=sharing"
            ),
        )
        for link in cases:
            with self.subTest(link=link):
                self._assert_code(
                    link,
                    GoogleFormsLinkErrorCode.UNSAFE_URL_COMPONENT,
                )

    def test_error_object_does_not_retain_source_link(self):
        secret = "private-form-id"
        link = f"https://evil.example/forms/d/{secret}/edit"

        with self.assertRaises(GoogleFormsLinkError) as caught:
            parse_google_forms_edit_link(link)

        error = caught.exception
        self.assertEqual(error.code, GoogleFormsLinkErrorCode.UNSUPPORTED_HOST)
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error))
        self.assertFalse(any(secret in str(value) for value in vars(error).values()))

    def _assert_code(
        self,
        link: str,
        expected: GoogleFormsLinkErrorCode,
    ) -> None:
        with self.assertRaises(GoogleFormsLinkError) as caught:
            parse_google_forms_edit_link(link)
        self.assertEqual(caught.exception.code, expected)
        self.assertNotIn(link, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
