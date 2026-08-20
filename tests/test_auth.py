from app.auth import is_allowed_telegram_user


def test_allowed_user_passes():
    assert is_allowed_telegram_user(123, {123, 456}) is True


def test_unknown_user_rejected():
    assert is_allowed_telegram_user(999, {123, 456}) is False


def test_none_user_rejected():
    assert is_allowed_telegram_user(None, {123}) is False


def test_empty_whitelist_fails_closed():
    # Liste blanche vide = personne n'est autorise (fail-closed), pas un service public.
    assert is_allowed_telegram_user(123, set()) is False
