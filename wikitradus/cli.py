"""Prerequisiti: la CLI di traduzione e l'autenticazione a GitHub."""
import shutil
import subprocess
import sys
import tempfile

# Le due CLI supportate, con il modo di invocarle non interattivamente.
#
# I default sono del progetto, non della macchina: senza di essi le traduzioni
# girerebbero su quanto ciascuno ha in ~/.codex/config.toml, e la stessa voce
# verrebbe tradotta da modelli diversi a seconda di chi lancia lo script.
# Si sceglie il modello piu leggero di ogni famiglia con reasoning minimo:
# tradurre markdown non richiede un modello di frontiera.
#
# 'claude' non ha un flag di reasoning effort: l'argomento si ignora, non e'
# una dimenticanza da correggere.
ASSISTANTS = {
    "claude": {
        "ask": lambda prompt, model, effort: [
            "claude", "-p", "--model", model, prompt
        ],
        "auth": ["claude", "auth", "login"],
        "model": "claude-haiku-4-5",
        "effort": "low",
    },
    "codex": {
        "ask": lambda prompt, model, effort: [
            "codex", "exec",
            "-m", model,
            "-c", f"model_reasoning_effort={effort}",
            prompt,
        ],
        "auth": ["codex", "login", "--device-auth"],
        "model": "gpt-5.4-mini",
        "effort": "low",
    },
}

PROBE_PROMPT = "Rispondi esattamente OK"
PROBE_TIMEOUT = 120
TRANSLATE_TIMEOUT = 900

# Come installare ciò che manca. Gli id winget sono quelli della documentazione
# ufficiale di Git e di GitHub CLI.
INSTALL_HINTS = {
    "git": {
        "cosa": "il sistema di versionamento",
        "Windows": "winget install --id Git.Git -e --source winget",
        "macOS": "xcode-select --install",
        "Linux": "sudo apt install git",
        "url": "https://git-scm.com/install/windows",
    },
    "gh": {
        "cosa": "la riga di comando di GitHub",
        "Windows": "winget install --id GitHub.cli -e --source winget",
        "macOS": "brew install gh",
        "Linux": "sudo apt install gh",
        "url": "https://cli.github.com",
    },
    "claude": {
        "cosa": "l'assistente Claude da riga di comando",
        "Windows": "npm install -g @anthropic-ai/claude-code",
        "macOS": "npm install -g @anthropic-ai/claude-code",
        "Linux": "npm install -g @anthropic-ai/claude-code",
        "url": "https://claude.com/claude-code",
    },
    "codex": {
        "cosa": "l'assistente Codex da riga di comando",
        "Windows": "npm install -g @openai/codex",
        "macOS": "npm install -g @openai/codex",
        "Linux": "npm install -g @openai/codex",
        "url": "https://developers.openai.com/codex/cli",
    },
}

# Come le CLI segnalano un modello o un effort che non conoscono. Verificati sul
# campo: 'codex' risponde con un 400 dell'API o con un errore di parsing del
# config, 'claude' con una frase in inglese.
BAD_MODEL_MESSAGES = (
    "model is not supported",
    "unknown variant",
    "issue with the selected model",
    "may not exist or you may not have access",
    "invalid_request_error",
)

LIMIT_MESSAGES = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "usage limit",
    "usage_limit",
    "credit balance",
    "insufficient quota",
    "limite d'uso",
    "limiti d'uso",
    "quota superata",
)


class PrerequisiteError(Exception):
    """Un prerequisito non è soddisfatto: lo script non può proseguire."""


class UsageLimitError(RuntimeError):
    """La CLI ha segnalato che sono stati superati i limiti d'uso."""


def _run(command, timeout):
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, check=False,
    )


class Assistant:
    """La CLI che esegue le traduzioni: `claude` oppure `codex`."""

    def __init__(self, name, config, model=None, effort=None):
        self.name = name
        self._build = config["ask"]
        self._auth = config["auth"]
        # None significa "usa il default del progetto", non "usa quello della
        # macchina": si scavalca solo con un valore esplicito.
        self.model = model or config["model"]
        self.effort = effort or config["effort"]

    def ask(self, prompt, timeout=TRANSLATE_TIMEOUT):
        """Interroga la CLI da una directory vuota.

        Entrambe le CLI leggono i file di istruzioni della directory da cui
        partono - `CLAUDE.md`, `AGENTS.md` - e il clone ne contiene uno, con le
        regole di questo repository. Sono istruzioni che non riguardano la
        traduzione: finirebbero in ogni prompt, occuperebbero contesto e
        potrebbero spingere la CLI a fare altro. Il testo da tradurre viaggia
        già dentro il prompt, quindi la traduzione non ha bisogno di vedere
        nessun file: si esegue in una directory vuota.
        """
        with tempfile.TemporaryDirectory(prefix="wikitradus-") as empty:
            # Qui lo stdout *è* la traduzione, non un messaggio di servizio:
            # si dichiara l'encoding e si lascia la decodifica stretta. Un
            # carattere sostituito in silenzio finirebbe committato dentro una
            # voce, ed è il difetto che si vuole evitare; chi chiama tratta
            # l'eccezione come «lotto non tradotto» e lo rifà.
            result = subprocess.run(
                self._build(prompt, self.model, self.effort),
                capture_output=True, text=True, encoding="utf-8",
                timeout=timeout, cwd=empty, check=False,
            )
            diagnostic = result.stderr
            if result.returncode != 0:
                diagnostic = "\n".join(
                    part for part in (result.stdout, result.stderr) if part
                )
            if _looks_like_usage_limit(diagnostic):
                raise UsageLimitError(diagnostic.strip())
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "la CLI ha fallito")
            return result.stdout.strip()

    def probe(self):
        """Verifica che la CLI risponda davvero, non solo che esista.

        Non basta che il comando sia nel PATH o che un file di credenziali sia
        presente: una sessione può essere scaduta. `codex` intercala righe di
        servizio nell'output, quindi si cerca OK fra le righe invece di
        pretendere una corrispondenza esatta.

        Un modello o un effort che la CLI non riconosce si distingue qui: non è
        una sessione scaduta e rilanciare l'autenticazione non lo aggiusta, così
        l'errore si propaga invece di diventare un `False` fuorviante.
        """
        try:
            answer = self.ask(PROBE_PROMPT, timeout=PROBE_TIMEOUT)
        # `UnicodeDecodeError` sta in elenco perché la risposta si decodifica
        # in UTF-8 stretto: una CLI che risponde con altri byte è una CLI che
        # non va, e va trattata come tale invece di interrompere tutto - così
        # resta possibile provare l'altra.
        except (
            subprocess.TimeoutExpired, RuntimeError, UnicodeDecodeError
        ) as exc:
            if _looks_like_bad_model(str(exc)):
                raise PrerequisiteError(
                    f"'{self.name}' non accetta il modello '{self.model}' o "
                    f"l'effort '{self.effort}':\n\n{exc}\n\n"
                    "Controlla i valori passati a --modello e --effort. "
                    "I modelli disponibili si elencano con 'codex debug models'."
                ) from exc
            return False
        return any(line.strip().upper() == "OK" for line in answer.splitlines())

    def authenticate(self):
        """Avvia solo il flusso auth browser/device, senza aprire la UI."""
        subprocess.run(self._auth, check=False)


def _install_hint(name):
    """Come installare un comando mancante, sui tre sistemi operativi."""
    hints = INSTALL_HINTS[name]
    lines = [f"  {name}: {hints['cosa']}"]
    for system in ("Windows", "macOS", "Linux"):
        lines.append(f"      {system:<8} {hints[system]}")
    if "url" in hints:
        lines.append(f"      oppure  {hints['url']}")
    return "\n".join(lines)


def _looks_like_usage_limit(text):
    lowered = text.lower()
    return any(message in lowered for message in LIMIT_MESSAGES)


def _looks_like_bad_model(text):
    lowered = text.lower()
    return any(message in lowered for message in BAD_MODEL_MESSAGES)


def _enabled_assistant_names(selection=None):
    if selection is None:
        return list(ASSISTANTS)
    return [name for name in ASSISTANTS if selection.get(name, True)]


def check_commands(selection=None):
    """Verifica che i comandi necessari siano nel PATH, prima di ogni altra cosa.

    Un prerequisito mancante va scoperto subito e tutto insieme: scoprirne uno
    per volta, dopo minuti di lavoro, è il modo peggiore di fallire.
    """
    enabled = _enabled_assistant_names(selection)
    if not enabled:
        raise PrerequisiteError(
            "Devi abilitare almeno un assistente: usa --claude o --codex."
        )

    missing = [name for name in ("git", "gh") if not shutil.which(name)]
    available_assistants = [name for name in enabled if shutil.which(name)]
    if not available_assistants:
        missing.extend(enabled)

    if not missing:
        return

    message = ["Mancano dei programmi necessari.\n"]
    for name in missing:
        message.append(_install_hint(name))
    message.append(
        "\nInstallali, riapri il terminale e rilancia lo script."
    )
    raise PrerequisiteError("\n".join(message))


def find_assistant(selection=None, model=None, effort=None):
    """Trova claude o codex nel PATH e ne verifica l'autenticazione."""
    enabled = _enabled_assistant_names(selection)
    available = [
        Assistant(name, ASSISTANTS[name], model, effort)
        for name in enabled if shutil.which(name)
    ]
    if not available:
        raise PrerequisiteError(
            "Serve una CLI abilitata fra 'claude' e 'codex'."
        )

    for assistant in available:
        print(f"Verifico che '{assistant.name}' risponda…", flush=True)
        if assistant.probe():
            print(f"  '{assistant.name}' pronto, modello '{assistant.model}'.")
            return assistant
        print(
            f"  '{assistant.name}' non risponde: avvio il flusso di "
            "autenticazione browser/device.",
            flush=True,
        )
        assistant.authenticate()
        if assistant.probe():
            print(f"  '{assistant.name}' pronto, modello '{assistant.model}'.")
            return assistant

    raise PrerequisiteError(
        "Nessuna CLI risponde all'interrogazione di prova.\n"
        "Completa l'autenticazione browser/device e rilancia: senza un\n"
        "assistente funzionante non ha senso prenotare un gruppo di voci."
    )


def ensure_github_login():
    """Verifica il login a GitHub, avviandolo se manca. È bloccante."""
    if _run(["gh", "auth", "status"], timeout=60).returncode == 0:
        return

    print("Non risulti autenticato su GitHub: apro il browser.", flush=True)
    subprocess.run(
        ["gh", "auth", "login", "--web", "--git-protocol", "https"], check=False
    )
    if _run(["gh", "auth", "status"], timeout=60).returncode != 0:
        raise PrerequisiteError(
            "Login a GitHub non riuscito.\n"
            "Senza accesso a GitHub non si può creare il fork, prenotare il\n"
            "gruppo, pubblicare il lavoro né aprire la pull request."
        )


def check_prerequisites(selection=None, model=None, effort=None):
    """Verifica tutto ciò che serve prima di prenotare un gruppo.

    Prima l'esistenza dei comandi, tutti insieme, poi le autenticazioni: un
    programma mancante si scopre in un istante, autenticarsi richiede il
    browser.
    """
    check_commands(selection)
    assistant = find_assistant(selection, model, effort)
    ensure_github_login()
    return assistant
