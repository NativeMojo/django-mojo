from mojo.helpers import logit
from ..models import Book, Page, PageRevision, Asset


class DocItService:
    """
    Business logic service for DocIt operations

    Handles complex operations that span multiple models or contain
    business logic that doesn't belong in individual model methods.
    """

    @staticmethod
    def create_book_with_homepage(title, description, group, user, homepage_title="Home"):
        """
        Create a new book with an initial homepage

        This is a common pattern where new books should have at least one page
        """
        try:
            # Create the book
            book = Book.objects.create(
                title=title,
                description=description,
                group=group,
                user=user,
                created_by=user,
                modified_by=user
            )

            # Create the homepage
            homepage = Page.objects.create(
                book=book,
                title=homepage_title,
                content=f"# {homepage_title}\n\nWelcome to {title}.",
                order_priority=1000,  # High priority to appear first
                user=user,
                created_by=user,
                modified_by=user
            )

            # Create initial revision
            homepage.create_revision(
                user=user,
                change_summary="Initial page creation"
            )

            logit.info(f"Created new book '{title}' with homepage for user {user.username}")

            return book, homepage

        except Exception as e:
            logit.error(f"Failed to create book '{title}': {str(e)}")
            raise

    @staticmethod
    def move_page(page, new_parent=None, new_position=None):
        """
        Move a page to a new location in the hierarchy

        Handles validation and maintains data integrity
        """
        try:
            old_parent = page.parent
            old_path = page.full_path

            # Validate the move
            if new_parent and new_parent.book != page.book:
                raise ValueError(f"Cannot move page to a different book: from '{page.book.id}' to '{new_parent.book.id}'")

            if new_parent and page._would_create_cycle(new_parent):
                raise ValueError("Move would create circular reference")

            # Update the page
            page.parent = new_parent

            if new_position is not None:
                page.order_priority = new_position

            page.save()

            new_path = page.full_path
            logit.info(f"Moved page '{page.title}' from '{old_path}' to '{new_path}'")

            return page

        except Exception as e:
            logit.error(f"Failed to move page '{page.title}': {str(e)}")
            raise

    @staticmethod
    def duplicate_page(page, new_title, new_parent=None, include_children=False, user=None):
        """
        Create a duplicate of a page, optionally with its children
        """
        try:
            # Create the duplicate
            duplicate = Page.objects.create(
                book=page.book,
                parent=new_parent or page.parent,
                title=new_title,
                content=page.content,
                order_priority=page.order_priority,
                metadata=page.metadata.copy(),
                is_published=False,  # Start as draft
                user=page.user,
                created_by=user or page.created_by,
                modified_by=user or page.modified_by
            )

            # Create initial revision for the duplicate
            duplicate.create_revision(
                user=user or page.created_by,
                change_summary=f"Duplicated from '{page.title}'"
            )

            # Duplicate children if requested
            if include_children:
                for child in page.get_all_children(include_unpublished=True):
                    DocItService.duplicate_page(
                        page=child,
                        new_title=child.title,
                        new_parent=duplicate,
                        include_children=True,  # Recursive
                        user=user
                    )

            logit.info(f"Duplicated page '{page.title}' as '{new_title}' (children: {include_children})")

            return duplicate

        except Exception as e:
            logit.error(f"Failed to duplicate page '{page.title}': {str(e)}")
            raise

    @staticmethod
    def bulk_update_page_status(pages, is_published, user):
        """
        Bulk update publication status for multiple pages
        """
        try:
            updated_count = 0

            for page in pages:
                if page.is_published != is_published:
                    page.is_published = is_published
                    page.modified_by = user
                    page.save()
                    updated_count += 1

            status = "published" if is_published else "unpublished"
            logit.info(f"Bulk updated {updated_count} pages to {status} by {user.username}")

            return updated_count

        except Exception as e:
            logit.error(f"Failed to bulk update page status: {str(e)}")
            raise

    @staticmethod
    def get_book_structure(book, include_unpublished=False):
        """
        Get the complete hierarchical structure of a book

        Returns a nested dictionary representing the page tree
        """
        def build_tree(pages, parent_id=None):
            tree = []
            for page in pages:
                if page.parent_id == parent_id:
                    page_data = {
                        'id': page.id,
                        'title': page.title,
                        'slug': page.slug,
                        'is_published': page.is_published,
                        'order_priority': page.order_priority,
                        'children': build_tree(pages, page.id)
                    }
                    tree.append(page_data)
            return tree

        try:
            queryset = book.pages.all()
            if not include_unpublished:
                queryset = queryset.filter(is_published=True)

            pages = list(queryset.order_by('-order_priority', 'title'))
            structure = build_tree(pages)

            logit.debug(f"Generated structure for book '{book.title}' with {len(pages)} pages")

            return structure

        except Exception as e:
            logit.error(f"Failed to get book structure for '{book.title}': {str(e)}")
            raise

    @staticmethod
    def organize_assets(book, asset_ids_in_order):
        """
        Reorder assets within a book based on provided ID list
        """
        try:
            assets = Asset.objects.filter(book=book, id__in=asset_ids_in_order)

            updated_count = 0
            for index, asset_id in enumerate(asset_ids_in_order):
                asset = assets.filter(id=asset_id).first()
                if asset:
                    new_priority = len(asset_ids_in_order) - index  # Higher index = higher priority
                    if asset.order_priority != new_priority:
                        asset.order_priority = new_priority
                        asset.save()
                        updated_count += 1

            logit.info(f"Reorganized {updated_count} assets in book '{book.title}'")

            return updated_count

        except Exception as e:
            logit.error(f"Failed to organize assets for book '{book.title}': {str(e)}")
            raise

    @staticmethod
    def cleanup_orphaned_revisions(max_revisions_per_page=50):
        """
        Clean up old revisions across all pages to prevent database bloat
        """
        try:
            total_cleaned = 0

            # Get all pages that have more than the max revisions
            for page in Page.objects.all():
                revision_count = page.revisions.count()

                if revision_count > max_revisions_per_page:
                    cleaned = PageRevision.cleanup_old_revisions(page, max_revisions_per_page)
                    total_cleaned += cleaned

            if total_cleaned > 0:
                logit.info(f"Cleaned up {total_cleaned} old page revisions")

            return total_cleaned

        except Exception as e:
            logit.error(f"Failed to cleanup orphaned revisions: {str(e)}")
            raise

    @staticmethod
    def get_book_statistics(book):
        """
        Get comprehensive statistics for a book
        """
        try:
            stats = {
                'total_pages': book.get_page_count(),
                'published_pages': book.pages.filter(is_published=True).count(),
                'draft_pages': book.pages.filter(is_published=False).count(),
                'total_assets': book.get_asset_count(),
                'image_assets': book.assets.filter(file__category='image').count(),
                'document_assets': book.assets.filter(file__category='document').count(),
                'total_revisions': PageRevision.objects.filter(page__book=book).count(),
                'root_pages': book.get_root_pages(published_only=False).count(),
                'max_depth': 0
            }

            # Calculate maximum page depth
            for page in book.pages.all():
                depth = page.get_depth()
                if depth > stats['max_depth']:
                    stats['max_depth'] = depth

            logit.debug(f"Generated statistics for book '{book.title}'")

            return stats

        except Exception as e:
            logit.error(f"Failed to get statistics for book '{book.title}': {str(e)}")
            raise

    @staticmethod
    def validate_book_integrity(book):
        """
        Validate the integrity of a book and its pages

        Returns a list of issues found
        """
        issues = []

        try:
            # Check for circular references in page hierarchy
            for page in book.pages.all():
                try:
                    _ = page.full_path  # This will fail if there's a cycle
                except RecursionError:
                    issues.append(f"Circular reference detected in page hierarchy: {page.title}")

            # Check for orphaned assets (assets without files)
            orphaned_assets = book.assets.filter(file__isnull=True)
            if orphaned_assets.exists():
                issues.append(f"Found {orphaned_assets.count()} orphaned assets without files")

            # Check for pages with same slug
            slugs = book.pages.values_list('slug', flat=True)
            duplicate_slugs = [slug for slug in set(slugs) if slugs.count(slug) > 1]
            if duplicate_slugs:
                issues.append(f"Duplicate page slugs found: {duplicate_slugs}")

            logit.info(f"Book integrity check for '{book.title}' found {len(issues)} issues")

            return issues

        except Exception as e:
            logit.error(f"Failed to validate book integrity for '{book.title}': {str(e)}")
            raise
