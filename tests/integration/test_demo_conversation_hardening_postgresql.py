"""The offline dialogue campaign on the official disposable PostgreSQL target."""

import unittest
from tests.integration import test_public_reservation_api_postgresql as fixture
from tests.test_demo_conversation_simulation import DialogueContract


@unittest.skipIf(fixture.SKIP_REASON is not None, fixture.SKIP_REASON or "")
class PostgreSQLDialogueTests(DialogueContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.PublicReservationAPIPostgreSQLTests.setUpClass.__func__(cls)
