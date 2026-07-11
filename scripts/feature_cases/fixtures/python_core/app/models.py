class User:
    def __init__(self, name: str):
        self.name = name

    def save(self, record: str) -> str:
        return f"{self.name}:{record}"


class Other:
    def save(self, record: str) -> str:
        return record.upper()


def unused_helper() -> str:
    return "unused"


def referenced_helper() -> str:
    return "used"
