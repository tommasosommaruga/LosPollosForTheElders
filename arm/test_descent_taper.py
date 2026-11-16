"""Execute the descent loop with deterministic encoders; never connect to hardware."""
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_integration


class DescentTaperTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        test_integration.ControllerRoutingTests.setUpClass()
        cls.module = test_integration.ControllerRoutingTests.reach_module

    async def run_descent(self, sag=-57, contact_height=None, floor=2.):
        m = self.module
        r = m.Reacher.__new__(m.Reacher)
        names = m.MOTOR_JOINTS
        joints = {i: types.SimpleNamespace(raw=1900, goal=1900, torque=True,
                    range_min=1000, range_max=3000) for i in names}
        joints[2].goal += sag
        arm = types.SimpleNamespace(joints=joints, sag={2: sag}, hold=AsyncMock(), last_update=0.)
        arm._settle_and_ease = AsyncMock()
        r.arm = arm
        # Local shoulder sensitivity in the supplied run: 57 steps ~= 2.85 cm.
        r._angle = lambda motor, raw: raw
        r._raw = lambda motor, angle: round(angle)
        r.tip = lambda q: ((0., .25, (2. + (2000. - q[names[2]]) / 20.) / 100.), None, [])
        r.table = types.SimpleNamespace(height=lambda p: p[2])
        def state():
            return (0., 25., 2. + (2000. - joints[2].raw) / 20., 0.)
        r.state = state
        path = [{n: (1900 + 10*i if motor == 2 else 1900) for motor, n in names.items()}
                for i in range(1, 11)]
        r.plan_line = Mock(return_value=path)
        clock = [0.]
        writes = []
        async def write(commands):
            goals = {i: int.from_bytes(raw, 'little') for i, _, raw in commands}
            writes.append((clock[0], goals))
            for i, goal in goals.items():
                joints[i].goal = goal
                joints[i].raw = goal
            if contact_height is not None and state()[2] < contact_height:
                joints[2].raw = 2000 - (contact_height - 2.) * 20
            arm.last_update += 1.
        async def sleep(seconds):
            clock[0] += seconds
        arm._write = write
        report = {}
        with patch.dict(m.Reacher.descend_guarded.__globals__, {
                'time': types.SimpleNamespace(monotonic=lambda: clock[0]),
                'asyncio': types.SimpleNamespace(sleep=sleep), 'roll_raw': lambda: None}):
            result = await r.descend_guarded(0., 25., floor, report=report)
        return r, result, report, writes

    async def test_recorded_shoulder_bias_tapers_to_zero_at_closing_height(self):
        r, result, report, writes = await self.run_descent()
        self.assertEqual(writes[0][1][2], 1843)  # unchanged initial servo reference
        self.assertEqual(writes[-1][1][2], 2000)  # NOT 1943 (+2.85 cm too high)
        self.assertAlmostEqual(report['final_motor_target_cm'], 2.)
        self.assertAlmostEqual(result['final_cm'], 2.)
        self.assertFalse(result['contact'])
        r.arm._settle_and_ease.assert_awaited_once()
        self.assertFalse(r.arm._settle_and_ease.await_args.args[1])
        self.assertEqual(r.arm._settle_and_ease.await_args.args[0], writes[-1][1])
        self.assertFalse(r.plan_line.call_args.kwargs['partial'])
        for (ta, a), (tb, b) in zip(writes, writes[1:]):
            self.assertLessEqual(abs(b[2] - a[2]), self.module.MAX_JOINT_SPEED * (tb-ta) + 1)
        self.assertTrue(all(set(w) == set(range(1, 7)) for _, w in writes))

    async def test_no_compensation_still_finishes_at_requested_height(self):
        _, result, report, _ = await self.run_descent(sag=0)
        self.assertEqual(result['final_cm'], 2.)
        self.assertEqual(report['stop_reason'], 'planned_path_complete')

    async def test_contact_backoff_uses_compensated_motor_path_without_fresh_push(self):
        r, result, report, writes = await self.run_descent(sag=0, contact_height=3.5)
        self.assertTrue(result['contact'])
        self.assertEqual(report['stop_reason'], 'contact_inferred')
        self.assertAlmostEqual(result['final_cm'], 3.8, delta=.06)
        self.assertFalse(r.arm._settle_and_ease.await_args.args[1])

    async def test_partial_below_table_tool_is_not_reported_as_reaching_floor(self):
        r, _, report, _ = await self.run_descent(floor=-2.5)
        self.assertTrue(r.plan_line.call_args.kwargs['partial'])
        self.assertEqual(report['stop_reason'], 'partial_path_complete')


if __name__ == '__main__':
    unittest.main()
