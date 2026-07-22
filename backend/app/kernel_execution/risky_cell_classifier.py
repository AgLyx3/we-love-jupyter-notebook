from __future__ import annotations

import ast
import re

from .models import RiskClassification


class RiskyCellClassifier:
    """Conservative static classifier; it is a prompt guard, not a sandbox."""

    _TEXT_RULES = (
        (re.compile(r"(?m)^\s*!"), "shell_escape", "Runs a shell command"),
        (re.compile(r"(?im)^\s*%%?(?:sh|bash|script)\b"), "shell_magic", "Runs a shell command"),
        (re.compile(r"(?im)^\s*%(?:pip|conda|env|set_env)\b|\b(?:pip|conda)\s+install\b|\buv\s+pip\s+install\b"), "package_change", "Changes packages or the environment"),
        (re.compile(r"(?i)\b(?:INSERT\s+INTO|UPDATE\s+[\w.]+\s+SET|DELETE\s+FROM|DROP\s+(?:TABLE|DATABASE|SCHEMA)|ALTER\s+TABLE|TRUNCATE\s+(?:TABLE\s+)?|CREATE\s+(?:TABLE|DATABASE|SCHEMA)|MERGE\s+INTO)\b"), "database_write", "May modify a database"),
        (re.compile(r"(?i)(?:\.env\b|dotenv\b|\.pem\b|\.key\b|\.ssh\b|credentials?|\.aws/|\.config/gcloud)"), "credential_access", "Accesses secrets or credentials"),
        (re.compile(r"(?i)\b(?:rm|mv|cp)\s+"), "shell_file_change", "May write, move, or delete files"),
        (re.compile(r"(?i)\b(?:restart_kernel|shutdown_kernel|do_shutdown)\b"), "kernel_control", "Controls the notebook kernel"),
    )
    _CALL_RULES = {
        "subprocess.run": ("process_execution", "Starts another process"),
        "subprocess.Popen": ("process_execution", "Starts another process"),
        "subprocess.call": ("process_execution", "Starts another process"),
        "os.system": ("process_execution", "Starts another process"),
        "os.popen": ("process_execution", "Starts another process"),
        "os.remove": ("file_delete", "Deletes a file"),
        "os.unlink": ("file_delete", "Deletes a file"),
        "shutil.rmtree": ("file_delete", "Deletes files or directories"),
        "shutil.copy": ("file_write", "Copies a file"),
        "shutil.copy2": ("file_write", "Copies a file"),
        "shutil.move": ("file_write", "Moves a file"),
        "Path.write_text": ("file_write", "Writes a file"),
        "Path.write_bytes": ("file_write", "Writes a file"),
        "pathlib.Path.unlink": ("file_delete", "Deletes a file"),
        "requests.get": ("network_client", "Makes a network request"),
        "requests.post": ("network_client", "Makes a network request"),
        "httpx.get": ("network_client", "Makes a network request"),
        "httpx.post": ("network_client", "Makes a network request"),
        "urllib.request.urlopen": ("network_client", "Makes a network request"),
        "aiohttp.ClientSession": ("network_client", "Creates a network client"),
        "socket.socket": ("network_client", "Creates a network connection"),
        "requests.Session": ("network_client", "Creates a network client"),
        "httpx.Client": ("network_client", "Creates a network client"),
        "httpx.AsyncClient": ("network_client", "Creates a network client"),
    }
    for _client in ("requests", "httpx"):
        for _method in ("get", "post", "put", "patch", "delete", "head", "options", "request"):
            _CALL_RULES[f"{_client}.{_method}"] = (
                "network_client", "Makes a network request",
            )

    def classify(self, source: str) -> RiskClassification:
        matches: list[tuple[str, str]] = []
        for pattern, name, reason in self._TEXT_RULES:
            if pattern.search(source):
                matches.append((name, reason))
        try:
            tree = ast.parse(source)
        except SyntaxError:
            self._fallback(source, matches)
        else:
            aliases = self._import_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = self._name(node.func, aliases)
                    rule = self._CALL_RULES.get(name)
                    if rule:
                        matches.append(rule)
                    if name == "open" and self._open_writes(node):
                        matches.append(("file_write", "Writes a file"))
                    if name.endswith((".write_text", ".write_bytes", ".mkdir", ".touch", ".rename", ".replace")):
                        matches.append(("file_write", "Writes a file"))
                    if name.endswith((".unlink", ".rmdir")):
                        matches.append(("file_delete", "Deletes a file"))
                    if name.startswith("os.exec"):
                        matches.append(("process_execution", "Replaces or starts a process"))
                    if name in {"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}:
                        matches.append(("process_execution", "Starts another process"))
                    if (
                        name.startswith(("requests.", "httpx.", "urllib.", "aiohttp."))
                        and name.rsplit(".", 1)[-1]
                        in {"get", "post", "put", "patch", "delete", "head", "options", "request", "urlopen"}
                    ):
                        matches.append(("network_client", "Makes a network request"))
                    if self._looks_like_cloud_client(name):
                        matches.append(("cloud_client", "Creates a cloud or network client"))
                elif isinstance(node, ast.Attribute):
                    name = self._name(node, aliases)
                    if name.startswith("os.environ") or name in {"os.getenv", "os.putenv"}:
                        matches.append(("environment_secret", "Reads environment variables that may contain secrets"))
        unique = list(dict.fromkeys(matches))
        return RiskClassification(
            level="confirm" if unique else "safe",
            matched_patterns=tuple(item[0] for item in unique),
            reasons=tuple(dict.fromkeys(item[1] for item in unique)),
        )

    @staticmethod
    def _name(node: ast.AST, aliases: dict[str, str] | None = None) -> str:
        if isinstance(node, ast.Name):
            return (aliases or {}).get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            left = RiskyCellClassifier._name(node.value, aliases)
            return f"{left}.{node.attr}" if left else node.attr
        if isinstance(node, ast.Call):
            return RiskyCellClassifier._name(node.func, aliases)
        return ""

    @staticmethod
    def _import_aliases(tree: ast.Module) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    bound = item.asname or item.name.split(".", 1)[0]
                    aliases[bound] = item.name if item.asname else bound
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    if item.name != "*":
                        aliases[item.asname or item.name] = f"{node.module}.{item.name}"
        return aliases

    @staticmethod
    def _open_writes(node: ast.Call) -> bool:
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for keyword in node.keywords:
            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        return isinstance(mode, str) and any(flag in mode for flag in "wax+")

    @staticmethod
    def _looks_like_cloud_client(name: str) -> bool:
        return any(name.startswith(prefix) for prefix in ("boto3.client", "boto3.resource", "google.cloud", "azure."))

    @staticmethod
    def _fallback(source: str, matches: list[tuple[str, str]]) -> None:
        patterns = (
            (r"\b(?:subprocess\.|os\.system\s*\()", "process_execution", "Starts another process"),
            (r"\b(?:requests|httpx|urllib\.request|aiohttp|socket)\.", "network_client", "Makes a network request"),
            (r"\bopen\s*\([^\n]*(?:['\"](?:w|a|x)|mode\s*=)", "file_write", "Writes a file"),
            (r"\bos\.environ\b", "environment_secret", "Reads environment variables that may contain secrets"),
        )
        for pattern, name, reason in patterns:
            if re.search(pattern, source, re.I):
                matches.append((name, reason))
