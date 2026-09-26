"""Offline pre-commit checks against an isolated copy of the Git index."""
import ast
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from urllib.parse import unquote

SKILL = '.claude/skills/stock-trend/'


def git(*args, input=None):
    return subprocess.check_output(['git', *args], input=input)


def required(path):
    if not path.is_file():
        raise ValueError(f'必检文件缺失: {path}')
    return path


def check_skill(root):
    import yaml
    skill = root / SKILL
    entry = required(skill / 'SKILL.md')
    text = entry.read_text()
    parts = text.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError('SKILL.md 缺少 YAML frontmatter')
    metadata = yaml.safe_load(parts[1])
    if not isinstance(metadata, dict) or any(
        not isinstance(metadata.get(key), str) or not metadata[key].strip()
        for key in ('name', 'description')
    ):
        raise ValueError('SKILL.md 必须包含非空 name 和 description')
    for doc in [entry, *sorted((skill / 'references').rglob('*.md'))]:
        content = doc.read_text()
        for match in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', content):
            target = match.group(1).strip().split(' "', 1)[0]
            if target.startswith('<') and target.endswith('>'):
                target = target[1:-1]
                if not target.startswith(('references/', 'assets/', './', '../')):
                    continue  # Documentation placeholders, not concrete file links.
            if re.match(r'^[a-zA-Z][\w+.-]*:', target) or target.startswith(('#', '/')):
                continue
            target = unquote(target.split('#', 1)[0])
            if target:
                resolved = (doc.parent / target).resolve()
                if not resolved.is_relative_to(root) or not resolved.exists():
                    raise ValueError(f'{doc.relative_to(root)} 引用不存在: {target}')
        for ref in re.findall(r'(?:\.claude/skills/stock-trend/)?scripts/[\w./-]+\.py', content):
            required(root / (ref if ref.startswith('.claude/') else SKILL + ref))
    print('✓ Skill 元数据和本地引用')


def check_modules(root):
    sys.path.insert(0, str(root / SKILL / 'scripts'))
    modules = {}
    for module, symbol in (
        ('analysis.technical', 'calc_support_resistance'),
        ('analysis.scores', 'redistribute_weights'),
        ('analysis.scores', 'validate_input'),
        ('reporting.report', 'render_template'),
        ('reporting.report', 'build_context'),
    ):
        required(root / SKILL / 'scripts' / (module.replace('.', '/') + '.py'))
        loaded = importlib.import_module(module)
        if not callable(getattr(loaded, symbol, None)):
            raise ValueError(f'必检接口缺失: {module}.{symbol}')
        modules[module] = loaded
    scores = modules['analysis.scores']
    valid = {'summary': {'total_score': 5.2, 'direction': 'bullish',
                         'confidence': 'medium', 'data_quality': 'good'}}
    if scores.validate_input(valid, {}) or not scores.validate_input({'summary': {}}, {}):
        raise ValueError('analysis.scores.validate_input 合法/非法输入校验失败')
    print('✓ 当前模块导入和输入校验')
    report = modules['reporting.report']
    required(root / SKILL / 'assets/report-template.md')
    required(root / SKILL / 'assets/report-template.html')
    if report.render_template('{{probe}}', {'probe': 'hook-smoke'}) != 'hook-smoke':
        raise ValueError('reporting.report.render_template 变量渲染校验失败')
    source = ast.parse(Path(report.__file__).read_text())
    names = {node.attr for node in ast.walk(source)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
             and node.value.id == 'args'}
    args = SimpleNamespace(**dict.fromkeys(names))
    args.horizon = '日线'
    context = report.build_context(args)
    for path in sorted((root / SKILL / 'assets').glob('report-template.*')):
        template = path.read_text()
        validate_template(template, context, path.name)
        rendered = report.render_template(template, context)
        if re.search(r'\{\{.*?\}\}', rendered):
            raise ValueError(f'{path.name} 渲染后仍有未处理变量')
    print('✓ 报告模板真实渲染')


def validate_template(template, context, label):
    """Validate active branches using the renderer's list-item scope rules."""
    sections = re.compile(r'\{\{([#^])(\w+)\}\}(.*?)\{\{/\2\}\}', re.DOTALL)
    def section(match):
        mode, key, body = match.groups()
        value = context.get(key)
        if mode == '^':
            if not value:
                validate_template(body, context, label)
        elif value:
            values = value if isinstance(value, list) else [context]
            for item in values:
                scope = item if isinstance(item, dict) else {key + '_item': item}
                validate_template(body, scope, label)
        return ''
    remainder = sections.sub(section, template)
    missing = set(re.findall(r'\{\{(\w+)\}\}', remainder)) - context.keys()
    if missing:
        raise ValueError(f'{label} context 缺少变量: {sorted(missing)}')


def worker(root, changed):
    os.chdir(root)
    import socket
    def offline(*args, **kwargs):
        raise RuntimeError('pre-commit 离线检查禁止网络访问')
    socket.socket.connect = offline
    socket.create_connection = offline
    for name in changed:
        path = root / name
        if name.endswith('.py') and path.is_file():
            compile(path.read_bytes(), name, 'exec')
    print('✓ 暂存 Python 语法')
    check_skill(root)
    integration = any(name.startswith((SKILL + 'scripts/', SKILL + 'tests/',
                                      SKILL + 'assets/', 'requirements/')) or
                      name in ('pyproject.toml', 'tools/python.sh', 'tools/check_staged.py',
                               '.githooks/pre-commit') for name in changed)
    if integration:
        check_modules(root)
        golden = required(root / SKILL / 'tests/test_golden.py')
        if os.environ.get('STOCK_TREND_SKIP_GOLDEN') == '1':
            print('⚠ Golden 未验证：STOCK_TREND_SKIP_GOLDEN=1；必须记录外部阻塞原因')
        else:
            import runpy
            sys.argv = [str(golden), '--diff']
            runpy.run_path(str(golden), run_name='__main__')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--snapshot':
        worker(Path.cwd(), sys.argv[2:])
        return 0
    changed = [os.fsdecode(name) for name in git(
        'diff', '--cached', '--name-only', '-z', '--diff-filter=ACMRD').split(b'\0') if name]
    relevant = [name for name in changed if name.startswith(
        (SKILL, 'tools/', 'requirements/', '.githooks/')) or name == 'pyproject.toml']
    if not relevant:
        print('✓ 无相关暂存变更')
        return 0
    with tempfile.TemporaryDirectory(prefix='stock-trend-index-') as temp:
        root = Path(temp).resolve()
        paths = [path for path in git('ls-files', '-z').split(b'\0') if path and (
            path.startswith((SKILL.encode(), b'tools/', b'requirements/', b'.githooks/'))
            or path == b'pyproject.toml')]
        if paths:
            git('checkout-index', '--prefix=' + str(root) + '/', '-z', '--stdin',
                input=b'\0'.join(paths) + b'\0')
        for path in root.rglob('*'):
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                raise ValueError(f'暂存树引用外部路径: {path.relative_to(root)}')
        checker = required(root / 'tools/check_staged.py')
        env = os.environ.copy()
        for key in list(env):
            if key.startswith('GIT_') or key in ('PYTHONPATH', 'PYTHONHOME'):
                env.pop(key)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run([sys.executable, str(checker), '--snapshot', *relevant],
                              cwd=root, env=env).returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, SyntaxError, ImportError, subprocess.CalledProcessError) as error:
        print(f'✗ pre-commit 检查失败: {error}', file=sys.stderr)
        sys.exit(1)
