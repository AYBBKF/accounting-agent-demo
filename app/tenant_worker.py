"""Un seul processus, plusieurs comptabilites.

Ce module est le point ou l'agent cesse d'etre mono-entreprise. Il ne
duplique PAS le worker : il en garde exactement un par entreprise, et se
contente de decider, email par email, lequel a le droit de travailler.

Trois choix structurent tout le reste.

  * UN SEUL SONDEUR GMAIL. La boite est unique et partagee entre les
    alias ; N workers qui interrogeraient Gmail chacun de leur cote
    consommeraient N fois le quota, se marcheraient dessus sur le curseur
    et pourraient traiter deux fois le meme email. Le sondage est donc
    fait ici, une fois, puis chaque message est confie a l'entreprise que
    le routage a designee.

  * LE ROUTAGE DECIDE AVANT TOUT TRAVAIL. Aucun octet n'est telecharge,
    aucun appel LLM n'est emis, aucune ligne n'est ecrite tant qu'une
    entreprise n'a pas ete identifiee par les EN-TETES DE LIVRAISON. Un
    email non routable est mis en quarantaine tel quel : il ne coute
    rien et ne salit aucune comptabilite. C'est la seconde barriere -
    celle qui refuse les emails de verification - et elle reste entiere.

  * L'ISOLATION EST PORTEE PAR LE CONTEXTE, PAS PAR LA DISCIPLINE. Le
    worker d'une entreprise est construit a partir de son
    `TenantContext` : classeur, dossier Drive, canal Telegram, taux de
    TVA et `company_id` viennent tous du registre, ensemble. Il n'existe
    aucun chemin ou l'on passerait "juste un sheet_id" a une fonction
    d'ecriture.

Le curseur Gmail merite une explication. Il est stocke PAR ENTREPRISE,
mais la recherche Gmail est commune : la borne de date envoyee a Gmail
est donc le PLUS ANCIEN des curseurs. Prendre le plus recent ferait
disparaitre en silence les emails d'une entreprise en retard - une
comptabilite entiere sautee sans la moindre erreur. Chaque entreprise
n'avance ensuite que son propre curseur, et seulement sur les emails qui
lui ont ete attribues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app import companies as registry
from app import routing
from app.mail_worker import MailSummary, MailWorker
from app.tenant_context import TenantContext, TenantError, TenantNotWritable

logger = logging.getLogger(__name__)


# Motifs de mise en quarantaine d'un EMAIL (et non d'un document).
QUARANTINE_UNROUTABLE = "email-non-routable"
QUARANTINE_NOT_WRITABLE = "entreprise-non-ecrivable"


@dataclass
class RoutedEmail:
    """Trace de ce qu'un email est devenu, routage compris.

    Sert de preuve d'isolation : pour chaque message on sait quelle
    entreprise l'a pris, sur quel en-tete, et sinon pourquoi il a ete
    refuse.
    """

    message_id: str
    outcome: str
    company_id: str = ""
    source: str = ""
    reason: str = ""
    summary: MailSummary | None = None

    @property
    def processed(self) -> bool:
        return self.summary is not None


@dataclass
class CycleReport:
    """Resultat d'UN tour de boucle, entreprise par entreprise."""

    seen: int = 0
    routed: dict[str, int] = field(default_factory=dict)
    quarantined: list[RoutedEmail] = field(default_factory=list)
    emails: list[RoutedEmail] = field(default_factory=list)
    skipped_busy: tuple[str, ...] = ()

    @property
    def summaries(self) -> list[MailSummary]:
        return [e.summary for e in self.emails if e.summary is not None]

    def for_company(self, company_id: str) -> list[RoutedEmail]:
        return [e for e in self.emails if e.company_id == company_id]


class TenantWorker:
    """Sonde Gmail une fois, puis confie chaque email a son entreprise."""

    def __init__(
        self,
        *,
        api_key: str,
        chat_id: int,
        db_path: str,
        query: str,
        poll_seconds: int = 60,
        max_per_cycle: int = 5,
        zip_limits: Any | None = None,
        vision: Any | None = None,
        vision_max_calls: int = 0,
        locks: Any | None = None,
        worker_factory: Callable[..., MailWorker] | None = None,
    ) -> None:
        if not str(query or "").strip():
            # Un fallback silencieux vers une requete plus large ferait
            # entrer en comptabilite des emails de test et des messages
            # sans rapport. On refuse de demarrer plutot que d'elargir.
            raise ValueError(
                "aucune requete Gmail configuree : refus de demarrer avec "
                "une requete par defaut plus large"
            )
        self._api_key = api_key
        self._chat_id = chat_id
        self._db_path = db_path
        self._query = query
        self._poll_seconds = poll_seconds
        self._max_per_cycle = max_per_cycle
        self._zip_limits = zip_limits
        self._vision = vision
        self._vision_max_calls = vision_max_calls
        from app.tenant_context import TenantLocks

        self._locks = locks if locks is not None else TenantLocks()
        self._workers: dict[str, MailWorker] = {}
        self._factory = worker_factory or self._build_worker
        # Sondeur unique : il ne sert qu'a interroger Gmail et n'a AUCUNE
        # entreprise, donc aucun classeur et aucun droit d'ecriture.
        self._probe = MailWorker(
            api_key=api_key, chat_id=chat_id, db_path=db_path,
            query=query, poll_seconds=poll_seconds,
            max_per_cycle=max_per_cycle, zip_limits=zip_limits,
        )

    # -- proprietes --------------------------------------------------------

    @property
    def query(self) -> str:
        return self._query

    @property
    def poll_seconds(self) -> int:
        return self._poll_seconds

    @property
    def probe(self) -> MailWorker:
        return self._probe

    def active_companies(self) -> tuple[str, ...]:
        """Entreprises qui ont le droit d'ecrire, a cet instant."""
        return tuple(
            c.company_id
            for c in registry.list_companies(self._db_path)
            if c.can_write
        )

    # -- workers par entreprise -------------------------------------------

    def _build_worker(self, tenant: TenantContext) -> MailWorker:
        return MailWorker(
            api_key=self._api_key,
            chat_id=tenant.chat_id,
            db_path=self._db_path,
            spreadsheet_id=tenant.sheet_id,
            query=self._query,
            poll_seconds=self._poll_seconds,
            company_name=tenant.display_name,
            drive_folder=tenant.drive_folder_id,
            max_per_cycle=self._max_per_cycle,
            zip_limits=self._zip_limits,
            allowed_vat_rates=tenant.allowed_vat_rates or None,
            vision=self._vision,
            vision_max_calls=self._vision_max_calls,
            company_id=tenant.company_id,
            account_mapping=tenant.account_mapping,
            company_ice=tenant.ice,
        )

    def worker_for(self, company_id: str) -> MailWorker:
        """Worker DE cette entreprise, construit a partir du registre.

        Leve `TenantNotWritable` si l'entreprise existe mais n'a pas de
        classeur, pas de dossier Drive ou n'est pas ACTIVE : mieux vaut
        une quarantaine explicite qu'une ecriture au mauvais endroit.
        """
        identifiant = registry.normalize_company_id(company_id)
        connu = self._workers.get(identifiant)
        if connu is not None:
            return connu
        tenant = TenantContext.for_company(self._db_path, identifiant)
        worker = self._factory(tenant)
        if worker.company_id != tenant.company_id:
            raise TenantError(
                "worker construit pour la mauvaise entreprise : "
                f"{worker.company_id!r} au lieu de {tenant.company_id!r}"
            )
        self._workers[identifiant] = worker
        return worker

    # -- curseur commun ----------------------------------------------------

    def oldest_floor(self) -> int:
        """Borne de date a envoyer a Gmail : le PLUS ANCIEN des curseurs.

        Prendre le plus recent ferait sauter en silence les emails d'une
        entreprise en retard.
        """
        from datetime import datetime, timezone

        from app import doc_store as store

        maintenant = int(datetime.now(timezone.utc).timestamp())
        bornes: list[int] = []
        for company_id in self.active_companies():
            try:
                tenant = TenantContext.for_company(self._db_path, company_id)
            except TenantError:
                continue
            bornes.append(store.query_floor(tenant.cursor(maintenant)))
        valides = [b for b in bornes if b > 0]
        if not valides or len(valides) < len(bornes):
            # Une entreprise sans borne exploitable veut dire "depuis
            # toujours" : on n'impose alors aucune borne commune.
            return 0
        return min(valides)

    def effective_query(self) -> str:
        """Requete reellement envoyee. Journalisee, jamais elargie."""
        from app.mail_worker import _HAS_DATE_BOUND

        if _HAS_DATE_BOUND.search(self._query):
            return self._query
        floor = self.oldest_floor()
        if floor <= 0:
            return self._query
        return f"{self._query} after:{floor}"

    def search_messages(self) -> list[dict[str, Any]]:
        data = self._probe.execute(
            "GMAIL_FETCH_EMAILS",
            {"query": self.effective_query(), "max_results": self._max_per_cycle},
        )
        return data.get("messages") or []

    # -- cycle -------------------------------------------------------------

    def process_once(self) -> CycleReport:
        """Un tour : un sondage Gmail, puis un traitement par entreprise."""
        logger.info("Requete Gmail chargee (multi-tenant) : %s", self.effective_query())
        rapport = CycleReport()
        occupees: list[str] = []
        messages = self.search_messages()
        rapport.seen = len(messages)
        # Curseur par entreprise : on ne fait avancer celui d'une
        # entreprise que sur les emails qui lui ont ete ATTRIBUES.
        plus_recent: dict[str, int] = {}

        for meta in messages:
            message_id = str(meta.get("messageId") or meta.get("id") or "")
            if not message_id:
                continue
            entree = self._handle_message(message_id, plus_recent, occupees)
            rapport.emails.append(entree)
            if entree.company_id:
                rapport.routed[entree.company_id] = (
                    rapport.routed.get(entree.company_id, 0) + 1
                )
            if not entree.processed:
                rapport.quarantined.append(entree)

        for company_id, interne in plus_recent.items():
            if interne <= 0:
                continue
            try:
                TenantContext.for_company(self._db_path, company_id).advance_cursor(
                    interne
                )
            except TenantError as exc:  # pragma: no cover - defensif
                logger.warning("Curseur non avance pour %s : %s", company_id, exc)

        rapport.skipped_busy = tuple(dict.fromkeys(occupees))
        return rapport

    def _handle_message(
        self,
        message_id: str,
        plus_recent: dict[str, int],
        occupees: list[str],
    ) -> RoutedEmail:
        """Route UN email et, s'il est routable, le fait traiter."""
        try:
            message = self._probe.fetch_message(message_id)
        except Exception as exc:  # noqa: BLE001 - un email n'en bloque pas d'autres
            logger.warning("Email %s illisible : %s", message_id, exc)
            return RoutedEmail(
                message_id=message_id, outcome=routing.UNKNOWN_COMPANY,
                reason=f"email illisible : {exc}",
            )

        decision = routing.route_message(self._db_path, message)
        if not decision.accepted:
            # Rien n'est telecharge, rien n'est extrait, rien n'est facture.
            logger.warning(
                "Email %s NON route (%s) : %s",
                message_id, decision.outcome, decision.reason,
            )
            return RoutedEmail(
                message_id=message_id, outcome=decision.outcome,
                company_id=decision.company_id, source=decision.source,
                reason=decision.reason or QUARANTINE_UNROUTABLE,
            )

        company_id = decision.company_id
        try:
            worker = self.worker_for(company_id)
        except TenantNotWritable as exc:
            logger.warning("Email %s : %s", message_id, exc)
            return RoutedEmail(
                message_id=message_id, outcome=routing.NOT_WRITABLE,
                company_id=company_id, source=decision.source,
                reason=f"{QUARANTINE_NOT_WRITABLE} : {exc}",
            )
        except TenantError as exc:
            logger.warning("Email %s : %s", message_id, exc)
            return RoutedEmail(
                message_id=message_id, outcome=routing.UNKNOWN_COMPANY,
                company_id=company_id, source=decision.source, reason=str(exc),
            )

        # Verrou PAR ENTREPRISE : une comptabilite lente ou en erreur ne
        # retarde pas les autres, mais deux cycles ne traitent jamais la
        # meme en parallele.
        with self._locks.hold(company_id, timeout=0) as pris:
            if not pris:
                occupees.append(company_id)
                logger.info(
                    "Email %s : %s deja en cours de traitement, report au "
                    "cycle suivant.", message_id, company_id,
                )
                return RoutedEmail(
                    message_id=message_id, outcome=decision.outcome,
                    company_id=company_id, source=decision.source,
                    reason="entreprise deja en cours de traitement",
                )
            summary, interne = worker.process_message(message_id)

        plus_recent[company_id] = max(plus_recent.get(company_id, 0), interne)
        logger.info(
            "[%s] email %s attribue par %s", company_id, message_id, decision.source
        )
        return RoutedEmail(
            message_id=message_id, outcome=decision.outcome,
            company_id=company_id, source=decision.source, summary=summary,
        )


def companies_touched(reports: Iterable[CycleReport]) -> tuple[str, ...]:
    """Entreprises qui ont REELLEMENT traite quelque chose.

    Utile comme preuve de non-contamination : un cycle qui ne concernait
    qu'une entreprise ne doit en citer qu'une.
    """
    vues: dict[str, None] = {}
    for rapport in reports:
        for entree in rapport.emails:
            if entree.processed and entree.company_id:
                vues[entree.company_id] = None
    return tuple(vues)
