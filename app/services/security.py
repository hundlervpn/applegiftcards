import bcrypt

_BCRYPT_ROUNDS = 12
_TIMING_EQUALIZER_PASSWORD = b"awe-timing-equalizer"
# Реального хеша несуществующего пользователя нет, но ответ должен стоить
# столько же, сколько проверка существующего, иначе по времени ответа видно,
# что email не зарегистрирован.
_DUMMY_HASH = bcrypt.hashpw(_TIMING_EQUALIZER_PASSWORD, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(rounds=_BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if hashed is None:
        bcrypt.checkpw(_TIMING_EQUALIZER_PASSWORD, _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False
