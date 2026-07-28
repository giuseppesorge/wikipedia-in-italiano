#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4", "markdownify"]
# ///
"""Traduttore EN→IT delle voci di Wikipedia (issue #3).

Si prenota un gruppo di voci aprendo una issue sul repository, le estrae in
markdown, le fa tradurre da `claude` o `codex` e apre una pull request.

    ./traduci.py                   lavora su un gruppo scelto a caso
    ./traduci.py --group 0001-0    lavora su un gruppo specifico
    ./traduci.py --group test1     gruppo di prova: una voce sola
"""
import argparse
import random
import subprocess
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wikitradus import force_utf8_output, repo
from wikitradus.cli import PrerequisiteError, check_prerequisites
from wikitradus.translate import LimitReached, process_group, read_group

DEFAULT_WORKDIR = Path.home() / ".wikipedia-in-italiano"
POST_TEXT = "Ho contribuito a tradurre Wikipedia in italiano, il mio batch è {group}"


def pick_group(workdir, requested=None):
    """Sceglie un gruppo libero: quello richiesto, o uno a caso."""
    index = workdir.path / "groups" / "groups.txt"
    groups = [
        line.strip()
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    if requested:
        name = requested if requested.endswith(".txt") else f"{requested}.txt"
        # Il gruppo richiesto si valida sul file, non sull'indice: i gruppi di
        # prova test1..test9 esistono ma sono fuori da groups.txt, così non
        # vengono mai estratti dalla scelta casuale.
        if not (workdir.path / "groups" / name).exists():
            raise SystemExit(f"Il gruppo '{requested}' non esiste in groups/")
        if repo.group_is_taken(name[:-4]):
            raise SystemExit(f"Il gruppo '{requested}' è già assegnato.")
        return name[:-4]

    candidates = groups[:]
    random.shuffle(candidates)
    for candidate in candidates:
        group = candidate[:-4] if candidate.endswith(".txt") else candidate
        print(f"Provo il gruppo {group}…", flush=True)
        if not repo.group_is_taken(group):
            return group
    raise SystemExit("Nessun gruppo libero: sono tutti assegnati.")


def share(group):
    """Propone di annunciare il contributo. Facoltativo e da confermare."""
    text = POST_TEXT.format(group=group)
    print(f"\nVuoi annunciare il contributo?\n  «{text}»")
    answer = input("Apro il browser per pubblicarlo? [s/N] ").strip().lower()
    if answer not in {"s", "si", "sì", "y", "yes"}:
        print("Nessun problema: il lavoro è comunque completato.")
        return
    quoted = urllib.parse.quote(text)
    for url in (
        f"https://www.linkedin.com/feed/?shareActive=true&text={quoted}",
        f"https://x.com/intent/tweet?text={quoted}",
    ):
        subprocess.run(["open", url], check=False)
    print("Rivedi il testo nel browser prima di pubblicarlo.")


def assistant_flag(value):
    """Converte i flag --claude/--codex: 1 abilita, 0 disabilita."""
    if isinstance(value, bool):
        return value
    value = value.lower()
    if value in {"1", "true", "yes", "y", "si", "sì"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("usa 1/0, true/false oppure yes/no")


def assistant_selection(args):
    """Se l'utente nomina una CLI, usa solo le CLI nominate e abilitate."""
    mentioned = {
        "claude": args.claude is not None,
        "codex": args.codex is not None,
    }
    if any(mentioned.values()):
        return {
            "claude": mentioned["claude"] and args.claude,
            "codex": mentioned["codex"] and args.codex,
        }
    return {"claude": True, "codex": True}


def main():
    force_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--group", "--gruppo", dest="gruppo",
        help="lavora su un gruppo specifico invece di sceglierlo a caso "
             "(es. --group test1)",
    )
    parser.add_argument(
        "--workdir", type=Path, default=DEFAULT_WORKDIR,
        help=f"dove tenere il clone (default: {DEFAULT_WORKDIR})",
    )
    parser.add_argument(
        "--lingua", default="en", help="lingua di partenza (default: en)"
    )
    parser.add_argument(
        "--claude", nargs="?", const=True, default=None, type=assistant_flag,
        help="usa Claude Code, o disabilitalo con --claude 0",
    )
    parser.add_argument(
        "--codex", nargs="?", const=True, default=None, type=assistant_flag,
        help="usa Codex, o disabilitalo con --codex 0",
    )
    parser.add_argument(
        "--modello", "--model", dest="modello",
        help="scavalca il modello scelto dal progetto "
             "(default: gpt-5.4-mini per codex, claude-haiku-4-5 per claude)",
    )
    parser.add_argument(
        "--effort", choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        help="profondita di ragionamento, solo per codex (default: low)",
    )
    args = parser.parse_args()

    # 1. Prerequisiti: nessuna issue viene aperta prima che la CLI risponda.
    try:
        assistant = check_prerequisites(
            assistant_selection(args), args.modello, args.effort
        )
    except PrerequisiteError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    fork = repo.ensure_fork()
    workdir = repo.ensure_clone(fork, args.workdir)

    # 2. Il branch corrente dice se c'è un lavoro in corso.
    current = workdir.branch
    resuming = current != repo.MAIN_BRANCH
    if resuming:
        group = current
        print(f"Riprendo il lavoro sul gruppo {group}.")
        workdir.include_group(group)
        if workdir.commit_all(f"traduzioni: {group}, lavoro recuperato"):
            workdir.push()
    else:
        # 3-5. Sceglie un gruppo libero, lo prenota e crea il branch.
        group = pick_group(workdir, args.gruppo)
        print(f"Prenoto il gruppo {group}…", flush=True)
        repo.open_issue(group)
        workdir.checkout_new(group)
        workdir.include_group(group)

    entries = read_group(workdir.path / "groups" / f"{group}.txt")
    print(f"Il gruppo {group} contiene {len(entries)} voci.")

    # 6-7. Le voci si scaricano e si traducono a lotti, con translated.txt
    # aggiornato per voce e un commit alla fine di ogni lotto.
    try:
        process_group(workdir, group, entries, assistant, args.lingua)
    except LimitReached as exc:
        print(f"\n{exc}", file=sys.stderr)
        print(
            "Il gruppo resta sul branch corrente e verrà ripreso al prossimo "
            "avvio; non apro la pull request e non chiudo la issue.",
            file=sys.stderr,
        )
        return 1

    # 8. Chiusura: PR, issue, ritorno su main. L'ordine conta.
    translated = len(workdir.translated_ids(group))
    total = len(list(workdir.group_dir(group).glob("*.md")))
    print(f"\nTradotte {translated} voci su {total} estratte.")

    pull_request = repo.open_pull_request(fork, group, group, translated, total)
    print(f"Pull request aperta: {pull_request}")

    issue = repo.find_issue(group)
    if issue:
        repo.close_issue(issue, f"Traduzione completata: {pull_request}")
        print(f"Issue #{issue} chiusa.")

    workdir.checkout(repo.MAIN_BRANCH)
    print(f"Tornato su {repo.MAIN_BRANCH}: nessun lavoro in corso.")

    # 9. Condivisione facoltativa.
    share(group)
    return 0


if __name__ == "__main__":
    sys.exit(main())
