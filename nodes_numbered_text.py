class CoachBateNumberedText:
    RETURN_TYPES  = ("STRING",)
    RETURN_NAMES  = ("text",)
    FUNCTION      = "execute"
    CATEGORY      = "CoachBate"
    DESCRIPTION   = (
        "A multiline text input that displays numbered line labels in the node UI "
        "(one number per non-blank line) and passes the full text through as a STRING output."
    )
    RETURN_TOOLTIPS = ("The full multiline text as entered.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "multiline_text": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": "Multiline text input. Each non-blank line is numbered in the gutter.",
                }),
            },
        }

    def execute(self, multiline_text: str = ""):
        return (multiline_text,)


NODE_CLASS_MAPPINGS = {
    "CoachBateNumberedText": CoachBateNumberedText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CoachBateNumberedText": "CoachBate Numbered Text",
}
