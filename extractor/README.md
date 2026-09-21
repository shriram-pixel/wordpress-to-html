# Optional external extractor

The converter reads `.wpress` archives with its own pure-Python implementation
(`app/services/wpress_extractor.py`), so nothing needs to go in this directory.

It exists for the alternative backend. Dropping the prebuilt
[fifthsegment/Wpress-Extractor](https://github.com/fifthsegment/Wpress-Extractor)
binary here (`wpress-extractor.exe` on Windows, `wpress_extractor` elsewhere)
lets `get_extractor("binary")` use it instead — useful for cross-checking the
Python reader against the reference implementation.

Note that the reference binary always extracts into the current working
directory and performs no path-traversal validation, so the adapter runs it
inside the job workspace and re-verifies containment afterwards. The
pure-Python backend remains the default for that reason.
