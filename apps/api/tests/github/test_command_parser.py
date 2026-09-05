import pytest

from app.github.command_parser import CodeForgeCommandType, CommandParser, InvalidCodeForgeCommand


def test_implement_command():
    res = CommandParser.parse("/codeforge implement")
    assert res.command == CodeForgeCommandType.IMPLEMENT
    assert res.arguments is None

def test_fix_command_with_args():
    res = CommandParser.parse("/codeforge fix issue 123")
    assert res.command == CodeForgeCommandType.FIX
    assert res.arguments == "issue 123"

def test_analyze_command_multiline():
    text = "/codeforge analyze\nHere is some context."
    res = CommandParser.parse(text)
    assert res.command == CodeForgeCommandType.ANALYZE
    assert res.arguments == "Here is some context."

def test_ordinary_comment_ignored():
    assert CommandParser.parse("This is just a regular comment.") is None

def test_malformed_command_ignored():
    with pytest.raises(InvalidCodeForgeCommand, match="Missing command action"):
        CommandParser.parse("/codeforge")

def test_unsupported_command_rejected():
    with pytest.raises(InvalidCodeForgeCommand, match="Unsupported command action"):
        CommandParser.parse("/codeforge destroy")

def test_oversized_command_bounded():
    text = "/codeforge implement " + "A" * 5000
    res = CommandParser.parse(text)
    assert res.command == CodeForgeCommandType.IMPLEMENT
    assert len(res.arguments) == 2000

def test_shell_like_input_never_executed():
    # It just parses, execution is elsewhere, but parser shouldn't eval
    res = CommandParser.parse("/codeforge fix rm -rf /")
    assert res.command == CodeForgeCommandType.FIX
    assert res.arguments == "rm -rf /"
