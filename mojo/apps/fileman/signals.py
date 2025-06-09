import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from mojo.apps.fileman.models import File
from mojo.apps.fileman.tasks import process_file_renditions, cleanup_renditions

logger = logging.getLogger(__name__)

@receiver(post_save, sender=File)
def handle_file_upload_completed(sender, instance, created, **kwargs):
    """
    When a file is marked as completed, automatically create renditions
    
    This connects to the post_save signal of the File model and checks if the
    file has been marked as completed. If so, it queues a task to create renditions.
    """
    # Only process if the file upload is completed
    if instance.is_completed:
        # Check if this is a status change to completed
        if not created:
            try:
                # Get the previous instance from the database
                old_instance = sender.objects.get(pk=instance.pk)
                if old_instance.upload_status != instance.upload_status and instance.upload_status == File.COMPLETED:
                    logger.info(f"File {instance.id} ({instance.filename}) marked as completed, creating renditions")
                    # Queue task to create renditions
                    process_file_renditions.delay(instance.id)
            except Exception as e:
                logger.error(f"Error checking file status change: {str(e)}")
        else:
            # For new files that are already completed
            if instance.upload_status == File.COMPLETED:
                logger.info(f"New file {instance.id} ({instance.filename}) is completed, creating renditions")
                # Queue task to create renditions
                process_file_renditions.delay(instance.id)

@receiver(post_delete, sender=File)
def handle_file_deleted(sender, instance, **kwargs):
    """
    When a file is deleted, clean up its renditions
    
    This connects to the post_delete signal of the File model and ensures that
    when a file is deleted, its renditions are also removed from storage.
    """
    try:
        # Check if we have any renditions to clean up
        from mojo.apps.fileman.models import FileRendition
        if FileRendition.objects.filter(original_file_id=instance.id).exists():
            logger.info(f"File {instance.id} ({instance.filename}) deleted, cleaning up renditions")
            # Queue task to clean up renditions
            # Note: We can't use instance.id directly in the task since the object is being deleted
            # So we pass the ID value
            cleanup_renditions.delay(instance.id)
    except Exception as e:
        logger.error(f"Error queueing rendition cleanup for deleted file: {str(e)}")

# Note: No need to manually connect signals as Django will automatically
# discover and connect properly decorated signal handlers