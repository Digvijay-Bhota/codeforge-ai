from pydantic import BaseModel, Field

# Boundaries for MCP
MAX_FILE_SIZE = 512 * 1024  # 512 KB
MAX_LIST_FILES = 1000
MAX_SEARCH_RESULTS = 50
MAX_TOOL_OUTPUT_BYTES = 512 * 1024  # 512 KB
MAX_GIT_STATUS_FILES = 50  # Limit tracked/untracked git.status paths

class ListFilesInput(BaseModel):
    path: str = Field(default=".", description="Relative path in the repository to list files from.")

class ReadFileInput(BaseModel):
    path: str = Field(..., description="Relative path to the file to read.")

class SearchFilesInput(BaseModel):
    query: str = Field(..., description="Query string to search for.", max_length=1000)
    path: str = Field(default=".", description="Relative path in the repository to search within.")

class MetadataInput(BaseModel):
    pass

class GitStatusInput(BaseModel):
    pass

class CreateFileInput(BaseModel):
    path: str = Field(..., description="Relative path to create the file at.")
    content: str = Field(..., description="Content to write to the file.", max_length=MAX_FILE_SIZE)

class ModifyFileInput(BaseModel):
    path: str = Field(..., description="Relative path to modify the file at.")
    content: str = Field(..., description="New complete content for the file.", max_length=MAX_FILE_SIZE)

class DeleteFileInput(BaseModel):
    path: str = Field(..., description="Relative path to the file to delete.")

class SandboxExecuteInput(BaseModel):
    command: str = Field(..., description="Command to execute in the sandbox.", max_length=4096)
