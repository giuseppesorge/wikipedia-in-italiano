"""Le due fasi del lavoro: estrazione delle voci e traduzione."""
import re
import time
import urllib.parse

from . import repo
from .cli import UsageLimitError
from .extract import NotFound, RateLimited, fetch_html, to_markdown

# Pausa fra le richieste a Wikipedia, per non martellare i loro server.
#
# A lotti la pausa deve fare tutto il lavoro da sola. Prima le richieste si
# distanziavano da sole perché fra un download e l'altro c'era una traduzione:
# ora se ne scaricano BATCH_MAX_ENTRIES di fila prima di una sola chiamata alla
# CLI, quindi il ritmo verso Wikipedia dipende solo da questo valore.
#
# Wikimedia non dichiara un limite numerico per le letture anonime: la loro
# API:Etiquette chiede richieste in serie invece che in parallelo, ed è quello
# che facciamo. Una prova con 30 voci distinte senza pausa non ha prodotto
# nessun 429 (si ferma da sola a ~2 req/s per la latenza di rete), quindi un
# secondo di pausa è deliberatamente prudente: il costo è trascurabile rispetto
# al tempo di traduzione, e il lavoro non è urgente.
FETCH_PAUSE = 1.0

# Quando Wikipedia risponde 429, quante volte riprovare e quanto aspettare se
# non indica un Retry-After. L'attesa cresce a ogni tentativo.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_PAUSE = 30

# Quanto testo mandare in una volta. La voce mediana sta sotto i 2 KB, quindi un
# lotto da 24 KB ne tiene una decina: il preambolo di sistema della CLI, che è il
# costo fisso di ogni invocazione, si paga una volta ogni dieci voci invece che
# a ogni voce. Il tetto sul numero serve a non spedire centinaia di voci minuscole
# in un colpo solo.
BATCH_MAX_BYTES = 24_000
BATCH_MAX_ENTRIES = 12

# Il delimitatore che separa le voci dentro un lotto. Deve sopravvivere al
# viaggio di andata e ritorno: la CLI lo rilegge e lo riemette, e su di esso si
# risplitta la risposta per sapere quale traduzione appartiene a quale voce.
MARKER = "===== VOCE {page_id} ====="
MARKER_RE = re.compile(r"^=+\s*VOCE\s+(\S+?)\s*=+$", re.MULTILINE)

TRANSLATE_PROMPT = """\
Traduci in italiano il testo markdown qui sotto.

Usa un italiano idiomatico e naturale, come lo scriverebbe un madrelingua, non
una traduzione letterale. Conserva la struttura markdown (intestazioni, elenchi,
grassetti). Lascia in inglese i nomi propri che non hanno un esonimo italiano
consolidato. Non aggiungere link: il testo non ne contiene.

Rispondi con la sola traduzione: nessun commento, nota o preambolo, e nessun
blocco di codice attorno al risultato.

--- TESTO DA TRADURRE ---
{text}
"""

BATCH_PROMPT = """\
Traduci in italiano le {count} voci markdown qui sotto.

Usa un italiano idiomatico e naturale, come lo scriverebbe un madrelingua, non
una traduzione letterale. Conserva la struttura markdown (intestazioni, elenchi,
grassetti). Lascia in inglese i nomi propri che non hanno un esonimo italiano
consolidato. Non aggiungere link: il testo non ne contiene.

Ogni voce è preceduta da una riga delimitatore nella forma
`===== VOCE <identificativo> =====`. Riemetti ogni delimitatore, identico e da
solo sulla sua riga, prima della traduzione della voce corrispondente. Mantieni
l'ordine ricevuto e non unire, non saltare e non riordinare le voci: devi
restituire tutte e {count}.

Rispondi con i soli delimitatori e le traduzioni: nessun commento, nota o
preambolo, e nessun blocco di codice attorno al risultato.

--- VOCI DA TRADURRE ---
{text}
"""

# La CLI può incorniciare la risposta in un blocco di codice nonostante la
# richiesta contraria: lo si toglie prima di salvare.
FENCE = re.compile(r"^```(?:markdown|md)?\n(.*)\n```$", re.DOTALL)


class LimitReached(Exception):
    """Il lavoro deve fermarsi perché un servizio ha segnalato limiti superati."""


def read_group(path):
    """Legge un file di gruppo: restituisce [(page_id, titolo), …]."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        page_id, _, title = line.partition("\t")
        entries.append((page_id.strip(), title.strip()))
    return entries


def _wiki_url(title, lang="en"):
    quoted = urllib.parse.quote(title.replace(" ", "_"), safe="")
    return f"https://{lang}.wikipedia.org/wiki/{quoted}"


def _fetch_with_retry(title, lang):
    """Scarica una voce, aspettando se Wikipedia chiede di rallentare.

    Un `429` non e' un errore fatale: e' il server che dice di andare piu'
    piano. Aspettare e riprovare costa qualche secondo, arrendersi costa
    l'interruzione del gruppo. Si riprova un numero limitato di volte: se il
    limite persiste, allora conviene davvero fermarsi.
    """
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            return fetch_html(title, lang)
        except RateLimited as exc:
            if attempt == RATE_LIMIT_RETRIES - 1:
                raise
            # L'attesa indicata dal server ha la precedenza sulla nostra.
            wait = exc.retry_after or RATE_LIMIT_PAUSE * (attempt + 1)
            print(
                f"  Wikipedia chiede di rallentare: aspetto {wait}s "
                f"(tentativo {attempt + 1}/{RATE_LIMIT_RETRIES - 1}).",
                flush=True,
            )
            time.sleep(wait)


def _extract_one(destination, page_id, title, lang):
    """Scarica una voce e la salva in markdown. None se non esiste più."""
    path = destination / f"{page_id}.md"
    if path.exists():
        return path
    try:
        markdown = to_markdown(_fetch_with_retry(title, lang), lang)
    except NotFound:
        # La voce è stata cancellata o rinominata dopo la generazione dei
        # batch: in entrambi i casi il titolo non risolve più e si salta.
        return None
    except RateLimited as exc:
        raise LimitReached(
            "Wikipedia continua a rispondere '429 Too Many Requests'. "
            "Ferma il lavoro e rilancialo più tardi."
        ) from exc
    except Exception as exc:
        print(f"  {title}: {exc}", flush=True)
        return None

    header = (
        f"# {title}\n\n"
        f"*[Voce originale su Wikipedia]({_wiki_url(title, lang)})*\n\n"
    )
    path.write_text(header + markdown + "\n", encoding="utf-8")
    return path


def _label(path, text=None):
    """Il titolo della voce, per i messaggi. Ripiega sul nome del file.

    I file estratti iniziano con `# Titolo`: leggerlo da lì evita di far
    comparire `12345.md` in mezzo a un elenco di titoli.

    Un file scritto da una versione precedente può non essere UTF-8 valido:
    anche quello vale come «non riesco a leggere il titolo», non come motivo
    per interrompere il lavoro.
    """
    try:
        contenuto = text if text is not None else path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path.stem
    first = contenuto.lstrip().split("\n", 1)[0]
    return first[1:].strip() if first.startswith("# ") else path.stem


def _translate_one(path, assistant):
    """Traduce un file sul posto. False se la CLI non ha prodotto nulla."""
    before = path.read_text(encoding="utf-8")
    try:
        answer = assistant.ask(TRANSLATE_PROMPT.format(text=before))
    except UsageLimitError as exc:
        raise LimitReached(
            f"'{assistant.name}' segnala che sono stati superati i limiti "
            "d'uso. Rilancia lo script quando il limite si azzera."
        ) from exc
    except Exception as exc:
        print(f"  {_label(path, before)}: {exc}", flush=True)
        return False

    fenced = FENCE.match(answer.strip())
    result = (fenced.group(1) if fenced else answer).strip()

    # Se la risposta è vuota o identica all'originale la CLI non ha lavorato:
    # la voce non conta come tradotta e verrà ripresa al rilancio.
    if not result or result == before.strip():
        print(f"  {_label(path, before)}: invariato", flush=True)
        return False

    path.write_text(result + "\n", encoding="utf-8")
    return True


def _chunks(items, size):
    """Divide una lista in gruppetti di al piu' `size` elementi."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def make_batches(paths, max_bytes=BATCH_MAX_BYTES, max_entries=BATCH_MAX_ENTRIES):
    """Raggruppa i file in lotti da tradurre insieme.

    I lotti si compongono per dimensione, non per numero fisso: le voci variano
    da poche centinaia di byte a qualche decina di KB, e un numero fisso
    manderebbe lotti enormi o sprecherebbe la maggior parte della soglia. Una
    voce piu grande della soglia finisce da sola nel proprio lotto: e' il caso
    normale per le voci lunghe, non un errore.
    """
    batches = []
    current, current_bytes = [], 0
    for path in paths:
        size = path.stat().st_size
        too_big = current_bytes + size > max_bytes
        too_many = len(current) >= max_entries
        if current and (too_big or too_many):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def split_batch_answer(answer, page_ids):
    """Divide la risposta a lotti in {page_id: traduzione}.

    Restituisce solo i blocchi riconosciuti: un `page_id` assente dal risultato
    dice al chiamante che quella voce va ritradotta da sola. Gli identificativi
    inattesi si scartano - accettarli significherebbe scrivere una traduzione su
    un file che non le corrisponde.
    """
    fenced = FENCE.match(answer.strip())
    text = (fenced.group(1) if fenced else answer).strip()

    matches = list(MARKER_RE.finditer(text))
    if not matches:
        return {}

    wanted = set(page_ids)
    blocks = {}
    for index, match in enumerate(matches):
        page_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        # Un delimitatore ripetuto per la stessa voce e' ambiguo: senza sapere
        # quale blocco sia quello buono, si preferisce ritradurla da sola.
        if page_id in blocks:
            blocks[page_id] = None
        elif page_id in wanted and body:
            blocks[page_id] = body
    return {pid: body for pid, body in blocks.items() if body}


def _translate_batch(paths, assistant):
    """Traduce un lotto di file. Restituisce i path effettivamente tradotti.

    Le voci che la CLI non ha restituito - o che ha restituito in modo
    ambiguo - non vengono toccate: il chiamante le ritraduce una a una. Meglio
    ripetere il lavoro che scrivere la traduzione di una voce sul file di
    un'altra, perche' `translated.txt` la marcherebbe come fatta e l'errore non
    verrebbe piu' notato.
    """
    if len(paths) == 1:
        return [paths[0]] if _translate_one(paths[0], assistant) else []

    originals = {path.stem: path.read_text(encoding="utf-8") for path in paths}
    body = "\n\n".join(
        f"{MARKER.format(page_id=path.stem)}\n{originals[path.stem]}"
        for path in paths
    )

    try:
        answer = assistant.ask(BATCH_PROMPT.format(count=len(paths), text=body))
    except UsageLimitError as exc:
        raise LimitReached(
            f"'{assistant.name}' segnala che sono stati superati i limiti "
            "d'uso. Rilancia lo script quando il limite si azzera."
        ) from exc
    except Exception as exc:
        print(f"  lotto di {len(paths)} voci: {exc}", flush=True)
        return []

    blocks = split_batch_answer(answer, [path.stem for path in paths])

    translated = []
    for path in paths:
        result = blocks.get(path.stem)
        # Una traduzione identica all'originale significa che la CLI ha copiato
        # il testo invece di tradurlo: vale come non fatta, esattamente come nel
        # percorso a voce singola.
        if not result or result == originals[path.stem].strip():
            continue
        path.write_text(result + "\n", encoding="utf-8")
        translated.append(path)
    return translated


def process_group(workdir, group, entries, assistant, lang="en"):
    """Estrae e traduce le voci a lotti, alternando i due passi.

    Si scarica un lotto di voci e lo si traduce con una sola invocazione della
    CLI, poi si passa al successivo. Il costo fisso di ogni invocazione - avvio
    del processo e preambolo di sistema - domina su una voce mediana da poche
    centinaia di token, quindi raggrupparle lo ammortizza.

    L'alternanza fra i due passi resta: scaricare l'intero gruppo in blocco
    significherebbe mandare a Wikipedia centinaia di richieste ravvicinate, che
    finiscono per incontrare il rate limit (`429 Too Many Requests`). Con i lotti
    pero' il ritmo non e' piu' regolato dalla lentezza della CLI, ma solo da
    `FETCH_PAUSE`.
    """
    destination = workdir.group_dir(group)
    destination.mkdir(parents=True, exist_ok=True)
    done = workdir.translated_ids(group)

    pending = [(pid, title) for pid, title in entries if pid not in done]
    if not pending:
        print(f"Le {len(entries)} voci del gruppo sono già tradotte.")
        return 0

    print(f"Elaboro {len(pending)} voci con '{assistant.name}'.", flush=True)

    titles = dict(pending)
    translated = skipped = 0
    done_count = 0

    def stop_and_commit():
        if workdir.commit_all(f"traduzioni: {group}, stop per limiti"):
            workdir.push()

    for chunk in _chunks(pending, BATCH_MAX_ENTRIES):
        # 1. Estrazione: si scarica il gruppetto, saltando le voci sparite.
        paths = []
        for page_id, title in chunk:
            # Una voce gia' su disco non tocca Wikipedia: non va contata per la
            # pausa, altrimenti riprendere un gruppo estratto a meta' pagherebbe
            # un'attesa per ogni file che c'e' gia'.
            cached = (destination / f"{page_id}.md").exists()
            try:
                path = _extract_one(destination, page_id, title, lang)
            except LimitReached:
                stop_and_commit()
                raise
            done_count += 1
            progress = f"[{done_count}/{len(pending)}]"
            if path is None:
                skipped += 1
                print(f"  {progress} non disponibile {title}", flush=True)
                continue
            # Si annuncia ogni voce, non solo quelle che falliscono: fra un
            # download e l'altro passa una pausa, e senza una riga per ciascuna
            # lo script sembrerebbe fermo per tutto il gruppetto. Le voci gia'
            # su disco non si annunciano: non c'e' nessuna attesa da spiegare.
            if not cached:
                print(f"  {progress} scarico {title}", flush=True)
            paths.append(path)
            if not cached:
                time.sleep(FETCH_PAUSE)

        if not paths:
            continue

        # 2. Traduzione: un lotto per volta, per stare sotto la soglia di byte.
        # Il raggruppamento non si annuncia: e' un dettaglio di come si chiama
        # la CLI, e le voci tradotte si elencano comunque una per una.
        for batch in make_batches(paths):
            try:
                ok = _translate_batch(batch, assistant)
            except LimitReached:
                stop_and_commit()
                raise

            # Le voci che il lotto non ha reso si ritraducono una a una: una
            # risposta incompleta o ambigua non deve far perdere l'intero lotto.
            missing = [path for path in batch if path not in ok]
            if missing and len(batch) > 1:
                print(
                    f"  {len(missing)} voci su {len(batch)} non tornate dal "
                    "lotto: le ritraduco singolarmente.",
                    flush=True,
                )
                for path in missing:
                    try:
                        if _translate_one(path, assistant):
                            ok.append(path)
                    except LimitReached:
                        stop_and_commit()
                        raise

            # translated.txt si aggiorna per voce, non per lotto: la ripresa
            # resta granulare anche se un lotto va a meta'.
            for path in ok:
                workdir.mark_translated(group, path.stem)
                translated += 1
                print(
                    f"  [{translated}/{len(pending)}] tradotta "
                    f"{titles.get(path.stem) or _label(path)}",
                    flush=True,
                )

            # Si pubblica a fine lotto, non ogni N voci: il lotto e' l'unita' di
            # lavoro effettiva, quindi il commit corrisponde a qualcosa di
            # concluso invece di tagliare a meta' una traduzione in corso. Un
            # lotto che non ha reso nulla non produce un commit vuoto.
            if ok:
                workdir.commit_all(
                    f"traduzioni: {group}, {translated} voci"
                )
                workdir.push()
                print(f"  … {translated} voci pubblicate sul fork", flush=True)

    if workdir.commit_all(f"traduzioni: {group}, {translated} voci tradotte"):
        workdir.push()
    print(f"Tradotte {translated} voci, saltate {skipped}.")
    return translated
