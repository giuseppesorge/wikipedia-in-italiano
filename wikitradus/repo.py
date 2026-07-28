"""Operazioni su git e GitHub: fork, clone, branch, issue, pull request."""
import json
import subprocess
from pathlib import Path

UPSTREAM = "OpenEmmaLab/wikipedia-in-italiano"
BASE_BRANCH = "traduzioni"
MAIN_BRANCH = "main"
TRANSLATIONS_DIR = "traduzioni"
ASSIGNED_PREFIX = "Assegnata:"


def gh(*args, check=True):
    """Invoca la CLI di GitHub e restituisce stdout."""
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


class Workdir:
    """Il clone locale del fork, su cui si svolge il lavoro."""

    def __init__(self, path):
        self.path = Path(path)

    def git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True,
            encoding="utf-8", check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    # -- stato ----------------------------------------------------------------

    @property
    def branch(self):
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    @property
    def is_dirty(self):
        return bool(self.git("status", "--porcelain"))

    def group_dir(self, group):
        return self.path / TRANSLATIONS_DIR / group

    def translated_file(self, group):
        return self.group_dir(group) / "translated.txt"

    def translated_ids(self, group):
        """I page_id già tradotti, letti da translated.txt."""
        path = self.translated_file(group)
        if not path.exists():
            return set()
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def mark_translated(self, group, page_id):
        """Registra una voce come tradotta, in coda a translated.txt."""
        path = self.translated_file(group)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{page_id}\n")

    # -- sparse checkout ------------------------------------------------------

    def include_group(self, group):
        """Aggiunge la cartella del gruppo allo sparse checkout.

        Senza questo passaggio la ripresa si romperebbe in silenzio: la cartella
        risulterebbe vuota sul disco anche con le traduzioni già pushate, e lo
        script rifarebbe da capo lavoro già svolto.
        """
        if not (self.path / ".git" / "info" / "sparse-checkout").exists():
            return
        self.git("sparse-checkout", "add", f"{TRANSLATIONS_DIR}/{group}")

    # -- commit ---------------------------------------------------------------

    def commit_all(self, message):
        """Committa tutto ciò che è pendente. False se non c'era nulla."""
        if not self.is_dirty:
            return False
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return True

    def push(self, branch=None):
        """Pubblica il branch sul fork. Silenzioso se non c'è un remote."""
        if not self.git("remote", check=False):
            return False
        self.git("push", "-u", "origin", branch or self.branch)
        return True

    def checkout_new(self, branch):
        self.git("checkout", "-b", branch)

    def checkout(self, branch):
        self.git("checkout", branch)


def ensure_fork():
    """Restituisce il fork dell'utente, creandolo se manca."""
    user = json.loads(gh("api", "user"))["login"]
    fork = f"{user}/{UPSTREAM.split('/')[1]}"
    result = subprocess.run(
        ["gh", "repo", "view", fork, "--json", "name"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        print(f"Creo il fork {fork}…", flush=True)
        gh("repo", "fork", UPSTREAM, "--clone=false")
    return fork


def ensure_clone(fork, path):
    """Clona il fork se manca, shallow e parziale. Se c'è già, usa quello.

    Non si sincronizza con upstream: groups/ è stabile e le traduzioni altrui
    non servono, quindi riallineare a ogni avvio sarebbe lavoro inutile.
    """
    path = Path(path)
    if (path / ".git").exists():
        return Workdir(path)

    print(f"Clono {fork} in {path}…", flush=True)
    subprocess.run(
        [
            "git", "clone", "--depth", "1", "--filter=blob:none",
            "--sparse", f"https://github.com/{fork}.git", str(path),
        ],
        check=True,
    )
    workdir = Workdir(path)
    workdir.git("sparse-checkout", "set", "groups")
    return workdir


# -- issue --------------------------------------------------------------------

def issue_title(group):
    return f"{ASSIGNED_PREFIX} {group}"


def group_is_taken(group):
    """Vero se esiste già una issue per il gruppo, aperta o chiusa."""
    found = gh(
        "issue", "list", "--repo", UPSTREAM, "--state", "all",
        "--search", f'"{issue_title(group)}" in:title',
        "--json", "title", "--limit", "50",
    )
    title = issue_title(group)
    return any(item["title"].strip() == title for item in json.loads(found or "[]"))


def open_issue(group):
    """Prenota il gruppo aprendo la issue. Restituisce il numero."""
    url = gh(
        "issue", "create", "--repo", UPSTREAM,
        "--title", issue_title(group),
        "--body", (
            f"Traduzione del gruppo `{group}` in corso.\n\n"
            f"Aperta automaticamente dal traduttore (vedi #3)."
        ),
    )
    return url.rstrip("/").split("/")[-1]


def find_issue(group):
    """Il numero della issue del gruppo, se esiste."""
    found = gh(
        "issue", "list", "--repo", UPSTREAM, "--state", "all",
        "--search", f'"{issue_title(group)}" in:title',
        "--json", "number,title", "--limit", "50",
    )
    title = issue_title(group)
    for item in json.loads(found or "[]"):
        if item["title"].strip() == title:
            return str(item["number"])
    return None


def close_issue(number, comment):
    gh("issue", "close", number, "--repo", UPSTREAM, "--comment", comment)


def open_pull_request(fork, branch, group, translated, total):
    """Apre la PR dal branch del gruppo verso il branch traduzioni."""
    owner = fork.split("/")[0]
    body = (
        f"Traduzione in italiano del gruppo `{group}`.\n\n"
        f"{translated} voci tradotte su {total} estratte.\n\n"
        f"Prodotta dal traduttore descritto in #3."
    )
    return gh(
        "pr", "create", "--repo", UPSTREAM,
        "--base", BASE_BRANCH, "--head", f"{owner}:{branch}",
        "--title", f"Traduzione del gruppo {group}",
        "--body", body,
    )
