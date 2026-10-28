"""Offline tests. No WeChat UI or real model is used."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch, Mock
import zipfile

from local_config import DEFAULTS, ROOT, validate, save_config, read_config, validate_installation
import local_control
from build_release import build, FILES


def fixture():
    return DEFAULTS | {'group': '示例群', 'self_name': '本人', 'members': ['本人', '阿甲'], 'consent': True}


class ConfigurationTests(unittest.TestCase):
    def test_first_launch_is_unconfigured(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(read_config(folder)['group'], '')
            with self.assertRaises(ValueError):
                validate_installation(folder)

    def test_requires_owner_consent(self):
        with self.assertRaises(ValueError):
            validate(fixture() | {'consent': False})

    def test_ambiguous_and_incomplete_identity_is_rejected(self):
        for members in [['本人', '阿甲', '阿甲'], ['阿甲'], ['本人', '阿甲', '阿甲 同学'], ['本人', '']]:
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate(fixture() | {'members': members})

    def test_save_derives_only_local_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            save_config(fixture(), folder)
            config = validate_installation(folder)
            settings = json.loads((Path(folder) / 'bot_settings.json').read_text())
            self.assertEqual(config['model'], '')
            self.assertEqual(settings['execution_mode'], 'local')
            self.assertEqual(settings['reader_backend'], 'selection')
            self.assertEqual(settings['group_tools_mode'], 'off')
            self.assertFalse(settings['auto_collect_stickers'])
            self.assertEqual(settings['service_tier'], 'default')

    def test_scope_change_archives_old_queue_and_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(fixture(), root)
            (root / '.runtime').mkdir()
            (root / '.runtime/messages.sqlite3').write_text('old queue')
            (root / 'group_profile.json').write_text('old identity')
            save_config(fixture() | {'group': '新示例群'}, root)
            self.assertFalse((root / '.runtime').exists())
            self.assertFalse((root / 'group_profile.json').exists())
            self.assertEqual(len(list((root / 'local_data').glob('*/.runtime/messages.sqlite3'))), 1)
            self.assertEqual(validate_installation(root)['group'], '新示例群')

    def test_style_change_keeps_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(fixture(), root)
            (root / '.runtime').mkdir()
            save_config(fixture() | {'style': '沉稳简洁'}, root)
            self.assertTrue((root / '.runtime').exists())

    def test_invalid_save_preserves_previous_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            save_config(fixture(), folder)
            with self.assertRaises(ValueError):
                save_config(fixture() | {'members': []}, folder)
            self.assertEqual(validate_installation(folder)['members'], fixture()['members'])

    def test_tampered_derived_settings_block_start(self):
        with tempfile.TemporaryDirectory() as folder:
            save_config(fixture(), folder)
            (Path(folder) / 'bot_settings.json').write_text('{}')
            with self.assertRaises(ValueError):
                validate_installation(folder)


class BackendTests(unittest.TestCase):
    def test_no_remote_database_or_group_actions(self):
        from reply_bot import load_settings
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(fixture(), root)
            path = root / 'bot_settings.json'
            original = json.loads(path.read_text())
            for update in [{'execution_mode': 'remote'}, {'reader_backend': 'database'}, {'group_tools_mode': 'autonomous'}]:
                path.write_text(json.dumps(original | update))
                with self.subTest(update=update), self.assertRaises(ValueError):
                    load_settings(path)

    def test_default_model_and_completed_response(self):
        import reply_bot
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(fixture(), root)
            payload = '$(Get-Content secret); `cmd`'
            def completed(args, prompt, cwd, **kwargs):
                self.assertNotIn('--model', args)
                self.assertIn('--ignore-user-config', args)
                self.assertIn('--ignore-rules', args)
                self.assertIn('read-only', args)
                self.assertIn(payload, prompt)
                self.assertNotIn(payload, args)
                for feature in ['shell_tool', 'unified_exec', 'apps', 'plugins', 'computer_use']:
                    at = args.index(feature)
                    self.assertEqual(args[at - 1], '--disable')
                kwargs['on_reaped']()
                return json.dumps({'messages': [{'kind': 'text', 'text': '你好', 'sticker_id': ''}],
                                   'actions': [], 'sticker_description': '', 'sticker_tags': []}), {'seconds': 0.1}
            with patch.object(reply_bot, 'ROOT', root), patch.object(reply_bot, 'resolve_codex_executable', return_value='codex.exe'), patch.object(reply_bot, 'run_completed', side_effect=completed):
                backend = reply_bot.CodexBackend(root / 'persona.json', root / 'bot_settings.json')
                self.assertEqual(backend.reply(payload), '你好')

    def test_completed_event_required_and_failed_turn_rejected(self):
        from codex_runner import CompletedReply
        parser = CompletedReply()
        self.assertIsNone(parser.accept({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'draft'}}))
        with self.assertRaises(RuntimeError):
            parser.accept({'type': 'turn.failed'})
        with self.assertRaises(RuntimeError):
            CompletedReply().accept({'type': 'turn.completed'})


class LifecycleTests(unittest.TestCase):
    def test_pid_reuse_does_not_look_alive(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(local_control, 'RUN', Path(folder)):
            (Path(folder) / 'launch.json').write_text(json.dumps({'pid': 123, 'stamp': 100}))
            with patch.object(local_control, 'process_stamp', return_value=101):
                self.assertFalse(local_control.status()['process_running'])
            with patch.object(local_control, 'process_stamp', return_value=100):
                self.assertTrue(local_control.status()['process_running'])
                self.assertEqual(local_control.status()['state'], 'starting')

    def test_process_identity_reads_current_process(self):
        if os.name != 'nt':
            self.skipTest('Windows only')
        self.assertIsInstance(local_control.process_stamp(os.getpid()), int)

    def test_running_configuration_cannot_change(self):
        from contextlib import nullcontext
        with patch.object(local_control, 'lifecycle_lock', return_value=nullcontext()), patch.object(local_control, 'status', return_value={'process_running': True}):
            with self.assertRaises(RuntimeError):
                local_control.save(fixture())

    def test_local_click_never_targets_foreign_window(self):
        from local_driver import LocalDriver
        gui, auto = Mock(), Mock()
        driver = LocalDriver(gui, auto)
        driver._win = Mock(NativeWindowHandle=10)
        gui.GetForegroundWindow.return_value = 11
        with self.assertRaises(RuntimeError):
            driver._click_at(100, 100)
        auto.Click.assert_not_called()
        gui.GetForegroundWindow.return_value = 10
        gui.GetAncestor.return_value = 11
        gui.GetWindow.return_value = 0
        with self.assertRaises(RuntimeError):
            driver._click_at(100, 100)
        auto.Click.assert_not_called()

    def test_owned_popup_can_be_clicked(self):
        from local_driver import LocalDriver
        gui, auto = Mock(), Mock()
        driver = LocalDriver(gui, auto)
        driver._win = Mock(NativeWindowHandle=10)
        gui.GetForegroundWindow.return_value = 10
        gui.GetAncestor.return_value = 11
        gui.GetWindow.return_value = 10
        self.assertTrue(driver._click_at(100, 100))
        auto.Click.assert_called_once_with(100, 100)

    def test_start_status_stop_with_an_isolated_dummy_worker(self):
        import subprocess
        import time
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            worker = """import json, os, time
from pathlib import Path
run = Path(__file__).resolve().parent / '.runtime'
(run / 'bot_status.json').write_text(json.dumps({'state':'listening','pid':os.getpid()}))
while not (run / 'STOP').exists():
    time.sleep(0.1)
"""
            (root / 'wechat_bot.py').write_text(worker, encoding='utf-8')
            with patch.object(local_control, 'ROOT', root), patch.object(local_control, 'RUN', root / '.runtime'), patch('local_config.validate_installation', return_value=fixture()), patch('local_codex.check_codex', return_value={}), patch.object(local_control.subprocess, 'Popen', side_effect=spawn):
                try:
                    self.assertTrue(local_control.start()['process_running'])
                    self.assertEqual(local_control.start()['state'], 'listening')
                    self.assertEqual(len(children), 1)
                    local_control.stop()
                    children[0].wait(timeout=5)
                    self.assertFalse(local_control.status()['process_running'])
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.terminate()
                        child.wait(timeout=5)


class PackagingTests(unittest.TestCase):
    def test_clean_export_ignores_installed_personal_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in FILES:
                shutil.copy2(ROOT / name, root / name)
            for name in ['config.json', 'sender_settings.json', 'auth.json', '.runtime/messages.sqlite3', 'local_data/old.txt', 'remote/key']:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('PRIVATE-SENTINEL-DO-NOT-SHARE')
            path = build(root)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(len(archive.namelist()), len(FILES) + 1)
                prefix = archive.namelist()[0].split('/')[0] + '/'
                manifest = json.loads(archive.read(prefix + 'MANIFEST.json'))
                for name, digest in manifest['sha256'].items():
                    content = archive.read(prefix + name)
                    self.assertEqual(hashlib.sha256(content).hexdigest(), digest)
                    # A test's literal is code, so check installed data filenames separately.
                self.assertNotIn(prefix + 'config.json', archive.namelist())
                self.assertFalse(any('/remote/' in n or '/.runtime/' in n for n in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
