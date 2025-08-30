from django.db import models
from mojo.models import MojoModel
from mojo.helpers import logit


class PageRevision(models.Model, MojoModel):
    """
    Version history for pages

    Each revision captures a snapshot of page content at a point in time,
    providing a complete audit trail and version control system.
    """

    # Relationships
    page = models.ForeignKey(
        'docit.Page',
        on_delete=models.CASCADE,
        related_name='revisions',
        help_text="Page this revision belongs to"
    )

    # Content snapshot
    content = models.TextField(
        help_text="Markdown content snapshot at time of revision"
    )

    # Version tracking
    version = models.IntegerField(
        db_index=True,
        help_text="Sequential version number for this page"
    )

    # Optional metadata
    change_summary = models.CharField(
        max_length=200,
        blank=True,
        help_text="Brief description of changes made in this revision"
    )

    # Ownership and tracking (inherits from page/book permissions)
    user = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        help_text="User for permission inheritance (from page/book)"
    )
    created_by = models.ForeignKey(
        'account.User',
        on_delete=models.PROTECT,
        related_name='created_revisions',
        help_text="User who created this revision"
    )

    # Standard MOJO timestamps
    created = models.DateTimeField(
        auto_now_add=True,
        editable=False,
        db_index=True
    )
    modified = models.DateTimeField(
        auto_now=True,
        db_index=True
    )

    class Meta:
        ordering = ['-version']
        verbose_name = 'Page Revision'
        verbose_name_plural = 'Page Revisions'
        # Ensure version is unique within a page
        unique_together = ['page', 'version']

    class RestMeta:
        VIEW_PERMS = ['all']
        SAVE_PERMS = ['manage_docit', 'owner']
        DELETE_PERMS = ['manage_docit', 'owner']

        GRAPHS = {
            'default': {
                "fields": [
                    'id', 'version', 'change_summary', 'created'
                ],
            },
            'detail': {
                "fields": [
                    'id', 'content', 'version', 'change_summary',
                    'created', 'page', 'created_by'
                ],
            },
            'list': {
                "fields": [
                    'id', 'version', 'change_summary', 'created'
                ],
            },
            'content_only': {
                "fields": [
                    'id', 'version', 'content'
                ],
            }
        }

    def __str__(self):
        return f"{self.page.title} v{self.version}"

    def save(self, *args, **kwargs):
        """Override save to validate version and log operations"""

        # Auto-assign version number if not provided
        if not self.version:
            last_revision = PageRevision.objects.filter(
                page=self.page
            ).order_by('-version').first()

            self.version = (last_revision.version + 1) if last_revision else 1

        # Inherit user from page if not set
        if not self.user_id and self.page_id:
            self.user = self.page.user

        # Log the operation
        if self.pk:
            logit.info(f"Updating revision v{self.version} for page: {self.page.title} (ID: {self.pk})")
        else:
            logit.info(f"Creating revision v{self.version} for page: {self.page.title}")

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Override delete to log the operation"""
        logit.warn(f"Deleting revision v{self.version} for page: {self.page.title} (ID: {self.pk})")
        super().delete(*args, **kwargs)

    @property
    def is_latest(self):
        """Check if this is the latest revision for the page"""
        latest = PageRevision.objects.filter(
            page=self.page
        ).order_by('-version').first()

        return latest and latest.id == self.id

    def get_content_diff(self, other_revision=None):
        """
        Get content difference between this revision and another

        This will be implemented in Phase 2 with proper diff functionality
        """
        return None

    def get_previous_revision(self):
        """Get the revision immediately before this one"""
        return PageRevision.objects.filter(
            page=self.page,
            version__lt=self.version
        ).order_by('-version').first()

    def get_next_revision(self):
        """Get the revision immediately after this one"""
        return PageRevision.objects.filter(
            page=self.page,
            version__gt=self.version
        ).order_by('version').first()

    def restore_to_page(self, user):
        """
        Restore this revision's content to the current page

        This creates a new revision with the restored content.
        """
        # Update the page content
        self.page.content = self.content
        self.page.modified_by = user
        self.page.save()

        # Create a new revision to track the restoration
        new_revision = self.page.create_revision(
            user=user,
            change_summary=f"Restored from v{self.version}"
        )

        logit.info(f"Restored page '{self.page.title}' to revision v{self.version}")
        return new_revision

    def get_age_since_created(self):
        """Get time elapsed since this revision was created"""
        from django.utils import timezone
        return timezone.now() - self.created

    @classmethod
    def cleanup_old_revisions(cls, page, keep_count=50):
        """
        Clean up old revisions, keeping only the most recent ones

        This is a utility method for revision management
        """
        revisions = cls.objects.filter(page=page).order_by('-version')

        if revisions.count() > keep_count:
            old_revisions = revisions[keep_count:]
            count = len(old_revisions)

            for revision in old_revisions:
                revision.delete()

            logit.info(f"Cleaned up {count} old revisions for page: {page.title}")
