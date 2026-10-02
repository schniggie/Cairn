from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path

from cairn.auth.models import AuthCaptureManifest, AuthMeta, utcnow


class PathTraversalError(ValueError):
    """Raised when a project id or auth ref attempts to escape the auth store root."""


class AuthStore:
    """Manages the on-disk layout of project-scoped auth profiles.

    Layout::

        <root>/
        └── <project_id>/
            └── <auth_ref>/
                ├── state.json   (Playwright storage state)
                └── meta.json    (non-secret profile metadata)

    ``project_id`` and ``auth_ref`` are sanitized so they can never escape the store
    root via ``../``, absolute paths or backslashes.
    """

    STATE_FILENAME = "state.json"
    META_FILENAME = "meta.json"
    MANIFEST_FILENAME = "manifest.json"

    def __init__(self, root: Path):
        self.root = Path(root)

    # -- path computation -------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        return self.root / self._sanitize_component(project_id)

    def profile_dir(self, project_id: str, auth_ref: str) -> Path:
        return self.project_dir(project_id) / self._sanitize_component(auth_ref)

    def state_file(self, project_id: str, auth_ref: str) -> Path:
        return self.profile_dir(project_id, auth_ref) / self.STATE_FILENAME

    def meta_file(self, project_id: str, auth_ref: str) -> Path:
        return self.profile_dir(project_id, auth_ref) / self.META_FILENAME

    def manifest_file(self, project_id: str, auth_ref: str) -> Path:
        return self.profile_dir(project_id, auth_ref) / self.MANIFEST_FILENAME

    # -- profile management ------------------------------------------------
    def profile_exists(self, project_id: str, auth_ref: str) -> bool:
        return self.state_file(project_id, auth_ref).is_file()

    def ensure_project_dir(self, project_id: str) -> Path:
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def ensure_profile_dir(self, project_id: str, auth_ref: str) -> Path:
        directory = self.profile_dir(project_id, auth_ref)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def list_profiles(self, project_id: str) -> list[str]:
        """Return the sorted list of auth refs that have a saved ``state.json``."""
        directory = self.project_dir(project_id)
        if not directory.is_dir():
            return []
        refs: list[str] = []
        for child in directory.iterdir():
            if child.is_dir() and (child / self.STATE_FILENAME).is_file():
                refs.append(child.name)
        return sorted(refs)

    # -- persistence -------------------------------------------------------
    def write_state(self, project_id: str, auth_ref: str, storage_state: dict) -> Path:
        path = self.state_file(project_id, auth_ref)
        self.ensure_profile_dir(project_id, auth_ref)
        self._atomic_write(path, json.dumps(storage_state, ensure_ascii=False))
        self._restrict_permissions(path)
        return path

    def write_capture(
        self,
        project_id: str,
        auth_ref: str,
        storage_state: dict,
        *,
        request_id: str,
        actor_id: str,
        capture_generation: int | None = None,
    ) -> AuthCaptureManifest:
        """Durably replace state, then publish its request-bound manifest.

        State and manifest use file-plus-directory fsync and ``os.replace``. A
        manifest is never published before the corresponding state bytes exist.
        Generation is monotonic per profile unless supplied by a trusted caller.
        """
        self.ensure_profile_dir(project_id, auth_ref)
        state_bytes = json.dumps(
            storage_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        state_path = self.state_file(project_id, auth_ref)
        self._atomic_write_bytes(state_path, state_bytes)
        self._restrict_permissions(state_path)

        if capture_generation is None:
            try:
                capture_generation = self.load_manifest(project_id, auth_ref).capture_generation + 1
            except (FileNotFoundError, ValueError):
                capture_generation = 1
        manifest = AuthCaptureManifest(
            request_id=request_id,
            auth_ref=auth_ref,
            actor_id=actor_id,
            capture_generation=capture_generation,
            captured_at=utcnow(),
            state_sha256=hashlib.sha256(state_bytes).hexdigest(),
        )
        self._atomic_write_bytes(
            self.manifest_file(project_id, auth_ref),
            manifest.model_dump_json().encode("utf-8"),
        )
        self._restrict_permissions(self.manifest_file(project_id, auth_ref))
        return manifest

    def load_state(self, project_id: str, auth_ref: str) -> dict:
        path = self.state_file(project_id, auth_ref)
        if not path.is_file():
            raise FileNotFoundError(f"auth state not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def load_manifest(self, project_id: str, auth_ref: str) -> AuthCaptureManifest:
        path = self.manifest_file(project_id, auth_ref)
        if not path.is_file():
            raise FileNotFoundError(f"auth capture manifest not found: {path}")
        try:
            return AuthCaptureManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"capture mismatch: invalid manifest: {path}") from exc

    def validate_capture(
        self,
        project_id: str,
        auth_ref: str,
        *,
        request_id: str,
        actor_id: str,
        capture_generation: int,
    ) -> AuthCaptureManifest:
        """Validate the exact binding and digest Dispatcher requires."""
        try:
            manifest = self.load_manifest(project_id, auth_ref)
            state_bytes = self.state_file(project_id, auth_ref).read_bytes()
        except (FileNotFoundError, OSError) as exc:
            raise FileNotFoundError(f"capture store unavailable: {auth_ref}") from exc
        if (
            manifest.request_id != request_id
            or manifest.auth_ref != auth_ref
            or manifest.actor_id != actor_id
            or manifest.capture_generation != capture_generation
            or manifest.state_sha256 != hashlib.sha256(state_bytes).hexdigest()
        ):
            raise ValueError("capture mismatch")
        return manifest

    def write_meta(self, project_id: str, auth_ref: str, meta: AuthMeta) -> Path:
        path = self.meta_file(project_id, auth_ref)
        self.ensure_profile_dir(project_id, auth_ref)
        self._atomic_write(path, meta.model_dump_json(indent=2))
        return path

    def load_meta(self, project_id: str, auth_ref: str) -> AuthMeta:
        path = self.meta_file(project_id, auth_ref)
        if not path.is_file():
            raise FileNotFoundError(f"auth meta not found: {path}")
        return AuthMeta.model_validate_json(path.read_text(encoding="utf-8"))

    def remove_profile(self, project_id: str, auth_ref: str) -> bool:
        directory = self.profile_dir(project_id, auth_ref)
        if not directory.is_dir():
            return False
        # Best-effort removal of the profile directory contents only (never parents).
        for child in sorted(directory.iterdir(), reverse=True):
            try:
                if child.is_dir():
                    child.rmdir()
                else:
                    child.unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass
        return not directory.exists()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _sanitize_component(value: str) -> str:
        """Reject path components that could escape the store root.

        Rules enforced by the plan: reject ``../``, absolute paths (``/`` prefix or
        Windows drive letter), and backslashes (which are separators on Windows and
        can smuggle traversal on POSIX).
        """
        if not value or not value.strip():
            raise PathTraversalError("project id / auth ref must not be empty")
        text = value.strip()
        if "/" in text or "\\" in text:
            raise PathTraversalError(f"invalid path component: {value!r}")
        if text in (".", ".."):
            raise PathTraversalError(f"invalid path component: {value!r}")
        if text.startswith(".") and ("/" in text or "\\" in text):
            raise PathTraversalError(f"invalid path component: {value!r}")
        # Reject anything that resolves to an absolute path.
        candidate = text.replace("\\", "/")
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise PathTraversalError(f"invalid path component: {value!r}")
        return text

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        AuthStore._atomic_write_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows does not support opening directories for fsync; replace is
            # still atomic and the file itself has been flushed.
            pass

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        """Best-effort chmod 600 for the secret state file (no-op on Windows)."""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
