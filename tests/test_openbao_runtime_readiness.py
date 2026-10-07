import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('openbao_platform', ROOT / 'scripts/openbao_platform.py')
platform = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = platform
spec.loader.exec_module(platform)


class OriginalBaoReadinessTests(unittest.TestCase):
    def test_process_health_does_not_prove_unsealed_original_ciphertext_keys(self):
        self.assertFalse(platform.original_cluster_ready({'initialized': True, 'sealed': True, 'cluster_id': 'original'}, 'original'))
        self.assertFalse(platform.original_cluster_ready({'initialized': False, 'sealed': False, 'cluster_id': 'original'}, 'original'))
        self.assertFalse(platform.original_cluster_ready({'initialized': True, 'sealed': False, 'cluster_id': 'new-empty-cluster'}, 'original'))
        self.assertFalse(platform.original_cluster_ready({'initialized': True, 'sealed': False, 'cluster_id': 'original'}, ''))
        self.assertTrue(platform.original_cluster_ready({'initialized': True, 'sealed': False, 'cluster_id': 'original'}, 'original'))


if __name__ == '__main__':
    unittest.main()
