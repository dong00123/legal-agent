"""Backend package bootstrap.

The Windows desktop environment can expose Intel OpenMP DLLs from both the
Python distribution and PyTorch.  They are ABI-compatible for this process,
but Intel's runtime aborts when it sees both copies initialized.  Set this
before any backend module imports NumPy/Torch so IDE launches behave like the
command-line launcher.
"""

import os


os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
