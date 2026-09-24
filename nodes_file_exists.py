"""Small filesystem predicates for ComfyUI workflows."""

import os


class CoachBateFileExists:
    """Return whether a supplied path points to an existing file."""

    CATEGORY = "CoachBate/Utils"
    FUNCTION = "check"
    RETURN_TYPES = ("BOOLEAN",)
    RETURN_NAMES = ("exists",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "path": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": True,
                        "tooltip": "File path to test. Matching outer quotes are ignored.",
                    },
                ),
            }
        }

    def check(self, path):
        value = "" if path is None else str(path).strip()

        # Prompt lists commonly quote Windows paths containing spaces.
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1].strip()

        if not value:
            return (False,)

        expanded = os.path.expandvars(os.path.expanduser(value))
        return (os.path.isfile(expanded),)
