from django.conf import settings
from django.db import models

from accounts.models import TenantOwnedModel
from workspaces.models import Workspace


class PythonScript(TenantOwnedModel):
    """
    A Python script a workspace keeps.

    **Nothing on the server executes this.** The Python console runs in the
    visitor's own browser, on Pyodide -- CPython compiled to WebAssembly --
    inside the tab's sandbox. The code cannot reach this process, this
    database, the filesystem, or the network beyond what the browser already
    allows the page itself.

    That is not a temporary shortcut, it is the entire reason a Python console
    could ship at all. Running submitted Python on the server means a sandbox,
    and the honest options for one are a container or a microVM per run, with
    a scheduler, a resource budget and a security boundary to maintain.
    `exec()` with a restricted namespace is not on that list -- every
    generation of that idea, RestrictedPython included, has been escaped, and
    an escape here is a shell next to every tenant's data.

    So this table stores text and only text. If server-side execution is ever
    added it gets its own model with a run-state machine, because "code that
    runs somewhere else" and "code that runs here" are different objects with
    different risks, and one row that means both is how the distinction gets
    lost.
    """

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name='python_scripts'
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    code = models.TextField(
        blank=True,
        help_text='Executed in the browser by Pyodide, never on the server.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='scripts_created',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'python script'
        verbose_name_plural = 'python scripts'
        constraints = [
            models.UniqueConstraint(
                fields=['workspace', 'name'], name='unique_script_name_per_workspace'
            ),
        ]

    def __str__(self):
        return self.name
