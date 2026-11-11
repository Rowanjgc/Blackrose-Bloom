"""The request clock follows the system's time zone, including daylight saving."""
from datetime import UTC, datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from blackrose import clock_context as clock


class SystemTimeZoneTests(unittest.TestCase):
    def zone(self, tz=None, localtime='/etc/localtime'):
        env = {k: v for k, v in os.environ.items() if k != 'TZ'}
        if tz is not None:
            env['TZ'] = tz
        with patch.dict(os.environ, env, clear=True), \
                patch.object(clock.os.path, 'realpath', return_value=localtime):
            return clock.system_time_zone()

    def test_tz_names_the_zone(self):
        self.assertEqual(self.zone('Europe/Berlin').key, 'Europe/Berlin')
        self.assertEqual(self.zone(':Asia/Tokyo').key, 'Asia/Tokyo')
        self.assertEqual(self.zone('/usr/share/zoneinfo/Australia/Sydney').key, 'Australia/Sydney')

    def test_localtime_link_names_the_zone_without_tz(self):
        self.assertEqual(self.zone(localtime='/usr/share/zoneinfo/America/Sao_Paulo').key,
                         'America/Sao_Paulo')

    def test_invalid_tz_falls_back_to_localtime(self):
        zone = self.zone('Not/A_Zone', localtime='/usr/share/zoneinfo/Europe/Lisbon')
        self.assertEqual(zone.key, 'Europe/Lisbon')

    def test_unnamed_system_zone_uses_the_local_offset(self):
        zone = self.zone(localtime='/etc/localtime')
        self.assertIsNone(getattr(zone, 'key', None))
        self.assertIsNotNone(datetime.now(zone).utcoffset())


class ClockContextTests(unittest.TestCase):
    def test_reports_the_given_zone_across_daylight_saving(self):
        berlin = ZoneInfo('Europe/Berlin')
        winter = clock.clock_context(datetime(2026, 1, 15, 12, tzinfo=UTC), berlin)
        summer = clock.clock_context(datetime(2026, 7, 15, 12, tzinfo=UTC), berlin)
        self.assertIn('Local date/time: 2026-01-15T13:00:00+01:00', winter)
        self.assertIn('UTC offset: +01:00', winter)
        self.assertIn('Local date/time: 2026-07-15T14:00:00+02:00', summer)
        self.assertIn('UTC offset: +02:00', summer)
        self.assertIn('IANA time zone: Europe/Berlin', summer)
        self.assertIn('UTC date/time: 2026-07-15T12:00:00Z', summer)

    def test_unnamed_zone_omits_the_iana_line(self):
        text = clock.clock_context(datetime(2026, 1, 15, 12, tzinfo=UTC),
                                   timezone(timedelta(hours=-3, minutes=-30)))
        self.assertIn('UTC offset: -03:30', text)
        self.assertNotIn('IANA time zone', text)

    def test_defaults_to_the_system_zone(self):
        with patch.object(clock, 'system_time_zone', return_value=ZoneInfo('Asia/Kolkata')):
            text = clock.clock_context(datetime(2026, 1, 15, 12, tzinfo=UTC))
        self.assertIn('IANA time zone: Asia/Kolkata', text)
        self.assertIn('Local date/time: 2026-01-15T17:30:00+05:30', text)

    def test_naive_instants_are_refused(self):
        with self.assertRaises(ValueError):
            clock.clock_context(datetime(2026, 1, 15, 12))


if __name__ == '__main__':
    unittest.main()
