"""Workspace API - pick a tool workspace and browse its text files."""
import errno
import hashlib
import os
import stat
import tempfile
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from src.auth_helpers import get_current_user
from src.tool_security import owner_is_admin_or_single_user

# Cap entries returned per directory (mirrors filesystem_tools._CODENAV_MAX_HITS).
# A huge directory shouldn't dump thousands of rows into the picker; the user can
# type/paste a path to jump straight in instead.
_MAX_BROWSE_DIRS = 500
_MAX_WORKSPACE_ENTRIES = 500
_MAX_WORKSPACE_FILE_BYTES = 1_000_000


class WorkspaceCreateRequest(BaseModel):
    parent: str = ""
    name: str


class WorkspaceFileUpdateRequest(BaseModel):
    workspace: str
    path: str
    content: str
    revision: str


def _is_link(path: str) -> bool:
    """Reject symlinks and Windows junctions instead of operating through them."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _client_path(path: str) -> str:
    return path.replace(os.sep, "/")


def _resolve_workspace_entry(
    workspace: str, relative_path: str, *, allow_root: bool
) -> tuple[str, str, str]:
    """Resolve one relative path inside a vetted workspace without links."""
    from src.tool_execution import _resolve_tool_path_in_workspace, vet_workspace

    root = vet_workspace(workspace)
    if not root:
        raise HTTPException(status_code=400, detail="Choose a valid workspace")

    raw = "" if relative_path is None else str(relative_path)
    drive, _ = os.path.splitdrive(raw)
    if "\x00" in raw or os.path.isabs(raw) or drive:
        raise HTTPException(status_code=400, detail="Use a relative workspace path")

    normalized = os.path.normpath(raw or ".")
    if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        raise HTTPException(status_code=400, detail="Path is outside the workspace")

    current = root
    for part in normalized.split(os.sep):
        if part in {"", "."}:
            continue
        current = os.path.join(current, part)
        if _is_link(current):
            raise HTTPException(status_code=400, detail="Workspace links are not supported")

    try:
        resolved = root if normalized == "." else _resolve_tool_path_in_workspace(root, normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    same_as_root = os.path.normcase(resolved) == os.path.normcase(root)
    if same_as_root and not allow_root:
        raise HTTPException(status_code=400, detail="The workspace root cannot be changed")
    relative = "" if same_as_root else _client_path(os.path.relpath(resolved, root))
    return root, resolved, relative


def _read_workspace_text(path: str) -> tuple[bytes, str, os.stat_result]:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Cannot read workspace file") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Cannot read workspace file") from exc

    if not stat.S_ISREG(info.st_mode):
        raise HTTPException(status_code=400, detail="Choose a regular file")
    if info.st_size > _MAX_WORKSPACE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Workspace file is too large to edit")

    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_WORKSPACE_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Cannot read workspace file") from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail="Cannot read workspace file") from exc

    if len(data) > _MAX_WORKSPACE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Workspace file is too large to edit")
    if b"\x00" in data:
        raise HTTPException(status_code=415, detail="Binary files cannot be edited here")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=415, detail="Only UTF-8 text files can be edited") from exc
    return data, text, info


def _revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_workspace_text(
    path: str,
    content: str,
    mode: int,
    current: bytes,
    *,
    revalidate=None,
) -> bytes:
    """Atomically replace text while preserving the file's mode and newline style."""
    crlf = current.count(b"\r\n")
    lf = current.count(b"\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    output = (normalized.replace("\n", newline) if newline != "\n" else normalized).encode("utf-8")
    if len(output) > _MAX_WORKSPACE_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Workspace file is too large to edit")

    parent = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.aegis-", dir=parent)
    try:
        os.chmod(temp_path, stat.S_IMODE(mode))
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        if revalidate is not None:
            revalidate()
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    return output


def setup_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])

    @router.get("/browse")
    def browse(request: Request, path: str = Query(default="")):
        """List subdirectories of `path` (default: home) so the UI can navigate
        the server filesystem and pick a workspace folder. Directories only.

        ADMIN-ONLY: this enumerates the server filesystem, so it is gated the
        same way the file/shell tools are (read_file/write_file/bash are in
        NON_ADMIN_BLOCKED_TOOLS). A non-admin who can't use those tools must not
        be able to map the host's directory tree either.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")

        # Resolve symlinks so the reported path is canonical and the UI navigates
        # real directories (defends against symlink games in displayed paths).
        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            target = os.path.realpath(os.path.expanduser("~"))

        dirs = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        # Don't follow symlinks when classifying - a symlinked
                        # dir is skipped rather than letting the browser wander
                        # off via a link. Hidden entries are omitted.
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            # Build the child path server-side with os.path.join
                            # so it's correct on Windows (backslashes) and Linux.
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        dirs_sorted = sorted(dirs, key=lambda d: d["name"].lower())
        truncated = len(dirs_sorted) > _MAX_BROWSE_DIRS
        parent = os.path.dirname(target)
        from src.tool_execution import vet_workspace
        return {
            "path": target,
            "parent": parent if parent and parent != target else None,
            "dirs": dirs_sorted[:_MAX_BROWSE_DIRS],
            "truncated": truncated,
            # Whether this directory may be bound as a workspace (filesystem
            # roots and sensitive dirs may be browsed through but not chosen).
            "selectable": vet_workspace(target) is not None,
        }

    @router.get("/vet")
    def vet(request: Request, path: str = Query(default="")):
        """Validate a workspace path without binding it.

        The UI calls this before persisting a manually typed path (/workspace
        set) so a typo, file path, deleted folder, sensitive dir, or filesystem
        root is rejected up front with the canonical path returned on success,
        instead of being stored client-side and silently dropped at chat time.
        Admin-gated like /browse: it confirms path existence on the host.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace selection is admin-only")
        from src.tool_execution import vet_workspace
        resolved = vet_workspace(path)
        return {"ok": resolved is not None, "path": resolved}

    @router.post("/create", status_code=201)
    def create(request: Request, payload: WorkspaceCreateRequest):
        """Create and return one workspace folder under an existing folder."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace creation is admin-only")

        from src.tool_execution import vet_workspace
        parent = vet_workspace(payload.parent or "~")
        if not parent:
            raise HTTPException(status_code=400, detail="Choose a valid parent folder")

        name = (payload.name or "").strip()
        if (
            not name
            or name in {".", ".."}
            or name.startswith(".")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise HTTPException(status_code=400, detail="Use one visible folder name")

        target = os.path.realpath(os.path.join(parent, name))
        try:
            inside_parent = os.path.commonpath(
                [os.path.normcase(target), os.path.normcase(parent)]
            ) == os.path.normcase(parent)
        except ValueError:
            inside_parent = False
        if not inside_parent or target == parent:
            raise HTTPException(status_code=400, detail="Invalid workspace folder")

        try:
            os.mkdir(target)
        except FileExistsError:
            raise HTTPException(status_code=409, detail="A folder with that name already exists")
        except PermissionError:
            raise HTTPException(status_code=403, detail="Cannot create a folder here")
        except OSError:
            raise HTTPException(status_code=400, detail="Could not create workspace folder")

        resolved = vet_workspace(target)
        direct_child = bool(
            resolved
            and os.path.normcase(os.path.dirname(resolved)) == os.path.normcase(parent)
        )
        if not direct_child:
            try:
                os.rmdir(target)
            except OSError:
                pass
            raise HTTPException(status_code=400, detail="Invalid workspace folder")
        return {"path": resolved}

    @router.get("/entries")
    def entries(
        request: Request,
        workspace: str = Query(...),
        path: str = Query(default=""),
    ):
        """List one directory inside a selected workspace."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")

        root, target, relative = _resolve_workspace_entry(workspace, path, allow_root=True)
        _, target, relative = _resolve_workspace_entry(root, relative, allow_root=True)
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="Workspace directory not found")
        if not os.path.isdir(target):
            raise HTTPException(status_code=400, detail="Choose a workspace directory")

        rows = []
        try:
            with os.scandir(target) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink() or _is_link(entry.path):
                            continue
                        child_path = f"{relative}/{entry.name}" if relative else entry.name
                        _, _, child_relative = _resolve_workspace_entry(
                            root, child_path, allow_root=False
                        )
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            kind = "directory"
                            size = 0
                        elif stat.S_ISREG(info.st_mode):
                            kind = "file"
                            size = info.st_size
                        else:
                            continue
                        rows.append(
                            {
                                "name": entry.name,
                                "path": child_relative,
                                "type": kind,
                                "size": size,
                            }
                        )
                    except (HTTPException, OSError):
                        continue
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Workspace directory not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Cannot open workspace directory") from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail="Cannot open workspace directory") from exc

        rows.sort(key=lambda row: (row["type"] != "directory", row["name"].casefold()))
        truncated = len(rows) > _MAX_WORKSPACE_ENTRIES
        parent = None if not relative else relative.rsplit("/", 1)[0] if "/" in relative else ""
        return {
            "workspace": root,
            "path": relative,
            "parent": parent,
            "entries": rows[:_MAX_WORKSPACE_ENTRIES],
            "truncated": truncated,
        }

    @router.get("/file")
    def read_workspace_file(
        request: Request,
        workspace: str = Query(...),
        path: str = Query(...),
    ):
        """Return one bounded UTF-8 text file and its edit revision."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace viewing is admin-only")

        root, target, relative = _resolve_workspace_entry(workspace, path, allow_root=False)
        _, target, relative = _resolve_workspace_entry(root, relative, allow_root=False)
        data, text, _ = _read_workspace_text(target)
        return {
            "workspace": root,
            "path": relative,
            "content": text,
            "size": len(data),
            "revision": _revision(data),
        }

    @router.put("/file")
    def update_workspace_file(request: Request, payload: WorkspaceFileUpdateRequest):
        """Update one existing text file unless it changed after being opened."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace editing is admin-only")

        root, target, relative = _resolve_workspace_entry(
            payload.workspace, payload.path, allow_root=False
        )
        _, target, relative = _resolve_workspace_entry(root, relative, allow_root=False)
        current, _, info = _read_workspace_text(target)
        if not payload.revision or payload.revision != _revision(current):
            raise HTTPException(status_code=409, detail="Workspace file changed; reopen it before saving")
        if "\x00" in payload.content:
            raise HTTPException(status_code=415, detail="Binary content cannot be saved here")
        if len(payload.content.encode("utf-8")) > _MAX_WORKSPACE_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Workspace file is too large to edit")

        expected_identity = (info.st_dev, info.st_ino)

        def revalidate_before_replace():
            _, latest_target, _ = _resolve_workspace_entry(root, relative, allow_root=False)
            if os.path.normcase(latest_target) != os.path.normcase(target):
                raise HTTPException(
                    status_code=409,
                    detail="Workspace file changed; reopen it before saving",
                )
            latest, _, latest_info = _read_workspace_text(latest_target)
            if (
                (latest_info.st_dev, latest_info.st_ino) != expected_identity
                or _revision(latest) != payload.revision
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Workspace file changed; reopen it before saving",
                )

        try:
            output = _write_workspace_text(
                target,
                payload.content,
                info.st_mode,
                current,
                revalidate=revalidate_before_replace,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Workspace file changed; reopen it before saving") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Cannot update workspace file") from exc
        except OSError as exc:
            raise HTTPException(status_code=400, detail="Cannot update workspace file") from exc
        return {
            "workspace": root,
            "path": relative,
            "size": len(output),
            "revision": _revision(output),
        }

    @router.delete("/entry")
    def delete_workspace_entry(
        request: Request,
        workspace: str = Query(...),
        path: str = Query(...),
    ):
        """Delete one regular file or empty directory, never the workspace root."""
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace deletion is admin-only")

        root, target, relative = _resolve_workspace_entry(workspace, path, allow_root=False)
        _, target, relative = _resolve_workspace_entry(root, relative, allow_root=False)
        try:
            info = os.lstat(target)
            if stat.S_ISREG(info.st_mode):
                os.unlink(target)
                kind = "file"
            elif stat.S_ISDIR(info.st_mode):
                try:
                    os.rmdir(target)
                except OSError as exc:
                    if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise HTTPException(status_code=409, detail="Only empty folders can be deleted") from exc
                    raise
                kind = "directory"
            else:
                raise HTTPException(status_code=400, detail="Only regular files or empty folders can be deleted")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Workspace entry not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Cannot delete workspace entry") from exc
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=400, detail="Cannot delete workspace entry") from exc
        return {"ok": True, "workspace": root, "path": relative, "type": kind}

    return router
