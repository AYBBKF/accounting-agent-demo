"""Borne de date envoyee a Gmail avec la requete de surveillance.

Deux pieges reels, verifies contre l'API Gmail :

  - `after:0` ne rend AUCUN message : la borne est rejetee, pas
    interpretee comme "depuis toujours" ;
  - deux bornes `after:` dans la meme requete rendent un resultat VIDE.

Une borne posee par l'exploitant dans la requete configuree etait donc
annulee par celle du curseur : le worker ne trouvait plus jamais rien, en
silence et sans erreur.
"""
from app.mail_worker import MailWorker


class Stub:
    """Juste ce qu'il faut pour evaluer effective_query, sans reseau."""

    def __init__(self, query, floor):
        self._query = query
        self._floor = floor

    def cursor(self):
        return {"last_internal_date": self._floor}


def effective(query, floor):
    return MailWorker.effective_query(Stub(query, floor))


BASE = "label:X has:attachment"


def test_a_configured_after_bound_is_not_doubled():
    """Deux `after:` = resultat vide : celui de l'exploitant fait foi."""
    query = f"{BASE} after:1787871600"
    rendu = effective(query, 1787900000)
    assert rendu == query
    assert rendu.count("after:") == 1


def test_other_date_operators_are_respected_too():
    for borne in ("before:1787871600", "newer_than:2d", "older_than:1y"):
        query = f"{BASE} {borne}"
        assert effective(query, 1787900000) == query


def test_a_query_without_date_bound_still_gets_the_cursor_bound():
    from app import doc_store as store

    date = 1787871600
    attendu = store.query_floor({"last_internal_date": date})
    assert attendu > 0
    assert effective(BASE, date) == f"{BASE} after:{attendu}"


def test_a_zero_floor_never_produces_after_zero():
    """`after:0` ne rend rien : mieux vaut aucune borne du tout."""
    assert effective(BASE, 0) == BASE
    assert "after:" not in effective(BASE, 0)
