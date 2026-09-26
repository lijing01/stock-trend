import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_COMMIT = REPO_ROOT / ".githooks" / "pre-commit"
INSTALL_HOOKS = REPO_ROOT / ".githooks" / "install-hooks.sh"
PYTHON_WRAPPER = REPO_ROOT / "tools" / "python.sh"
STAGED_CHECKER = REPO_ROOT / "tools" / "check_staged.py"


class PreCommitHookTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.temp_dir = Path(self._temporary_directory.name)
        self.repo_dir = self.temp_dir / "repository with spaces"
        self.repo_dir.mkdir()

        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Hook Fixture")
        self.run_git("config", "user.email", "hook-fixture@example.invalid")

        self.copy_repository_tools()
        self.create_skill_fixture()
        self.run_git("add", ".")
        self.create_baseline_head()

    def copy_repository_tools(self):
        hooks_dir = self.repo_dir / ".githooks"
        tools_dir = self.repo_dir / "tools"
        hooks_dir.mkdir()
        tools_dir.mkdir()
        shutil.copy2(PRE_COMMIT, hooks_dir / "pre-commit")
        shutil.copy2(INSTALL_HOOKS, hooks_dir / "install-hooks.sh")
        shutil.copyfile(PYTHON_WRAPPER, tools_dir / "python.sh")
        if STAGED_CHECKER.exists():
            shutil.copyfile(STAGED_CHECKER, tools_dir / "check_staged.py")

    def create_skill_fixture(self):
        self.write(
            ".claude/skills/stock-trend/SKILL.md",
            """\
            ---
            name: stock-trend
            description: Minimal hook regression fixture.
            ---

            Run `python3 .claude/skills/stock-trend/scripts/analysis/technical.py`.
            """,
        )
        self.write(
            ".claude/skills/stock-trend/references/local-runtime.md",
            "Run `python3 .claude/skills/stock-trend/scripts/analysis/scores.py`.\n",
        )
        self.write(
            ".claude/skills/stock-trend/assets/report-template.md",
            "Result: {{name}}\n",
        )
        self.write(
            ".claude/skills/stock-trend/assets/report-template.html",
            "<p>{{name}}</p>\n",
        )

        for package in ("analysis", "reporting"):
            self.write(
                f".claude/skills/stock-trend/scripts/{package}/__init__.py",
                "",
            )
        self.write(
            ".claude/skills/stock-trend/scripts/analysis/technical.py",
            """\
            def calc_support_resistance(*args, **kwargs):
                return {"support": None, "resistance": None}
            """,
        )
        self.write(
            ".claude/skills/stock-trend/scripts/analysis/scores.py",
            """\
            def redistribute_weights(base_weights, tech_weight_override=None):
                return dict(base_weights)


            def validate_input(technical_data, dimension_scores, data_dir=None):
                if not isinstance(technical_data, dict):
                    return ["technical_data must be a dict"]
                summary = technical_data.get("summary")
                if not isinstance(summary, dict):
                    return ["summary must be a dict"]
                required = ("total_score", "direction", "data_quality")
                return [f"missing {key}" for key in required if key not in summary]
            """,
        )
        self.write(
            ".claude/skills/stock-trend/scripts/reporting/report.py",
            """\
            import re


            def build_context(args):
                return {"name": "fixture"}


            def render_template(template_str, context):
                return re.sub(
                    r"\\{\\{(\\w+)\\}\\}",
                    lambda match: str(context.get(match.group(1), "")),
                    template_str,
                )
            """,
        )
        self.write(
            ".claude/skills/stock-trend/tests/test_golden.py",
            """\
            import sys


            if __name__ == "__main__":
                raise SystemExit(0 if "--diff" in sys.argv else 2)
            """,
        )
        self.write("README.md", "fixture\n")

    def write(self, relative_path, contents):
        path = self.repo_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(contents), encoding="utf-8")
        return path

    def run_git(self, *arguments, env=None):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repo_dir,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def create_baseline_head(self):
        tree = self.run_git("write-tree").stdout.strip()
        commit = self.run_git("commit-tree", tree, "-m", "fixture baseline").stdout.strip()
        self.run_git("update-ref", "HEAD", commit)

    def stage(self, *paths):
        self.run_git("add", "--", *paths)

    def stage_all(self):
        self.run_git("add", "-A")

    def touch_script(self):
        path = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        with (self.repo_dir / path).open("a", encoding="utf-8") as stream:
            stream.write("\n# staged trigger\n")
        self.stage(path)

    def run_hook(self, *, env=None):
        runtime_env = os.environ.copy()
        runtime_env["STOCK_TREND_PYTHON"] = sys.executable
        runtime_env.pop("STOCK_TREND_SKIP_GOLDEN", None)
        if env:
            runtime_env.update(env)
        return subprocess.run(
            ["bash", str(self.repo_dir / ".githooks" / "pre-commit")],
            cwd=self.repo_dir,
            env=runtime_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_hook_passes(self, result):
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def assert_hook_fails(self, result):
        self.assertNotEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_unrelated_staged_file_passes_without_running_skill_checks(self):
        self.write("README.md", "unrelated change\n")
        self.stage("README.md")
        self.write(
            ".claude/skills/stock-trend/scripts/analysis/technical.py",
            "this is invalid working tree syntax\n",
        )

        result = self.run_hook()

        self.assert_hook_passes(result)

    def test_nested_script_syntax_error_fails(self):
        path = ".claude/skills/stock-trend/scripts/core/nested.py"
        self.write(path, "def broken(:\n    pass\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_script_path_with_spaces_is_checked(self):
        path = ".claude/skills/stock-trend/scripts/core/path with spaces.py"
        self.write(path, "def broken(:\n    pass\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_staged_invalid_script_is_not_masked_by_worktree_fix(self):
        path = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        self.write(path, "def broken(:\n    pass\n")
        self.stage(path)
        self.write(path, "def calc_support_resistance(*args, **kwargs):\n    return {}\n")

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_valid_staged_script_is_not_rejected_by_unstaged_error(self):
        path = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        self.write(
            path,
            "def calc_support_resistance(*args, **kwargs):\n    return {'staged': True}\n",
        )
        self.stage(path)
        self.write(path, "def broken(:\n    pass\n")

        result = self.run_hook()

        self.assert_hook_passes(result)

    def test_renamed_nested_script_is_checked(self):
        source = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        destination = ".claude/skills/stock-trend/scripts/analysis/renamed module.py"
        self.run_git("mv", source, destination)
        self.write(destination, "def broken(:\n    pass\n")
        self.stage_all()

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_deleted_required_module_fails(self):
        path = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        self.run_git("rm", "--", path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_missing_required_symbol_fails(self):
        path = ".claude/skills/stock-trend/scripts/analysis/technical.py"
        self.write(path, "def another_function():\n    return None\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_broken_template_renderer_fails_smoke_check(self):
        path = ".claude/skills/stock-trend/scripts/reporting/report.py"
        self.write(path, "def render_template(template_str, context):\n    return template_str\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_unknown_template_variable_fails(self):
        path = ".claude/skills/stock-trend/assets/report-template.md"
        self.write(path, "Result: {{unknown_fixture_value}}\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_missing_html_template_fails(self):
        path = ".claude/skills/stock-trend/assets/report-template.html"
        self.run_git("rm", "--", path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_broken_scores_validation_fails_smoke_check(self):
        path = ".claude/skills/stock-trend/scripts/analysis/scores.py"
        self.write(
            path,
            """\
            def redistribute_weights(base_weights, tech_weight_override=None):
                return dict(base_weights)


            def validate_input(technical_data, dimension_scores, data_dir=None):
                return []
            """,
        )
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_golden_failure_rejects_script_change(self):
        self.write(
            ".claude/skills/stock-trend/tests/test_golden.py",
            "raise SystemExit(7)\n",
        )
        self.stage(".claude/skills/stock-trend/tests/test_golden.py")
        self.touch_script()

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_staged_failing_golden_is_not_masked_by_worktree_fix(self):
        golden = ".claude/skills/stock-trend/tests/test_golden.py"
        self.write(golden, "raise SystemExit(9)\n")
        self.stage(golden)
        self.write(
            golden,
            "import sys\nraise SystemExit(0 if '--diff' in sys.argv else 2)\n",
        )

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_staged_passing_golden_is_not_rejected_by_worktree_failure(self):
        golden = ".claude/skills/stock-trend/tests/test_golden.py"
        self.write(
            golden,
            "import sys\nraise SystemExit(0 if '--diff' in sys.argv else 2)\n",
        )
        self.stage(golden)
        self.write(golden, "raise SystemExit(9)\n")

        result = self.run_hook()

        self.assert_hook_passes(result)

    def test_missing_golden_test_rejects_script_change(self):
        golden = ".claude/skills/stock-trend/tests/test_golden.py"
        self.run_git("rm", "--", golden)
        self.touch_script()

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_explicit_golden_skip_remains_available(self):
        self.write(
            ".claude/skills/stock-trend/tests/test_golden.py",
            "raise SystemExit(7)\n",
        )
        self.stage(".claude/skills/stock-trend/tests/test_golden.py")
        self.touch_script()

        result = self.run_hook(env={"STOCK_TREND_SKIP_GOLDEN": "1"})

        self.assert_hook_passes(result)
        self.assertIn("STOCK_TREND_SKIP_GOLDEN", result.stdout + result.stderr)

    def test_invalid_explicit_interpreter_fails(self):
        self.touch_script()

        result = self.run_hook(
            env={"STOCK_TREND_PYTHON": "missing-stock-trend-python"}
        )

        self.assert_hook_fails(result)

    def test_missing_staged_checker_fails(self):
        checker = "tools/check_staged.py"
        self.assertTrue((self.repo_dir / checker).is_file())
        self.run_git("rm", "--", checker)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_nonexistent_script_reference_fails(self):
        path = ".claude/skills/stock-trend/references/local-runtime.md"
        self.write(
            path,
            "Run `python3 .claude/skills/stock-trend/scripts/missing.py`.\n",
        )
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_missing_skill_metadata_fails(self):
        path = ".claude/skills/stock-trend/SKILL.md"
        self.write(path, "---\nname: stock-trend\n---\n")
        self.stage(path)

        result = self.run_hook()

        self.assert_hook_fails(result)

    def test_installer_sets_repository_relative_hooks_path_from_subdirectory(self):
        nested_directory = self.repo_dir / "nested" / "directory"
        nested_directory.mkdir(parents=True)

        result = subprocess.run(
            ["bash", str(self.repo_dir / ".githooks" / "install-hooks.sh")],
            cwd=nested_directory,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        configured = self.run_git("config", "--local", "--get", "core.hooksPath")
        self.assertEqual(configured.stdout.strip(), ".githooks")

    def test_installer_refuses_to_overwrite_an_unknown_hooks_path(self):
        self.run_git("config", "--local", "core.hooksPath", "custom-hooks")

        result = subprocess.run(
            ["bash", str(self.repo_dir / ".githooks" / "install-hooks.sh")],
            cwd=self.repo_dir,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        configured = self.run_git("config", "--local", "--get", "core.hooksPath")
        self.assertEqual(configured.stdout.strip(), "custom-hooks")


if __name__ == "__main__":
    unittest.main()
