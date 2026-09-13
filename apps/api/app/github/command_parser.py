"""Deterministic command parser for issue comments."""

import enum
import re

from pydantic import BaseModel


class InvalidCodeForgeCommand(Exception):
    """Raised when a CodeForge command syntax or argument is invalid."""

    pass


class CodeForgeCommandType(str, enum.Enum):
    FIX = "fix"
    IMPLEMENT = "implement"
    REVIEW = "review"
    EXPLAIN = "explain"
    ANALYZE = "analyze"


class CodeForgeCommand(BaseModel):
    name: CodeForgeCommandType
    arguments: str | None = None
    normalized_text: str

    @property
    def command(self) -> CodeForgeCommandType:
        """Backward-compatibility alias for name."""
        return self.name


# Backward compatibility alias
ParsedCommand = CodeForgeCommand


class CommandParser:
    """Parses explicit @codeforge commands from comment text."""

    PREFIXES = ("@codeforge", "/codeforge")
    _PREFIX_REGEX = re.compile(r"^(@codeforge|/codeforge)(?:[:,\s]|$)(.*)$", re.IGNORECASE)
    MAX_ARGUMENT_LENGTH = 2000

    @classmethod
    def parse(cls, comment_body: str) -> CodeForgeCommand | None:
        """Parse a comment for a CodeForge command.

        Returns None if no command prefix is found.
        Raises InvalidCodeForgeCommand if the command is unrecognized, malformed,
        or contains forbidden control characters.
        """
        if not comment_body or not comment_body.strip():
            return None

        if "\0" in comment_body:
            raise InvalidCodeForgeCommand("Comment contains forbidden control characters")

        raw_lines = [line.strip() for line in comment_body.splitlines()]
        non_empty_lines = [line for line in raw_lines if line]
        if not non_empty_lines:
            return None

        # Find the first line that begins with @codeforge or /codeforge
        cmd_line_idx = -1
        cmd_match: re.Match[str] | None = None
        for idx, line in enumerate(non_empty_lines):
            match = cls._PREFIX_REGEX.match(line)
            if match:
                cmd_line_idx = idx
                cmd_match = match
                break

        if cmd_match is None or cmd_line_idx == -1:
            return None

        # Remainder of the command line after prefix and separator
        remainder = cmd_match.group(2).strip()

        parts = remainder.split(None, 1)  # split into at most 2 parts: action and first-line args
        if not parts:
            raise InvalidCodeForgeCommand("Missing command action (e.g. @codeforge fix)")

        action_str = parts[0].lower()
        try:
            command_type = CodeForgeCommandType(action_str)
        except ValueError as exc:
            raise InvalidCodeForgeCommand(f"Unsupported command action: {action_str}") from exc

        first_line_args = parts[1].strip() if len(parts) > 1 else ""

        # Subsequent lines after the command line form the rest of the arguments
        subsequent_lines = non_empty_lines[cmd_line_idx + 1 :]
        subsequent_text = "\n".join(subsequent_lines).strip()

        if first_line_args and subsequent_text:
            combined_args = f"{first_line_args}\n{subsequent_text}"
        elif first_line_args:
            combined_args = first_line_args
        elif subsequent_text:
            combined_args = subsequent_text
        else:
            combined_args = ""

        # Bound arguments to prevent prompt injection bombs
        if combined_args:
            args: str | None = combined_args[: cls.MAX_ARGUMENT_LENGTH]
        else:
            args = None

        normalized_text = f"@codeforge {command_type.value}"
        if args:
            normalized_text += f" {args}"

        return CodeForgeCommand(
            name=command_type,
            arguments=args,
            normalized_text=normalized_text,
        )
