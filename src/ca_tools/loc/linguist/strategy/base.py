from abc import ABC, abstractmethod

from ..blob import Blob


class BaseStrategy(ABC):
    @abstractmethod
    def call(self, blob: Blob, candidates: list):
        """Detect language based on some strategy."""
