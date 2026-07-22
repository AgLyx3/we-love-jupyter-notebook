import pytest

from backend.app.kernel_execution.risky_cell_classifier import RiskyCellClassifier


@pytest.mark.parametrize("source,pattern", [
    ("!ls", "shell_escape"),
    ("%pip install pandas", "package_change"),
    ('open("out.txt", "w").write("x")', "file_write"),
    ("subprocess.run(['echo', 'x'])", "process_execution"),
    ("requests.get('https://example.com')", "network_client"),
    ('cursor.execute("DELETE FROM users")', "database_write"),
    ("token = os.environ['TOKEN']", "environment_secret"),
    ("manager.restart_kernel()", "kernel_control"),
    ("Path('out.txt').open('w').write('x')", "file_write"),
    ("eval(\"__import__('os').system('echo x')\")", "dynamic_execution"),
    ("exec(dynamic_source)", "dynamic_execution"),
])
def test_risky_categories_require_confirmation(source, pattern):
    result = RiskyCellClassifier().classify(source)
    assert result.level == "confirm"
    assert pattern in result.matched_patterns


@pytest.mark.parametrize("source", [
    "total = sum(range(10))",
    "import requests",
    "text = open('ordinary.txt').read()",
    "df.update(other)",
    "ax = df.plot()",
])
def test_safe_computation_and_reads(source):
    assert RiskyCellClassifier().classify(source).level == "safe"


def test_invalid_python_falls_back_to_text_patterns():
    result = RiskyCellClassifier().classify("if broken:\n  requests.get('https://x')\n  ???")
    assert result.level == "confirm"
    assert "network_client" in result.matched_patterns


@pytest.mark.parametrize("source,pattern", [
    ("import requests as rq\nrq.delete('https://example.com')", "network_client"),
    ("from httpx import patch as send\nsend('https://example.com')", "network_client"),
    ("from subprocess import run as launch\nlaunch(['echo'])", "process_execution"),
    ("from subprocess import Popen\nPopen(['echo'])", "process_execution"),
    ("import os as operating\noperating.system('echo x')", "process_execution"),
    ("from os import unlink as discard\ndiscard('x')", "file_delete"),
    ("from pathlib import Path as P\nP('x').unlink()", "file_delete"),
    ("import pathlib as paths\npaths.Path('x').rename('y')", "file_write"),
    ("import socket as s\ns.create_connection(('localhost', 80))", "network_client"),
    ("from socket import create_connection as connect\nconnect(('localhost', 80))", "network_client"),
])
def test_import_aliases_and_direct_side_effect_calls(source, pattern):
    result = RiskyCellClassifier().classify(source)
    assert result.level == "confirm"
    assert pattern in result.matched_patterns


@pytest.mark.parametrize("source", [
    "mode = input('mode')\nopen('out.txt', mode)",
    "from pathlib import Path\nmode = choose_mode()\nPath('out.txt').open(mode)",
])
def test_runtime_controlled_open_modes_require_confirmation(source):
    result = RiskyCellClassifier().classify(source)
    assert result.level == "confirm"
    assert "file_write" in result.matched_patterns


@pytest.mark.parametrize("source", [
    "runner = eval\nrunner(source)",
    "import builtins as b\nrunner = b.exec\nrunner(source)",
    "from builtins import eval as evaluate\nevaluate(source)",
    "getattr(__builtins__, 'exec')(source)",
])
def test_dynamic_execution_aliases_and_getattr_require_confirmation(source):
    result = RiskyCellClassifier().classify(source)
    assert result.level == "confirm"
    assert "dynamic_execution" in result.matched_patterns
