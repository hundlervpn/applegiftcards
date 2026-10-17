import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.services.email import build_gift_card_email
from app.services.order_fulfillment import GiftCardDelivery

CODE_ONE = "ABCD-EFGH-IJKL"
CODE_TWO = "MNOP-QRST-UVWX"


def make_delivery(**overrides) -> GiftCardDelivery:
    values = {
        "order_id": 7,
        "recipient_email": "buyer@example.com",
        "gift_card_codes": (CODE_ONE, CODE_TWO),
        "items": ({"nominal_usd": 10, "quantity": 1, "price_rub": "999.00"},),
        "total_amount_rub": Decimal("999.00"),
        "created_at": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return GiftCardDelivery(**values)


class BuildGiftCardEmailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = build_gift_card_email(
            make_delivery(),
            from_email="no-reply@okno.example",
        )

    def test_plain_text_part_is_preserved(self) -> None:
        plain_part = self.message.get_body(("plain",))
        self.assertIsNotNone(plain_part)
        plain = plain_part.get_content()
        self.assertIn(CODE_ONE, plain)
        self.assertIn(CODE_TWO, plain)
        self.assertIn("999.00", plain)

    def test_html_part_substitutes_template_variables(self) -> None:
        html_part = self.message.get_body(("html",))
        self.assertIsNotNone(html_part)
        html = html_part.get_content()

        self.assertIn("ОКНО В ЕВРОПУ", html)          # бренд
        self.assertIn("№7", html)                     # order_id
        self.assertIn("18.10.2026", html)             # order_date
        self.assertIn("999.00", html)                 # total_amount_rub
        self.assertIn("$10", html)                    # nominal_usd
        self.assertIn(CODE_ONE, html)                 # codes
        self.assertIn(CODE_TWO, html)
        self.assertIn("buyer@example.com", html)      # recipient_email
        self.assertIn("Redeem Gift Card or Code", html)  # инструкция

    def test_headers_are_filled(self) -> None:
        self.assertEqual(self.message["To"], "buyer@example.com")
        self.assertEqual(self.message["From"], "no-reply@okno.example")
        self.assertIn("7", self.message["Subject"])

    def test_html_is_escaped_against_injection(self) -> None:
        delivery = make_delivery(gift_card_codes=('<script>alert("x")</script>',))
        message = build_gift_card_email(delivery, from_email="no-reply@okno.example")

        html = message.get_body(("html",)).get_content()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
