import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_WRAPPER = REPO_ROOT / "tools" / "python.sh"


class PythonRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.temp_dir = Path(self._temporary_directory.name)
        self.bin_dir = self.temp_dir / "bin"
        self.bin_dir.mkdir()
        self.repo_dir = self.temp_dir / "repository"
        tools_dir = self.repo_dir / "tools"
        tools_dir.mkdir(parents=True)
        self.python_wrapper = tools_dir / "python.sh"
        shutil.copyfile(PYTHON_WRAPPER, self.python_wrapper)

    def make_fake_python(self, name, version="3.10.0"):
        executable = self.bin_dir / name
        executable.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                case "${{2-}}" in
                    *sys.version_info*)
                        printf '%s\\n' '{version}'
                        case '{version}' in
                            0.*|1.*|2.*|3.[0-9].*) exit 1 ;;
                            *) exit 0 ;;
                        esac
                        ;;
                esac
                exec "{os.fsdecode(os.fsencode(sys.executable))}" "$@"
                """
            ),
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable

    def run_wrapper(self, *arguments, env=None, cwd=None):
        runtime_env = os.environ.copy()
        runtime_env.pop("STOCK_TREND_PYTHON", None)
        runtime_env["PATH"] = f"{self.bin_dir}{os.pathsep}{runtime_env['PATH']}"
        if env:
            runtime_env.update(env)
        return subprocess.run(
            ["bash", str(self.python_wrapper), *arguments],
            cwd=cwd or self.temp_dir,
            env=runtime_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_explicit_interpreter_has_priority_and_preserves_stdout(self):
        explicit = self.make_fake_python("explicit-python", "3.12.4")
        self.make_fake_python("python3", "3.11.9")

        result = self.run_wrapper(
            "-c",
            "import sys; print(sys.argv[1])",
            "value with spaces",
            env={"STOCK_TREND_PYTHON": str(explicit)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "value with spaces\n")
        self.assertIn(f"using Python interpreter: {explicit}", result.stderr)
        self.assertIn("Python version: 3.12.4", result.stderr)

    def test_explicit_command_name_is_resolved_from_path(self):
        explicit = self.make_fake_python("chosen-python", "3.10.8")

        result = self.run_wrapper(
            "-c",
            "print('ok')",
            env={"STOCK_TREND_PYTHON": explicit.name},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")
        self.assertIn(str(explicit), result.stderr)

    def test_empty_explicit_interpreter_fails_without_fallback(self):
        self.make_fake_python("python3", "3.12.0")

        result = self.run_wrapper(env={"STOCK_TREND_PYTHON": ""})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("STOCK_TREND_PYTHON is set but empty", result.stderr)

    def test_invalid_explicit_interpreter_fails_without_fallback(self):
        self.make_fake_python("python3", "3.12.0")

        result = self.run_wrapper(
            env={"STOCK_TREND_PYTHON": "missing-stock-trend-python"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an executable path or command", result.stderr)

    def test_python_older_than_310_is_rejected(self):
        old_python = self.make_fake_python("old-python", "3.9.18")

        result = self.run_wrapper(env={"STOCK_TREND_PYTHON": str(old_python)})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Python 3.10 or newer is required", result.stderr)
        self.assertIn("version: 3.9.18", result.stderr)

    def test_broken_repository_venv_fails_without_system_fallback(self):
        venv_dir = self.repo_dir / ".venv"
        venv_dir.mkdir()
        self.make_fake_python("python3", "3.12.0")

        result = self.run_wrapper()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("virtual environment exists", result.stderr)

    def test_repository_venv_has_priority_over_system_python(self):
        system_python = self.make_fake_python("python3", "3.11.7")
        venv_python = self.repo_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_source = self.make_fake_python("venv-source", "3.12.2")
        shutil.copyfile(venv_source, venv_python)
        venv_python.chmod(venv_python.stat().st_mode | stat.S_IXUSR)

        result = self.run_wrapper("-c", "print('venv')")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "venv\n")
        self.assertIn(str(venv_python), result.stderr)
        self.assertNotIn(str(system_python), result.stderr)

    def test_system_python3_is_used_from_an_unrelated_working_directory(self):
        system_python = self.make_fake_python("python3", "3.11.7")
        unrelated_cwd = self.temp_dir / "elsewhere"
        unrelated_cwd.mkdir()

        result = self.run_wrapper(
            "-c", "print('system')", cwd=unrelated_cwd
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "system\n")
        self.assertIn(str(system_python), result.stderr)


if __name__ == "__main__":
    unittest.main()
