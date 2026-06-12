"""Open Reachout CLI (PRD FR-1.x). M0: validate + doctor are real; pipeline
commands land with their milestones and say so honestly instead of pretending."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from open_reachout import __version__
from open_reachout.core.config import ConfigError, expand_autonomy, load_tenant

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Open Reachout")


@app.command()
def version() -> None:
    typer.echo(f"open-reachout {__version__}")


@app.command()
def validate(
    path: Path = typer.Argument(  # noqa: B008 — typer idiom
        ..., help="tenant config file or directory of tenant dirs"
    ),
) -> None:
    """Validate tenant config(s). Exit 0 only if everything passes (FR-1.1)."""
    files = sorted(path.rglob("tenant.yaml")) if path.is_dir() else [path]
    if not files:
        typer.secho(f"no tenant.yaml found under {path}", fg="red")
        raise typer.Exit(2)
    failures = 0
    for f in files:
        try:
            cfg = load_tenant(f)
        except ConfigError as exc:
            failures += 1
            typer.secho(f"FAIL {f}", fg="red")
            typer.echo(f"  {exc}")
            continue
        knobs = expand_autonomy(cfg.brief.autonomy)
        personas = ", ".join(p.id for p in cfg.personas)
        typer.secho(f"ok   {f}", fg="green")
        typer.echo(
            f"     tenant={cfg.tenant} autonomy={cfg.brief.autonomy} "
            f"(cohort_launch={knobs.cohort_launch}) personas=[{personas}]"
        )
    raise typer.Exit(1 if failures else 0)


@app.command()
def doctor() -> None:
    """Environment checks (FR-1.5). M0: secrets hygiene + DSN presence."""
    problems: list[str] = []
    if not os.environ.get("OR_DATABASE_DSN"):
        problems.append("OR_DATABASE_DSN is not set")
    for var in ("OR_API_TOKENS",):
        value = os.environ.get(var, "")
        if value and len(value) < 16:
            problems.append(f"{var} looks too short to be a real credential")
    if problems:
        for p in problems:
            typer.secho(f"warn: {p}", fg="yellow")
        raise typer.Exit(1)
    typer.secho("doctor: ok (M0 checks only — DNS/provider checks land in M1)", fg="green")


def _not_yet(milestone: str) -> None:
    typer.secho(f"not implemented yet (lands in {milestone})", fg="yellow")
    raise typer.Exit(3)


@app.command()
def init() -> None:
    """Brief interview -> program synthesis (FR-0.2)."""
    _not_yet("M4")


@app.command("dry-run")
def dry_run(
    config: Path = typer.Argument(..., help="tenant.yaml path"),  # noqa: B008 — typer idiom
    n: int = typer.Option(3, help="prospects per cohort"),
    out: Path = typer.Option(Path("dryrun-review.md")),  # noqa: B008 — typer idiom
    llm: str = typer.Option("fake", help="fake | gemini (default live backend) | anthropic"),
    nppes_csv: Path = typer.Option(  # noqa: B008 — typer idiom
        None, help="NPPES dissemination CSV for nppes-sourced cohorts"
    ),
) -> None:
    """Run the pipeline through compose with no sends (FR-1.2)."""
    from open_reachout.adapters.fakes import FakeEnricher, FakeFinder, FakeSource, FakeVerifier
    from open_reachout.core import dryrun
    from open_reachout.core.interfaces import Candidate, DataBasis, SourceAdapter

    cfg = load_tenant(config)
    ctx = dryrun.validator_context(cfg)

    backend: object
    if llm == "gemini":
        from open_reachout.adapters.llm.gemini_backend import GeminiBackend

        backend = GeminiBackend()
    elif llm == "anthropic":
        from open_reachout.adapters.llm.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend()
    else:
        backend = dryrun.ScriptedLLM(ctx, cfg.brief.about_us.identity.sender)

    sources: dict[str, SourceAdapter] = {}
    for persona in cfg.personas:
        for cohort in persona.cohorts:
            for source_name in cohort.sources:
                if source_name == "nppes" and nppes_csv:
                    from open_reachout.adapters.sources.nppes import NppesSource

                    sources[source_name] = NppesSource(nppes_csv)
                elif source_name not in sources:
                    sources[source_name] = FakeSource(
                        [
                            Candidate(
                                display_name=f"Sample Prospect{i} ({cohort.id})",
                                org_name=f"Sample Org {i}",
                                website="https://example.test",
                                email_raw=f"prospect{i}@{cohort.id}.example.test",
                                source_adapter=source_name,
                                data_basis=DataBasis.GOVERNMENT_PUBLIC,
                            )
                            for i in range(n)
                        ]
                    )

    report = dryrun.run(
        cfg, sources, FakeEnricher(), FakeFinder(), FakeVerifier(), backend, n, out  # type: ignore[arg-type]
    )
    typer.secho(
        f"dry-run: {len(report.composed)} drafts, {len(report.disqualified)} disqualified, "
        f"{len(report.escalated)} escalated -> {out}",
        fg="green",
    )


@app.command()
def run() -> None:
    _not_yet("M2")


@app.command()
def halt(tenant: str = typer.Option(None)) -> None:
    """Stop all sending immediately; human resume only (FR-1.3, gate 4)."""
    _not_yet("M2")


@app.command()
def resume(tenant: str = typer.Option(None)) -> None:
    _not_yet("M2")


@app.command()
def forget(ref: str) -> None:
    """One-call data-subject deletion (FR-1.4, gate 5)."""
    _not_yet("M2")


@app.command()
def approve() -> None:
    _not_yet("M3")


@app.command()
def report() -> None:
    _not_yet("M3")


def main() -> None:  # console_scripts shim
    sys.exit(app())


if __name__ == "__main__":
    main()
