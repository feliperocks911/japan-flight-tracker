import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import tracker

CONFIG = tracker.read_config(tracker.ROOT / 'config.json')
NOW = datetime(2026, 9, 30, 15, tzinfo=timezone.utc)


def offer(total='2700.00'):
    segment = {'passengers': [{'cabin_class': 'economy'}] * 3,
               'operating_carrier': {'name': 'Example Airline'}}
    return {'id': 'off_example', 'total_amount': total, 'total_currency': 'USD',
            'live_mode': True, 'passengers': [{'type': 'adult'}] * 3,
            'expires_at': '2026-09-30T17:00:00Z',
            'slices': [{'segments': [copy.deepcopy(segment)]} for _ in range(2)]}


class TrackerTests(unittest.TestCase):
    def test_reset_in_summer_and_winter(self):
        for before, after, expected in [
            ('2026-09-30T12:59:59+00:00', '2026-09-30T13:00:00+00:00', '2026-09-30'),
            ('2026-12-01T13:59:59+00:00', '2026-12-01T14:00:00+00:00', '2026-12-01'),
            ('2026-11-01T13:59:59+00:00', '2026-11-01T14:00:00+00:00', '2026-11-01')]:
            self.assertNotEqual(tracker.tracking_day(datetime.fromisoformat(before), CONFIG), expected)
            self.assertEqual(tracker.tracking_day(datetime.fromisoformat(after), CONFIG), expected)

    def test_offer_filters(self):
        valid = offer()
        self.assertTrue(tracker.eligible(valid, CONFIG, NOW))
        for key, value in [('total_currency', 'GBP'), ('live_mode', False),
                           ('expires_at', '2026-09-30T14:00:00Z'), ('passengers', [{}])]:
            bad = copy.deepcopy(valid)
            bad[key] = value
            self.assertFalse(tracker.eligible(bad, CONFIG, NOW))
        bad = copy.deepcopy(valid)
        bad['slices'][0]['segments'] *= 3
        self.assertFalse(tracker.eligible(bad, CONFIG, NOW))
        valid['slices'][0]['segments'] *= 2
        self.assertTrue(tracker.eligible(valid, CONFIG, NOW))
        bad = copy.deepcopy(valid)
        bad['slices'][1]['segments'][0]['passengers'][0]['cabin_class'] = 'business'
        self.assertFalse(tracker.eligible(bad, CONFIG, NOW))

    def test_thresholds_before_rounding(self):
        _, drop, reasons = tracker.fare_decision(Decimal('900'), Decimal('1125'), Decimal('1125'), CONFIG)
        self.assertEqual(drop, 20)
        self.assertEqual(len(reasons), 2)
        _, _, reasons = tracker.fare_decision(Decimal('900.001'), Decimal('1000'), Decimal('1000'), CONFIG)
        self.assertEqual(reasons, [])

    def test_state_alerts_no_offers_and_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = tracker.open_db(Path(directory) / 'state.db')
            args = (conn, CONFIG, 'HND', '2027-10-01', '2027-10-16')
            tracker.record(*args, None, NOW)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily').fetchone()[0], 0)
            tracker.record(*args, offer('3600'), NOW)
            tracker.record(*args, offer('2880'), NOW)
            tracker.record(*args, offer('2880'), NOW)
            tracker.record(*args, offer('2700'), NOW)
            tracker.record(*args, offer('3300'), NOW)
            row = conn.execute('SELECT * FROM daily').fetchone()
            self.assertEqual(Decimal(row['start']), 1200)
            self.assertEqual(Decimal(row['low']), 900)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 2)
            tracker.record(*args, offer('3000'), datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
            row = conn.execute("SELECT * FROM daily WHERE day='2026-10-01'").fetchone()
            self.assertEqual(Decimal(row['start']), 1000)
            payload = json.loads(conn.execute('SELECT payload FROM history LIMIT 1').fetchone()[0])
            self.assertEqual(len(payload), len(tracker.HEADERS))
            conn.close()

    def test_missing_secrets_do_not_fail_validation(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(len(tracker.missing_credentials()), 5)
            self.assertEqual(tracker.run(CONFIG), 0)

    def test_search_all_pages_and_three_adults(self):
        replies = [{'data': {'id': 'orq_1'}},
                   {'data': [offer('3300')], 'meta': {'after': 'cursor'}},
                   {'data': [offer('2700')], 'meta': {'after': None}}]
        with patch.dict(os.environ, {'DUFFEL_ACCESS_TOKEN': 'test'}), patch.object(tracker, 'api', side_effect=replies) as api:
            result = tracker.search(None, CONFIG, 'HND', '2027-10-01', '2027-10-16', NOW)
            self.assertEqual(result['total_amount'], '2700')
            body = api.call_args_list[0].kwargs['json']['data']
            self.assertEqual(body['passengers'], [{'type': 'adult'}] * 3)
            self.assertEqual(body['slices'][1]['destination'], 'DFW')

    def test_telegram_failure_leaves_alert_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = tracker.open_db(Path(directory) / 'state.db')
            tracker.record(conn, CONFIG, 'HND', '2027-10-01', '2027-10-16', offer(), NOW)
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret', 'TELEGRAM_CHAT_ID': '123'}):
                with patch.object(tracker, 'api', side_effect=tracker.ServiceError('Telegram: HTTP 503')):
                    with self.assertRaises(tracker.ServiceError):
                        tracker.deliver_telegram(conn, None)
                self.assertEqual(conn.execute('SELECT delivered FROM alerts').fetchone()[0], 0)
                with patch.object(tracker, 'api', return_value={'ok': True}):
                    tracker.deliver_telegram(conn, None)
                self.assertEqual(conn.execute('SELECT delivered FROM alerts').fetchone()[0], 1)
            conn.close()

    def test_sheets_retry_deduplicates_rows_and_keeps_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = tracker.open_db(Path(directory) / 'state.db')
            tracker.record(conn, CONFIG, 'HND', '2027-10-01', '2027-10-16', None, NOW)
            row_id = conn.execute('SELECT id FROM history').fetchone()[0]
            tab = Mock()
            tab.col_values.return_value = [tracker.HEADERS[0]]
            tab.append_rows.side_effect = RuntimeError('simulated timeout')
            client = Mock()
            client.open_by_key.return_value.worksheet.return_value = tab
            with patch.dict(os.environ, {'GOOGLE_SERVICE_ACCOUNT_JSON': '{}', 'GOOGLE_SHEET_ID': 'example'}), patch('google.oauth2.service_account.Credentials.from_service_account_info'), patch('gspread.authorize', return_value=client):
                with self.assertRaises(tracker.ServiceError):
                    tracker.deliver_sheets(conn, CONFIG)
                self.assertEqual(conn.execute('SELECT delivered FROM history').fetchone()[0], 0)
                tab.col_values.return_value = [tracker.HEADERS[0], row_id]
                tab.append_rows.reset_mock()
                tab.append_rows.side_effect = None
                tracker.deliver_sheets(conn, CONFIG)
                tab.append_rows.assert_not_called()
                self.assertEqual(conn.execute('SELECT delivered FROM history').fetchone()[0], 1)
            conn.close()


if __name__ == '__main__':
    unittest.main()
