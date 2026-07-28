"""Traduttore EN→IT delle voci di Wikipedia. Vedi issue #3."""
import sys


def force_utf8_output():
    """Fa scrivere stdout e stderr in UTF-8 su qualunque piattaforma.

    Mentre si lavora si stampa il titolo di ogni voce, e il grosso dei gruppi
    ne contiene almeno uno che cp1252 non sa rappresentare. Su Windows, quando
    l'uscita non è la console ma un file o una pipe, Python usa l'encoding di
    sistema e `print` interrompe tutto con UnicodeEncodeError: basta tenere un
    registro del lavoro con `uv run traduci.py > log.txt` per incontrarlo.

    È lo stesso difetto delle letture, ma in scrittura, e qui non si può
    dichiarare l'encoding a ogni `print`: lo si dichiara una volta sul flusso.

    Si cambia l'encoding e nient'altro. `reconfigure` riporta `errors` al
    valore di default quando non glielo si passa, e `stderr` nasce con
    `backslashreplace` apposta, perché un messaggio d'errore deve poter uscire
    sempre: riportarlo a `strict` significherebbe perdere proprio la
    diagnostica, che è l'opposto di quel che si vuole.
    """
    for stream in (sys.stdout, sys.stderr):
        # Sotto test o dietro una cattura, i flussi possono non essere file di
        # testo riconfigurabili: in quel caso non c'è niente da sistemare.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=stream.errors)
