from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import gpu_runtime


class RuntimePublishTests(unittest.TestCase):
    def test_publish_moves_only_verified_task_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / '.cache' / 'tasks' / 'owned' / 'gpu-site'
            staging.mkdir(parents=True)
            (staging / 'fixture').write_text('runtime')
            runtime = root / '.runtime' / 'gpu' / 'cu12-v1'
            with patch.object(gpu_runtime, 'ROOT', root), patch.object(gpu_runtime, 'RUNTIME', runtime), \
                 patch.object(gpu_runtime, '_complete', return_value=True):
                result = gpu_runtime.publish_runtime({'ok': True, '_runtime_staging': str(staging)})
            self.assertEqual(result, {'ok': True})
            self.assertEqual((runtime / 'fixture').read_text(), 'runtime')
            self.assertFalse(staging.exists())

    def test_publish_rejects_path_outside_owned_task_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / 'outside' / 'gpu-site'
            staging.mkdir(parents=True)
            with patch.object(gpu_runtime, 'ROOT', root), patch.object(gpu_runtime, '_complete', return_value=True):
                with self.assertRaises(ValueError):
                    gpu_runtime.publish_runtime({'_runtime_staging': str(staging)})
            self.assertTrue(staging.exists())

    def test_incomplete_existing_install_is_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / '.cache' / 'tasks' / 'owned' / 'gpu-site'
            runtime = root / '.runtime' / 'gpu' / 'cu12-v1'
            staging.mkdir(parents=True)
            runtime.mkdir(parents=True)
            (runtime / 'keep').write_text('original')
            with patch.object(gpu_runtime, 'ROOT', root), patch.object(gpu_runtime, 'RUNTIME', runtime), \
                 patch.object(gpu_runtime, '_complete', side_effect=lambda path: path == staging):
                with self.assertRaises(RuntimeError):
                    gpu_runtime.publish_runtime({'_runtime_staging': str(staging)})
            self.assertEqual((runtime / 'keep').read_text(), 'original')
            self.assertTrue(staging.exists())


if __name__ == '__main__':
    unittest.main()
