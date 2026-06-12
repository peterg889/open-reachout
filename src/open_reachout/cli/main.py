"""Open Reachout CLI (PRD FR-1.x). Working: validate, doctor, dry-run, run,
halt/resume, forget, approve, report. Remaining stubs say so honestly."""

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
def init(
    from_brief: Path = typer.Option(  # noqa: B008 — typer idiom
        ..., "--from-brief", help="brief.yaml (or any yaml with a top-level brief: key)"
    ),
    tenant: str = typer.Option(..., help="tenant slug for the generated program"),
    out: Path = typer.Option(Path("."), help="output directory"),  # noqa: B008
    llm: str = typer.Option(
        "fake", help="fake = deterministic scaffold | gemini | anthropic = real synthesis"
    ),
    sample: int = typer.Option(5, help="dry-run sample emails in the proposal"),
) -> None:
    """Brief -> program synthesis -> Program Proposal (FR-0.2).

    Writes tenant.yaml (with generated_by provenance) and program-proposal.md
    (cohort summary + sample emails). Review the proposal, edit the config,
    then `reachout validate` before anything runs.
    """
    from open_reachout.adapters.fakes import FakeEnricher, FakeFinder, FakeSource, FakeVerifier
    from open_reachout.agents import synthesizer
    from open_reachout.core import dryrun
    from open_reachout.core.config import dump_tenant, load_brief
    from open_reachout.core.interfaces import Candidate, DataBasis

    brief = load_brief(from_brief)
    if llm == "fake":
        cfg = synthesizer.template_program(brief, tenant)
        mode = "deterministic scaffold (edit before launch; --llm gemini for real synthesis)"
    else:
        if llm == "gemini":
            from open_reachout.adapters.llm.gemini_backend import GeminiBackend

            backend: object = GeminiBackend()
        else:
            from open_reachout.adapters.llm.anthropic_backend import AnthropicBackend

            backend = AnthropicBackend()
        try:
            cfg = synthesizer.synthesize(backend, brief, tenant)  # type: ignore[arg-type]
        except synthesizer.SynthesisEscalation as exc:
            typer.secho(f"synthesis escalated: {exc}", fg="red")
            raise typer.Exit(1) from exc
        mode = f"synthesized via {llm}"

    out.mkdir(parents=True, exist_ok=True)
    tenant_path = out / "tenant.yaml"
    tenant_path.write_text(dump_tenant(cfg))

    # Program Proposal: cohort summary + dry-run sample (operators judge
    # programs by reading emails, not YAML — PRD FR-0.2).
    ctx = dryrun.validator_context(cfg)
    scripted = dryrun.ScriptedLLM(ctx, cfg.brief.about_us.identity.sender)
    sources = {
        s: FakeSource(
            [
                Candidate(
                    display_name=f"Sample Prospect{i}", org_name=f"Sample Org {i}",
                    website="https://example.test",
                    email_raw=f"prospect{i}@sample.example.test",
                    source_adapter=s, data_basis=DataBasis.GOVERNMENT_PUBLIC,
                )
                for i in range(sample)
            ]
        )
        for p in cfg.personas for c in p.cohorts for s in c.sources
    }
    proposal_path = out / "program-proposal.md"
    report = dryrun.run(
        cfg, sources, FakeEnricher(), FakeFinder(), FakeVerifier(),
        scripted, sample, proposal_path,
    )
    summary = [
        "# Program Proposal", "",
        f"- mode: {mode}",
        f"- tenant: {cfg.tenant} (autonomy: {cfg.brief.autonomy})",
        f"- provenance: {cfg.generated_by.agent if cfg.generated_by else 'hand-written'} "
        f"hash={cfg.generated_by.config_hash if cfg.generated_by else '-'}",
        "",
        "## Cohorts",
    ]
    for p in cfg.personas:
        for c in p.cohorts:
            summary.append(
                f"- {p.id}/{c.id}: {c.monthly_budget}/mo via {', '.join(c.sources)} "
                f"({len(p.variants)} variants)"
            )
    summary += ["", "## Sample emails (scripted placeholders; live LLM at dry-run)", ""]
    proposal_path.write_text("\n".join(summary) + "\n" + proposal_path.read_text())

    typer.secho(f"wrote {tenant_path} and {proposal_path} ({mode})", fg="green")
    typer.echo(
        f"next: review {proposal_path.name}, edit {tenant_path.name}, then "
        f"`reachout validate {tenant_path}` and `reachout dry-run {tenant_path} --llm gemini`"
    )
    if report.escalated:
        typer.secho(f"note: {len(report.escalated)} sample(s) escalated", fg="yellow")


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
def run(
    config_dir: Path = typer.Argument(  # noqa: B008 — typer idiom
        ..., help="directory of tenant dirs (validator contexts come from config)"
    ),
    once: bool = typer.Option(False, "--once", help="drain pending jobs and exit"),
    llm: str = typer.Option("fake", help="fake | gemini | anthropic"),
    provider: str = typer.Option("fake", help="fake (smartlead adapter lands with M2 spike)"),
) -> None:
    """Run the worker: control, classify, and deliver queues (spec 6)."""
    from open_reachout.adapters.fakes import FakeSendingProvider
    from open_reachout.core import dryrun, events, sendpath
    from open_reachout.core.db import engine_from_env
    from open_reachout.core.worker import Worker

    if provider != "fake":
        typer.secho("only the fake provider is wired yet (Smartlead spike pending)", fg="red")
        raise typer.Exit(2)

    contexts = {}
    senders = {}
    for f in sorted(config_dir.rglob("tenant.yaml")):
        cfg = load_tenant(f)
        contexts[cfg.tenant] = dryrun.validator_context(cfg)
        senders[cfg.tenant] = cfg.brief.about_us.identity.sender

    backend: object
    if llm == "gemini":
        from open_reachout.adapters.llm.gemini_backend import GeminiBackend

        backend = GeminiBackend()
    elif llm == "anthropic":
        from open_reachout.adapters.llm.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend()
    else:
        first = next(iter(contexts))
        backend = dryrun.ScriptedLLM(contexts[first], senders[first])

    sending = FakeSendingProvider()
    worker = Worker(
        engine_from_env(),
        handlers={
            "control": events.make_control_handler(sending),
            "classify": events.make_classify_handler(backend),  # type: ignore[arg-type]
            "deliver": sendpath.make_deliver_handler(sending, contexts),
        },
    )
    if once:
        processed = worker.drain()
        typer.secho(f"worker: processed {processed} job(s), queues idle", fg="green")
        return
    typer.secho("worker: running (ctrl-c to stop)", fg="green")
    worker.run_forever()


@app.command()
def halt(tenant: str = typer.Option(None, help="halt one tenant; omit for global")) -> None:
    """Stop all sending immediately; human resume only (FR-1.3, gate 4)."""
    from open_reachout.core import control
    from open_reachout.core.db import engine_from_env

    scope = tenant or control.GLOBAL_SCOPE
    with engine_from_env().begin() as conn:
        control.halt(conn, scope=scope, actor=f"operator:{os.environ.get('USER', 'cli')}")
    typer.secho(
        f"HALTED scope={scope}. No claims or dispatches will proceed; provider "
        "campaign pauses are queued. Only `reachout resume` restores sending.",
        fg="red",
    )


@app.command()
def resume(tenant: str = typer.Option(None, help="resume one tenant; omit for global")) -> None:
    """Clear a halt/kill-switch flag (human-only, invariant I-2)."""
    from open_reachout.core import control
    from open_reachout.core.db import engine_from_env

    scope = tenant or control.GLOBAL_SCOPE
    with engine_from_env().begin() as conn:
        cleared = control.resume(
            conn, scope=scope, actor=f"operator:{os.environ.get('USER', 'cli')}"
        )
    typer.secho(
        f"resumed scope={scope}" if cleared else f"no active flag for scope={scope}",
        fg="green" if cleared else "yellow",
    )


@app.command()
def forget(ref: str) -> None:
    """One-call data-subject deletion (FR-1.4, gate 5): email or entity id."""
    from open_reachout.core import forget as forget_mod
    from open_reachout.core.db import engine_from_env

    with engine_from_env().begin() as conn:
        receipt = forget_mod.forget(conn, ref)
    noun = "entity" if len(receipt.entity_ids) == 1 else "entities"
    typer.secho(
        f"forgotten: {len(receipt.entity_ids)} {noun}, "
        f"{receipt.addresses_tombstoned} address(es) tombstoned. "
        f"Receipt: {receipt.receipt_id} (provider propagation queued)",
        fg="green",
    )


@app.command()
def approve(
    resolve: str = typer.Option(None, help="escalation id to resolve"),
    note: str = typer.Option("", help="resolution note (audited)"),
) -> None:
    """Work the escalation queue (FR-4.6, RX-1): list open items or resolve one."""
    from open_reachout.core import escalations
    from open_reachout.core.db import engine_from_env

    actor = f"operator:{os.environ.get('USER', 'cli')}"
    with engine_from_env().begin() as conn:
        if resolve:
            ok = escalations.resolve(conn, resolve, actor=actor, note=note)
            typer.secho(
                f"resolved {resolve}" if ok else f"{resolve} is not an open escalation",
                fg="green" if ok else "yellow",
            )
            return
        items = escalations.list_open(conn)
    if not items:
        typer.secho("escalation queue is empty", fg="green")
        return
    for item in items:
        typer.echo(
            f"{item.id}  [{item.subject_type}] {item.reason}\n"
            f"    tenant={item.tenant} subject={item.subject_id} "
            f"at={item.created_at:%Y-%m-%d %H:%M}\n"
            f"    payload={item.payload}"
        )
    typer.echo(f"\n{len(items)} open. Resolve with: reachout approve --resolve <id>")


@app.command()
def report() -> None:
    """Print the operator digest (FR-8.1)."""
    from open_reachout.core.db import engine_from_env
    from open_reachout.core.report import build_report

    with engine_from_env().begin() as conn:
        typer.echo(build_report(conn))


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8714),
) -> None:
    """Serve the operator API + webhook endpoint (FR-1.6, spec 11).

    Requires OR_DATABASE_DSN, OR_ATTRIBUTION_KEY, OR_API_TOKENS.
    """
    try:
        import uvicorn
    except ImportError as exc:
        typer.secho("pip install 'open-reachout[api]' to serve", fg="red")
        raise typer.Exit(2) from exc
    from open_reachout.adapters.fakes import FakeSendingProvider
    from open_reachout.api.app import create_app
    from open_reachout.core.db import engine_from_env

    application = create_app(engine_from_env(), FakeSendingProvider())
    uvicorn.run(application, host=host, port=port)


def main() -> None:  # console_scripts shim
    sys.exit(app())


if __name__ == "__main__":
    main()
