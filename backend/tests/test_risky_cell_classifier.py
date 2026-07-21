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
