"""Explicit local commands; no daemon or automatic subscription usage."""
import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from . import __version__
from .subscription_probe import ProbeSettings, inspect_cli, run_subscription_probe


def save_receipt(path: Path, receipt: dict) -> None:
    """Replace a receipt atomically, without accepting a symlink target."""
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("receipt_path_is_symlink")
    if not path.parent.is_dir():
        raise ValueError("receipt_parent_missing")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".receipt-", delete=False) as stream:
            temp_path = Path(stream.name)
            os.chmod(temp_path, 0o600)
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Vérifier un Codex personnel connecté à un abonnement.")
    parser.add_argument("--version", action="version", version=__version__)
    subs = parser.add_subparsers(dest="command", required=True)
    from .runner_cli import configure_parser, run as run_runner
    configure_parser(subs.add_parser('runner', help='Exécutant personnel local, sans listener du board.'))
    from .context_cli import configure_parser as configure_context, run as run_context
    configure_context(subs.add_parser('context', help='Préparer et inspecter les sources d’un ticket.'))
    for command, help_text in (
        ("doctor", "Vérifier l’installation et le mode de connexion, sans appel de modèle."),
        ("probe", "Tester une réponse minimale ; consomme une petite part de l’abonnement."),
    ):
        p = subs.add_parser(command, help=help_text)
        p.add_argument("--codex", help="Chemin de l’exécutable Codex ; sinon chercher dans PATH.")
        p.add_argument("--output", type=Path, help="Écrire un reçu expurgé, sans identifiants de connexion.")
        if command == "probe":
            p.add_argument("--model", help="Modèle choisi explicitement ; sinon défaut du client Codex.")
            p.add_argument("--timeout", type=float, default=120, help="Délai maximum en secondes (1–300).")
    args = parser.parse_args(argv)
    if args.command == 'runner':
        return run_runner(args)
    if args.command == 'context':
        return run_context(args)
    executable = args.codex or shutil.which("codex")
    if not executable:
        print(json.dumps({"status": "blocked", "reason": "codex_not_installed"}))
        return 2
    try:
        timeout = getattr(args, "timeout", 120)
        if not 1 <= timeout <= 300:
            raise ValueError("timeout_out_of_range")
        settings = ProbeSettings(executable=executable, model=getattr(args, "model", None),
                                 timeout_seconds=timeout)
        result = inspect_cli(settings) if args.command == "doctor" else run_subscription_probe(settings)
        receipt = {"format": "blackoutburst-personal-subscription-proof-v1", "tool_version": __version__,
                   "command": args.command, "scope": "personal_codex_cli", "result": result,
                   "limitations": ["Does not establish hosted-service eligibility.",
                                   "Does not establish a second owner's account access.",
                                   "Does not activate a board listener or execute a ticket."]}
        if args.output:
            save_receipt(args.output, receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in ("ok", "passed", "success", "ready") else 2
    except (ValueError, OSError):
        # Avoid exposing paths, environment or raw process diagnostics.
        print(json.dumps({"status": "blocked", "reason": "invalid_configuration_or_local_io"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
