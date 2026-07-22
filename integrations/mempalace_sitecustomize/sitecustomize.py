"""Apply Claudex-only MemPalace scan guards in the mining subprocess."""

import os
import sys


if os.environ.get("CLAUDEX_MEMPALACE_EXCLUDE_GENERATED") == "1":
    try:
        from mempalace import palace

        skip_directories = palace.SKIP_DIRS
        if not isinstance(skip_directories, set):
            raise TypeError("mempalace.palace.SKIP_DIRS is not a mutable set")
        skip_directories.add("graphify-out")
    except Exception as error:  # Fail closed instead of mining generated data.
        print(
            "Claudex could not apply the MemPalace generated-artifact guard: "
            f"{error}",
            file=sys.stderr,
            flush=True,
        )
        os._exit(78)
