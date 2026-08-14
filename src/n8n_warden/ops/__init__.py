"""Mutations, grouped by the entity they act on.

Every function here takes `(db, batch, ...)` and records its work through the
batch rather than writing directly, so the whole package is undoable by
construction.
"""

from .credentials import share_credential, transfer_credential, unshare_credential
from .folders import (create_folder, delete_folder, rename_folder, share_folder,
                      transfer_folder, unshare_folder)
from .projects import (add_member, create_project, delete_project, remove_member,
                       rename_project)
from .users import (bcrypt_available, clear_mfa, create_user, delete_user,
                    set_disabled, set_global_role, set_password)
from .workflows import (move_workflow_to_folder, share_workflow, transfer_workflow,
                        unshare_workflow)

__all__ = [
    "share_credential", "transfer_credential", "unshare_credential",
    "create_folder", "delete_folder", "rename_folder", "share_folder",
    "transfer_folder", "unshare_folder",
    "add_member", "create_project", "delete_project", "remove_member",
    "rename_project",
    "bcrypt_available", "clear_mfa", "create_user", "delete_user",
    "set_disabled", "set_global_role", "set_password",
    "move_workflow_to_folder", "share_workflow", "transfer_workflow",
    "unshare_workflow",
]
