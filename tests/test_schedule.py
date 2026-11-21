import unittest

from schedule import schedule


class ScheduleTests(unittest.TestCase):
    def test_worker_overhead(self):
        self.assertEqual(schedule([], {}), [])
        self.assertEqual(schedule([1, 0], {0: 1100, 1: 1000}), [[0, 1]])
        self.assertEqual(schedule([0, 1], {0: 7400, 1: 7400}), [[0], [1]])

    def test_manifest_numbers_and_zero_duration(self):
        durations = {100 + i: 0 if i % 3 == 0 else i * 300 for i in range(38)}
        workers = schedule(list(durations), durations)
        self.assertTrue(1 <= len(workers) <= 8)
        self.assertTrue(all(workers))
        self.assertEqual(sorted(n for w in workers for n in w), sorted(durations))
