import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from printer_faults import load_profile
from status_calibration import calibrate, derive_profile


def samples(normal='0001c1', opened='0103ed', empty='0103ee', recovered='0001c0'):
    return {'normal': [normal] * 3, 'cover_open': [opened] * 3,
            'paper_missing': [empty] * 3, 'recovered': [recovered] * 3}


class CalibrationTests(unittest.TestCase):
    def test_shared_flag_is_reported_as_combined_paper_or_cover_fault(self):
        profile = derive_profile(samples(), 'test-device')
        self.assertEqual([rule['code'] for rule in profile['rules']], ['paper_or_cover_problem'])
        self.assertEqual(profile['rules'][0]['mask'], 1)

    def test_separate_observed_flags_can_be_distinguished(self):
        profile = derive_profile(samples(opened='0201c1', empty='0101c1'), 'test-device')
        self.assertEqual([rule['code'] for rule in profile['rules']], ['cover_open', 'paper_missing'])

    def test_later_payload_bytes_are_not_guessed_to_be_fault_bits(self):
        profile = derive_profile(samples(opened='0003ed', empty='0003ee'), 'test-device')
        self.assertEqual(profile['rules'], [])
        self.assertEqual(profile['unverified_conditions'], ['cover_open', 'paper_missing'])

    def test_recovery_must_restore_normal_flag_state(self):
        with self.assertRaises(ValueError):
            derive_profile(samples(recovered='0101c1'), 'test-device')

    def test_varying_fault_flags_cannot_be_misclassified_as_a_separate_sensor(self):
        data = samples(opened='0301c1')
        data['paper_missing'] = ['0101c1', '0301c1', '0101c1']
        with self.assertRaises(ValueError):
            derive_profile(data, 'test-device')

    def test_insufficient_or_malformed_samples_are_rejected(self):
        data = samples()
        data['cover_open'] = ['0101c1']
        with self.assertRaises(ValueError):
            derive_profile(data, 'test-device')
        data['cover_open'] = ['not-hex'] * 3
        with self.assertRaises(ValueError):
            derive_profile(data, 'test-device')

    def test_cannot_calibrate_using_a_different_printers_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'diagnostics').mkdir()
            for label, values in samples().items():
                (state / 'diagnostics' / f'{label}.json').write_text(json.dumps({
                    'label': label, 'samples': values,
                    'device_id_sha256': hashlib.sha256(b'another-device').hexdigest()}))
            with self.assertRaises(ValueError):
                calibrate(state, 'test-device')
            self.assertFalse((state / 'status-profile.json').exists())

    def test_invalid_profile_rules_are_treated_as_unverified(self):
        profile = derive_profile(samples(), 'test-device')
        profile['rules'][0]['mask'] = 'guess'
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'status-profile.json').write_text(json.dumps(profile))
            self.assertIsNone(load_profile(state, 'test-device'))


if __name__ == '__main__':
    unittest.main()
