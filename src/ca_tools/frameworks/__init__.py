"""Framework-specific hooks — imported to register pipeline hooks.

Each module registers its own hooks via @hook decorators.
Importing this package activates all framework hooks.
"""

from ca_tools.frameworks import alembic, django, fastapi, pytest, python  # noqa: F401
