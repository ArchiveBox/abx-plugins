#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "forum-dl",
#   "pydantic",
# ]
# ///
#
# Wrapper for forum-dl that applies Pydantic v2 compatibility patches.
# Fixes forum-dl 0.3.0's incompatibility with Pydantic v2 by monkey-patching the JsonlWriter class.
#
# Usage:
#     ./forum-dl-wrapper.py [...] > events.jsonl

import sys

# Apply Pydantic v2 compatibility patch BEFORE importing forum_dl
try:
    from forum_dl.writers.jsonl import JsonlWriter
    from pydantic import BaseModel

    # Check if we're using Pydantic v2
    if hasattr(BaseModel, 'model_dump_json'):
        def _patched_serialize_entry(self, entry):
            """Use Pydantic v2's model_dump_json() instead of deprecated json(models_as_dict=False)"""
            return entry.model_dump_json()

        JsonlWriter._serialize_entry = _patched_serialize_entry
except (ImportError, AttributeError):
    # forum-dl not installed or already compatible - no patch needed
    pass

# Now import and run forum-dl's main function
from forum_dl import main

if __name__ == '__main__':
    sys.exit(main())
