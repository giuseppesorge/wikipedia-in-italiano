#!/usr/bin/env python3
"""Test per wikitradus: scelta del modello e traduzione a lotti.
Eseguire con: python3 -m unittest test_wikitradus -v"""

import ast
import re
import tempfile
import types
import unittest
from pathlib import Path

from unittest import mock

import wikitradus
from wikitradus.cli import ASSISTANTS, Assistant, _looks_like_bad_model
from wikitradus.extract import RateLimited
from wikitradus import translate
from wikitradus.translate import (
    MARKER,
    make_batches,
    split_batch_answer,
)

RADICE = Path(__file__).resolve().parent

# I sorgenti percorsi da chi traduce, più questo file: le fixture qui dentro
# vengono rilette dal codice di produzione, quindi la regola vale anche per
# loro. `wikibatches/` è tooling dei manutentori e dichiara già l'encoding
# quasi ovunque: resta fuori da questa pull request.
SORGENTI_CONTROLLATE = [
    RADICE / "traduci.py",
    RADICE / "test_wikitradus.py",
    *sorted((RADICE / "wikitradus").glob("*.py")),
]


class TestModelloEdEffort(unittest.TestCase):
    """Il modello e' scelto dal progetto, non dalla configurazione locale."""

    def _comando(self, name, model=None, effort=None):
        assistant = Assistant(name, ASSISTANTS[name], model, effort)
        return assistant._build("PROMPT", assistant.model, assistant.effort)

    def test_default_codex(self):
        comando = self._comando("codex")
        self.assertIn("gpt-5.4-mini", comando)
        self.assertIn("model_reasoning_effort=low", comando)

    def test_default_claude(self):
        comando = self._comando("claude")
        self.assertIn("claude-haiku-4-5", comando)

    def test_override_modello(self):
        comando = self._comando("codex", model="gpt-5.5")
        self.assertIn("gpt-5.5", comando)
        self.assertNotIn("gpt-5.4-mini", comando)

    def test_override_effort(self):
        comando = self._comando("codex", effort="high")
        self.assertIn("model_reasoning_effort=high", comando)

    def test_claude_ignora_effort(self):
        # 'claude' non ha un flag di reasoning effort: passarlo non deve
        # comparire nel comando ne' far fallire la costruzione.
        comando = self._comando("claude", effort="xhigh")
        self.assertNotIn("xhigh", comando)
        self.assertIn("claude-haiku-4-5", comando)

    def test_il_prompt_resta_ultimo(self):
        # Il prompt e' posizionale: se finisse prima di un flag verrebbe letto
        # come valore di quel flag.
        for name in ASSISTANTS:
            with self.subTest(cli=name):
                self.assertEqual(self._comando(name)[-1], "PROMPT")


class TestIsolamentoDalRepo(unittest.TestCase):
    """La CLI non deve vedere i file del clone.

    Entrambe le CLI leggono CLAUDE.md / AGENTS.md dalla directory da cui
    partono, e il clone ne contiene uno con le regole di questo repository:
    istruzioni estranee alla traduzione, che occuperebbero contesto in ogni
    chiamata e potrebbero spingere la CLI a fare altro.
    """

    def _esegui(self):
        """Restituisce (cwd, contenuto) osservati mentre la CLI 'gira'."""
        visto = {}

        def spia(*args, **kwargs):
            cwd = Path(kwargs["cwd"])
            # Va guardata adesso: al ritorno di ask() e' gia' stata rimossa.
            visto["cwd"] = cwd
            visto["contenuto"] = sorted(p.name for p in cwd.iterdir())
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        assistant = Assistant("codex", ASSISTANTS["codex"])
        with mock.patch("wikitradus.cli.subprocess.run", side_effect=spia):
            assistant.ask("prompt")
        return visto

    def test_gira_in_una_directory_vuota(self):
        self.assertEqual(self._esegui()["contenuto"], [])

    def test_non_gira_nel_repository(self):
        cwd = self._esegui()["cwd"].resolve()
        progetto = Path(__file__).resolve().parent
        self.assertFalse(
            cwd == progetto or progetto in cwd.parents or cwd in progetto.parents,
            f"la CLI verrebbe eseguita dentro il repository: {cwd}",
        )

    def test_la_directory_viene_rimossa(self):
        # Una per chiamata: non devono accumularsi sul disco a ogni voce.
        self.assertFalse(self._esegui()["cwd"].exists())


class TestRiconoscimentoModelloIgnoto(unittest.TestCase):
    """Messaggi osservati sul campo, non inventati."""

    def test_codex_modello_inesistente(self):
        self.assertTrue(_looks_like_bad_model(
            'ERROR: {"type":"error","status":400,"error":{"type":'
            '"invalid_request_error","message":"The \'gpt-nonexistent-9\' model '
            'is not supported when using Codex with a ChatGPT account."}}'
        ))

    def test_codex_effort_inesistente(self):
        self.assertTrue(_looks_like_bad_model(
            "Error loading config.toml: unknown variant `nonesuch`, expected "
            "one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`"
        ))

    def test_claude_modello_inesistente(self):
        self.assertTrue(_looks_like_bad_model(
            "There's an issue with the selected model (modello-inesistente-9). "
            "It may not exist or you may not have access to it."
        ))

    def test_non_confonde_altri_errori(self):
        # Una sessione scaduta non e' un modello sbagliato: deve restare sul
        # percorso dell'autenticazione.
        self.assertFalse(_looks_like_bad_model("Not logged in. Run codex login."))
        self.assertFalse(_looks_like_bad_model("network unreachable"))


class TestComposizioneLotti(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _file(self, name, size):
        path = self.dir / f"{name}.md"
        path.write_text("x" * size, encoding="utf-8")
        return path

    def test_raggruppa_fino_alla_soglia(self):
        paths = [self._file(str(i), 1000) for i in range(5)]
        lotti = make_batches(paths, max_bytes=2500, max_entries=99)
        self.assertEqual([len(l) for l in lotti], [2, 2, 1])

    def test_tetto_sul_numero_di_voci(self):
        paths = [self._file(str(i), 10) for i in range(7)]
        lotti = make_batches(paths, max_bytes=100_000, max_entries=3)
        self.assertEqual([len(l) for l in lotti], [3, 3, 1])

    def test_voce_sopra_soglia_va_da_sola(self):
        # La voce piu' lunga misurata era 27 KB, sopra la soglia di lotto: deve
        # partire da sola invece di bloccare il raggruppamento.
        grande = self._file("grande", 30_000)
        piccola = self._file("piccola", 500)
        lotti = make_batches([grande, piccola], max_bytes=24_000)
        self.assertEqual(lotti, [[grande], [piccola]])

    def test_nessun_file(self):
        self.assertEqual(make_batches([]), [])

    def test_non_perde_voci(self):
        paths = [self._file(str(i), 900) for i in range(11)]
        lotti = make_batches(paths, max_bytes=2000, max_entries=4)
        self.assertEqual([p for lotto in lotti for p in lotto], paths)


class TestSplitRisposta(unittest.TestCase):
    def _risposta(self, coppie):
        return "\n\n".join(
            f"{MARKER.format(page_id=pid)}\n{testo}" for pid, testo in coppie
        )

    def test_risposta_completa(self):
        answer = self._risposta([("11", "Prima voce"), ("22", "Seconda voce")])
        self.assertEqual(
            split_batch_answer(answer, ["11", "22"]),
            {"11": "Prima voce", "22": "Seconda voce"},
        )

    def test_blocco_mancante(self):
        # Il chiamante ritraduce da sola la voce assente.
        answer = self._risposta([("11", "Prima voce")])
        self.assertEqual(
            split_batch_answer(answer, ["11", "22"]), {"11": "Prima voce"}
        )

    def test_identificativo_inatteso_scartato(self):
        # Accettarlo significherebbe scrivere una traduzione sul file sbagliato.
        answer = self._risposta([("11", "Prima"), ("999", "Voce mai inviata")])
        self.assertEqual(split_batch_answer(answer, ["11", "22"]), {"11": "Prima"})

    def test_identificativo_ripetuto_scartato(self):
        # Due blocchi per la stessa voce: senza sapere quale sia quello buono,
        # meglio ritradurla che indovinare.
        answer = self._risposta([("11", "Una versione"), ("11", "Un'altra")])
        self.assertEqual(split_batch_answer(answer, ["11"]), {})

    def test_ordine_invertito(self):
        # I delimitatori portano l'identificativo, quindi l'ordine non conta.
        answer = self._risposta([("22", "Seconda"), ("11", "Prima")])
        self.assertEqual(
            split_batch_answer(answer, ["11", "22"]),
            {"11": "Prima", "22": "Seconda"},
        )

    def test_nessun_delimitatore(self):
        # La CLI ha ignorato il formato: niente e' recuperabile.
        self.assertEqual(
            split_batch_answer("Solo testo tradotto", ["11", "22"]), {}
        )

    def test_blocco_vuoto_scartato(self):
        answer = self._risposta([("11", ""), ("22", "Seconda")])
        self.assertEqual(split_batch_answer(answer, ["11", "22"]), {"22": "Seconda"})

    def test_risposta_dentro_code_fence(self):
        # La CLI incornicia il risultato nonostante la richiesta contraria.
        inner = self._risposta([("11", "Prima"), ("22", "Seconda")])
        answer = f"```markdown\n{inner}\n```"
        self.assertEqual(
            split_batch_answer(answer, ["11", "22"]),
            {"11": "Prima", "22": "Seconda"},
        )

    def test_markdown_interno_conservato(self):
        testo = "# Titolo\n\n**grassetto** e *corsivo*\n\n- uno\n- due"
        answer = self._risposta([("11", testo)])
        self.assertEqual(split_batch_answer(answer, ["11"]), {"11": testo})

    def test_delimitatore_con_spaziatura_diversa(self):
        # La CLI puo' riemettere il delimitatore con un numero di '=' diverso.
        answer = "=== VOCE 11 ===\nPrima voce"
        self.assertEqual(split_batch_answer(answer, ["11"]), {"11": "Prima voce"})

    def test_preambolo_ignorato(self):
        # Il testo prima del primo delimitatore non appartiene a nessuna voce:
        # attaccarlo alla prima la sporcherebbe.
        answer = "Ecco le traduzioni:\n\n" + self._risposta([("11", "Prima")])
        self.assertEqual(split_batch_answer(answer, ["11"]), {"11": "Prima"})

    def test_delimitatore_finale_senza_corpo(self):
        # Risposta troncata a meta': l'ultima voce non e' tradotta e va ripresa.
        answer = self._risposta([("11", "Prima")]) + "\n\n" + MARKER.format(page_id="22")
        self.assertEqual(split_batch_answer(answer, ["11", "22"]), {"11": "Prima"})

    def test_identificativo_non_numerico(self):
        answer = self._risposta([("1-2_3", "Testo")])
        self.assertEqual(split_batch_answer(answer, ["1-2_3"]), {"1-2_3": "Testo"})


class TestEtichettaVoce(unittest.TestCase):
    """Nei messaggi compare il titolo, non il numero del file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _file(self, name, testo):
        path = self.dir / f"{name}.md"
        path.write_text(testo, encoding="utf-8")
        return path

    def test_legge_il_titolo_dall_intestazione(self):
        path = self._file("123", "# Get Happy\n\n*[Voce originale](...)*\n\ntesto")
        self.assertEqual(translate._label(path), "Get Happy")

    def test_senza_intestazione_ripiega_sul_nome(self):
        path = self._file("456", "testo senza intestazione")
        self.assertEqual(translate._label(path), "456")

    def test_file_vuoto(self):
        self.assertEqual(translate._label(self._file("789", "")), "789")

    def test_file_inesistente(self):
        # Non deve sollevare: e' solo un'etichetta per un messaggio.
        self.assertEqual(translate._label(self.dir / "000.md"), "000")

    def test_usa_il_testo_gia_letto(self):
        # Passando il contenuto non si rilegge il file da disco.
        path = self.dir / "111.md"
        self.assertEqual(translate._label(path, "# Titolo\n\ntesto"), "Titolo")


class TestCommitPerLotto(unittest.TestCase):
    """Si pubblica alla fine di ogni lotto, non ogni N voci."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        # Lotti da 3 voci, cosi' con 7 voci se ne producono tre.
        for name, value in (("BATCH_MAX_ENTRIES", 3), ("FETCH_PAUSE", 0)):
            patcher = mock.patch.object(translate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.commits = []
        self.tradotte = 0
        self.pubblicate = 0

        def commit_all(message):
            # Come il vero commit_all: senza nulla di nuovo restituisce False.
            if self.pubblicate == self.tradotte:
                return False
            self.pubblicate = self.tradotte
            self.commits.append(message)
            return True

        self.workdir = types.SimpleNamespace(
            path=self.dir,
            group_dir=lambda g: self.dir / g,
            translated_ids=lambda g: set(),
            mark_translated=lambda g, pid: None,
            commit_all=commit_all,
            push=lambda: None,
        )

        def fake_batch(paths, assistant):
            for path in paths:
                path.write_text("(tradotto)\n", encoding="utf-8")
            self.tradotte += len(paths)
            return list(paths)

        for name, value in (
            ("_translate_batch", fake_batch),
            ("_fetch_with_retry", lambda t, l: "<div class='mw-parser-output'><p>x</p></div>"),
        ):
            patcher = mock.patch.object(translate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _esegui(self, quante):
        voci = [(str(i), f"Voce_{i}") for i in range(1, quante + 1)]
        return translate.process_group(
            self.workdir, "t", voci, types.SimpleNamespace(name="codex")
        )

    def test_un_commit_per_lotto(self):
        # 7 voci in lotti da 3: tre lotti, tre commit. Con la vecchia regola
        # (ogni 10) non ce ne sarebbe stato nessuno fino alla fine.
        self._esegui(7)
        self.assertEqual(len(self.commits), 3)

    def test_il_commit_riporta_il_totale_progressivo(self):
        self._esegui(7)
        self.assertEqual(
            self.commits,
            [
                "traduzioni: t, 3 voci",
                "traduzioni: t, 6 voci",
                "traduzioni: t, 7 voci",
            ],
        )

    def test_nessun_commit_duplicato_alla_fine(self):
        # L'ultimo lotto ha gia' pubblicato: la chiusura non ripete lo stesso
        # stato, perche' commit_all vede che non c'e' nulla di nuovo.
        self._esegui(6)
        self.assertEqual(self.commits[-1], "traduzioni: t, 6 voci")
        self.assertEqual(len(self.commits), 2)

    def test_lotto_senza_traduzioni_non_committa(self):
        # Se la CLI non rende nulla non si crea un commit vuoto.
        with mock.patch.object(
            translate, "_translate_batch", lambda p, a: []
        ), mock.patch.object(translate, "_translate_one", lambda p, a: False):
            self._esegui(4)
        self.assertEqual(self.commits, [])


class TestRateLimitWikipedia(unittest.TestCase):
    """Un 429 e' una richiesta di rallentare, non la fine del lavoro."""

    def setUp(self):
        # Le attese non devono rallentare i test.
        patcher = mock.patch.object(translate.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_riprova_dopo_il_429(self):
        with mock.patch.object(
            translate, "fetch_html",
            side_effect=[RateLimited("429", 5), "<html>ok</html>"],
        ):
            self.assertEqual(
                translate._fetch_with_retry("Voce", "en"), "<html>ok</html>"
            )
        self.sleep.assert_called_once_with(5)

    def test_usa_il_retry_after_del_server(self):
        # L'attesa indicata da Wikipedia vince sulla nostra.
        with mock.patch.object(
            translate, "fetch_html",
            side_effect=[RateLimited("429", 42), "ok"],
        ):
            translate._fetch_with_retry("Voce", "en")
        self.sleep.assert_called_once_with(42)

    def test_senza_retry_after_attesa_crescente(self):
        with mock.patch.object(
            translate, "fetch_html",
            side_effect=[RateLimited("429"), RateLimited("429"), "ok"],
        ):
            translate._fetch_with_retry("Voce", "en")
        self.assertEqual(
            [c.args[0] for c in self.sleep.call_args_list],
            [translate.RATE_LIMIT_PAUSE, translate.RATE_LIMIT_PAUSE * 2],
        )

    def test_si_arrende_se_il_limite_persiste(self):
        # Esauriti i tentativi l'errore risale: il gruppo si ferma e riprende
        # dopo, invece di insistere all'infinito.
        with mock.patch.object(
            translate, "fetch_html",
            side_effect=RateLimited("429", 1),
        ):
            with self.assertRaises(RateLimited):
                translate._fetch_with_retry("Voce", "en")

    def test_altri_errori_non_vengono_ritentati(self):
        with mock.patch.object(
            translate, "fetch_html", side_effect=ValueError("rotto"),
        ) as fetch:
            with self.assertRaises(ValueError):
                translate._fetch_with_retry("Voce", "en")
        self.assertEqual(fetch.call_count, 1)


class TestEncodingEsplicito(unittest.TestCase):
    """Ogni lettura e scrittura di testo dichiara il proprio encoding.

    Senza `encoding=` Python usa quello della piattaforma: UTF-8 su Linux e
    macOS, cp1252 su Windows. I file di `groups/` e le voci estratte sono
    UTF-8, quindi su Windows la lettura o fallisce subito o corrompe i titoli
    in silenzio - e una voce dal titolo corrotto non risolve, viene marcata
    «non disponibile» e saltata senza che nessuno se ne accorga.

    Il danno maggiore è però in uscita dalla CLI di traduzione: lo stdout
    decodificato male produce accenti corrotti dentro il testo tradotto, che
    non essendo mai uguale all'originale supera il controllo «traduzione
    identica = non fatta» e finisce committato e spedito in pull request.

    Su una macchina UTF-8 la differenza non si vede, quindi serve un controllo
    che non dipenda dalla piattaforma di chi esegue i test.

    Il controllo è deliberatamente limitato: riconosce le forme che questo
    repository usa - `read_text`, `write_text`, `open` e le funzioni di
    `subprocess` in modo testuale - e verifica che l'encoding sia dichiarato,
    non che valga UTF-8. Che valga UTF-8 lo prova `TestGiroCompletoConAccenti`.
    """

    # I modi di leggere e scrivere testo che, senza `encoding=`, seguono la
    # piattaforma.
    IO_TESTUALE = ("read_text", "write_text", "open")

    # Le funzioni di `subprocess` che decodificano l'output del figlio.
    SOTTOPROCESSI = ("run", "check_output", "Popen", "check_call", "call")

    # I due modi di chiedere a `subprocess` di decodificare: il secondo è il
    # nome storico del primo e fa esattamente la stessa cosa.
    MODO_TESTO = ("text", "universal_newlines")

    # `.open()` di questi moduli non apre un file di testo: `gzip` e `tarfile`
    # non ci sono apposta, perché in modo testuale `encoding` lo accettano
    # eccome ed è giusto che il controllo lo pretenda.
    NON_SU_FILE = ("webbrowser", "os", "dbm")

    # Un modo di apertura si riconosce dalla forma, non dalla posizione: nella
    # `open()` incorporata il primo argomento è il percorso, in `Path.open` è
    # già il modo. Cercarlo per posizione sbaglia in entrambe le direzioni.
    # Le lettere possono stare in qualsiasi ordine (`"rb"` e `"br"` valgono
    # uguale), quindi si accetta qualunque combinazione breve.
    MODO = re.compile(r"^[rwxabt+]{1,4}$")

    def _nome(self, nodo):
        """Il nome della funzione chiamata, senza il modulo che la contiene."""
        if isinstance(nodo.func, ast.Attribute):
            return nodo.func.attr
        if isinstance(nodo.func, ast.Name):
            return nodo.func.id
        return None

    def _ricevitore(self, nodo):
        """Il nome a sinistra del punto: `webbrowser` in `webbrowser.open(…)`."""
        funzione = nodo.func
        if isinstance(funzione, ast.Attribute):
            if isinstance(funzione.value, ast.Name):
                return funzione.value.id
        return None

    def _argomento(self, nodo, nome):
        """Il valore dell'argomento con nome `nome`, None se assente."""
        for keyword in nodo.keywords:
            if keyword.arg == nome:
                return keyword.value
        return None

    def _e_vero(self, nodo, nome):
        """Vero se l'argomento `nome` è la costante letterale True."""
        valore = self._argomento(nodo, nome)
        return isinstance(valore, ast.Constant) and valore.value is True

    def _dichiara_encoding(self, nodo):
        """Vero se la chiamata dichiara un encoding.

        `encoding=None` non conta: è la scrittura esplicita del default di
        piattaforma, cioè esattamente il difetto, non la sua correzione.
        """
        valore = self._argomento(nodo, "encoding")
        if valore is None:
            return False
        return not (isinstance(valore, ast.Constant) and valore.value is None)

    def _e_binario(self, nodo):
        """Vero se si apre in binario: lì `encoding` non si applica."""
        return any(
            isinstance(valore, ast.Constant)
            and isinstance(valore.value, str)
            and self.MODO.match(valore.value)
            and "b" in valore.value
            for valore in (*nodo.args, self._argomento(nodo, "mode"))
        )

    def _io_senza_encoding(self, sorgente):
        """I punti di I/O testuale privi di `encoding=`: [(riga, cosa), …]."""
        trovati = []
        for nodo in ast.walk(ast.parse(sorgente.read_text(encoding="utf-8"))):
            if not isinstance(nodo, ast.Call):
                continue
            if self._dichiara_encoding(nodo):
                continue
            nome = self._nome(nodo)
            if nome == "open" and (
                self._e_binario(nodo)
                or self._ricevitore(nodo) in self.NON_SU_FILE
            ):
                continue
            if nome in self.IO_TESTUALE:
                trovati.append((nodo.lineno, f"{nome}()"))
                continue
            # Senza uno dei due il processo restituisce byte grezzi e non c'è
            # nessun encoding da dichiarare.
            if nome in self.SOTTOPROCESSI:
                for argomento in self.MODO_TESTO:
                    if self._e_vero(nodo, argomento):
                        trovati.append(
                            (nodo.lineno, f"{nome}({argomento}=True)")
                        )
                        break
        return trovati

    def test_nessun_io_testuale_senza_encoding(self):
        # Se il glob si svuotasse, il test passerebbe senza controllare nulla.
        self.assertGreater(len(SORGENTI_CONTROLLATE), 2)
        for sorgente in SORGENTI_CONTROLLATE:
            with self.subTest(sorgente=sorgente.name):
                trovati = self._io_senza_encoding(sorgente)
                dettaglio = ", ".join(
                    f"riga {riga}: {cosa}" for riga, cosa in trovati
                )
                self.assertEqual(
                    trovati, [],
                    f"{sorgente.name}: I/O testuale senza encoding= a "
                    f"{dettaglio}",
                )

    def _controlla(self, codice):
        with tempfile.TemporaryDirectory() as tmp:
            sorgente = Path(tmp) / "esempio.py"
            sorgente.write_text(codice, encoding="utf-8")
            return self._io_senza_encoding(sorgente)

    def test_il_controllo_riconosce_il_difetto(self):
        """Un controllo che non sa fallire non protegge nulla."""
        difettosi = (
            "path.read_text()",
            "path.write_text(testo)",
            'path.open("a")',
            'open(percorso, "w")',
            # Il nome del file contiene una 'b': non è un modo binario.
            'open("batch.txt")',
            "subprocess.run(comando, text=True)",
            "subprocess.run(comando, universal_newlines=True)",
        )
        for codice in difettosi:
            with self.subTest(codice=codice):
                self.assertTrue(self._controlla(codice))

    def test_il_controllo_non_segnala_i_casi_leciti(self):
        """Encoding esplicito, modo binario e byte grezzi vanno bene."""
        leciti = (
            'path.read_text(encoding="utf-8")',
            'path.write_text(testo, encoding="utf-8")',
            'path.open("a", encoding="utf-8")',
            'path.open("rb")',
            'open(percorso, "rb")',
            'open(percorso, mode="wb")',
            # Apre il browser, non un file: `encoding` non si applica.
            "webbrowser.open(url)",
            "subprocess.run(comando)",
            "subprocess.run(comando, text=False)",
            'subprocess.run(comando, text=True, encoding="utf-8")',
        )
        for codice in leciti:
            with self.subTest(codice=codice):
                self.assertEqual(self._controlla(codice), [])


class TestGiroCompletoConAccenti(unittest.TestCase):
    """Il testo accentato attraversa il giro e resta UTF-8 sul disco.

    `TestEncodingEsplicito` verifica che l'encoding sia *dichiarato*: scrivere
    `encoding="cp1252"` lo soddisferebbe lo stesso. Qui invece si guardano i
    byte, così il valore dichiarato è vincolato oltre alla sua presenza.
    """

    TITOLO = "The_World_Tour_(Def_Leppard_and_Mötley_Crüe)"
    VOCE = "# Città di Kraków\n\nUna società con sede a Łódź.\n"

    def test_read_group_conserva_i_titoli_accentati(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gruppo.txt"
            path.write_bytes(f"123\t{self.TITOLO}\n".encode("utf-8"))
            self.assertEqual(
                translate.read_group(path), [("123", self.TITOLO)]
            )

    def test_estrazione_scrive_utf8_sul_disco(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                translate, "_fetch_with_retry", lambda t, l: "<html/>"
            ):
                with mock.patch.object(
                    translate, "to_markdown", lambda html, lang: "Società"
                ):
                    path = translate._extract_one(
                        Path(tmp), "123", self.TITOLO, "en"
                    )
            # Decodificare in UTF-8 stretto è il punto: se fosse stato scritto
            # con un altro encoding, questa riga solleverebbe.
            testo = path.read_bytes().decode("utf-8")
            self.assertIn(self.TITOLO, testo)
            self.assertIn("Società", testo)

    def test_la_traduzione_si_scrive_utf8_sul_disco(self):
        tradotta = "# Città di Cracovia\n\nUna società con sede a Łódź."
        assistente = types.SimpleNamespace(
            name="finto", ask=lambda prompt: tradotta
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "123.md"
            path.write_text(self.VOCE, encoding="utf-8")
            self.assertTrue(translate._translate_one(path, assistente))
            self.assertEqual(
                path.read_bytes(), (tradotta + "\n").encode("utf-8")
            )

    def test_il_titolo_accentato_si_rilegge_dal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "123.md"
            path.write_bytes(self.VOCE.encode("utf-8"))
            self.assertEqual(translate._label(path), "Città di Kraków")

    def test_un_file_illeggibile_non_ferma_il_lavoro(self):
        """Un `.md` lasciato da una versione precedente può non essere UTF-8."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "123.md"
            path.write_bytes("# Città\n".encode("cp1252"))
            self.assertEqual(translate._label(path), "123")


class TestUscitaInUtf8(unittest.TestCase):
    """Anche la scrittura su schermo va dichiarata, non solo quella su file.

    Su Windows, quando l'uscita è rediretta su file o in una pipe, Python usa
    l'encoding di sistema: stampare il titolo di una voce che cp1252 non
    rappresenta interrompe il lavoro con `UnicodeEncodeError`. È lo stesso
    difetto delle letture, ma in scrittura, e non si può correggere a ogni
    `print`: si dichiara una volta sola sul flusso.
    """

    class FlussoFinto:
        """Imita `TextIOWrapper.reconfigure`, che accetta solo keyword."""

        def __init__(self, encoding, errors):
            self.encoding = encoding
            self.errors = errors

        def reconfigure(self, *, encoding=None, errors=None):
            if encoding is not None:
                self.encoding = encoding
            if errors is not None:
                self.errors = errors

    def _riconfigura(self, stdout, stderr):
        with mock.patch("sys.stdout", stdout):
            with mock.patch("sys.stderr", stderr):
                wikitradus.force_utf8_output()

    def test_dichiara_utf8_su_stdout_e_stderr(self):
        flussi = (
            self.FlussoFinto("cp1252", "strict"),
            self.FlussoFinto("cp1252", "backslashreplace"),
        )
        self._riconfigura(*flussi)
        self.assertEqual([f.encoding for f in flussi], ["utf-8", "utf-8"])

    def test_non_tocca_la_gestione_degli_errori(self):
        """`stderr` nasce tollerante apposta: un errore deve poter uscire.

        `reconfigure` riporta `errors` al default se non glielo si passa, e
        rimetterlo a «strict» farebbe sparire in silenzio proprio i messaggi
        d'errore - il contrario di ciò che questa correzione vuole ottenere.
        """
        flussi = (
            self.FlussoFinto("cp1252", "strict"),
            self.FlussoFinto("cp1252", "backslashreplace"),
        )
        self._riconfigura(*flussi)
        self.assertEqual(
            [f.errors for f in flussi], ["strict", "backslashreplace"]
        )

    def test_tollera_flussi_non_riconfigurabili(self):
        """Dietro una cattura i flussi possono non essere file di testo."""
        self._riconfigura(object(), object())

    def test_main_dichiara_l_encoding_prima_di_qualunque_stampa(self):
        """Se lo facesse dopo, la prima riga stampata potrebbe già rompersi."""
        sorgente = (RADICE / "traduci.py").read_text(encoding="utf-8")
        corpo = next(
            (
                nodo for nodo in ast.parse(sorgente).body
                if isinstance(nodo, ast.FunctionDef) and nodo.name == "main"
            ),
            None,
        )
        self.assertIsNotNone(corpo, "traduci.py non definisce più main()")
        # Una docstring iniziale non è codice: non conta come «prima cosa».
        istruzioni = [
            nodo for nodo in corpo.body
            if not (
                isinstance(nodo, ast.Expr)
                and isinstance(nodo.value, ast.Constant)
                and isinstance(nodo.value.value, str)
            )
        ]
        self.assertEqual(ast.unparse(istruzioni[0]), "force_utf8_output()")


if __name__ == "__main__":
    unittest.main()
