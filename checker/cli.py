import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import typer
import yaml

app = typer.Typer(help="DriftScribe consistency gate")


@dataclass
class CheckResult:
    ok: bool
    failures: List[str] = field(default_factory=list)


def _split_sections(md: str) -> dict[str, str]:
    """Return {section_title: section_body} for `## section` headers."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in md.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf)
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


def _path_safe(p: str) -> bool:
    return not p.startswith("/") and ".." not in Path(p).parts


def _lines_mentioning(body: str, pattern: re.Pattern[str]) -> list[int]:
    return [i for i, line in enumerate(body.splitlines()) if pattern.search(line)]


def _operator_note_near(body: str, var_pattern: re.Pattern[str]) -> bool:
    """True if 'operator note' (case-insensitive) appears in the same line as a
    var mention or within the next 2 lines (continuation block)."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if var_pattern.search(line):
            window = "\n".join(lines[i : i + 3]).lower()
            if "operator note" in window:
                return True
    return False


def check_docs_cover_contract(contract_path: Path, repo_root: Path) -> CheckResult:
    failures: list[str] = []
    try:
        raw = yaml.safe_load(contract_path.read_text())
    except yaml.YAMLError as e:
        failures.append(f"contract {contract_path}: YAML parse error: {e}")
        return CheckResult(ok=False, failures=failures)
    except FileNotFoundError:
        failures.append(f"contract {contract_path}: file not found")
        return CheckResult(ok=False, failures=failures)
    if not isinstance(raw, dict):
        failures.append(f"contract {contract_path} is empty or not a YAML mapping")
        return CheckResult(ok=False, failures=failures)
    expected_env = raw.get("expected_env", {})
    if not isinstance(expected_env, dict):
        failures.append(
            f"contract {contract_path}: 'expected_env' must be a mapping, got "
            f"{type(expected_env).__name__}"
        )
        return CheckResult(ok=False, failures=failures)
    for var_name, rule in expected_env.items():
        if not isinstance(rule, dict):
            failures.append(f"{var_name}: contract entry is not a mapping")
            continue
        docs = rule.get("docs")
        if not isinstance(docs, dict):
            failures.append(f"{var_name}: contract entry missing 'docs' mapping")
            continue
        docs_file = docs.get("file")
        if not docs_file:
            failures.append(f"{var_name}: contract entry missing 'docs.file'")
            continue
        if not _path_safe(docs_file):
            failures.append(f"{var_name}: docs.file path rejected: {docs_file!r}")
            continue

        section_name = docs.get("section")
        if not section_name:
            failures.append(f"{var_name}: contract entry missing 'docs.section'")
            continue

        full_path = repo_root / docs_file
        if not full_path.exists():
            failures.append(f"{var_name}: docs file missing at {full_path}")
            continue

        sections = _split_sections(full_path.read_text())
        if section_name not in sections:
            failures.append(
                f"{var_name}: docs section '{section_name}' not found in {docs_file}"
            )
            continue

        section_body = sections[section_name]
        # Match the env var as a token (word boundary), case-sensitive
        var_pattern = re.compile(rf"\b{re.escape(var_name)}\b")
        var_lines = _lines_mentioning(section_body, var_pattern)
        if not var_lines:
            failures.append(
                f"{var_name}: not mentioned in section '{section_name}' of {docs_file}"
            )
            continue

        if rule.get("allow_manual_change"):
            # Per-variable check: 'operator note' must appear in the same line as
            # the variable, OR within the next 2 lines (for indented continuations).
            if not _operator_note_near(section_body, var_pattern):
                failures.append(
                    f"{var_name}: allow_manual_change=true but no 'operator note' "
                    f"near its mention in section '{section_name}'"
                )

    return CheckResult(ok=len(failures) == 0, failures=failures)


@app.command()
def check(
    contract: Path = typer.Option(Path("demo/ops-contract.yaml")),
    repo_root: Path = typer.Option(Path(".")),
):
    """Run the consistency gate."""
    result = check_docs_cover_contract(contract, repo_root)
    if result.ok:
        typer.echo("✓ All contract vars are documented.")
        raise typer.Exit(0)
    typer.echo("✗ Consistency gate failed:")
    for f in result.failures:
        typer.echo(f"  - {f}")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
