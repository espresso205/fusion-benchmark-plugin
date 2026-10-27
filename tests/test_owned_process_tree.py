"""Real disposable process trees; no CLI provider, model or CAD is launched."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class OwnedProcessTreeTests(unittest.TestCase):
    def test_posix_root_is_reaped_before_group_liveness_check(self):
        from fusion_pipeline.adapters import _PosixProcessTree
        reaped = []
        process = SimpleNamespace(pid=12345, wait=lambda timeout: reaped.append(True))
        def signal_group(group, signal):
            self.assertEqual(group, process.pid)
            if signal == 0:
                self.assertTrue(reaped, 'the exited root must be reaped before checking its group')
                raise ProcessLookupError()
        tree = _PosixProcessTree(process)
        with (patch('fusion_pipeline.adapters.os.killpg', side_effect=signal_group, create=True),
              patch('signal.SIGKILL', 9, create=True)):
            tree.stop()
        self.assertTrue(tree.verified_stopped)
        self.assertEqual(tree.remaining_processes, 0)

    def test_exited_parent_does_not_hide_surviving_owned_children(self):
        from fusion_pipeline.adapters import spawn_owned_process
        from fusion_pipeline.runner import stop_tree
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'child.pid'
            script = ('import pathlib,subprocess,sys; '
                      'p=subprocess.Popen([sys.executable,"-B","-c","import time; time.sleep(120)"],'
                      'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
                      'pathlib.Path(sys.argv[1]).write_text(str(p.pid))')
            process = spawn_owned_process([sys.executable, '-B', '-c', script, str(marker)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(stop_tree, process)
            self.assertEqual(process.wait(timeout=15), 0)
            self.assertTrue(marker.is_file())
            tree = process._benchmark_owned_tree
            self.assertFalse(tree.verified_stopped)
            stop_tree(process)
            self.assertTrue(tree.verified_stopped)
            self.assertEqual(tree.remaining_processes, 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows suspended assignment boundary')
    def test_failed_job_assignment_never_resumes_candidate(self):
        from fusion_pipeline.adapters import spawn_owned_process, _WindowsProcessTree
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'executed'
            script = 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("must not run")'
            with patch.object(_WindowsProcessTree, 'attach', side_effect=RuntimeError('assignment failed')):
                with self.assertRaisesRegex(RuntimeError, 'assignment failed'):
                    spawn_owned_process([sys.executable, '-B', '-c', script, str(marker)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
