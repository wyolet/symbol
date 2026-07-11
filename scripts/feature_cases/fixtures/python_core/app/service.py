import functools

from app.models import Other, User, referenced_helper


@functools.cache
def cached_label() -> str:
    return referenced_helper()


def persist(user: User) -> str:
    return user.save(record="primary")


def persist_other(other: Other) -> str:
    return other.save(record="decoy")
