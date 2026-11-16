"""Tests for local configuration discovery and its cross-process cache."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import local_metadata as metadata
import stats


class TestLocalMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, 'config')
        self.project = os.path.join(self.tmp.name, 'project')
        os.makedirs(self.config)
        os.makedirs(self.project)
        patcher = mock.patch.object(stats, '_CONFIG_DIR', self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.input = {'workspace': {'current_dir': self.project}}

    def write(self, relative, value, root=None, encoding='utf-8'):
        path = os.path.join(root or self.config, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding=encoding) as stream:
            json.dump(value, stream)
        return path

    def skill(self, relative, root=None):
        return self.write(relative + '/SKILL.md', {}, root)

    def load(self):
        return metadata.load_local_metadata(self.input)

    def cache_path(self):
        folder = os.path.join(self.config, 'plugins', 'data', 'statusline', 'metadata')
        return os.path.join(folder, os.listdir(folder)[0])

    def test_missing_config(self):
        result = self.load()
        self.assertEqual(result['mcp_count'], 0)
        self.assertEqual(result['skill_count'], 0)
        self.assertEqual(result['cwd'], self.project)

    def test_bom_settings_and_labels(self):
        self.write('settings.json', {'reasoningEffort': 'high',
                   'permissions': {'defaultMode': 'plan'}}, encoding='utf-8-sig')
        result = self.load()
        self.assertEqual(result['reasoning_effort'], 'high')
        self.assertEqual(result['permission_mode'], 'plan')

    def test_null_fields(self):
        self.write('settings.json', {'permissions': None, 'enabledPlugins': None})
        self.assertIsNone(self.load()['permission_mode'])

    def test_malformed_settings(self):
        path = self.write('settings.json', {})
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write('{')
        self.assertIsNone(self.load()['skill_count'])

    def test_mcp_sources(self):
        self.write('mcp.json', {'mcpServers': {'same': {}, 'other': {}}})
        self.write('.mcp.json', {'mcpServers': {'same': {}}}, self.project)
        self.write('.codebuddy/mcp.json', {'mcpServers': {'third': {}}}, self.project)
        self.assertEqual(self.load()['mcp_count'], 4)

    def test_duplicate_mcp_paths(self):
        path = self.write('mcp.json', {'mcpServers': {'one': {}}})
        self.assertEqual(metadata._count_mcp([path, path]), 1)

    def test_malformed_mcp(self):
        self.write('mcp.json', {'mcpServers': ['invalid']})
        self.assertIsNone(self.load()['mcp_count'])

    def test_unreadable_json(self):
        with mock.patch('builtins.open', side_effect=PermissionError):
            self.assertIsNone(metadata._read_object('unreadable'))

    def test_skill_sources_and_enabled_plugins(self):
        self.skill('skills/user')
        self.skill('.codebuddy/skills/project', self.project)
        self.skill('.claude/skills/second', self.project)
        self.skill('plugins/cache/publisher/on/skills/a')
        self.skill('plugins/cache/publisher/off/skills/a')
        self.write('settings.json', {'enabledPlugins': {'on@publisher': True, 'off@publisher': False}})
        self.assertEqual(self.load()['skill_count'], 4)

    def test_skill_path_deduplication(self):
        self.skill('skills/a')
        root = os.path.join(self.config, 'skills')
        self.assertEqual(metadata._count_skills([root, root, os.path.join(root, 'a')]), 1)

    def test_linked_skills_counted_without_cycles(self):
        shared = os.path.join(self.tmp.name, 'shared')
        self.skill('one', shared)
        root = os.path.join(self.config, 'skills')
        os.makedirs(root)
        try:
            os.symlink(shared, os.path.join(root, 'linked'), target_is_directory=True)
            os.symlink(root, os.path.join(shared, 'cycle'), target_is_directory=True)
        except OSError as error:
            self.skipTest('Directory symlinks unavailable: ' + str(error))
        self.assertEqual(metadata._count_skills([root, shared]), 1)

    def test_plugin_path_traversal_rejected(self):
        roots = metadata._plugin_roots(self.config, {
            '../escape@publisher': True, 'plugin@../../escape': True,
            'C:escape@publisher': True, 'good@publisher': True})
        self.assertEqual(roots, [os.path.join(self.config, 'plugins', 'cache', 'publisher', 'good')])

    def test_unreadable_skill_directory(self):
        with mock.patch.object(metadata.os, 'scandir', side_effect=PermissionError):
            self.assertIsNone(metadata._count_skills([self.project]))

    def test_cache_hit_skips_discovery(self):
        self.load()
        with mock.patch.object(metadata, '_discover_counts', side_effect=AssertionError('scanned')):
            self.assertEqual(self.load()['skill_count'], 0)

    def test_ttl_refresh(self):
        with mock.patch.object(metadata.time, 'time', return_value=100):
            self.load()
        self.skill('skills/new')
        with mock.patch.object(metadata.time, 'time', return_value=161):
            self.assertEqual(self.load()['skill_count'], 1)

    def test_clock_rollback_refresh(self):
        with mock.patch.object(metadata.time, 'time', return_value=100):
            self.load()
        self.skill('skills/new')
        with mock.patch.object(metadata.time, 'time', return_value=99):
            self.assertEqual(self.load()['skill_count'], 1)

    def test_plugin_change_refreshes(self):
        self.load()
        self.skill('plugins/cache/pub/new/skills/a')
        self.write('settings.json', {'enabledPlugins': {'new@pub': True}})
        self.assertEqual(self.load()['skill_count'], 1)

    def test_settings_labels_update_without_scan(self):
        self.load()
        self.write('settings.json', {'reasoningEffort': 'low'})
        with mock.patch.object(metadata, '_discover_counts', side_effect=AssertionError('scanned')):
            self.assertEqual(self.load()['reasoning_effort'], 'low')

    def test_project_cache_isolation(self):
        self.load()
        other = os.path.join(self.tmp.name, 'other')
        self.skill('.codebuddy/skills/a', other)
        self.input = {'workspace': {'current_dir': other}}
        self.assertEqual(self.load()['skill_count'], 1)

    def test_config_cache_isolation(self):
        self.load()
        other = os.path.join(self.tmp.name, 'other-config')
        self.skill('skills/a', other)
        with mock.patch.object(stats, '_CONFIG_DIR', other):
            self.assertEqual(self.load()['skill_count'], 1)

    def test_invalid_cache_refreshed(self):
        self.load()
        with open(self.cache_path(), 'w', encoding='utf-8') as stream:
            stream.write('{')
        self.skill('skills/a')
        self.assertEqual(self.load()['skill_count'], 1)

    def test_invalid_version_refreshed(self):
        self.load()
        with mock.patch.object(metadata, 'CACHE_VERSION', 2):
            with mock.patch.object(metadata, '_discover_counts', return_value={'mcp_count': 5, 'skill_count': 6}):
                self.assertEqual(self.load()['mcp_count'], 5)

    def test_cache_write_failure(self):
        with mock.patch.object(metadata.os, 'replace', side_effect=PermissionError):
            self.assertEqual(self.load()['mcp_count'], 0)
        folder = os.path.join(self.config, 'plugins', 'data', 'statusline', 'metadata')
        self.assertEqual(os.listdir(folder), [])

    def test_failed_scan_is_cached(self):
        with mock.patch.object(metadata, '_count_skills', return_value=None):
            self.assertIsNone(self.load()['skill_count'])
        with mock.patch.object(metadata, '_discover_counts', side_effect=AssertionError('scanned')):
            self.assertIsNone(self.load()['skill_count'])

    def test_cache_contains_only_summary(self):
        self.write('settings.json', {'secret': 'do-not-cache', 'reasoningEffort': 'high'})
        self.load()
        with open(self.cache_path(), encoding='utf-8') as stream:
            cached = json.load(stream)
        self.assertEqual(set(cached), {'v', 'time', 'plugins', 'mcp_count', 'skill_count'})

    def test_cache_reused_in_new_process(self):
        self.load()
        self.skill('skills/created-after-cache')
        code = ('import json,local_metadata; print(json.dumps(local_metadata.load_local_metadata('
                + repr(self.input) + ')))')
        result = subprocess.run([sys.executable, '-c', code],
                                env=dict(os.environ, CODEBUDDY_CONFIG_DIR=self.config),
                                cwd=os.path.dirname(__file__), capture_output=True,
                                text=True, encoding='utf-8', timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['skill_count'], 0)

    def test_project_root_override(self):
        self.skill('.codebuddy/skills/a', self.project)
        self.input = {'workspace': {'current_dir': self.config, 'project_dir': self.project}}
        self.assertEqual(self.load()['skill_count'], 1)
        self.assertEqual(self.load()['cwd'], self.config)


if __name__ == '__main__':
    unittest.main()
