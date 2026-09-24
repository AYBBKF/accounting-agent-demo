"""Read-only operator dashboard. Loopback only; no browser-side writes."""
from __future__ import annotations

import base64
import hmac
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

from app.operations import snapshot


CSS = """
:root{font-family:Segoe UI,system-ui,sans-serif;color:#173442;background:#f2f6f5}
*{box-sizing:border-box}body{margin:0}header{background:#102b3c;color:white;padding:32px max(5vw,24px)}
header b{color:#9ce5cb;letter-spacing:.12em;font-size:12px}h1{font-size:32px;margin:12px 0}
main{max-width:1240px;margin:auto;padding:28px 24px}h2{margin:0 0 10px}h3{font-size:17px}
.muted{color:#576e78;font-size:13px}.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.badge{background:#e2ece8;border-radius:20px;padding:6px 12px;font-size:12px;display:inline-block}
.warn{background:#fff0d0;color:#78510d}.card{background:white;border:1px solid #dce6e1;border-radius:16px;padding:22px;margin:22px 0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:24px 0}
.stat{border-radius:10px;background:#f1f6f4;padding:16px}.stat strong{font-size:28px;display:block}
a{color:#086e76;text-decoration:underline}a:focus,summary:focus{outline:3px solid #3c9ec0;outline-offset:3px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:13px 10px;border-bottom:1px solid #e1e8e5;vertical-align:top}
th{background:#eef4f1}td:first-child{max-width:260px;overflow-wrap:anywhere}.scroll{overflow:auto}
details{margin:10px 0;background:#f6f8f7;padding:12px;border-radius:8px}summary{cursor:pointer;font-weight:600}
code{font-size:11px;overflow-wrap:anywhere}footer{padding:20px 0;color:#586c75;font-size:12px}
.reason{white-space:pre-wrap;overflow-wrap:anywhere}.links{display:flex;gap:18px;margin:16px 0}
@media(max-width:650px){.stats{grid-template-columns:repeat(2,1fr)}h1{font-size:26px}.card{padding:16px}main{padding:14px}}
"""


def e(value) -> str:
    return escape(str(value if value is not None else ''), quote=True)


def link(url: str, label: str) -> str:
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)}</a>' if url else '<span class="muted">Lien indisponible</span>'


def render(data: dict) -> str:
    health = data['health']
    labels = {'ok': 'Dernier cycle réussi', 'degraded': 'Cycle en échec',
              'processing': 'Traitement en cours', 'stalled': 'Traitement anormalement long — à vérifier',
              'stale': 'Suivi des cycles trop ancien', 'unknown': 'Suivi des cycles indisponible'}
    chunks = ['<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
              '<title>Suivi comptable | FLUX INTELLIGENT</title>', f'<style>{CSS}</style>',
              '<header><b>FLUX INTELLIGENT</b><h1>Votre suivi comptable</h1><p>Les dossiers, les exceptions et les preuves de traitement.</p></header><main>',
              '<div class="bar"><span class="badge ' + ('' if health['status'] == 'ok' else 'warn') + '">' +
              e(labels.get(health['status'], health['status'])) + '</span><span class="muted">Actualisé : ' + e(data['generated_at']) + '</span></div>',
              '<p class="muted">Consultez les dossiers et les pièces à examiner. Actualisez la page pour afficher les dernières informations.</p>']
    for co in data['companies']:
        counts = co['counts']
        queue = sum(counts.get(s,0) for s in ('needs_review', 'failed', 'partial'))
        chunks += ['<section class="card"><div class="bar"><h2>' + e(co['legal_name'] or co['company_id']) + '</h2>',
                   '<span class="badge">' + e(co['status']) + '</span></div><p class="muted">Dossier : ' + e(co['company_id']) + '</p>']
        if co['approval_required']:
            chunks.append('<p class="badge warn">Identité et paramétrage à valider avant activation</p>')
        chunks.append('<div class="links">' + link(co['sheet_link'], 'Ouvrir le classeur') + link(co['drive_link'], 'Ouvrir les archives') + '</div><div class="stats">')
        for title, count in [('Documents tracés',sum(counts.values())), ('Traitements terminés',counts.get('completed',0)),
                             ('À examiner',queue), ('Doublons / rattachés',counts.get('duplicate',0)+counts.get('superseded',0))]:
            chunks.append(f'<div class="stat"><span>{e(title)}</span><strong>{count}</strong></div>')
        chunks.append('</div><h3>File de revue</h3><p class="muted">Les traitements terminés ne sont pas tous des factures comptabilisées. Les états affichés proviennent du registre documentaire.</p>')
        if not co['reviews']:
            chunks.append('<p>Aucun document en attente dans cette file.</p>')
        else:
            chunks.append('<div class="scroll"><table><thead><tr><th>Document</th><th>État</th><th>Motif et justificatif</th></tr></thead><tbody>')
            for doc in co['reviews']:
                reasons = doc['reasons'] if isinstance(doc['reasons'], list) else [doc['reasons']]
                reason = '\n'.join(str(r) for r in reasons)
                if not reason:
                    reason = 'Erreur technique : consulter les journaux opérateur.' if doc['has_error'] else 'Motif à consulter dans 21_A_VERIFIER.'
                chunks.append('<tr><td><strong>' + e(doc['filename']) + '</strong><br>' + e(doc['numero']) + '<br><code>' + e(doc['doc_key']) + '</code></td><td>' +
                              e(doc['state']) + '</td><td><div class="reason">' + e(reason) + '</div><p>' + link(doc['drive_link'], 'Voir l’original') + '</p></td></tr>')
            chunks.append('</tbody></table></div>')
            if queue > len(co['reviews']):
                chunks.append(f'<p class="muted">{len(co["reviews"])} documents affichés sur {queue}. Le classeur contient le suivi détaillé.</p>')
        chunks.append('<details><summary>Consommation IA</summary>')
        if co['usage']:
            for u in co['usage']:
                chunks.append('<p>' + e(u['model']) + ' / ' + e(u['level']) + f' : {u["calls"]} appels, {u["input_tokens"]} tokens en entrée, {u["output_tokens"]} en sortie.</p>')
        else:
            chunks.append('<p>Aucun appel enregistré.</p>')
        chunks.append('<p class="muted">Les tokens ne constituent pas un prix facturé. Aucun coût nul supposé.</p></details>')
        chunks.append('<details><summary>Notes de revue et validations</summary>')
        for note in co['audit']:
            # Configuration approval audit contains legal data: the dashboard
            # displays the event, not the full configuration/chat destination.
            detail = note['detail'] if note['action'] == 'review_note' else 'Configuration approuvée par l’opérateur.'
            chunks.append('<p><strong>' + e(note['actor']) + '</strong> · ' + e(note['created_at']) + '</p><p class="reason">' + e(detail) + '</p>')
        if not co['audit']:
            chunks.append('<p>Aucune note ou validation enregistrée.</p>')
        chunks.append('</details><details><summary>Révision de configuration pour l’opérateur</summary><code>' + e(co['revision']) + '</code></details></section>')
    if not data['companies']:
        chunks.append('<p>Aucune entreprise enregistrée.</p>')
    chunks.append('<footer>Une note de revue ne valide pas une écriture. La correction de données comptables doit suivre le circuit du comptable.</footer></main></html>')
    return ''.join(chunks)


def make_server(db_path: str, company_id: str | None, port: int, token: str):
    if len(token) < 32:
        raise ValueError('OPERATIONS_TOKEN doit contenir au moins 32 caracteres')
    expected = 'Basic ' + base64.b64encode(('operator:' + token).encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # No Authorization headers, document URLs or user-supplied paths.

        def send(self, status: int, body: str):
            encoded = body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if status == 401:
                self.send_header('WWW-Authenticate', 'Basic realm="Operations", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), expected.encode('utf-8')):
                return self.send(401, 'Authentification requise.')
            if self.path != '/':
                return self.send(404, 'Page inconnue.')
            try:
                self.send(200, render(snapshot(db_path, company_id)))
            except Exception:
                self.send(503, 'Lecture temporairement indisponible. Consulter les journaux opérateur.')

        def do_POST(self):
            self.send(405, 'Lecture seule.')

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def serve(db_path: str, company_id: str | None, port: int):
    server = make_server(db_path, company_id, port, os.environ.get('OPERATIONS_TOKEN', ''))
    print(f'Console locale : http://127.0.0.1:{server.server_port} (utilisateur : operator)')
    try:
        server.serve_forever()
    finally:
        server.server_close()
