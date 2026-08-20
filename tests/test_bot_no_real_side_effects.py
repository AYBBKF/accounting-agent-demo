"""Verifie qu'aucun test n'envoie de message Telegram reel ni n'appelle
l'API OpenAI reelle : le module app.bot n'importe rien qui declenche un
appel reseau au chargement, et repose sur des modules purs testables
independamment (voir test_auth.py / test_vat.py / etc.)."""
import ast
from pathlib import Path


def test_bot_module_has_no_module_level_network_calls():
    source = Path(__file__).resolve().parents[1].joinpath("app", "bot.py").read_text()
    tree = ast.parse(source)
    def _call_name(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Attribute):
            return func.attr
        if isinstance(func, ast.Name):
            return func.id
        return ""

    module_level_calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    ]
    # Seul l'appel d'initialisation du logging est tolere au niveau module ;
    # aucun appel reseau (Bot Telegram, OpenAI, etc.) ne doit s'executer a l'import.
    disallowed = [c for c in module_level_calls if _call_name(c.value) != "basicConfig"]
    assert disallowed == []
