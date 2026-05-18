"""DriftScribe CLI.

Currently exposes a single command, ``driftscribe init``, which bootstraps an
``ops-contract.yaml`` from the live Cloud Run state.

Conservative defaults:
- Every observed var is written with ``allow_manual_change: false`` so the
  operator must explicitly mark vars as manual-flip-safe (and provide an
  ``operator_note``) before DriftScribe will sanction docs PRs for them.
- Values are written as YAML strings so a literal ``false`` or ``42``
  doesn't get round-tripped as a YAML bool/int.
- ``value_source`` secrets are skipped upstream by ``read_live_env`` so we
  never write a Secret Manager-backed var into the contract.

The user reviews the generated file, edits ``allow_manual_change`` /
``operator_note`` / ``docs.section`` for flags that need them, then opens a
bootstrap PR with ``gh pr create``.
"""

from pathlib import Path

import typer
import yaml

from agent.cloud_run_client import read_live_env

app = typer.Typer(help="DriftScribe CLI")


@app.callback()
def _main():
    """DriftScribe CLI.

    A no-op callback so Typer treats ``init`` as a true subcommand rather than
    auto-promoting it to the root command (which would mean users invoke
    ``driftscribe --service ...`` instead of ``driftscribe init --service ...``).
    """


def _needs_quoting(s: str) -> bool:
    """A string needs explicit quoting if YAML would otherwise re-parse it
    as a non-string (e.g. ``'false'`` -> bool False, ``'42'`` -> int 42,
    ``'null'`` -> None). Plain identifier-ish strings round-trip fine and
    are left unquoted to keep the generated contract readable."""
    try:
        return yaml.safe_load(s) != s
    except yaml.YAMLError:
        return True


def _str_representer(dumper, data):
    """Quote string scalars only when needed — keeps ``value: 'false'`` from
    being read back as a YAML bool, but leaves keys and plain identifiers
    unquoted for readability."""
    if _needs_quoting(data):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


class _QuotedDumper(yaml.SafeDumper):
    pass


_QuotedDumper.add_representer(str, _str_representer)


@app.command()
def init(
    service: str = typer.Option(..., help="Cloud Run service name"),
    region: str = typer.Option("asia-northeast1", help="Cloud Run region"),
    project: str = typer.Option(..., help="GCP project ID"),
    github_repo: str = typer.Option(..., help="owner/repo of the runbook repository"),
    output: Path = typer.Option(Path("ops-contract.yaml"), help="Where to write the contract"),
    docs_file: str = typer.Option(
        "docs/runbook.md", help="Default docs.file for every var (edit per-var afterwards)"
    ),
    docs_section: str = typer.Option(
        "Runtime Configuration", help="Default docs.section for every var"
    ),
):
    """Bootstrap ops-contract.yaml from current live Cloud Run state."""
    live = read_live_env(service, region, project)
    contract = {
        "service": service,
        "environment": "production",
        "cloud_run_service": service,
        "region": region,
        "github_repo": github_repo,
        "expected_env": {
            name: {
                "value": value,
                "docs": {"file": docs_file, "section": docs_section},
                "allow_manual_change": False,
            }
            for name, value in live.items()
        },
    }
    output.write_text(
        yaml.dump(contract, Dumper=_QuotedDumper, sort_keys=False, default_flow_style=False)
    )
    typer.echo(f"✓ Wrote {output}")
    typer.echo("")
    typer.echo("Next steps (review before opening the PR):")
    typer.echo("  1. Edit the generated contract: for each var an operator can flip")
    typer.echo("     without redeploying, set allow_manual_change: true and add an")
    typer.echo("     operator_note describing what flipping it does.")
    typer.echo("  2. Set docs.section per-var to point at the right runbook section.")
    typer.echo(f"  3. git add {output} && gh pr create --title 'driftscribe: bootstrap'")


if __name__ == "__main__":
    app()
