"""Row layout constants.

No wrapper classes. Rows are plain tuples. These names are the only
documentation of which position means what.
"""

# Symbol row tuple positions.
S_PATH = 0
S_FILE = 1
S_SLINE = 2
S_ELINE = 3
S_SBYTE = 4
S_EBYTE = 5
S_KIND = 6
S_LANG = 7
S_PARENT = 8

# Import row tuple positions.
I_FILE = 0
I_NAME = 1
I_SOURCE = 2
I_LINE = 3

# Ref row tuple positions.
R_SRC = 0
R_NAME = 1
R_KIND = 2
R_LINE = 3

# Ref kinds.
REF_NAME = 0   # free variable reference
REF_ATTR = 1   # attribute tail (.foo)
REF_LABELS = {REF_NAME: "name", REF_ATTR: "attr"}
