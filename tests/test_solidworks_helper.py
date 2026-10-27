import unittest
import json
import os
import subprocess
import sys
import threading
import types
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from fusion_pipeline.solidworks_helper import verify_instance, validate_health, validate_references


class ReadOnlyWorkerTimeoutTests(unittest.TestCase):
    def call_worker(self, name, timeout, *, expires=False):
        from fusion_pipeline.solidworks_helper import _serve
        instance = {'pid':100, 'session_id':1, 'version':'34.4', 'executable':'C:/fixture/SolidWorks'}
        vendor = types.ModuleType('sim_plugin_solidworks')
        tools = types.ModuleType('sim_plugin_solidworks.tools')
        server = types.ModuleType('sim_plugin_solidworks.mcp_server')
        vendor.tools, vendor.mcp_server = tools, server
        tools.list_tool_specs = lambda: []
        tools.run_tool = lambda *args, **kwargs: None
        tools._result = lambda success, tool, message, data, **kwargs: dict(success=success,tool=tool,data=data,**kwargs)
        process = Mock(pid=123,returncode=0)
        reply=(b'{"success":true,"ok":true}',b'')
        process.communicate.side_effect = [subprocess.TimeoutExpired('owned-worker',timeout),reply] if expires else [reply]
        captured = []
        server.run = lambda: captured.append(tools._run_isolated_request({'tool':name,'arguments':{}},tool=name,timeout_s=timeout))
        with patch.dict(sys.modules,{'sim_plugin_solidworks':vendor,'sim_plugin_solidworks.tools':tools,
                'sim_plugin_solidworks.mcp_server':server}), patch.dict(os.environ,{'CAD_BENCHMARK_EXPECTED_INSTANCE':json.dumps(instance)}), \
                patch('fusion_pipeline.solidworks_helper._load_vendor'), \
                patch('fusion_pipeline.solidworks_helper.record_worker_before_dispatch'), \
                patch('fusion_pipeline.solidworks_helper.subprocess.Popen',return_value=process):
            _serve('C:/fixture/vendor')
        return captured[0], process

    def test_readonly_worker_timeout_matches_adapter_budget_without_changing_modeling(self):
        for tool,configured,expected in [('get_solidworks_info',180,30),('benchmark_workspace',180,30),
                                         ('benchmark_workspace',20,20),('extrude_sketch',180,180),('loft',600,600)]:
            with self.subTest(tool=tool,configured=configured):
                result,process=self.call_worker(tool,configured)
                self.assertEqual(process.communicate.call_args.kwargs['timeout'],expected)
                self.assertEqual(result['process_isolation']['hard_timeout_s'],expected)
                self.assertEqual(result['executor']['timeout_s'],expected)
                process.kill.assert_not_called()

    def test_readonly_worker_timeout_still_reports_unknown_and_never_replays_request(self):
        result,process=self.call_worker('benchmark_workspace',180,expires=True)
        self.assertEqual(process.communicate.call_args_list[0].kwargs['timeout'],30)
        self.assertEqual(result['data']['completion_state'],'unknown')
        self.assertTrue(result['data']['partial_mutation_possible'])
        self.assertFalse(result['data']['automatic_retry_allowed'])
        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_args_list[1].kwargs,{'timeout':5})
        self.assertEqual(process.communicate.call_args_list[1].args,())


class HelperEvidenceTests(unittest.TestCase):
    def test_reused_worker_pid_proves_recorded_worker_exited_without_termination(self):
        from fusion_pipeline.solidworks_helper import terminate_owned_worker
        record = {'pid': 123, 'created': 100, 'executable': 'C:/fixture/python.exe'}
        for executable in ['C:/fixture/python.exe', 'C:/unrelated/other.exe']:
            with self.subTest(executable=executable):
                actual = {**record, 'created': 200, 'executable': executable}
                kernel = types.SimpleNamespace(CloseHandle=Mock(), TerminateProcess=Mock(), WaitForSingleObject=Mock())
                with patch('fusion_pipeline.solidworks_helper._process_handle', return_value=(kernel, 999)), patch(
                        'fusion_pipeline.solidworks_helper._process_record_handle', return_value=actual):
                    evidence = terminate_owned_worker(record)
                self.assertIs(evidence.get('already_exited'), True)
                self.assertIs(evidence.get('pid_reused'), True)
                self.assertFalse(evidence['terminated'])
                self.assertEqual(evidence['recorded_identity'], record)
                self.assertEqual(evidence['observed_identity'], actual)
                kernel.TerminateProcess.assert_not_called()
                kernel.WaitForSingleObject.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(999)

    def test_same_creation_time_executable_mismatch_does_not_certify_exit(self):
        from fusion_pipeline.solidworks_helper import terminate_owned_worker
        record = {'pid': 123, 'created': 100, 'executable': 'C:/fixture/python.exe'}
        actual = {**record, 'executable': 'C:/unrelated/other.exe'}
        kernel = types.SimpleNamespace(CloseHandle=Mock(), TerminateProcess=Mock())
        with patch('fusion_pipeline.solidworks_helper._process_handle', return_value=(kernel, 999)), patch(
                'fusion_pipeline.solidworks_helper._process_record_handle', return_value=actual):
            evidence = terminate_owned_worker(record)
        self.assertFalse(evidence.get('already_exited', False))
        self.assertIn('error', evidence)
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(999)

    def test_unreadable_process_identity_does_not_certify_exit(self):
        from fusion_pipeline.solidworks_helper import terminate_owned_worker
        record = {'pid': 123, 'created': 100, 'executable': 'C:/fixture/python.exe'}
        with patch('fusion_pipeline.solidworks_helper._process_handle', side_effect=PermissionError('access denied')):
            evidence = terminate_owned_worker(record)
        self.assertFalse(evidence.get('already_exited', False))
        self.assertIn('error', evidence)
        kernel = types.SimpleNamespace(CloseHandle=Mock(), TerminateProcess=Mock())
        with patch('fusion_pipeline.solidworks_helper._process_handle', return_value=(kernel, 999)), patch(
                'fusion_pipeline.solidworks_helper._process_record_handle', side_effect=OSError('identity unreadable')):
            with self.assertRaises(OSError):
                terminate_owned_worker(record)
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(999)

    def test_malformed_current_creation_identity_does_not_certify_reuse(self):
        from fusion_pipeline.solidworks_helper import terminate_owned_worker
        record = {'pid': 123, 'created': 100, 'executable': 'C:/fixture/python.exe'}
        for changed in [{'pid': 456, 'created': 200}, {'created': True}, {'created': None}, {'created': -1}]:
            with self.subTest(changed=changed):
                actual = {**record, **changed}
                kernel = types.SimpleNamespace(CloseHandle=Mock(), TerminateProcess=Mock())
                with patch('fusion_pipeline.solidworks_helper._process_handle', return_value=(kernel, 999)), patch(
                        'fusion_pipeline.solidworks_helper._process_record_handle', return_value=actual):
                    evidence = terminate_owned_worker(record)
                self.assertFalse(evidence.get('already_exited', False))
                self.assertIn('error', evidence)
                kernel.TerminateProcess.assert_not_called()

    def test_native_warning_policy_distinguishes_presentation_and_geometry(self):
        from fusion_pipeline.solidworks_helper import classify_native_warnings
        self.assertEqual(classify_native_warnings('load',0)['status'],'clear')
        self.assertEqual(classify_native_warnings('load',2)['status'],'allowed')
        self.assertEqual(classify_native_warnings('save',64|128|256)['status'],'allowed')
        self.assertEqual(classify_native_warnings('save',1|64)['status'],'blocked')
        self.assertEqual(classify_native_warnings('load',32|1048576)['status'],'blocked')

    def test_native_warning_policy_unknown_is_not_silently_zero_or_blocked(self):
        from fusion_pipeline.solidworks_helper import classify_native_warnings
        for kind,code in [('save',1<<20),('save',4),('load',4),('load',None),('load',True),('other',0)]:
            self.assertEqual(classify_native_warnings(kind,code)['status'],'unknown')
        evidence=classify_native_warnings('save',(1<<20)|1)
        self.assertEqual(evidence['unknown_bits'],1<<20)
        self.assertEqual(evidence['flags'][0]['name'],'swFileSaveWarning_RebuildError')

    def test_configuration_activation_accepts_verified_already_active_noop(self):
        from fusion_pipeline.solidworks_helper import activate_configuration
        active=types.SimpleNamespace(Name='Desired')
        doc=types.SimpleNamespace(ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=active),
                                  ShowConfiguration2=lambda name:False)
        evidence=activate_configuration(doc,'Desired')
        self.assertEqual(evidence['status'],'passed')
        self.assertEqual(evidence['before'],'Desired')
        self.assertFalse(evidence['native_return'])
        self.assertEqual(evidence['after'],'Desired')

    def test_configuration_activation_rejects_failed_switch_or_wrong_readback(self):
        from fusion_pipeline.solidworks_helper import activate_configuration
        for before, native_return, after in [('Other',False,'Desired'),('Desired',True,'Other'),('Desired',None,'Desired')]:
            active=types.SimpleNamespace(Name=before)
            def activate(name):
                active.Name=after
                return native_return
            doc=types.SimpleNamespace(ConfigurationManager=types.SimpleNamespace(ActiveConfiguration=active),ShowConfiguration2=activate)
            self.assertEqual(activate_configuration(doc,'Desired')['status'],'failed')

    def test_failed_ownership_record_stops_worker_before_request(self):
        from fusion_pipeline.solidworks_helper import record_worker_before_dispatch
        process = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with patch('fusion_pipeline.solidworks_helper.process_record', side_effect=OSError('identity unavailable')):
                with self.assertRaises(ValueError):
                    record_worker_before_dispatch(process, 'unused-record')
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_maintenance_paths_reject_parent_alias_and_hardlink(self):
        from fusion_pipeline.solidworks_helper import plain_path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root/'original'
            original.mkdir()
            alias = root/'alias'
            os.symlink(original, alias, target_is_directory=True)
            with self.assertRaises(ValueError):
                plain_path(alias/'new-output', kind='new', allow_missing=True)
            first, second = original/'first', original/'second'
            first.write_bytes(b'bytes')
            os.link(first, second)
            with self.assertRaises(ValueError):
                plain_path(first)
            self.assertEqual(plain_path(root/'new-output', kind='new', allow_missing=True), root/'new-output')

    def test_current_version_template_fallback_is_unique_and_hashed(self):
        from fusion_pipeline.solidworks_helper import select_part_template
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            templates = root/'SOLIDWORKS'/'SOLIDWORKS 2026'/'templates'
            templates.mkdir(parents=True)
            part = templates/'gb_part.prtdot'
            part.write_bytes(b'public installation template')
            selected = select_part_template(None, {'version':'34.4.1'}, program_data=root)
            self.assertEqual(selected['path'], str(part.absolute()))
            self.assertEqual(selected['source'], 'verified_instance_version_standard_directory')
            self.assertEqual(len(selected['sha256']), 64)
            (templates/'second.prtdot').write_bytes(b'other template')
            with self.assertRaises(ValueError):
                select_part_template(None, {'version':'34.4.1'}, program_data=root)

    def test_template_fallback_does_not_use_other_version(self):
        from fusion_pipeline.solidworks_helper import select_part_template
        with tempfile.TemporaryDirectory() as folder:
            templates = Path(folder)/'SOLIDWORKS'/'SOLIDWORKS 2025'/'templates'
            templates.mkdir(parents=True)
            (templates/'gb_part.prtdot').write_bytes(b'template')
            with self.assertRaises(ValueError):
                select_part_template(None, {'version':'34.4.1'}, program_data=folder)

    def test_bound_worker_never_launches_replacement_cad(self):
        from fusion_pipeline.solidworks_helper import _install_instance_guard
        class Automation:
            def _try_connect_com(self):
                return False
            def connect(self, launch_if_needed=True):
                return {'launch_would_be_attempted':launch_if_needed}
        vendor=types.ModuleType('sim_plugin_solidworks._vendor.solidworks_mcp.automation.base')
        vendor.SolidWorksAutomation=Automation
        with patch.dict(sys.modules, {'sim_plugin_solidworks._vendor.solidworks_mcp.automation.base':vendor}):
            _install_instance_guard({'pid':7}, allow_launch=False)
            self.assertFalse(Automation().connect()['launch_would_be_attempted'])
    def test_native_postcondition_is_read_on_owning_com_executor(self):
        from fusion_pipeline.solidworks_helper import read_worker_instance
        instance = {'pid': 7, 'session_id': 1, 'version': '34.4.1', 'executable': 'C:/SW'}
        with ThreadPoolExecutor(max_workers=1) as executor:
            owner = executor.submit(threading.get_ident).result()
            def read_instance(app):
                if threading.get_ident() != owner:
                    raise RuntimeError('COM apartment mismatch')
                return instance
            vendor = types.ModuleType('sim_plugin_solidworks.tools')
            vendor._COM_EXECUTOR = executor
            vendor._sw_automation = lambda: types.SimpleNamespace(_sw_app=object())
            with patch.dict(sys.modules, {'sim_plugin_solidworks.tools':vendor}), patch(
                    'fusion_pipeline.solidworks_helper._instance', side_effect=read_instance):
                self.assertEqual(read_worker_instance(None), instance)
    @unittest.skipUnless(os.name == 'nt', 'Windows owned process identity')
    def test_cleanup_rejects_reused_or_mismatched_process_identity(self):
        from fusion_pipeline.solidworks_helper import process_record, terminate_owned_worker
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
        try:
            record = process_record(process.pid)
            bad = dict(record, created=record['created']+1)
            result = terminate_owned_worker(bad)
            self.assertFalse(result['terminated'])
            self.assertTrue(result.get('already_exited'))
            self.assertTrue(result.get('pid_reused'))
            self.assertIsNone(process.poll())
            result = terminate_owned_worker(record)
            self.assertTrue(result['terminated'])
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    def test_instance_drift_rejected(self):
        before = {'pid': 4, 'session_id': 1, 'version': '34.4', 'executable': 'C:/SW.exe'}
        with self.assertRaises(ValueError):
            verify_instance(before, dict(before, pid=5))
        self.assertEqual(verify_instance(before, dict(before)), before)

    def test_missing_native_instance_evidence_rejected(self):
        with self.assertRaises(ValueError):
            verify_instance(None, {'pid': 4, 'version': '34.4'})

    def test_unknown_body_health_and_feature_error_rejected(self):
        good = {'complete': True, 'features': [{'name': 'Boss', 'error_code': 0, 'warning': False}],
                'solid_body_count': 1, 'sheet_body_count': 0, 'volume_mm3': 1000,
                'bodies': [{'valid': True, 'fault_count': 0}]}
        self.assertTrue(validate_health(good))
        for value in (dict(good, complete=False), dict(good, volume_mm3=0),
                      dict(good, bodies=[{'valid': None, 'fault_count': None}]),
                      dict(good, features=[{'name': 'Boss', 'error_code': 1, 'warning': False}])):
            with self.assertRaises(ValueError):
                validate_health(value)

    def test_external_or_incomplete_references_rejected(self):
        self.assertTrue(validate_references({'complete': True, 'external': [], 'imported_features': []}))
        for value in ({'complete': False, 'external': []},
                      {'complete': True, 'external': ['outside.sldprt'], 'imported_features': []},
                      {'complete': True, 'external': [], 'imported_features': ['3DInterconnect']}):
            with self.assertRaises(ValueError):
                validate_references(value)


@unittest.skipUnless(os.environ.get('RUN_GEOMETRY_TESTS') == '1', 'explicit geometry runtime checks')
class IndependentStepTests(unittest.TestCase):
    def test_native_geometry_and_declared_units_must_agree(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.Interface import Interface_Static
        from fusion_pipeline.solidworks import verify_step_geometry
        native={'solid_body_count':1,'volume_mm3':6000.,'bbox_mm':[0.,0.,0.,10.,20.,30.]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'box.step'
            Interface_Static.SetCVal_s('write.step.schema','AP214IS')
            writer=STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs)
            writer.Write(str(path))
            result=verify_step_geometry(path,native)
            self.assertEqual(result['status'],'passed')
            text=path.read_text()
            changed=text.replace('.MILLI.,.METRE.', '$,.METRE.')
            self.assertNotEqual(changed,text)
            path.write_text(changed)
            with self.assertRaises(ValueError):
                verify_step_geometry(path,native)

    def test_wrong_application_protocol_is_not_certified_as_ap214(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.Interface import Interface_Static
        from fusion_pipeline.solidworks import verify_step_geometry
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'box.step'
            Interface_Static.SetCVal_s('write.step.schema','AP203')
            writer=STEPControl_Writer()
            writer.Transfer(BRepPrimAPI_MakeBox(10,20,30).Shape(),STEPControl_AsIs)
            writer.Write(str(path))
            self.assertIn('CONFIG_CONTROL_DESIGN',path.read_text())
            with self.assertRaises(ValueError):
                verify_step_geometry(path,{'solid_body_count':1,'volume_mm3':6000.,'bbox_mm':[0.,0.,0.,10.,20.,30.]})
