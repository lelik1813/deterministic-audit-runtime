from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class SnapshotError(Exception):
    """Base error for repository snapshot failures."""


class RepositoryNotFoundError(SnapshotError):
    """Raised when the target repository path does not exist."""


class NotGitRepositoryError(SnapshotError):
    """Raised when the target path is not inside a git repository."""


class SnapshotFileNotFoundError(SnapshotError):
    """Raised when a file is not present in the captured snapshot."""


class SnapshotLineRangeError(SnapshotError):
    """Raised when a requested line range is invalid."""


@dataclass(frozen=True)
class ResolvedLineRange:
    file_path: str
    snapshot_ref: str
    start_line: int
    end_line: int
    excerpt: str
    file_hash: str | None = None

    def to_source_reference(self) -> dict[str, object]:
        source_ref: dict[str, object] = {
            "file_path": self.file_path,
            "line_range": {
                "start": self.start_line,
                "end": self.end_line,
            },
            "snapshot_ref": self.snapshot_ref,
        }
        if self.file_hash is not None:
            source_ref["file_hash"] = self.file_hash
        if self.excerpt:
            source_ref["excerpt"] = self.excerpt
        return source_ref


@dataclass(frozen=True)
class RepositorySnapshot:
    repo_root: Path
    snapshot_ref: str

    @classmethod
    def capture(cls, repo_path: str | Path) -> "RepositorySnapshot":
        candidate_path = Path(repo_path).expanduser().resolve()
        if not candidate_path.exists():
            raise RepositoryNotFoundError(f"Repository path does not exist: {candidate_path}")

        repo_root = cls._resolve_repo_root(candidate_path)
        snapshot_ref = cls._git(repo_root, "rev-parse", "HEAD").strip()
        cls._git(repo_root, "cat-file", "-e", f"{snapshot_ref}^{{commit}}")
        return cls(repo_root=repo_root, snapshot_ref=snapshot_ref)

    def read_bytes(self, file_path: str) -> bytes:
        normalized_path = self._normalize_repo_relative_path(file_path)
        try:
            return self._git_bytes(self.repo_root, "show", f"{self.snapshot_ref}:{normalized_path}")
        except SnapshotError as exc:
            raise SnapshotFileNotFoundError(
                f"File '{normalized_path}' does not exist in snapshot '{self.snapshot_ref}'."
            ) from exc

    def read_text(self, file_path: str, encoding: str = "utf-8") -> str:
        return self.read_bytes(file_path).decode(encoding)

    def read_lines(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        *,
        encoding: str = "utf-8",
    ) -> ResolvedLineRange:
        if start_line < 1 or end_line < 1 or start_line > end_line:
            raise SnapshotLineRangeError(
                f"Invalid line range {start_line}:{end_line}. Line numbers must be 1-based and ordered."
            )

        normalized_path = self._normalize_repo_relative_path(file_path)
        text = self.read_text(normalized_path, encoding=encoding)
        lines = text.splitlines()

        if end_line > len(lines):
            raise SnapshotLineRangeError(
                f"Line range {start_line}:{end_line} exceeds file length {len(lines)} for '{normalized_path}'."
            )

        excerpt = "\n".join(lines[start_line - 1 : end_line])
        return ResolvedLineRange(
            file_path=normalized_path,
            snapshot_ref=self.snapshot_ref,
            start_line=start_line,
            end_line=end_line,
            excerpt=excerpt,
        )

    def list_tracked_files(self) -> list[str]:
        """Return all tracked file paths at the snapshot ref, relative to repo root."""
        output = self._git(self.repo_root, "ls-tree", "-r", "--name-only", self.snapshot_ref)
        return [line for line in output.splitlines() if line.strip()]

    def compute_file_hash(self, file_path: str, algorithm: str = "sha256") -> str:
        normalized_path = self._normalize_repo_relative_path(file_path)
        digest = hashlib.new(algorithm)
        digest.update(self.read_bytes(normalized_path))
        return digest.hexdigest()

    def build_source_reference(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        *,
        include_file_hash: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, object]:
        resolved = self.read_lines(file_path, start_line, end_line, encoding=encoding)
        if include_file_hash:
            resolved = ResolvedLineRange(
                file_path=resolved.file_path,
                snapshot_ref=resolved.snapshot_ref,
                start_line=resolved.start_line,
                end_line=resolved.end_line,
                excerpt=resolved.excerpt,
                file_hash=self.compute_file_hash(resolved.file_path),
            )
        return resolved.to_source_reference()

    @staticmethod
    def _resolve_repo_root(repo_path: Path) -> Path:
        try:
            repo_root = RepositorySnapshot._git(repo_path, "rev-parse", "--show-toplevel").strip()
        except SnapshotError as exc:
            raise NotGitRepositoryError(f"Path is not inside a git repository: {repo_path}") from exc
        return Path(repo_root).resolve()

    @staticmethod
    def _normalize_repo_relative_path(file_path: str) -> str:
        if not file_path:
            raise SnapshotFileNotFoundError("File path must be non-empty.")

        normalized = file_path.replace("\\", "/")
        posix_path = PurePosixPath(normalized)
        if posix_path.is_absolute():
            raise SnapshotFileNotFoundError("File path must be relative to the repository root.")
        if ".." in posix_path.parts:
            raise SnapshotFileNotFoundError("File path must not escape the repository root.")
        return posix_path.as_posix()

    @staticmethod
    def _git(repo_root: Path, *args: str) -> str:
        return RepositorySnapshot._git_bytes(repo_root, *args).decode("utf-8").rstrip("\n")

    @staticmethod
    def _git_bytes(repo_root: Path, *args: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as exc:  # pragma: no cover - depends on environment.
            raise SnapshotError("git executable was not found.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace").strip()
            raise SnapshotError(stderr or "git command failed.") from exc
        return completed.stdout
