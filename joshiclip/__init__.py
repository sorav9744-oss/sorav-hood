"""JoshiClip: deterministic, no-LLM vlog -> Shorts engine."""
import os as _os

__version__ = "6.0.0"

# Avast sets SSLKEYLOGFILE to a private pipe (\\.\aswMonFltProxy\<id>) per process tree. Python writes TLS keys to
# that file on every HTTPS connection; a long-lived parent (the Upload page) hands its children a name Avast no
# longer accepts, and then EVERY HTTPS call fails with "PermissionError / No such file" (2026-11-24: the whole fill
# produced nothing for 20 hours). We never need TLS key logging, so our processes (and what they start) drop it.
_os.environ.pop("SSLKEYLOGFILE", None)
