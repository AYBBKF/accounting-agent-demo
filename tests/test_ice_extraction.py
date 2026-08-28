"""Extraction de l'ICE : les VRAIES factures etiquettent souvent l'ICE
"ICE fournisseur : ..." / "ICE client : ...", pas seulement "ICE : ...".

Ce trou faisait tomber en quarantaine (« facture fournisseur sans ICE
exploitable ») une vraie facture propre dont l'ICE etait pourtant present
et lisible dans la couche texte. Les factures synthetiques, elles,
n'utilisaient que "ICE :", ce qui masquait le defaut.
"""
from app.doc_extract import _ICE_RE, build_lines, party


def test_ice_regex_reads_qualified_labels():
    assert _ICE_RE.search("ICE : 002345678000043").group(1) == "002345678000043"
    assert _ICE_RE.search("ICE fournisseur : 002345678000043").group(1) == "002345678000043"
    assert _ICE_RE.search("ICE client : 003456789000052").group(1) == "003456789000052"
    assert _ICE_RE.search("Numero ICE fournisseur 002345678000043").group(1) == "002345678000043"


def test_ice_regex_ignores_absent_ice():
    # Un ICE non renseigne (pas de chiffres) ne doit jamais matcher.
    assert _ICE_RE.search("ICE : NON RENSEIGNE") is None
    assert _ICE_RE.search("ICE fournisseur : NON RENSEIGNE") is None


def test_party_reads_supplier_ice_with_qualified_label():
    """Bloc fournisseur d'une vraie facture : nom + ICE etiquete."""
    pages = [
        "FACTURE FOURNISSEUR\n"
        "Fournisseur : ATLAS BUREAU SARL\n"
        "ICE fournisseur : 002345678000043\n"
        "Client : X BLASTE\n"
        "ICE client : 003456789000052\n"
    ]
    lines = build_lines(pages)
    nom, ice, _ = party(lines, ("Fournisseur",))
    assert nom == "ATLAS BUREAU SARL"
    assert ice == "002345678000043"          # ICE du bloc fournisseur, pas celui du client
    nom_c, ice_c, _ = party(lines, ("Client",))
    assert ice_c == "003456789000052"
