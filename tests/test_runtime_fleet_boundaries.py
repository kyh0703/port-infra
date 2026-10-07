import copy
import unittest

from test_runtime_fleet import fleet, registry


class AdmissionRegistryBoundary:
    """Independent registry DTOs; not a native ACK or Cloud evidence producer."""

    def __init__(self, *, initial, recovery, readiness, first_cutover=False):
        self.before = registry()
        self.after = copy.deepcopy(self.before)
        pool = self.after['pools'][0]
        pool.update(revision=9, initialAccepting=initial, recoveryAccepting=recovery)
        for launcher in self.after['launchers']:
            launcher['ackRevision'] = 9
        self.after['readiness'] = readiness
        self.evidence = {
            'evidenceId': pool['firstCutoverEvidenceId'],
            'evidenceHash': pool['firstCutoverEvidenceHash'],
        } if first_cutover else None
        if first_cutover:
            for name in ('firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt'):
                self.before['pools'][0][name] = None
        self.writes = []
        self.readbacks = []

    def registry(self, **kwargs):
        snapshot = copy.deepcopy(self.after if self.writes else self.before)
        self.readbacks.append(snapshot)
        return snapshot

    def request(self, method, path, body, **kwargs):
        if method != 'PUT' or path != '/internal/runtime-launchers/pools/a/admission':
            raise AssertionError('unexpected admission boundary operation')
        if body['expectedRevision'] != 8:
            raise AssertionError('admission did not use the observed CAS revision')
        self.writes.append(copy.deepcopy(body))
        # Persistence is not acknowledgment. The subsequent readback is an
        # independently supplied authoritative observation, not this PUT body.
        return {'revision': 9}


class FinalAdmissionReadinessTests(unittest.TestCase):
    def test_exact_incarnation_acks_cannot_complete_enabling_with_failed_final_data_readiness(self):
        for initial, recovery in ((True, False), (False, True), (True, True)):
            for first_cutover in (False, True):
                for field in ('database', 'redis', 'keyring'):
                    for unavailable in (False, None, 'true'):
                        with self.subTest(initial=initial, recovery=recovery, first_cutover=first_cutover,
                                field=field, unavailable=unavailable):
                            readiness = {'database': True, 'redis': True, 'keyring': True}
                            readiness[field] = unavailable
                            boundary = AdmissionRegistryBoundary(initial=initial, recovery=recovery,
                                readiness=readiness, first_cutover=first_cutover)
                            # The initial snapshot is ready and every final ACK
                            # and attestation is exact; only current data changed.
                            fleet.require_data_ready(boundary.before)
                            fleet.require_pool_ack(boundary.after, 'a', 2)
                            fleet.require_first_cutover_authorization(boundary.after['pools'][0])
                            with self.assertRaises(fleet.FleetError):
                                fleet.set_admission(boundary, 'a', 2, initial=initial, recovery=recovery,
                                    retiring=False, timeout=0, first_cutover_evidence=boundary.evidence)
                            self.assertEqual(len(boundary.writes), 1, 'reject completion, not the ready pre-mutation state')
                            self.assertEqual(boundary.readbacks[-1]['readiness'], readiness)

    def test_absent_final_readiness_cannot_complete_exact_acknowledged_admission(self):
        boundary = AdmissionRegistryBoundary(initial=True, recovery=True, readiness=None)
        del boundary.after['readiness']
        with self.assertRaises(fleet.FleetError):
            fleet.set_admission(boundary, 'a', 2, initial=True, recovery=True, retiring=False, timeout=0)
        self.assertEqual(len(boundary.writes), 1)

    def test_same_final_snapshot_with_exact_acks_attestation_and_ready_data_completes(self):
        for first_cutover in (False, True):
            with self.subTest(first_cutover=first_cutover):
                boundary = AdmissionRegistryBoundary(initial=True, recovery=True,
                    readiness={'database': True, 'redis': True, 'keyring': True}, first_cutover=first_cutover)
                result = fleet.set_admission(boundary, 'a', 2, initial=True, recovery=True,
                    retiring=False, timeout=0, first_cutover_evidence=boundary.evidence)
                self.assertEqual(result, boundary.after)
                self.assertEqual(result, boundary.readbacks[-1])
                self.assertEqual(len(boundary.writes), 1)

    def test_data_outage_does_not_prevent_non_enabling_withdrawal_acknowledgment(self):
        boundary = AdmissionRegistryBoundary(initial=False, recovery=False,
            readiness={'database': True, 'redis': False, 'keyring': True})
        boundary.before['readiness']['redis'] = False
        result = fleet.set_admission(boundary, 'a', 2, initial=False, recovery=False,
            retiring=False, timeout=0)
        self.assertFalse(result['pools'][0]['initialAccepting'])
        self.assertFalse(result['pools'][0]['recoveryAccepting'])
        self.assertFalse(result['readiness']['redis'])


if __name__ == '__main__':
    unittest.main()
