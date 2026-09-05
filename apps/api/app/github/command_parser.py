"""Deterministic command parser for issue comments."""

import enum

from pydantic import BaseModel


class InvalidCodeForgeCommand(Exception):
    pass

class CodeForgeCommandType(str, enum.Enum):
    IMPLEMENT = "implement"
    FIX = "fix"
    ANALYZE = "analyze"

class ParsedCommand(BaseModel):
    command: CodeForgeCommandType
    arguments: str | None = None

class CommandParser:
    """Parses explicit /codeforge commands from text."""

    # Do not execute arbitrary comments. Require exact prefix.
    PREFIX = "/codeforge"

    @staticmethod
    def parse(comment_body: str) -> ParsedCommand | None:
        """Parse a comment for a CodeForge command.

        Returns None if no command prefix is found.
        Raises InvalidCodeForgeCommand if the command is unrecognized or malformed.
        """
        if not comment_body:
            return None

        lines = [line.strip() for line in comment_body.splitlines() if line.strip()]
        if not lines:
            return None

        # Command must be at the very start of the first non-empty line
        first_line = lines[0]
        if not first_line.startswith(CommandParser.PREFIX):
            return None

        parts = first_line.split(" ", 2)
        if len(parts) < 2:
            raise InvalidCodeForgeCommand("Missing command action (e.g. /codeforge implement)")

        action_str = parts[1].lower()

        try:
            command_type = CodeForgeCommandType(action_str)
        except ValueError as exc:
            raise InvalidCodeForgeCommand(f"Unsupported command action: {action_str}") from exc

        # Optional arguments can be on the rest of the first line or subsequent lines
        args = ""
        if len(parts) == 3:
            args = parts[2].strip()

        # Combine with rest of body safely
        rest_body = "\n".join(lines[1:]).strip()
        if rest_body:
            if args:
                args = args + "\n" + rest_body
            else:
                args = rest_body

        # Bound arguments to prevent prompt injection bombs
        if args:
            args = args[:2000]

        return ParsedCommand(command=command_type, arguments=args if args else None)
