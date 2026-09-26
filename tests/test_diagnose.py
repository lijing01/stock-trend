"""Offline environment diagnostics: declaration, cache and secret boundaries."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from importlib.metadata import PackageNotFoundError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "diagnose", ROOT / ".claude/skills/stock-trend/scripts/diagnose.py")
diag = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diag)


class DiagnoseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'pyproject.toml').write_text('''[project]
name = "example"
requires-python = ">=3.10"
dependencies = ["numpy>=2,<3"]
[project.optional-dependencies]
market = ["akshare>=1,<2"]
test = ["tomli>=2; python_version < '3.11'"]
all = ["example[market,test]"]
''')
        self.root_patch = patch.object(diag, 'PROJECT_ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_core_and_optional_missing_are_separate(self):
        with patch('importlib.metadata.version', side_effect=PackageNotFoundError):
            result = diag.run_diagnostic(quick=True)
        self.assertEqual(result['checks']['python_deps']['numpy']['groups'], ['core'])
        self.assertEqual(result['checks']['python_deps']['akshare']['groups'], ['market'])
        self.assertNotIn('example', result['checks']['python_deps'])
        self.assertTrue(any('核心依赖 numpy' in w for w in result['warnings']))
        self.assertTrue(any('可选依赖 akshare' in w for w in result['warnings']))

    def test_incompatible_version_and_python(self):
        with patch('importlib.metadata.version', return_value='4.0'), patch.object(
                diag.sys, 'version_info', (3, 9, 6)):
            checks = diag.check_environment()
        self.assertEqual(checks['python']['status'], 'incompatible')
        self.assertEqual(checks['python_deps']['numpy']['status'], 'incompatible')

    def test_declaration_changes_drive_checks(self):
        with (self.root / 'pyproject.toml').open('a') as f:
            f.write('new = ["new-package>=7"]\n')
        with patch('importlib.metadata.version', return_value='7.0'):
            deps = diag.check_python_deps()
        self.assertIn('new-package', deps)
        self.assertEqual(deps['new-package']['groups'], ['new'])

    def test_broken_contract_is_visible(self):
        (self.root / 'pyproject.toml').write_text('broken [')
        checks = diag.check_environment()
        self.assertEqual(checks['environment_contract']['status'], 'error')

    def test_missing_diagnostic_tools_is_explicit(self):
        original_import = __import__
        def without_packaging(name, *args, **kwargs):
            if name.startswith('packaging'):
                raise ImportError('packaging missing')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=without_packaging):
            checks = diag.check_environment()
        self.assertEqual(checks['environment_contract']['status'], 'error')
        self.assertIn(".[test]", checks['environment_contract']['hint'])

    def test_quick_ignores_cache_and_never_calls_api(self):
        output = self.root / 'out.json'
        with patch.object(diag.sys, 'argv', ['diagnose', '--quick', '-o', str(output)]), \
                patch.object(diag, 'load_cache', side_effect=AssertionError('stale cache')), \
                patch.object(diag, 'check_eastmoney', side_effect=AssertionError('network')), \
                patch.object(diag, 'save_cache', side_effect=AssertionError('cache write')), \
                patch('importlib.metadata.version', return_value='2.0'):
            diag.main()
        data = json.loads(output.read_text())
        self.assertEqual(data['mode'], 'quick')
        self.assertEqual(data['checks']['python_deps']['numpy']['version'], '2.0')

    def test_credentials_never_include_secret_or_prefix(self):
        token = 'fake-secret-for-test-only'
        with patch.dict(diag.os.environ, {'TUSHARE_TOKEN': token}):
            result = diag.check_tushare_token()
        self.assertEqual(result, {'status': 'ok', 'source': 'env'})
        self.assertNotIn(token[:8], json.dumps(result))
        with patch.dict(diag.os.environ, {}, clear=True), \
                patch.object(diag.Path, 'home', return_value=self.root), \
                patch.object(diag.Path, 'exists', return_value=True), \
                patch('builtins.open', unittest.mock.mock_open(read_data=json.dumps({'token': token}))):
            result = diag.check_tushare_token()
        self.assertNotIn(token[:8], json.dumps(result))


if __name__ == '__main__':
    unittest.main()
