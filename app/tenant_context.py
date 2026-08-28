"""Contexte d'execution d'UNE entreprise, valide avant toute ecriture.

Rien n'ecrit dans un classeur ou un dossier Drive sans passer par ici. Une
fonction d'ecriture qui accepterait un simple `sheet_id` ne pourrait pas
distinguer le classeur de XBLASTE de celui de Flux Intelligent : il
suffirait d'une variable mal passee pour ecrire une facture dans la
comptabilite d'un autre client. Le contexte porte donc l'identite, la
destination ET les regles comptables ensemble, et il ne se construit pas
si l'entreprise n'a pas le droit d'ecrire.

Il expose aussi les recherches du magasin d'etat DEJA enfermees dans
l'entreprise : le code appelant ne peut pas oublier de passer le
`company_id`, puisqu'il ne le manipule jamais.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterator

from app import companies as registry
from app import doc_store as store


class TenantError(RuntimeError):
    """Contexte impossible a construire pour cette entreprise."""


class TenantNotWritable(TenantError):
    """L'entreprise existe mais n'a pas le droit d'ecrire.

    Volontairement distincte de `TenantError` : l'appelant doit pouvoir
    mettre la piece en quarantaine plutot que d'echouer, sans confondre
    ce cas avec une entreprise inconnue.
    """


@dataclass(frozen=True)
class TenantContext:
    """Tout ce qu'il faut pour traiter une piece d'UNE entreprise.

    Immuable : un contexte construit pour XBLASTE ne peut pas etre
    detourne en cours de traitement vers une autre comptabilite.
    """

    company_id: str
    display_name: str
    sheet_id: str
    drive_folder_id: str
    telegram_chat_id: str
    allowed_vat_rates: tuple[Decimal, ...]
    currency: str
    db_path: str
    chat_id: int

    # -- construction ------------------------------------------------------

    @classmethod
    def for_company(
        cls, db_path: str, company_id: str, *, chat_id: int | None = None
    ) -> "TenantContext":
        entreprise = registry.get_company(db_path, company_id)
        if entreprise is None:
            raise TenantError(f"entreprise inconnue : '{company_id}'")
        if not entreprise.can_write:
            manquants = entreprise.missing_for_activation
            detail = f"statut {entreprise.status}"
            if manquants:
                detail += f", il manque : {', '.join(manquants)}"
            raise TenantNotWritable(
                f"l'entreprise '{entreprise.company_id}' ne peut pas ecrire ({detail})"
            )
        cible = chat_id
        if cible is None:
            try:
                cible = int(entreprise.telegram_chat_id)
            except (TypeError, ValueError) as exc:
                raise TenantError(
                    f"l'entreprise '{entreprise.company_id}' n'a pas de canal "
                    "de notification exploitable"
                ) from exc
        return cls(
            company_id=entreprise.company_id,
            display_name=entreprise.display_name or entreprise.company_id,
            sheet_id=entreprise.sheet_id,
            drive_folder_id=entreprise.drive_folder_id,
            telegram_chat_id=str(entreprise.telegram_chat_id),
            allowed_vat_rates=entreprise.allowed_vat_rates,
            currency=entreprise.currency,
            db_path=db_path,
            chat_id=int(cible),
        )

    # -- recherches, toujours enfermees dans l'entreprise ------------------

    def claim_document(self, doc_key: str, **champs: Any) -> bool:
        return store.claim_document(
            self.db_path, doc_key, self.chat_id,
            company_id=self.company_id, **champs,
        )

    def find_by_sha256(self, file_sha256: str) -> dict[str, Any] | None:
        return store.find_by_sha256(
            self.db_path, self.chat_id, file_sha256, company_id=self.company_id
        )

    def find_open_twin(
        self, file_sha256: str, *, exclude_key: str = ""
    ) -> dict[str, Any] | None:
        return store.find_open_twin(
            self.db_path, self.chat_id, file_sha256,
            exclude_key=exclude_key, company_id=self.company_id,
        )

    def find_by_business_key(self, doc_type: str, numero: str) -> dict[str, Any] | None:
        return store.find_by_business_key(
            self.db_path, self.chat_id, doc_type, numero, company_id=self.company_id
        )

    def find_by_message_and_sha(
        self, gmail_message_id: str, file_sha256: str
    ) -> dict[str, Any] | None:
        return store.find_by_message_and_sha(
            self.db_path, self.chat_id, gmail_message_id, file_sha256,
            company_id=self.company_id,
        )

    def list_quarantined(self) -> list[dict[str, Any]]:
        return store.list_quarantined(
            self.db_path, self.chat_id, company_id=self.company_id
        )

    def claim_bank_line(
        self, fingerprint: str, row_index: int = 0, doc_key: str = ""
    ) -> bool:
        return store.claim_bank_line(
            self.db_path, self.chat_id, fingerprint, row_index, doc_key,
            company_id=self.company_id,
        )

    def cursor(self, now_epoch: int) -> dict[str, Any]:
        return store.get_or_init_cursor(
            self.db_path, self.chat_id, now_epoch, company_id=self.company_id
        )

    def advance_cursor(
        self, last_internal_date: int, history_id: str | None = None
    ) -> None:
        store.advance_cursor(
            self.db_path, self.chat_id, last_internal_date, history_id,
            company_id=self.company_id,
        )

    def notification_signature(self, message_id: str) -> str:
        return store.email_notification_signature(
            self.db_path, self.chat_id, message_id, company_id=self.company_id
        )

    def remember_notification(self, message_id: str, signature: str) -> None:
        store.remember_email_notification(
            self.db_path, self.chat_id, message_id, signature,
            company_id=self.company_id,
        )

    # -- journalisation ----------------------------------------------------

    def log_prefix(self) -> str:
        """Prefixe a poser sur CHAQUE ligne de journal de ce traitement.

        Sans lui, un incident multi-entreprises devient illisible : on voit
        des erreurs, sans savoir quelle comptabilite est touchee.
        """
        return f"[{self.company_id}]"


# --- verrous par entreprise ----------------------------------------------
#
# Une entreprise en erreur, en quota ou lente ne doit pas retarder les
# autres. Un verrou global les serialiserait toutes derriere la plus
# lente ; un verrou par entreprise garde l'exclusion la ou elle est
# necessaire - deux cycles ne doivent pas traiter la meme comptabilite en
# parallele - sans bloquer les voisines.

class TenantLocks:
    """Distributeur de verrous, un par entreprise, cree a la demande."""

    def __init__(self) -> None:
        self._verrous: dict[str, threading.Lock] = {}
        self._garde = threading.Lock()

    def lock_for(self, company_id: str) -> threading.Lock:
        identifiant = registry.normalize_company_id(company_id)
        with self._garde:
            if identifiant not in self._verrous:
                self._verrous[identifiant] = threading.Lock()
            return self._verrous[identifiant]

    @contextmanager
    def hold(self, company_id: str, *, timeout: float = -1) -> Iterator[bool]:
        """Prend le verrou d'UNE entreprise.

        Rend False si le verrou n'a pas pu etre pris dans le delai : un
        cycle qui trouve l'entreprise deja en cours passe a la suivante
        au lieu d'attendre, ce qui garde les autres comptabilites fluides.
        """
        verrou = self.lock_for(company_id)
        pris = verrou.acquire(timeout=timeout) if timeout >= 0 else verrou.acquire()
        try:
            yield pris
        finally:
            if pris:
                verrou.release()

    @property
    def known_companies(self) -> tuple[str, ...]:
        with self._garde:
            return tuple(sorted(self._verrous))
