import os
import urllib.parse

from .config.load import load_config

UnknownLanguage = {
    "name": "Unknown",
    "type": "unknown",
    "color": "#ededed",
    "ace_mode": "text",
    "language_id": -1,
}


class Language:
    _languages: list["Language"] = []
    _index: dict[str, "Language"] = {}
    _name_index: dict[str, "Language"] = {}
    _alias_index: dict[str, "Language"] = {}
    _language_id_index: dict[str, "Language"] = {}
    _extension_index: dict[str, list["Language"]] = {}
    _interpreter_index: dict[str, list["Language"]] = {}
    _filename_index: dict[str, list["Language"]] = {}
    _data_loaded: bool = False
    _popular_languages: set[str] = set()

    def __init__(self, attributes):
        self.name = attributes.get("name")
        if not self.name:
            raise ValueError("Missing name for Language")

        self.fs_name = attributes.get("fs_name")
        self.type = attributes.get("type")
        self.color = attributes.get("color")

        if self.type and self.type not in {"data", "markup", "programming", "prose", "unknown"}:
            raise ValueError(f"Invalid type: {self.type}")

        self.aliases = [self.default_alias()] + (attributes.get("aliases", []))
        self.tm_scope = attributes.get("tm_scope", "none")
        self.ace_mode = attributes.get("ace_mode")
        self.codemirror_mode = attributes.get("codemirror_mode")
        self.codemirror_mime_type = attributes.get("codemirror_mime_type")
        self.wrap = attributes.get("wrap", False)
        self.language_id = attributes.get("language_id")
        self.extensions = attributes.get("extensions", [])
        self.interpreters = attributes.get("interpreters", [])
        self.filenames = attributes.get("filenames", [])
        self.is_popular = attributes.get("popular", False)
        self.group = attributes.get("group", self.name)

    @classmethod
    def by_type(cls, lang_type):
        return [lang for lang in cls._languages if lang.type == lang_type]

    @classmethod
    def create(cls, attributes):
        language = cls(attributes)
        cls._languages.append(language)

        if language.name.lower() in cls._name_index:
            raise ValueError(f"Duplicate language name: {language.name}")

        cls._index[language.name.lower()] = cls._name_index[language.name.lower()] = language

        for alias in language.aliases:
            if alias.lower() in cls._alias_index:
                raise ValueError(f"Duplicate alias: {alias}")
            cls._index[alias.lower()] = cls._alias_index[alias.lower()] = language

        for extension in language.extensions:
            if not extension.startswith("."):
                raise ValueError(f"Extension must start with '.': {extension}")
            cls._extension_index.setdefault(extension.lower(), []).append(language)

        for interpreter in language.interpreters:
            cls._interpreter_index.setdefault(interpreter.lower(), []).append(language)

        for filename in language.filenames:
            cls._filename_index.setdefault(filename.lower(), []).append(language)

        if language.language_id is not None:
            cls._language_id_index[language.language_id] = language

        return language

    @classmethod
    def load_languages(cls):
        if cls._data_loaded:
            return

        cls._popular_languages = set(load_config("popular"))
        languages = load_config("languages")

        cls.create(UnknownLanguage)

        for name, options in languages.items():
            options["name"] = name
            options.setdefault("extensions", [])
            options.setdefault("interpreters", [])
            options.setdefault("filenames", [])
            options["popular"] = name in cls._popular_languages
            cls.create(options)

        cls._data_loaded = True

    @classmethod
    def all(cls):
        return cls._languages

    @classmethod
    def find_by_name(cls, name):
        if not isinstance(name, str) or not name.strip():
            return None
        return cls._name_index.get(name.lower()) or cls._name_index.get(name.split(",", 1)[0].strip().lower())

    @classmethod
    def find_by_alias(cls, alias):
        if not isinstance(alias, str) or not alias.strip():
            return None
        return cls._alias_index.get(alias.lower()) or cls._alias_index.get(alias.split(",", 1)[0].strip().lower())

    @classmethod
    def find_by_filename(cls, filename):
        basename = os.path.basename(filename)
        return cls._filename_index.get(basename.lower(), [])

    @classmethod
    def find_by_extension(cls, filename):
        ext = os.path.splitext(filename)[1].lower()
        return cls._extension_index.get(ext, [])

    @classmethod
    def find_by_interpreter(cls, interpreter):
        return cls._interpreter_index.get(interpreter.lower(), [])

    @classmethod
    def find_by_id(cls, language_id):
        return cls._language_id_index.get(language_id)

    @classmethod
    def popular(cls):
        return sorted([lang for lang in cls._languages if lang.is_popular], key=lambda lang: lang.name.lower())

    @classmethod
    def unpopular(cls):
        return sorted([lang for lang in cls._languages if not lang.is_popular], key=lambda lang: lang.name.lower())

    @classmethod
    def colors(cls):
        return sorted([lang for lang in cls._languages if lang.color], key=lambda lang: lang.name.lower())

    @property
    def key(self) -> str:
        """Canonical lowercase id used across the rest of the system.

        Matches what ``LanguageRegistry.register`` and ``_detect_language``
        already produce via ``name.lower()``. Use this anywhere a language
        needs to cross a module boundary (adapter lookup, index ``S_LANG``
        column, finding tags) instead of re-lowercasing ad hoc.
        """
        return self.name.lower()

    def default_alias(self):
        return self.name.lower().replace(" ", "-")

    def escaped_name(self):
        return urllib.parse.quote(self.name).replace("+", "%20")

    def __repr__(self):
        return f"<Language name={self.name}>"

    @classmethod
    def popular_languages(cls):
        return cls._popular_languages


Language.load_languages()
